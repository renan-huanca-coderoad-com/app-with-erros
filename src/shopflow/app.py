import json
import logging
import os
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .routes import catalog, customers, orders, reports

logger = logging.getLogger("shopflow")

ERROR_LOG = Path(os.environ.get("SHOPFLOW_ERROR_LOG", "logs/errors.log"))

MAX_BODY_CAPTURE = 2048


def _log_error(request: Request, body: bytes, exc: Exception) -> str:
    """Append an unhandled exception to the error log as one JSON line."""
    error_id = uuid.uuid4().hex[:12]
    record = {
        "error_id": error_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    return error_id


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
        try:
            return await call_next(request)
        except Exception as exc:
            error_id = _log_error(request, body, exc)
            logger.exception("unhandled error %s on %s %s",
                             error_id, request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error", "error_id": error_id},
            )

    app.include_router(catalog.router)
    app.include_router(customers.router)
    app.include_router(orders.router)
    app.include_router(reports.router)

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
