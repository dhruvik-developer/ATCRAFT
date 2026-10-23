"""Manually bound request models share FastAPI's HTTP validation contract."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.exception_handlers import register_exception_handlers
from src.modules.audit.router import audit_log_filters
from src.modules.audit.router import router as audit_router
from src.modules.audit.schemas import MAX_ENTITY_TYPE_LENGTH, MAX_REQUEST_ID_LENGTH
from src.modules.documents.router import router as documents_router
from src.modules.documents.schemas import DocumentUploadRequest
from src.modules.quality.router import router as quality_router
from src.modules.users.router import router as users_router

pytestmark = pytest.mark.unit


async def _no_database() -> AsyncIterator[None]:
    yield None


def _reader() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="query@example.test",
        full_name="Query validation",
        is_superuser=True,
        permissions=frozenset(),
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    register_exception_handlers(application)
    for router in (audit_router, documents_router, quality_router, users_router):
        application.include_router(router)
    application.dependency_overrides[get_db] = _no_database
    application.dependency_overrides[get_current_principal] = _reader
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as http:
        yield http


@pytest.mark.parametrize(
    ("path", "query", "field"),
    [
        ("/audit/logs", {"entity_type": "x" * (MAX_ENTITY_TYPE_LENGTH + 1)}, "query.entity_type"),
        ("/audit/logs", {"request_id": "x" * (MAX_REQUEST_ID_LENGTH + 1)}, "query.request_id"),
        (
            "/audit/approvals",
            {"entity_type": "x" * (MAX_ENTITY_TYPE_LENGTH + 1)},
            "query.entity_type",
        ),
        ("/documents", {"entity_id": str(uuid.uuid4())}, "query"),
        ("/roles", {"grants_permission": "inventory.unknown"}, "query.grants_permission"),
        (
            "/quality/inspections",
            {"inspected_from": "2026-05-02T00:00:00Z", "inspected_to": "2026-05-01T00:00:00Z"},
            "query",
        ),
    ],
)
async def test_query_model_failures_are_422_with_field_errors(
    client: httpx.AsyncClient, path: str, query: dict[str, str], field: str
) -> None:
    response = await client.get(path, params=query)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert field in body["error"]["details"]["fields"]


def test_audit_filter_normalization_keeps_accepting_valid_identifiers() -> None:
    filters = audit_log_filters(entity_type="  PURCHASE_ORDER  ", request_id="  request-123  ")
    assert filters.entity_type == "purchase_order"
    assert filters.request_id == "request-123"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"entity_type": "goods_receipt"}, "entity_type and entity_id must be supplied together."),
        ({"entity_id": str(uuid.uuid4())}, "entity_type and entity_id must be supplied together."),
        (
            {"entity_type": "invalid type", "entity_id": str(uuid.uuid4())},
            "entity_type must be lowercase snake_case, e.g. 'goods_receipt'.",
        ),
        ({"expires_at": "2100-01-01T00:00:00"}, "expires_at must include a timezone offset."),
        ({"expires_at": "2000-01-01T00:00:00Z"}, "expires_at must be in the future."),
    ],
)
async def test_multipart_model_failures_are_422_with_body_field_errors(
    client: httpx.AsyncClient, form: dict[str, str], message: str
) -> None:
    response = await client.post(
        "/documents",
        data=form,
        files={"file": ("example.txt", b"example", "text/plain")},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {"fields": {"_root": [message]}}


async def test_multipart_scalar_validation_uses_the_same_body_field_paths(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/documents",
        data={"entity_id": "not-a-uuid"},
        files={"file": ("example.txt", b"example", "text/plain")},
    )
    assert response.status_code == 422, response.text
    assert "entity_id" in response.json()["error"]["details"]["fields"]


def test_body_binding_preserves_document_normalization() -> None:
    from src.core.validation import bind_request_model

    entity_id = uuid.uuid4()
    model = bind_request_model(
        DocumentUploadRequest,
        {"entity_type": " GOODS_RECEIPT ", "entity_id": entity_id},
    )
    assert model.entity_type == "goods_receipt"
    assert model.entity_id == entity_id
