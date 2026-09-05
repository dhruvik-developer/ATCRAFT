"""FastAPI application factory.

Wires configuration, logging, middleware, error handling, routers, health probes
and metrics. Business logic lives in module services - this file only assembles
the application (master rules section 74).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Message, Receive, Scope, Send

from src.api.router import api_router, register_module_routers
from src.core.cache import check_redis_ready, close_redis
from src.core.config import settings
from src.core.database import check_database_ready, dispose_engine
from src.core.exception_handlers import register_exception_handlers
from src.core.logging import configure_logging, get_logger
from src.core.middleware import (
    BodySizeLimitMiddleware,
    HttpMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    is_final_body_message,
)
from src.core.observability import (
    http_request_duration,
    http_requests_total,
    render_metrics,
    setup_sentry,
    setup_tracing,
)
from src.core.registry import import_all_models

log = get_logger("app")

# Docker gives the readiness probe five seconds.  Leave enough headroom for the
# HTTP round trip and for a worker under load instead of allowing a dependency's
# own (longer) connection timeout to consume the whole probe window.
DEPENDENCY_CHECK_TIMEOUT_SECONDS = 2.0

# Every header the browser client sends on a request and reads off a response.
# CORS only matters when the SPA is served from a different origin than the
# API - the same-origin nginx topology never preflights - which is exactly the
# deployment where an omission here is first noticed: the CSRF header the
# client attaches to every mutating request was missing, so every cross-origin
# POST/PUT/PATCH/DELETE would have been refused at the preflight and the SPA
# could read but never write.
CORS_ALLOWED_REQUEST_HEADERS = [
    "Authorization",
    "Content-Type",
    "X-Request-ID",
    "Idempotency-Key",
    "X-CSRF-Token",
]
# The 429 handling the client implements reads Retry-After and the limit.
CORS_EXPOSED_RESPONSE_HEADERS = [
    "X-Request-ID",
    "X-Response-Time-ms",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "Retry-After",
]

# --- Response compression -----------------------------------------------------
# Three settings, and each one is load-bearing. Measured against this exact
# middleware with 64 KiB streaming chunks: a 25 MB download took 28 ms
# uncompressed and 831 ms compressed, with the event loop stalled for 825 ms of
# it - every other request on that worker waits behind one report.
#
# 1. What NOT to compress. Starlette's defaults already cover images, video and
#    zip; these two are what this application actually serves. A PDF is
#    compressed internally and an XLSX *is* a zip container, so gzip spends real
#    CPU to save almost nothing, and Content-Length is dropped on the way.
GZIP_EXCLUDED_CONTENT_TYPES = (
    *DEFAULT_EXCLUDED_CONTENT_TYPES,
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/octet-stream",
)

# 2. Level 5, not zlib's 9. Nine costs roughly three times the CPU of five for
#    about two percent less payload on JSON, and the deployment's nginx already
#    compresses at five - so nine was buying nothing anyone could measure.
GZIP_COMPRESS_LEVEL = 5

# 3. Anything at or above this is compressed in a worker thread instead of on
#    the event loop. Starlette applies the test per *chunk*, and a streaming
#    response here arrives in 64 KiB chunks, so the 128 KiB default meant every
#    chunk of every export was compressed inline. 32 KiB puts the streaming path
#    on a thread while leaving a small JSON body - where the thread hop would
#    cost more than the compression - on the loop.
GZIP_THREAD_MINIMUM_SIZE = 32 * 1024

DESCRIPTION = """
Manufacturing ERP for architectural hardware: inventory, multi-level BOM,
allocation, MRP, procurement, production, assembly, job work, quality, packing
and dispatch.

**Conventions**

* Every response uses the standard envelope: `{"success", "data", "message"}`.
* List endpoints are paginated. Nothing returns an unbounded result set.
* Authentication is a bearer access token; refresh tokens rotate on use.
* Authorisation is permission-based and enforced server-side on every route.
* Long-running work (MRP, exports, large reports) returns a job id to poll.
"""


async def _dependency_readiness() -> tuple[bool, bool]:
    """Check Postgres and Redis concurrently, with a bound on each check."""

    async def bounded(check: Callable[[], Awaitable[bool]]) -> bool:
        try:
            return await asyncio.wait_for(check(), timeout=DEPENDENCY_CHECK_TIMEOUT_SECONDS)
        except TimeoutError:
            return False

    database_ok, redis_ok = await asyncio.gather(
        bounded(check_database_ready),
        bounded(check_redis_ready),
    )
    return database_ok, redis_ok


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    Startup does not block on Postgres or Redis being reachable: in Docker the
    container may start before its dependencies are ready, and a crash loop is a
    worse failure mode than an application that reports "not ready" until they
    arrive (section 49A).
    """
    configure_logging()
    settings.ensure_storage_dirs()

    registered = import_all_models()
    mounted = register_module_routers()

    setup_sentry()
    setup_tracing(app)

    log.info(
        "app.startup",
        environment=str(settings.app_env),
        model_modules=len(registered),
        api_modules=len(mounted),
        debug=settings.app_debug,
    )

    db_ok, redis_ok = await _dependency_readiness()
    if not db_ok:
        log.warning(
            "app.startup_database_unreachable", hint="readiness stays false until it responds"
        )
    if not redis_ok:
        log.warning("app.startup_redis_unreachable", hint="cache and rate limiting degrade open")

    yield

    log.info("app.shutdown")
    await dispose_engine()
    await close_redis()


class MetricsMiddleware(HttpMiddleware):
    """Latency and outcome per route template (never per concrete path).

    Observed when the body has finished rather than when the headers were
    ready. The two differ only for a streaming response, and the streaming
    response is ``GET /documents/{id}/download`` - so the histogram whose job is
    to find slow routes was reporting the slowest route in the ERP as the
    fastest, timing the row lookup and excluding the entire transfer. See
    ``is_final_body_message``.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if not is_final_body_message(message):
                return
            # Set by the router while handling the request, so it is only
            # available now - which is another reason to observe here.
            template = getattr(scope.get("route"), "path", None)
            if not template:
                return
            method = scope["method"]
            elapsed = time.perf_counter() - started
            http_requests_total.labels(method=method, route=template, status=str(status)).inc()
            http_request_duration.labels(method=method, route=template).observe(elapsed)

        await self.app(scope, receive, send_wrapper)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are disabled in production: the schema is an accurate
        # map of every endpoint and its permissions.
        docs_url=None if settings.is_deployed else "/docs",
        redoc_url=None if settings.is_deployed else "/redoc",
        openapi_url=None if settings.is_deployed else "/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none"},
    )

    # Middleware runs bottom-up: the last one added is the outermost. Request
    # context wraps the application middleware so accepted requests and errors
    # carry an id. Trusted-host validation is registered last below so rejected
    # Host headers do not reach any of this request-processing stack.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=1024,
        compresslevel=GZIP_COMPRESS_LEVEL,
        thread_minimum_size=GZIP_THREAD_MINIMUM_SIZE,
        exclude_content_types=GZIP_EXCLUDED_CONTENT_TYPES,
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_size_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=CORS_ALLOWED_REQUEST_HEADERS,
        expose_headers=CORS_EXPOSED_RESPONSE_HEADERS,
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    # ---------------------------------------------------------------- probes --

    @app.get("/health/live", tags=["Health"], include_in_schema=False)
    async def liveness() -> dict[str, Any]:
        """Is the process up? Never touches a dependency - a dead database must
        not cause the orchestrator to restart a healthy application."""
        return {"status": "alive", "service": settings.otel_service_name}

    @app.get("/health/ready", tags=["Health"], include_in_schema=False)
    async def readiness(response: Response) -> dict[str, Any]:
        """Can this instance serve traffic? Checks the actual dependencies."""
        database_ok, redis_ok = await _dependency_readiness()
        ready = database_ok and redis_ok
        if not ready:
            response.status_code = 503
        return {
            "status": "ready" if ready else "not_ready",
            "checks": {"database": database_ok, "redis": redis_ok},
        }

    @app.get("/metrics", tags=["Health"], include_in_schema=False)
    async def metrics() -> Response:
        if not settings.prometheus_metrics_enabled:
            return Response(status_code=404)
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    app.add_middleware(MetricsMiddleware)

    # This must remain the final user-middleware registration. Starlette wraps
    # later additions around earlier ones, so this places host validation ahead
    # of metrics, request context, CORS, rate limiting, and body processing.
    if settings.is_deployed:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    return app


app = create_app()
