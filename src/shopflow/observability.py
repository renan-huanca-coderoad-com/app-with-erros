"""Structured logging, request correlation IDs and error fingerprinting.

Every log record is one JSON object on one line, written to a single
stream that carries successes and failures alike. An error is not a
separate feed here — it is one line among the ordinary traffic, which is
the only way a reader can see what a client was doing before it broke.

Three identifiers, kept deliberately distinct because incident tooling
answers three different questions with them:

``request_id``
    Identifies one HTTP request. Minted at the edge for *every* request
    (or taken from the caller's ``X-Request-ID``) and echoed back on the
    response, so client-side and server-side observations of the same
    request can be joined.

``event_id``
    Identifies one *occurrence* of an error. Unique per failure.

``fingerprint``
    Identifies the *class* of error. Deterministic, so three hundred
    occurrences of the same bug collapse into one issue instead of
    looking like three hundred separate incidents.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent


def _release() -> str:
    """The running release. Real deploys inject a tag or a git SHA here."""
    if "SHOPFLOW_VERSION" in os.environ:
        return os.environ["SHOPFLOW_VERSION"]
    try:
        return version("shopflow")
    except PackageNotFoundError:
        return "unknown"


# Deployment context: the fields you reach for first in an incident,
# because "which build, in which environment, on which box" is usually
# the difference between one bad rollout and a real bug.
SERVICE = os.environ.get("SHOPFLOW_SERVICE", "shopflow")
ENV = os.environ.get("SHOPFLOW_ENV", "prod")
HOST = os.environ.get("SHOPFLOW_HOST", socket.gethostname())

#: The current request's ID. Set by the error-capture middleware before
#: the request reaches a handler, so anything logging below it can stamp
#: its lines with the same ID without threading it through every call.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def uuid7() -> str:
    """An RFC 9562 UUIDv7: 48-bit millisecond timestamp, then random bits.

    Time-sortable, unlike uuid4, so IDs sort chronologically in a log
    file or a database index. Written out by hand because ``uuid.uuid7``
    only landed in Python 3.14.
    """
    ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF
    value = (
        (ms << 80)                      # 48 bits: unix time in ms
        | (0x7 << 76)                   #  4 bits: version
        | (secrets.randbits(12) << 64)  # 12 bits: random
        | (0b10 << 62)                  #  2 bits: variant
        | secrets.randbits(62)          # 62 bits: random
    )
    return str(uuid.UUID(int=value))


# Long enough to be a real ID, and no characters that could break a
# header or forge a line in the log we are about to write it into.
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._-]{8,128}\Z")


def take_request_id(inbound: str | None) -> str:
    """Honor the caller's request ID, or mint one.

    Accepting an upstream ID is what lets a trace span a load balancer,
    a gateway and this service. The value is validated rather than
    trusted: it ends up in both a response header and a log record.
    """
    if inbound is not None and _SAFE_REQUEST_ID.match(inbound):
        return inbound
    return uuid7()


_MESSAGE_NOISE = (
    # SQLAlchemy appends the statement, its bound parameters and a docs
    # link to every DBAPI error. All three vary between occurrences of
    # one bug, so grouping fails unless they come off first.
    (re.compile(r"\n\[SQL:.*\Z", re.S), ""),
    (re.compile(r"\n?\(Background on this error at: \S+\)"), ""),
    (re.compile(r"0x[0-9a-fA-F]+"), "ADDR"),
    (re.compile(r"\d+"), "N"),
    (re.compile(r"\s+"), " "),
)


def normalize_message(message: str) -> str:
    """Strip the per-occurrence detail out of an exception message."""
    for pattern, replacement in _MESSAGE_NOISE:
        message = pattern.sub(replacement, message)
    return message.strip()


def app_frame(exc: BaseException) -> str:
    """The deepest frame inside this package — where the bug actually is.

    Everything below it is framework plumbing that every error in the
    service shares. Deliberately identified by function rather than line
    number, so a fingerprint survives unrelated edits to the file.
    """
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(_PACKAGE_ROOT):
            return f"{path.relative_to(_PACKAGE_ROOT)}:{frame.name}"
    return "<no in-app frame>"


def fingerprint(exc: BaseException) -> str:
    """A stable ID for the class of failure this exception represents."""
    parts = (type(exc).__name__, normalize_message(str(exc)), app_frame(exc))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


class JsonFormatter(logging.Formatter):
    """Renders a log record as one JSON object on one line.

    Per-record fields travel in ``extra={"context": {...}}`` rather than
    as loose attributes, so nothing can collide with the reserved names
    on a ``LogRecord``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE,
            "env": ENV,
            "version": _release(),
            "host": HOST,
            "pid": record.process,
            "request_id": request_id_var.get(),
        }
        payload.update(getattr(record, "context", {}))

        if record.exc_info:
            exc = record.exc_info[1]
            payload["exception_type"] = type(exc).__name__
            payload["exception_message"] = str(exc)
            payload["fingerprint"] = fingerprint(exc)
            payload["traceback"] = self.formatException(record.exc_info)

        # default=str so an unexpected value in `context` degrades to a
        # string instead of losing the whole record to a TypeError
        return json.dumps(payload, default=str)


# Libraries that narrate every operation at INFO. Left alone they bury
# the application's own lines, so real deployments dial them down.
_NOISY_LIBRARIES = ("httpx", "httpcore", "urllib3", "asyncio", "multipart")


def configure_logging(path: Path) -> logging.Handler:
    """Send every log record in the process to `path` as JSON.

    Attached to the root logger, so anything a library logs lands in the
    same stream as the application's own lines — which is what a real
    deployment's log looks like.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_shopflow", False):
            root.removeHandler(existing)
            existing.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    handler._shopflow = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def level_for_status(status_code: int) -> int:
    """Access-line severity, so a reader can filter the stream by level."""
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO
