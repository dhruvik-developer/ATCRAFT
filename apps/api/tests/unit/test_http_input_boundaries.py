"""Untrusted headers and streamed bodies cannot bypass shared HTTP limits."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, File, UploadFile
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from src.core.constants import MAX_REQUEST_ID_LENGTH
from src.core.exception_handlers import register_exception_handlers
from src.core.logging import get_request_id, ip_address_ctx, resolve_client_ip
from src.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from src.modules.audit.models import AuditLog
from src.modules.audit.schemas import AuditLogFilters

pytestmark = pytest.mark.unit

BODY_LIMIT = 1024


class _Input(BaseModel):
    quantity: int


@pytest.fixture
def application() -> FastAPI:
    app = FastAPI()
    app.state.writes = 0
    register_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=BODY_LIMIT)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/context")
    async def context() -> dict[str, str | None]:
        return {"request_id": get_request_id(), "ip_address": ip_address_ctx.get()}

    @app.post("/write")
    async def write(data: _Input) -> dict[str, int]:
        app.state.writes += 1
        return {"quantity": data.quantity}

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> dict[str, int]:
        app.state.writes += 1
        return {"size": len(await file.read())}

    return app


@pytest.mark.parametrize("value", ["proxy-id.123_abc", "a" * MAX_REQUEST_ID_LENGTH])
async def test_valid_correlation_ids_survive_response_and_audit_validation(
    application: FastAPI, value: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/context", headers={"X-Request-ID": value})
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == value
    assert AuditLogFilters(request_id=value).request_id == value
    assert get_request_id() is None


@pytest.mark.parametrize(
    "value",
    [b"", b"a" * (MAX_REQUEST_ID_LENGTH + 1), b"unsafe\r\nheader", b"unsafe id", b"\xff"],
)
async def test_invalid_correlation_ids_are_replaced_before_audit_and_reflection(
    application: FastAPI, value: bytes
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/context", headers=[(b"X-Request-ID", value)])
    assert response.status_code == 200
    identifier = response.headers["X-Request-ID"]
    assert response.json()["request_id"] == identifier
    assert len(identifier) == 32
    assert identifier.isascii() and identifier.isalnum()
    assert AuditLogFilters(request_id=identifier).request_id == identifier
    assert get_request_id() is None


def test_correlation_limit_matches_the_persisted_audit_column() -> None:
    assert AuditLog.__table__.c.request_id.type.length == MAX_REQUEST_ID_LENGTH


@pytest.mark.parametrize("size", [BODY_LIMIT, BODY_LIMIT + 1])
@pytest.mark.parametrize("declared_size", [None, "1", "invalid", "\u00b2"])
async def test_streamed_json_is_bounded_using_actual_received_bytes(
    application: FastAPI, size: int, declared_size: str | None
) -> None:
    payload = b'{"quantity":1}'
    body = payload + b" " * (size - len(payload))

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(body), 128):
            yield body[offset : offset + 128]

    headers: dict[str, str | bytes] = {
        "Content-Type": "application/json",
        "X-Request-ID": "stream-request",
    }
    if declared_size is not None:
        headers["Content-Length"] = declared_size.encode("latin-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post("/write", content=chunks(), headers=headers)
    if size == BODY_LIMIT:
        assert response.status_code == 200
        assert application.state.writes == 1
    else:
        assert response.status_code == 413
        assert response.json() == {
            "success": False,
            "error": {
                "code": "PAYLOAD_TOO_LARGE",
                "message": f"Request body exceeds the {BODY_LIMIT:,} byte limit.",
                "details": None,
            },
            "request_id": "stream-request",
        }
        assert application.state.writes == 0
    assert response.headers["X-Request-ID"] == "stream-request"
    assert get_request_id() is None


async def test_declared_oversized_body_is_rejected_without_consuming_it(
    application: FastAPI,
) -> None:
    async def never_read() -> AsyncIterator[bytes]:
        pytest.fail("an oversized declared body must be rejected before reading it")
        yield b""  # pragma: no cover

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/write", content=never_read(), headers={"Content-Length": str(BODY_LIMIT + 1)}
        )
    assert response.status_code == 413
    assert application.state.writes == 0


async def test_streamed_upload_closes_partial_files_on_limit_failure(
    application: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def track_file(*args: Any, **kwargs: Any) -> Any:
        temporary = original(*args, **kwargs)
        opened.append(temporary)
        return temporary

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", track_file)

    async def multipart() -> AsyncIterator[bytes]:
        yield (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="sample.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
        )
        yield b"x" * BODY_LIMIT
        yield b"\r\n--boundary--\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/upload",
            content=multipart(),
            headers={"Content-Type": "multipart/form-data; boundary=boundary"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert application.state.writes == 0
    assert opened and all(temporary.closed for temporary in opened)


async def test_body_limit_keeps_the_error_contract_without_framework_handlers() -> None:
    async def raw_app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        await request.body()
        await JSONResponse({"success": True})(scope, receive, send)

    app = RequestContextMiddleware(BodySizeLimitMiddleware(raw_app, max_bytes=BODY_LIMIT))

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * BODY_LIMIT
        yield b"x"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/", content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("forwarded", ["invalid", "127.0.0.1, 10.0.0.1", "fe80::1%eth0"])
async def test_invalid_forwarded_ip_uses_the_actual_peer_for_audit(
    application: FastAPI, forwarded: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("192.0.2.1", 1234)),
        base_url="http://test",
    ) as client:
        response = await client.get("/context", headers={"X-Real-IP": forwarded})
    assert response.json()["ip_address"] == "192.0.2.1"
    assert ip_address_ctx.get() is None


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (("192.0.2.1",), "192.0.2.1"),
        (("2001:0db8::1",), "2001:db8::1"),
        (("invalid", "::1"), "::1"),
        ((None, "testclient"), None),
    ],
)
def test_audit_ip_normalization_handles_ipv4_ipv6_and_non_network_clients(
    addresses: tuple[str | None, ...], expected: str | None
) -> None:
    assert resolve_client_ip(*addresses) == expected


async def test_cancelled_request_always_releases_its_logging_context() -> None:
    async def cancelled(scope: Scope, receive: Receive, send: Send) -> None:
        assert get_request_id() is not None
        assert ip_address_ctx.get() == "127.0.0.1"
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=RequestContextMiddleware(cancelled)),
        base_url="http://test",
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get("/")
    assert get_request_id() is None
    assert ip_address_ctx.get() is None
