"""Every module's generated response contract must match the shared envelope."""

from __future__ import annotations

import importlib
from copy import deepcopy

import pytest
from fastapi import FastAPI
from scripts.response_contract import validate_response_contract

from src.api.router import MODULE_ORDER
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse

pytestmark = pytest.mark.unit


def test_every_module_documents_consistent_json_responses() -> None:
    application = FastAPI()
    for name in MODULE_ORDER:
        module = importlib.import_module(f"src.modules.{name}.router")
        application.include_router(module.router, prefix="/api/v1")

    spec = application.openapi()
    assert spec["paths"], "An empty contract cannot prove response consistency"
    validate_response_contract(spec)


@pytest.fixture
def response_spec() -> dict:
    application = FastAPI()

    @application.get("/example", responses=COMMON_ERROR_RESPONSES)
    async def example() -> ApiResponse[str]:
        return ApiResponse.ok("example")

    return application.openapi()


def test_shared_envelope_is_accepted(response_spec: dict) -> None:
    validate_response_contract(response_spec)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "properties": {"data": {"type": "string"}}},
        {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": {"type": "string"},
                "message": {"type": "string"},
            },
        },
    ],
)
def test_unwrapped_or_untyped_success_is_rejected(response_spec: dict, schema: dict) -> None:
    response_spec["paths"]["/example"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = schema
    with pytest.raises(ValueError, match=r"GET /example.*success JSON"):
        validate_response_contract(response_spec)


def test_every_alternative_in_a_union_must_be_an_envelope(response_spec: dict) -> None:
    content = response_spec["paths"]["/example"]["get"]["responses"]["200"]["content"]
    valid = deepcopy(content["application/json"]["schema"])
    content["application/json"]["schema"] = {"anyOf": [valid, {"type": "object"}]}
    with pytest.raises(ValueError, match="success JSON"):
        validate_response_contract(response_spec)


def test_framework_error_shapes_are_rejected(response_spec: dict) -> None:
    response_spec["paths"]["/example"]["get"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ] = {"type": "object", "properties": {"detail": {"type": "string"}}}
    with pytest.raises(ValueError, match="errors must use ErrorResponse"):
        validate_response_contract(response_spec)


def test_description_overrides_cannot_erase_error_schemas(response_spec: dict) -> None:
    response_spec["paths"]["/example"]["get"]["responses"]["429"] = {"description": "Rate limited"}
    with pytest.raises(ValueError, match="errors must document ErrorResponse JSON"):
        validate_response_contract(response_spec)


def test_binary_downloads_and_bodyless_responses_keep_their_http_contract(
    response_spec: dict,
) -> None:
    responses = response_spec["paths"]["/example"]["get"]["responses"]
    responses["200"]["content"] = {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }
    responses["204"] = {"description": "No content"}
    validate_response_contract(response_spec)
