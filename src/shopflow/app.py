import json
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .observability import fingerprint, request_id_var, take_request_id, uuid7
from .routes import catalog, customers, orders, reports

logger = logging.getLogger("shopflow")

ERROR_LOG = Path(os.environ.get("SHOPFLOW_ERROR_LOG", "logs/errors.log"))

MAX_BODY_CAPTURE = 2048


def _log_error(request: Request, body: bytes, exc: Exception) -> str:
    """Append an unhandled exception to the error log as one JSON line.

    Returns the event ID: this occurrence of the error. The request ID
    comes from the context set by the middleware, the same way a handler
    further down would reach it.
    """
    event_id = uuid7()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id_var.get(),
        "event_id": event_id,
        "fingerprint": fingerprint(exc),
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "body": body[:MAX_BODY_CAPTURE].decode("utf-8", errors="replace"),
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "traceback": traceback.format_exc(),
    }
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return event_id


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(
        title="NorthStar Supplies API",
        description="Wholesale office-supplies ordering backend.",
        version="1.0.0",
    )

    @app.middleware("http")
    async def error_capture(request: Request, call_next):
        body = await request.body()
        # Minted for every request, not only the ones that fail: an ID
        # that exists only on the error path is no use for reconstructing
        # what the client was doing before it broke.
        request_id = take_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            event_id = _log_error(request, body, exc)
            logger.exception("unhandled error on %s %s (request_id=%s event_id=%s)",
                             request.method, request.url.path, request_id, event_id)
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal Server Error",
                    "request_id": request_id,
                    "event_id": event_id,
                },
            )
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(catalog.router)
    app.include_router(customers.router)
    app.include_router(orders.router)
    app.include_router(reports.router)

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
