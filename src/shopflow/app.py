import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .observability import (
    configure_logging,
    level_for_status,
    log_shutdown,
    log_startup,
    request_id_var,
    take_request_id,
    uuid7,
)
from .routes import catalog, customers, orders, reports

access_logger = logging.getLogger("shopflow.access")
error_logger = logging.getLogger("shopflow.error")

LOG_PATH = Path(os.environ.get("SHOPFLOW_LOG", "logs/app.log"))

MAX_BODY_CAPTURE = 2048


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_startup()
    yield
    log_shutdown()


def create_app() -> FastAPI:
    configure_logging(LOG_PATH)
    init_db()
    app = FastAPI(
        title="NorthStar Supplies API",
        description="Wholesale office-supplies ordering backend.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        body = await request.body()
        # Minted for every request, not only the ones that fail: an ID
        # that exists only on the error path is no use for reconstructing
        # what the client was doing before it broke.
        request_id = take_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        event_id = None
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                event_id = uuid7()
                # exc_info makes the formatter attach the type, message,
                # fingerprint and traceback
                error_logger.error(
                    "unhandled exception in %s %s",
                    request.method,
                    request.url.path,
                    exc_info=exc,
                    extra={
                        "context": {
                            "event_id": event_id,
                            "http.method": request.method,
                            "http.path": request.url.path,
                            "http.query": str(request.url.query),
                            # only on the error line: an access log that
                            # carried every request body would be both
                            # enormous and a privacy problem
                            "http.body": body[:MAX_BODY_CAPTURE].decode(
                                "utf-8", errors="replace"
                            ),
                        }
                    },
                )
                response = JSONResponse(
                    status_code=500,
                    content={
                        "detail": "Internal Server Error",
                        "request_id": request_id,
                        "event_id": event_id,
                    },
                )

            context = {
                "http.method": request.method,
                "http.path": request.url.path,
                "http.query": str(request.url.query),
                "http.status_code": response.status_code,
                "http.duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "http.client_ip": request.client.host if request.client else None,
                "http.user_agent": request.headers.get("user-agent", ""),
            }
            if event_id is not None:
                context["event_id"] = event_id
            access_logger.log(
                level_for_status(response.status_code),
                "%s %s %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={"context": context},
            )

            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)

    app.include_router(catalog.router)
    app.include_router(customers.router)
    app.include_router(orders.router)
    app.include_router(reports.router)

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
