"""Free text, preferences and courier secrets are bounded at the request door.

Each notes and description field below was a bare ``str`` over a TEXT column,
and ``users.preferences`` an unvalidated JSONB blob, so one request could store
megabytes that every list and detail read then returned (docs/assumptions.md
§INPUT-1). The schema tests hold each field at its limit and one past it. The
HTTP tests mount each module's router on a bare application whose database and
principal are stubbed: an oversized value is refused while the request is
parsed, as a 422 naming the field, before any handler or database is reached.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from src.core.constants import MAX_DESCRIPTION_LENGTH, MAX_LINE_NOTES_LENGTH, MAX_NOTES_LENGTH
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.exception_handlers import register_exception_handlers
from src.modules.courier_tracking.router import router as courier_router
from src.modules.courier_tracking.schemas import (
    MAX_COURIER_PASSWORD_LENGTH,
    ShiprocketCredentials,
)
from src.modules.customers import schemas as customers
from src.modules.customers.router import router as customers_router
from src.modules.inventory import schemas as inventory
from src.modules.inventory.router import router as inventory_router
from src.modules.organization import schemas as organization
from src.modules.organization.router import router as organization_router
from src.modules.products import schemas as products
from src.modules.products.router import router as products_router
from src.modules.users.router import router as users_router
from src.modules.users.schemas import MAX_PREFERENCES_BYTES, UserCreate, UserUpdate
from src.modules.vendors import schemas as vendors
from src.modules.vendors.router import router as vendors_router
from src.modules.warehouses import schemas as warehouses
from src.modules.warehouses.router import router as warehouses_router

pytestmark = pytest.mark.unit


def _each(module: ModuleType, field: str, limit: int, *names: str) -> list[Any]:
    return [pytest.param(module, name, field, limit, id=f"{name}.{field}") for name in names]


BOUNDED_TEXT = [
    *_each(
        customers,
        "notes",
        MAX_NOTES_LENGTH,
        "CustomerCreate",
        "CustomerUpdate",
        "AddressCreate",
        "AddressUpdate",
        "ContactCreate",
        "ContactUpdate",
    ),
    *_each(
        inventory,
        "notes",
        MAX_NOTES_LENGTH,
        "StockBatchCreate",
        "StockSerialCreate",
        "StockSerialBulkCreate",
        "StockTransferCreate",
        "StockTransferUpdate",
        "StockAdjustmentCreate",
        "PhysicalCountCreate",
        "BinMoveCreate",
        "ScrapDisposalCreate",
        "InternalIssueCreate",
    ),
    *_each(
        inventory,
        "notes",
        MAX_LINE_NOTES_LENGTH,
        "StockTransferLineCreate",
        "StockAdjustmentLineCreate",
        "InternalIssueLineCreate",
        "PhysicalCountEntry",
    ),
    *_each(organization, "notes", MAX_NOTES_LENGTH, "FinancialYearCreate", "FinancialYearUpdate"),
    *_each(
        products,
        "description",
        MAX_DESCRIPTION_LENGTH,
        "CategoryCreate",
        "CategoryUpdate",
        "BrandCreate",
        "BrandUpdate",
        "ItemCreate",
        "ItemUpdate",
    ),
    *_each(
        products,
        "notes",
        MAX_NOTES_LENGTH,
        "ManufacturerCreate",
        "ManufacturerUpdate",
        "PriceCreate",
        "PriceUpdate",
    ),
    *_each(
        vendors,
        "notes",
        MAX_NOTES_LENGTH,
        "VendorCreate",
        "VendorUpdate",
        "VendorContactCreate",
        "VendorContactUpdate",
        "VendorCategoryAssign",
        "VendorCategoryUpdate",
        "TransporterCreate",
        "TransporterUpdate",
    ),
    *_each(
        warehouses,
        "notes",
        MAX_NOTES_LENGTH,
        "WarehouseCreate",
        "WarehouseUpdate",
        "StorageLocationCreate",
        "StorageLocationUpdate",
    ),
]


def test_the_bounded_fields_are_the_forty_four_that_were_not() -> None:
    assert len(BOUNDED_TEXT) == 44


@pytest.mark.parametrize(("module", "model", "field", "limit"), BOUNDED_TEXT)
def test_free_text_stops_at_its_limit(
    module: ModuleType, model: str, field: str, limit: int
) -> None:
    info = getattr(module, model).model_fields[field]
    constrained: Any = (
        Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
    )
    adapter: TypeAdapter[str | None] = TypeAdapter(constrained)

    assert adapter.validate_python("n" * limit) == "n" * limit
    assert adapter.validate_python(None) is None
    with pytest.raises(PydanticValidationError):
        adapter.validate_python("n" * (limit + 1))


def _preferences(size: int) -> dict[str, str]:
    """A preferences blob whose compact JSON is exactly ``size`` bytes."""
    envelope = len('{"layout":""}')
    return {"layout": "x" * (size - envelope)}


@pytest.mark.parametrize(
    "build",
    [
        lambda preferences: UserUpdate(preferences=preferences),
        lambda preferences: UserCreate(
            email="new.user@example.com",
            full_name="New User",
            password="Long-enough-pass-1",
            preferences=preferences,
        ),
    ],
    ids=["update", "create"],
)
def test_preferences_are_held_to_their_size(build: Any) -> None:
    fits = _preferences(MAX_PREFERENCES_BYTES)

    assert build(fits).preferences == fits
    assert build(None).preferences is None
    with pytest.raises(PydanticValidationError) as refused:
        build(_preferences(MAX_PREFERENCES_BYTES + 1))
    assert [error["loc"] for error in refused.value.errors()] == [("preferences",)]


def test_a_courier_password_is_held_to_its_size() -> None:
    longest = "p" * MAX_COURIER_PASSWORD_LENGTH
    credentials = ShiprocketCredentials(email="ops@example.com", password=longest)

    assert credentials.password.get_secret_value() == longest
    with pytest.raises(PydanticValidationError):
        ShiprocketCredentials(email="ops@example.com", password=longest + "p")


# -----------------------------------------------------------------------------
# Over HTTP: one route per module, refused before any handler runs
# -----------------------------------------------------------------------------


async def _no_database() -> AsyncIterator[None]:
    yield None


def _administrator() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="text-bounds@example.test",
        full_name="Text bounds",
        is_superuser=True,
        permissions=frozenset(),
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    register_exception_handlers(application)
    for router in (
        courier_router,
        customers_router,
        inventory_router,
        organization_router,
        products_router,
        users_router,
        vendors_router,
        warehouses_router,
    ):
        application.include_router(router)
    application.dependency_overrides[get_db] = _no_database
    application.dependency_overrides[get_current_principal] = _administrator
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as http:
        yield http


NOTES = "n" * (MAX_NOTES_LENGTH + 1)
LINE_NOTES = "n" * (MAX_LINE_NOTES_LENGTH + 1)
DESCRIPTION = "d" * (MAX_DESCRIPTION_LENGTH + 1)


@pytest.mark.parametrize(
    ("method", "path", "body", "field"),
    [
        ("POST", "/customers", {"notes": NOTES}, "notes"),
        (
            "POST",
            "/inventory/transfers",
            {"notes": NOTES, "lines": [{"notes": LINE_NOTES}]},
            "notes",
        ),
        (
            "POST",
            "/inventory/transfers",
            {"notes": "Fine", "lines": [{"notes": LINE_NOTES}]},
            "lines.0.notes",
        ),
        ("POST", "/organization/financial-years", {"notes": NOTES}, "notes"),
        ("POST", "/products/categories", {"description": DESCRIPTION}, "description"),
        ("POST", "/vendors", {"notes": NOTES}, "notes"),
        ("POST", "/warehouses", {"notes": NOTES}, "notes"),
        (
            "PATCH",
            f"/users/{uuid.uuid4()}",
            {"preferences": _preferences(MAX_PREFERENCES_BYTES + 1)},
            "preferences",
        ),
        (
            "POST",
            "/integrations/courier/accounts",
            {
                "credentials": {
                    "email": "ops@example.com",
                    "password": "p" * (MAX_COURIER_PASSWORD_LENGTH + 1),
                }
            },
            "credentials.password",
        ),
    ],
    ids=[
        "customer",
        "transfer",
        "transfer-line",
        "financial-year",
        "category",
        "vendor",
        "warehouse",
        "user-preferences",
        "courier-password",
    ],
)
async def test_oversized_text_is_a_field_error(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any], field: str
) -> None:
    response = await client.request(method, path, json=body)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", error
    assert field in error["details"]["fields"], error["details"]["fields"]
