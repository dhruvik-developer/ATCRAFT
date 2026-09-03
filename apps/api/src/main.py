"""FastAPI application factory.

Wires configuration, logging, middleware, error handling, routers, health probes
and metrics. Business logic lives in module services - this file only assembles
the application (master rules section 74).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.api.router import api_router, register_module_routers
from src.core.cache import check_redis_ready, close_redis
from src.core.config import settings
from src.core.database import check_database_ready, dispose_engine
from src.core.exception_handlers import register_exception_handlers
from src.core.logging import configure_logging, get_logger
from src.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
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

    db_ok = await check_database_ready()
    redis_ok = await check_redis_ready()
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


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are disabled in production: the schema is an accurate
        # map of every endpoint and its permissions.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none"},
    )

    # Middleware runs bottom-up: the last one added is the outermost. Request
    # context must be outermost so every log line and error carries the id.
    if settings.is_production:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_size_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms", "X-RateLimit-Remaining"],
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
        database_ok = await check_database_ready()
        redis_ok = await check_redis_ready()
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

    @app.middleware("http")
    async def record_metrics(request: Request, call_next: Any) -> Response:
        """Latency and outcome per route template (never per concrete path)."""
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        if template:
            elapsed = time.perf_counter() - started
            http_requests_total.labels(
                method=request.method, route=template, status=str(response.status_code)
            ).inc()
            http_request_duration.labels(method=request.method, route=template).observe(elapsed)
        return response

    return app


app = create_app()
