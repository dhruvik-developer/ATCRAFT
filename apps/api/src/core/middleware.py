"""HTTP middleware: correlation id, access logging, security headers, limits.

Master rules sections 51/54/55. Ordering matters and is set in ``main.py``:
the request-context middleware must run outermost so that everything inside it,
including the exception handlers, has a correlation id to report.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from src.core.config import settings
from src.core.errors import ErrorCode
from src.core.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    new_request_id,
)

log = get_logger("http")

# Probes and metrics scrapes would otherwise dominate the access log.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Establish the correlation id and emit one structured access log line."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour the id minted by Nginx so one identifier spans the whole hop
        # chain; generate one when the app is reached directly.
        request_id = request.headers.get("x-request-id") or new_request_id()

        client_ip = request.headers.get("x-real-ip") or (
            request.client.host if request.client else None
        )
        bind_request_context(
            request_id=request_id,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            clear_request_context()

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(duration_ms)

        if request.url.path not in _QUIET_PATHS:
            principal = getattr(request.state, "principal", None)
            log.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                route=_route_template(request),
                status=response.status_code,
                duration_ms=duration_ms,
                request_id=request_id,
                user_id=str(principal.user_id) if principal else None,
                company_id=str(principal.company_id) if principal else None,
            )
            # Section 57: flag anything materially past the engineering target
            # so slow endpoints surface without waiting for a user complaint.
            if duration_ms > 1000:
                log.warning(
                    "request.slow",
                    path=request.url.path,
                    duration_ms=duration_ms,
                    request_id=request_id,
                )

        return response


def _route_template(request: Request) -> str:
    """The parameterised path (``/items/{item_id}``) rather than the concrete one.

    Raw paths would create unbounded log/metric cardinality, one series per id.
    """
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defence in depth behind Nginx.

    Nginx sets these for browser traffic, but the API is also reached directly
    in development and by internal callers, and a header set in exactly one
    place is a header that silently disappears when the topology changes.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are buffered.

    Nginx enforces the same ceiling, but a request that reaches the app in
    development, or through an internal route, must not be able to exhaust
    worker memory.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "success": False,
                    "error": {
                        "code": str(ErrorCode.PAYLOAD_TOO_LARGE),
                        "message": (
                            f"Request body exceeds the "
                            f"{self.max_bytes // (1024 * 1024)} MB limit."
                        ),
                    },
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse per-principal API budget.

    Keyed on the authenticated user where possible and on the client address
    otherwise, so one noisy integration cannot spend everyone else's allowance.
    Endpoint-specific budgets live in :mod:`src.core.ratelimit` and are applied
    inside the routes that need them.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not settings.rate_limit_enabled or request.url.path in _QUIET_PATHS:
            return await call_next(request)

        from src.core.ratelimit import check_rate_limit

        auth = request.headers.get("authorization", "")
        if auth:
            # Hash the token tail rather than storing it: the identity only has
            # to be stable and unguessable, and a Redis key must never be a
            # usable credential if the cache is dumped.
            identity = f"tok:{hash(auth[-32:])}"
        else:
            client_host = request.client.host if request.client else "unknown"
            identity = f"ip:{request.headers.get('x-real-ip') or client_host}"

        result = await check_rate_limit(
            identity, limit=settings.rate_limit_default_per_minute, window_seconds=60, scope="api"
        )
        if not result.allowed:
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "success": False,
                    "error": {
                        "code": str(ErrorCode.RATE_LIMITED),
                        "message": "Too many requests. Please slow down.",
                        "details": {"retry_after_seconds": result.retry_after_seconds},
                    },
                    "request_id": getattr(request.state, "request_id", None),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response


__all__ = [
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
]
