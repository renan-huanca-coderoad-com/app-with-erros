"""Request correlation IDs and error fingerprinting.

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
import re
import secrets
import time
import traceback
import uuid
from contextvars import ContextVar
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent

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
