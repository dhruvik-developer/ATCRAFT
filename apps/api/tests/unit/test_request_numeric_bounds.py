"""Numeric request inputs carry an upper bound, so an oversized one is a 422.

A number with only a lower bound reaches ``to_quantity``/``to_money`` (whose
``Decimal.quantize`` raises ``InvalidOperation`` past 28 digits), a NUMERIC(18,4)
column (SQLSTATE 22003) or an INTEGER one (asyncpg refuses to encode it) - and
each of those surfaces as a 500 that names no field. These tests drive the
bounds through the HTTP contract: each route is mounted on a bare application
whose database and principal are stubbed, and the service call behind it is
replaced by a probe that records what reached it and answers 404. So a value
the door refuses is a 422 naming the field, and a value it admits arrives at
the service exactly as keyed - with no database involved.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.errors import NotFoundError
from src.core.exception_handlers import register_exception_handlers
from src.modules.customers.router import router as customers_router
from src.modules.customers.service import CustomerService
from src.modules.products import service as products_service
from src.modules.products.router import router as products_router
from src.modules.sales_returns import service as sales_returns_service
from src.modules.sales_returns.router import router as sales_returns_router

pytestmark = pytest.mark.unit

#: Too large for ``Decimal.quantize`` at 28 digits, for NUMERIC(18,4), and too
#: fine for its scale - the three ways an unbounded Decimal used to fail.
OVERSIZED = ("1e25", "100000000000000", "1.00001")
#: The largest NUMERIC(18,4) value: fourteen integer digits and four places.
LARGEST = "99999999999999.9999"


async def _no_database() -> AsyncIterator[None]:
    yield None


def _administrator() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="bounds@example.test",
        full_name="Request bounds",
        is_superuser=True,
        permissions=frozenset(),
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    register_exception_handlers(application)
    for router in (customers_router, products_router, sales_returns_router):
        application.include_router(router)
    application.dependency_overrides[get_db] = _no_database
    application.dependency_overrides[get_current_principal] = _administrator
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as http:
        yield http


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace each service call behind the routes with a recording probe."""
    seen: list[Any] = []

    async def probe(*args: Any, **kwargs: Any) -> None:
        seen.append(kwargs or args[-1])
        raise NotFoundError("Probe", "reached")

    async def credit_probe(self: CustomerService, customer_id: uuid.UUID, value: Any) -> None:
        seen.append(value)
        raise NotFoundError("Probe", "reached")

    monkeypatch.setattr(products_service, "convert_quantity", probe)
    monkeypatch.setattr(products_service, "resolve_price", probe)
    monkeypatch.setattr(sales_returns_service, "create_return", probe)
    monkeypatch.setattr(CustomerService, "get_credit_status", credit_probe)
    return seen


def _field_errors(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", error
    fields: dict[str, list[str]] = error["details"]["fields"]
    return fields


def _convert(quantity: str) -> dict[str, str]:
    return {"from_uom_id": str(uuid.uuid4()), "to_uom_id": str(uuid.uuid4()), "quantity": quantity}


def _resolved_price(quantity: str) -> dict[str, str]:
    return {"price_type": "SALE", "quantity": quantity}


def _sales_return(quantity: str) -> dict[str, Any]:
    return {
        "dispatch_id": str(uuid.uuid4()),
        "lines": [
            {"dispatch_line_id": str(uuid.uuid4()), "quantity": quantity, "reason_code": "DAMAGED"}
        ],
    }


# -----------------------------------------------------------------------------
# Decimals
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", OVERSIZED)
async def test_an_oversized_quantity_or_value_is_a_field_error(
    client: httpx.AsyncClient, reached: list[Any], quantity: str
) -> None:
    item = uuid.uuid4()
    customer = uuid.uuid4()

    assert "query.quantity" in _field_errors(
        await client.get(f"/products/items/{item}/resolved-price", params=_resolved_price(quantity))
    )
    assert "query.order_value" in _field_errors(
        await client.get(f"/customers/{customer}/credit-status", params={"order_value": quantity})
    )
    assert "lines.0.quantity" in _field_errors(
        await client.post("/sales-returns", json=_sales_return(quantity))
    )
    assert reached == []


@pytest.mark.parametrize("quantity", ("1e25", "100000000000000", "1.0000001"))
async def test_the_converter_refuses_past_its_six_places(
    client: httpx.AsyncClient, reached: list[Any], quantity: str
) -> None:
    response = await client.get("/products/uom-conversions/convert", params=_convert(quantity))

    assert "query.quantity" in _field_errors(response)
    assert reached == []


async def test_the_largest_stored_quantity_reaches_the_service_as_keyed(
    client: httpx.AsyncClient, reached: list[Any]
) -> None:
    item = uuid.uuid4()
    customer = uuid.uuid4()

    responses = [
        await client.get(f"/products/items/{item}/resolved-price", params=_resolved_price(LARGEST)),
        await client.get(f"/customers/{customer}/credit-status", params={"order_value": LARGEST}),
        await client.post("/sales-returns", json=_sales_return("0.0001")),
        # The converter multiplies before it rounds, so it keeps the six places
        # its dialog accepts.
        await client.get(
            "/products/uom-conversions/convert", params=_convert("99999999999999.123456")
        ),
    ]

    assert [response.status_code for response in responses] == [404] * 4
    price, credit, sales_return, conversion = reached
    assert price["quantity"] == Decimal(LARGEST)
    assert credit == Decimal(LARGEST)
    assert sales_return.lines[0].quantity == Decimal("0.0001")
    assert conversion["quantity"] == Decimal("99999999999999.123456")
