"""Exercise the error contract across handlers and middleware through ASGI."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from src.core.config import settings
from src.core.errors import AppError, ConflictError, RateLimitedError
from src.core.errors import ValidationError as AppValidationError
from src.core.exception_handlers import register_exception_handlers
from src.core.logging import get_request_id
from src.core.messages import MALFORMED_JSON, UNEXPECTED_ERROR
from src.core.middleware import (
    BodySizeLimitMiddleware,
    CORSMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from src.core.ratelimit import RateLimitResult
from src.core.responses import (
    AcceptedJobResponse,
    ApiResponse,
    CursorPaginatedResponse,
    ErrorResponse,
    PaginatedResponse,
)
from src.core.validation import bind_request_model

pytestmark = pytest.mark.unit


class _Input(BaseModel):
    quantity: int


class _NestedInput(BaseModel):
    body: _Input


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings, "app_debug", False)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example.test"],
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.post("/validate")
    async def validate(data: _Input) -> ApiResponse[int]:
        return ApiResponse.ok(data.quantity)

    @app.post("/nested")
    async def nested(data: _NestedInput) -> ApiResponse[int]:
        return ApiResponse.ok(data.body.quantity)

    @app.get("/failure/{kind}")
    async def failure(kind: str) -> None:
        match kind:
            case "application":
                raise ConflictError()
            case "rate-limited":
                raise RateLimitedError(retry_after_seconds=17)
            case "integrity":
                raise IntegrityError(
                    "private SQL", {}, Exception("unique constraint private_table")
                )
            case "stale":
                raise StaleDataError("private SQL")
            case "operational":
                raise OperationalError("private SQL", {}, Exception("database unavailable"))
            case "database":
                raise SQLAlchemyError("private SQL")
            case "http-auth":
                raise HTTPException(401, "Authentication required", {"WWW-Authenticate": "Bearer"})
            case "http-validation":
                raise HTTPException(422, "Invalid request")
            case "http-unavailable":
                raise HTTPException(503, "Please try again later")
            case "structured-details":
                raise ConflictError(details={"available": Decimal("1.25")})
            case "schema-bug":
                _Input.model_validate({"quantity": "private invalid response data"})
            case _:
                raise RuntimeError("private implementation detail")

    return app


def _assert_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"success", "error", "request_id"}
    assert body["success"] is False
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["request_id"] == response.headers["X-Request-ID"] == "contract-request"
    assert "private" not in response.text
    return body


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/failure/application", 409, "CONFLICT"),
        ("GET", "/failure/rate-limited", 429, "RATE_LIMITED"),
        ("GET", "/failure/integrity", 409, "DUPLICATE_DOCUMENT"),
        ("GET", "/failure/stale", 409, "CONFLICT"),
        ("GET", "/failure/operational", 503, "SERVICE_UNAVAILABLE"),
        ("GET", "/failure/database", 500, "INTERNAL_ERROR"),
        ("GET", "/failure/http-auth", 401, "UNAUTHENTICATED"),
        ("GET", "/failure/http-validation", 422, "VALIDATION_ERROR"),
        ("GET", "/failure/http-unavailable", 503, "SERVICE_UNAVAILABLE"),
        ("GET", "/failure/unexpected", 500, "INTERNAL_ERROR"),
        ("GET", "/failure/schema-bug", 500, "INTERNAL_ERROR"),
        ("GET", "/missing", 404, "NOT_FOUND"),
        ("GET", "/validate", 405, "HTTP_ERROR"),
    ],
)
async def test_handlers_share_the_error_contract(
    application: FastAPI, method: str, path: str, status: int, code: str
) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, path, headers={"X-Request-ID": "contract-request"})
    body = _assert_error(response, status, code)
    if path == "/failure/rate-limited":
        assert response.headers["Retry-After"] == "17"
    if path == "/failure/http-auth":
        assert response.headers["WWW-Authenticate"] == "Bearer"
    if status == 405:
        assert response.headers["Allow"] == "POST"
    if status != 429:
        assert body["error"]["details"] is None
    assert get_request_id() is None


async def test_validation_keeps_field_errors_in_the_shared_envelope(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/validate", json={"quantity": "invalid"}, headers={"X-Request-ID": "contract-request"}
        )
    body = _assert_error(response, 422, "VALIDATION_ERROR")
    assert set(body["error"]["details"]["fields"]) == {"quantity"}


async def test_malformed_json_is_a_400_without_decode_details(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/validate",
            content='{"quantity": "private submitted value",',
            headers={"Content-Type": "application/json", "X-Request-ID": "contract-request"},
        )
    body = _assert_error(response, 400, "VALIDATION_ERROR")
    assert body["error"]["message"] == MALFORMED_JSON
    assert body["error"]["details"] == {"fields": {"_root": [MALFORMED_JSON]}}


async def test_nested_field_named_body_keeps_its_path(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/nested",
            json={"body": {"quantity": "private invalid quantity"}},
            headers={"X-Request-ID": "contract-request"},
        )
    body = _assert_error(response, 422, "VALIDATION_ERROR")
    assert set(body["error"]["details"]["fields"]) == {"body.quantity"}


def test_manually_bound_body_field_named_body_keeps_its_path() -> None:
    with pytest.raises(AppValidationError) as exc:
        bind_request_model(_NestedInput, {"body": {"quantity": "invalid"}})
    assert set(exc.value.details["fields"]) == {"body.quantity"}


async def test_body_limit_uses_the_shared_envelope(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/validate", content=b"x" * 1025, headers={"X-Request-ID": "contract-request"}
        )
    assert _assert_error(response, 413, "PAYLOAD_TOO_LARGE")["error"]["details"] is None


async def test_rate_limit_middleware_keeps_retry_headers_and_details(
    application: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    async def exhausted(*args: Any, **kwargs: Any) -> RateLimitResult:
        return RateLimitResult(False, 100, 0, 23)

    monkeypatch.setattr("src.core.ratelimit.check_rate_limit", exhausted)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/validate", json={"quantity": 1}, headers={"X-Request-ID": "contract-request"}
        )
    body = _assert_error(response, 429, "RATE_LIMITED")
    assert body["error"]["details"] == {"retry_after_seconds": 23}
    assert response.headers["Retry-After"] == "23"
    assert response.headers["X-RateLimit-Limit"] == "100"
    assert response.headers["X-RateLimit-Remaining"] == "0"


async def test_cors_rejection_uses_the_shared_envelope_and_preserves_policy_headers(
    application: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/validate",
            headers={
                "X-Request-ID": "contract-request",
                "Origin": "https://attacker.example.test",
                "Access-Control-Request-Method": "POST",
            },
        )
    _assert_error(response, 400, "FORBIDDEN")
    assert response.headers["Vary"] == "Origin"
    assert response.headers["Access-Control-Allow-Methods"] == "POST"
    assert "Access-Control-Allow-Origin" not in response.headers
    assert int(response.headers["Content-Length"]) == len(response.content)


async def test_error_details_are_json_serialized(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/failure/structured-details")
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"available": "1.25"}


def test_error_payload_without_request_context_has_explicit_nullable_fields() -> None:
    assert AppError().to_payload() == {
        "success": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": UNEXPECTED_ERROR,
            "details": None,
        },
        "request_id": None,
    }


@pytest.mark.parametrize(
    "model",
    [ApiResponse, PaginatedResponse, CursorPaginatedResponse, AcceptedJobResponse],
)
def test_success_models_reject_an_error_discriminator(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"success": False, "data": {} if model is AcceptedJobResponse else []})


def test_error_model_rejects_a_success_discriminator() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate(
            {"success": True, "error": {"code": "INTERNAL_ERROR", "message": "Failed"}}
        )
