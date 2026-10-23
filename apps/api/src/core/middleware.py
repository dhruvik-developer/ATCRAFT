"""HTTP middleware: correlation id, access logging, security headers, limits.

Master rules sections 51/54/55. Ordering matters and is set in ``main.py``:
the request-context middleware must run outermost so that everything inside it,
including the exception handlers, has a correlation id to report.

Why these are pure ASGI and not ``BaseHTTPMiddleware``
-----------------------------------------------------
``BaseHTTPMiddleware`` is the comfortable spelling - it hands you a ``Request``
and a ``call_next`` - and it pays for that comfort by running the rest of the
application in a *separate anyio task*, wired to the caller through a pair of
memory object streams, for every request. Five of those were stacked here.

Measured, same machine, same endpoint, minimal app: 16.6 us/request bare,
1171 us/request with five no-op ``BaseHTTPMiddleware`` layers, 19.8 us/request
with five no-op pure-ASGI layers. That is 231 us per layer against 0.6 us -
about 1.15 ms of pure overhead on every request before a line of application
code runs, which on a cached reference lookup was most of the response time.

The pure-ASGI form is a little more work to read: instead of returning a
response you wrap ``send`` and act on the ``http.response.start`` message. In
exchange it costs essentially nothing, and it makes one thing possible that the
other form does not - see ``RequestContextMiddleware``, which can only measure a
streamed response honestly because it sees the final body message.
"""

from __future__ import annotations

import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware as StarletteCORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware as StarletteTrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.config import settings
from src.core.errors import AuthenticationError, ErrorCode
from src.core.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    get_request_id,
    resolve_client_ip,
    resolve_request_id,
)
from src.core.messages import RATE_LIMITED
from src.core.responses import ErrorResponse
from src.core.security import decode_token

log = get_logger("http")

# Probes and metrics scrapes would otherwise dominate the access log.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics", "/favicon.ico"})

_BEARER_PREFIX = "bearer "

#: Section 57's engineering target. Past this, a request is logged as slow so it
#: surfaces before a user complains rather than after.
SLOW_REQUEST_MS = 1000.0

#: Set unless the response already carries them. Nginx sets the same values for
#: browser traffic; these cover every other way the API is reached.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Cache-Control", "no-store"),
)

#: Only ever sent by something that terminates TLS, which in a deployment is the
#: host nginx - this is the direct-access fallback.
HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _request_id_from(scope: Scope) -> str | None:
    """The correlation id ``RequestContextMiddleware`` put on the request.

    Read off the scope rather than a ``Request``: the middlewares that need it
    are building an error envelope on a path where constructing a request
    object would be the most expensive thing they do. ``Request.state`` is
    backed by ``scope["state"]``, so this is the same value by the same route.
    """
    state = scope.get("state")
    return state.get("request_id") if isinstance(state, dict) else None


class HttpMiddleware:
    """Base for a pure-ASGI middleware that only cares about HTTP.

    Anything that is not an HTTP request - a lifespan message, a websocket -
    passes through untouched, which is the one piece of boilerplate every such
    middleware needs and the one that is easy to forget.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.handle(scope, receive, send)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError


class CORSMiddleware(StarletteCORSMiddleware):
    """Keep Starlette's CORS policy and headers while shaping failed preflights."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code < 400:
            return response
        # Rebuild only content headers: policy headers (including Vary) must
        # survive exactly as Starlette computed them for this origin.
        headers = {
            name: value
            for name, value in response.headers.items()
            if name not in {"content-length", "content-type"}
        }
        return JSONResponse(
            status_code=response.status_code,
            headers=headers,
            content=ErrorResponse.create(
                str(ErrorCode.FORBIDDEN),
                bytes(response.body).decode(),
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )


class TrustedHostMiddleware(StarletteTrustedHostMiddleware):
    """Reject invalid hosts before request processing using the API error shape.

    The normal Starlette validation and redirects remain in place for accepted
    hosts. A rejection happens outside request context, so its request id is null
    and no client-provided correlation header is trusted or reflected.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "").split(":")[0]
        valid_or_redirect = any(
            host == pattern
            or (pattern.startswith("*") and host.endswith(pattern[1:]))
            or (self.www_redirect and "www." + host == pattern)
            for pattern in self.allowed_hosts
        )
        if valid_or_redirect:
            await super().__call__(scope, receive, send)
            return
        response = JSONResponse(
            status_code=400,
            content=ErrorResponse.create(
                str(ErrorCode.VALIDATION_ERROR), "Invalid host header"
            ).model_dump(mode="json"),
        )
        await response(scope, receive, send)


def is_final_body_message(message: Message) -> bool:
    """Whether this ASGI message is the last byte of the response.

    The distinction the timing code turns on. A response is *started* when
    ``http.response.start`` goes out - that is when the headers are settled and
    when ``BaseHTTPMiddleware``'s ``call_next`` would have returned - and it is
    *finished* only when a ``http.response.body`` arrives without ``more_body``.

    For a JSON response the two are microseconds apart and the difference never
    showed. For ``GET /documents/{id}/download``, which streams, the gap is the
    entire transfer: timing to the start message recorded the row lookup and the
    object open and excluded everything else, so a 40 MB drawing pulled over a
    site VPN was logged at a few milliseconds. The slow-request warning could
    never fire for a download and the ``atcraft_http_request_duration``
    histogram reported the slowest route in the ERP as the fastest - the two
    signals whose whole purpose is finding slow endpoints were blind to the
    slowest one.
    """
    return message["type"] == "http.response.body" and not message.get("more_body", False)


def _bearer_rate_limit_identity(authorization: str) -> str | None:
    """The budget key for a *verified* access token, or ``None``.

    The first version keyed on a digest of whatever the header said. That is
    stable across workers, but the middleware runs before authentication, so
    ``Authorization: Bearer <anything random>`` minted a fresh bucket per
    request and the per-client budget was never consumed - only callers who
    cooperated were limited. Verifying the token here is a local HMAC check
    with no I/O; anything that does not verify is charged to the client
    address like any other anonymous request, and the auth dependency will
    reject it properly a few frames later.
    """
    if authorization[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = authorization[len(_BEARER_PREFIX) :].strip()
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
    except AuthenticationError:
        return None
    return f"user:{payload.subject}"


def _rate_limit_identity(request: Request) -> str:
    """Verified user where the request carries one, client address otherwise."""
    authorization = request.headers.get("authorization", "")
    if authorization:
        identity = _bearer_rate_limit_identity(authorization)
        if identity is not None:
            return identity
    client_host = request.client.host if request.client else "unknown"
    client_ip = resolve_client_ip(request.headers.get("x-real-ip"), client_host)
    return f"ip:{client_ip or 'unknown'}"


class RequestContextMiddleware(HttpMiddleware):
    """Establish the correlation id and emit one structured access log line."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)

        # Honour the id minted by Nginx so one identifier spans the whole hop
        # chain; generate one when the app is reached directly.
        request_id = resolve_request_id(request.headers.get("x-request-id"))

        client_ip = resolve_client_ip(
            request.headers.get("x-real-ip"), request.client.host if request.client else None
        )
        bind_request_context(
            request_id=request_id,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
        request.state.request_id = request_id

        started = time.perf_counter()
        status = 500
        first_byte_ms = 0.0
        accounted = False

        def account() -> None:
            """One access log line per request, once, after the last byte."""
            nonlocal accounted
            if accounted:
                return
            accounted = True
            try:
                if request.url.path in _QUIET_PATHS:
                    return
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                principal = getattr(request.state, "principal", None)
                log.info(
                    "request.completed",
                    method=request.method,
                    path=request.url.path,
                    route=_route_template(request),
                    status=status,
                    duration_ms=duration_ms,
                    first_byte_ms=first_byte_ms,
                    request_id=request_id,
                    user_id=str(principal.user_id) if principal else None,
                    company_id=str(principal.company_id) if principal else None,
                )
                # Section 57: flag anything materially past the engineering
                # target so slow endpoints surface without waiting for a user
                # complaint.
                if duration_ms > SLOW_REQUEST_MS:
                    log.warning(
                        "request.slow",
                        path=request.url.path,
                        duration_ms=duration_ms,
                        first_byte_ms=first_byte_ms,
                        request_id=request_id,
                    )
            finally:
                # The context belongs to the request, and this is the last thing
                # the request does.
                clear_request_context()

        async def send_wrapper(message: Message) -> None:
            nonlocal status, first_byte_ms
            if message["type"] == "http.response.start":
                status = message["status"]
                # The header can only ever be time-to-first-byte: it goes out
                # ahead of the body it would otherwise be describing. That is
                # honest, and is what its name says.
                first_byte_ms = round((time.perf_counter() - started) * 1000, 2)
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Response-Time-ms"] = str(first_byte_ms)
            await send(message)
            if is_final_body_message(message):
                account()

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            accounted = True
            clear_request_context()
            raise
        finally:
            # Disconnects and cancellation may leave no final body message.
            # Context must still be cleared, including CancelledError (which
            # inherits BaseException rather than Exception).
            account()


class SecurityHeadersMiddleware(HttpMiddleware):
    """Defence in depth behind Nginx.

    Nginx sets these for browser traffic, but the API is also reached directly
    in development and by internal callers, and a header set in exactly one
    place is a header that silently disappears when the topology changes.

    The ``setdefault`` semantics of the previous implementation are preserved: a
    route that deliberately sets its own ``Cache-Control`` keeps it.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
                if settings.is_production and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = HSTS_VALUE
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware(HttpMiddleware):
    """Reject oversized bodies before they are buffered.

    Nginx enforces the same ceiling, but a request that reaches the app in
    development, or through an internal route, must not be able to exhaust
    worker memory.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = f"Request body exceeds the {self.max_bytes:,} byte limit."

        async def reject() -> None:
            response = JSONResponse(
                status_code=413,
                content=ErrorResponse.create(
                    str(ErrorCode.PAYLOAD_TOO_LARGE),
                    message,
                    request_id=_request_id_from(scope),
                ).model_dump(mode="json"),
            )
            await response(scope, receive, send)

        content_length = Headers(scope=scope).get("content-length")
        try:
            declared_size = int(content_length) if content_length is not None else 0
        except ValueError:
            # Servers normally reject malformed Content-Length headers first.
            # Counting the stream still enforces the bound for direct ASGI calls.
            declared_size = 0
        if declared_size > self.max_bytes:
            await reject()
            return

        received_size = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received_size
            incoming = await receive()
            if incoming["type"] == "http.request":
                received_size += len(incoming.get("body", b""))
                if received_size > self.max_bytes:
                    # FastAPI preserves HTTPException during JSON/form parsing;
                    # a generic exception there would become a misleading 400.
                    raise _BodyTooLarge(message)
            return incoming

        async def track_send(outgoing: Message) -> None:
            nonlocal response_started
            if outgoing["type"] == "http.response.start":
                response_started = True
            await send(outgoing)

        try:
            await self.app(scope, bounded_receive, track_send)
        except _BodyTooLarge:
            # Plain ASGI applications may not have an HTTP exception handler.
            # A started response cannot be replaced with another status line.
            if response_started:
                raise
            await reject()


class _BodyTooLarge(HTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=413, detail=message)


class RateLimitMiddleware(HttpMiddleware):
    """Coarse per-principal API budget.

    Keyed on the authenticated user where possible and on the client address
    otherwise, so one noisy integration cannot spend everyone else's allowance.
    Endpoint-specific budgets live in :mod:`src.core.ratelimit` and are applied
    inside the routes that need them.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if not settings.rate_limit_enabled or request.url.path in _QUIET_PATHS:
            await self.app(scope, receive, send)
            return

        from src.core.ratelimit import check_rate_limit

        result = await check_rate_limit(
            _rate_limit_identity(request),
            limit=settings.rate_limit_default_per_minute,
            window_seconds=60,
            scope="api",
        )
        if not result.allowed:
            response = JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                },
                content=ErrorResponse.create(
                    str(ErrorCode.RATE_LIMITED),
                    RATE_LIMITED,
                    {"retry_after_seconds": result.retry_after_seconds},
                    _request_id_from(scope),
                ).model_dump(mode="json"),
            )
            await response(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-RateLimit-Limit"] = str(result.limit)
                headers["X-RateLimit-Remaining"] = str(result.remaining)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _route_template(request: Request) -> str:
    """The parameterised path (``/items/{item_id}``) rather than the concrete one.

    Raw paths would create unbounded log/metric cardinality, one series per id.
    """
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


__all__ = [
    "HSTS_VALUE",
    "SECURITY_HEADERS",
    "SLOW_REQUEST_MS",
    "BodySizeLimitMiddleware",
    "CORSMiddleware",
    "HttpMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "TrustedHostMiddleware",
    "is_final_body_message",
]
