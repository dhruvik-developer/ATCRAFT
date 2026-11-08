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

The last section is the guard that keeps it so: it reads every route's request
inputs as pydantic-core compiles them and fails on any int, float or Decimal
with no upper bound, unless :data:`UNBOUNDED_ALLOWED` names the reason. Its
self-tests feed it the violations it exists to catch.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from decimal import Decimal
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from src.core.constants import MAX_INTEGER_COLUMN
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.errors import NotFoundError
from src.core.exception_handlers import register_exception_handlers
from src.modules.bom.schemas import BomLineIn
from src.modules.customers.router import router as customers_router
from src.modules.customers.service import CustomerService
from src.modules.dealer_portal.schemas import DealerCheckoutRequest
from src.modules.notifications.alert_service import NotificationAlertService
from src.modules.notifications.alerts import MAX_ALERT_WINDOW_MINUTES
from src.modules.notifications.router import router as notifications_router
from src.modules.notifications.schemas import NotificationAlertRuleUpdate
from src.modules.procurement import inbound_service
from src.modules.procurement.inbound_schemas import InboundShipmentCreate
from src.modules.procurement.router import router as procurement_router
from src.modules.procurement.schemas import (
    PurchaseOrderAmendRequest,
    PurchaseOrderDuplicateResolutionRequest,
)
from src.modules.products import service as products_service
from src.modules.products.router import router as products_router
from src.modules.products.schemas import ItemCreate, ItemUpdate
from src.modules.sales_returns import service as sales_returns_service
from src.modules.sales_returns.router import router as sales_returns_router
from src.modules.vendor_portal.schemas import VendorPortalReady
from tests.route_support import assert_matches_contract, module_routes

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
    for router in (
        customers_router,
        notifications_router,
        procurement_router,
        products_router,
        sales_returns_router,
    ):
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

    async def rules_probe(self: NotificationAlertService, payload: Any) -> None:
        seen.append(payload)
        raise NotFoundError("Probe", "reached")

    monkeypatch.setattr(products_service, "convert_quantity", probe)
    monkeypatch.setattr(products_service, "resolve_price", probe)
    monkeypatch.setattr(sales_returns_service, "create_return", probe)
    monkeypatch.setattr(inbound_service, "record_inbound_shipment", probe)
    monkeypatch.setattr(CustomerService, "get_credit_status", credit_probe)
    monkeypatch.setattr(NotificationAlertService, "update_rules", rules_probe)
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


# -----------------------------------------------------------------------------
# Integers
# -----------------------------------------------------------------------------


def _alert_rule(**window: int) -> dict[str, Any]:
    rule = {"key": "PO_NOT_ACKNOWLEDGED", "is_enabled": True}
    return {"rules": [{**rule, "threshold_minutes": 60, "remind_every_minutes": 60, **window}]}


def _shipment(packages: int) -> dict[str, Any]:
    return {
        "courier_name": "Blue Dart",
        "shipped_at": "2026-11-01T09:00:00+05:30",
        "packages": packages,
    }


@pytest.mark.parametrize(
    ("model", "field", "largest"),
    [
        (InboundShipmentCreate, "packages", MAX_INTEGER_COLUMN),
        (VendorPortalReady, "packages", MAX_INTEGER_COLUMN),
        (ItemCreate, "shelf_life_days", MAX_INTEGER_COLUMN),
        (ItemUpdate, "shelf_life_days", MAX_INTEGER_COLUMN),
        (NotificationAlertRuleUpdate, "threshold_minutes", MAX_ALERT_WINDOW_MINUTES),
        (NotificationAlertRuleUpdate, "remind_every_minutes", MAX_ALERT_WINDOW_MINUTES),
        # A line names an operation its BOM defines, numbered as operations are.
        (BomLineIn, "operation_sequence", 100_000),
        # A version past the INTEGER column's range can never match the row.
        (DealerCheckoutRequest, "cart_version", MAX_INTEGER_COLUMN),
        (PurchaseOrderAmendRequest, "expected_version", MAX_INTEGER_COLUMN),
        (
            PurchaseOrderDuplicateResolutionRequest,
            "expected_candidate_version",
            MAX_INTEGER_COLUMN,
        ),
    ],
)
def test_an_integer_input_stops_at_its_bound(
    model: type[BaseModel], field: str, largest: int
) -> None:
    info = model.model_fields[field]
    # A bound written as ``Field(le=...)`` lives in the field's metadata; one
    # written inside ``Annotated[int, Field(...)] | None`` lives in the annotation.
    constrained: Any = (
        Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
    )
    adapter: TypeAdapter[int | None] = TypeAdapter(constrained)

    assert adapter.validate_python(largest) == largest
    with pytest.raises(PydanticValidationError):
        adapter.validate_python(largest + 1)


async def test_an_alert_window_past_ten_years_is_a_field_error(
    client: httpx.AsyncClient, reached: list[Any]
) -> None:
    too_long = MAX_ALERT_WINDOW_MINUTES + 1

    assert "rules.0.threshold_minutes" in _field_errors(
        await client.put("/notifications/alert-rules", json=_alert_rule(threshold_minutes=too_long))
    )
    assert "rules.0.remind_every_minutes" in _field_errors(
        await client.put(
            "/notifications/alert-rules", json=_alert_rule(remind_every_minutes=too_long)
        )
    )
    assert reached == []

    longest = _alert_rule(
        threshold_minutes=MAX_ALERT_WINDOW_MINUTES, remind_every_minutes=MAX_ALERT_WINDOW_MINUTES
    )
    assert (await client.put("/notifications/alert-rules", json=longest)).status_code == 404
    (saved,) = reached
    assert saved.rules[0].threshold_minutes == MAX_ALERT_WINDOW_MINUTES


async def test_a_package_count_past_the_integer_column_is_a_field_error(
    client: httpx.AsyncClient, reached: list[Any]
) -> None:
    url = f"/procurement/purchase-orders/{uuid.uuid4()}/shipments"

    assert "packages" in _field_errors(
        await client.post(url, json=_shipment(MAX_INTEGER_COLUMN + 1))
    )
    assert reached == []

    assert (await client.post(url, json=_shipment(MAX_INTEGER_COLUMN))).status_code == 404
    (recorded,) = reached
    assert recorded.packages == MAX_INTEGER_COLUMN


# -----------------------------------------------------------------------------
# The guard: no request number without an upper bound
# -----------------------------------------------------------------------------

#: The keywords that cap each numeric core-schema type from above. A Decimal is
#: also capped by ``max_digits``, which bounds its magnitude.
_UPPER_BOUNDS: dict[str, frozenset[str]] = {
    "decimal": frozenset({"le", "lt", "max_digits"}),
    "int": frozenset({"le", "lt"}),
    "float": frozenset({"le", "lt"}),
}

#: Core-schema keys that hold a nested schema, or a list or mapping of them.
_CHILD_KEYS = (
    "schema",
    "items_schema",
    "keys_schema",
    "values_schema",
    "lax_schema",
    "strict_schema",
    "json_schema",
    "python_schema",
    "steps",
    "choices",
)

#: Request numbers bounded somewhere the schema cannot show, each with the reason.
#: Keyed ``"METHOD path field"`` exactly as the guard reports an offender.
UNBOUNDED_ALLOWED: dict[str, str] = {
    "POST /notifications/push/subscriptions expiration_time": (
        "a browser push subscription's epoch milliseconds, larger than any INTEGER; "
        "the service refuses an instant the calendar cannot hold with a 422"
    ),
}


def request_numbers(schema: Mapping[str, Any], prefix: str = "") -> list[tuple[str, bool]]:
    """``(path, has an upper bound)`` for every int, float and Decimal in a core schema.

    Walks what pydantic-core will actually validate - so a bound written as
    ``Field(le=...)``, inside ``Annotated[...] | None``, on a list item or in a
    ``Query(...)`` all count - and follows each definition reference once per
    path, so a recursive model terminates.
    """
    found: list[tuple[str, bool]] = []
    definitions: dict[str, Mapping[str, Any]] = {}

    def walk(node: Any, where: str, entered: frozenset[str]) -> None:
        if isinstance(node, list | tuple):
            for child in node:
                walk(child, where, entered)
            return
        if not isinstance(node, Mapping):
            return
        kind = node.get("type")
        if kind == "definitions":
            definitions.update({entry["ref"]: entry for entry in node["definitions"]})
        elif kind == "definition-ref":
            ref = node["schema_ref"]
            if ref not in entered:
                walk(definitions[ref], where, entered | {ref})
            return
        elif kind in _UPPER_BOUNDS:
            found.append((where, any(key in node for key in _UPPER_BOUNDS[kind])))
            return
        elif kind in {"model-fields", "typed-dict"}:
            for name, field in node["fields"].items():
                walk(field["schema"], f"{where}.{name}" if where else name, entered)
            return
        for key in _CHILD_KEYS:
            child = node.get(key)
            if isinstance(child, Mapping) and key == "choices":
                child = list(child.values())
            if child is not None:
                walk(child, f"{where}.[]" if key == "items_schema" else where, entered)

    walk(schema, prefix, frozenset())
    return found


def _request_params(dependant: Any) -> Iterator[tuple[str, Any]]:
    for source in ("path", "query", "header", "cookie", "body"):
        for param in getattr(dependant, f"{source}_params"):
            yield source, param
    for dependency in dependant.dependencies:
        yield from _request_params(dependency)


def route_numbers() -> list[tuple[str, bool]]:
    """Every numeric request input of every mounted route, as ``METHOD path field``.

    Body fields are named as their field errors are (``lines.[].quantity``),
    every other input with its transport (``query.quantity``).
    """
    found: set[tuple[str, bool]] = set()
    for mounted in module_routes():
        for source, param in _request_params(mounted.route.dependant):
            info = param.field_info
            # How FastAPI itself builds the validator for one parameter.
            adapter: TypeAdapter[Any] = TypeAdapter(Annotated[info.annotation, info])
            prefix = "" if source == "body" else f"{source}.{param.alias}"
            for where, bounded in request_numbers(adapter.core_schema, prefix):
                found.add((f"{mounted} {where}", bounded))
    return sorted(found)


def test_the_number_guard_reads_every_route_and_its_numbers() -> None:
    assert_matches_contract(module_routes())
    # Hundreds today; a walker that stopped descending would find a handful.
    assert len(route_numbers()) > 300


def test_every_request_number_has_an_upper_bound() -> None:
    offenders = [
        name for name, bounded in route_numbers() if not bounded and name not in UNBOUNDED_ALLOWED
    ]
    assert not offenders, (
        "Bound these request numbers from above - le= for an int (MAX_INTEGER_COLUMN is "
        "an INTEGER column's own limit), max_digits/decimal_places for a Decimal - or, "
        f"if something else bounds one, add it to UNBOUNDED_ALLOWED with the reason: {offenders}"
    )


def test_every_number_allowance_is_still_needed() -> None:
    unbounded = {name for name, bounded in route_numbers() if not bounded}
    stale = sorted(set(UNBOUNDED_ALLOWED) - unbounded)
    assert not stale, f"remove these UNBOUNDED_ALLOWED entries, they are bounded now: {stale}"


class _Line(BaseModel):
    quantity: Decimal = Field(gt=0)
    sequence: int | None = Field(default=None, ge=1)
    position: Annotated[int, Field(ge=0, le=9)] = 0
    children: list[_Line] = []


class _Payload(BaseModel):
    lines: list[_Line]
    total: Decimal = Field(max_digits=18, decimal_places=4)
    ratio: float = Field(ge=0)
    weights: dict[str, Annotated[int, Field(lt=10)]] = {}


def test_the_number_guard_reports_what_has_no_upper_bound() -> None:
    found = request_numbers(_Payload.__pydantic_core_schema__)

    assert sorted(where for where, bounded in found if not bounded) == [
        "lines.[].quantity",
        "lines.[].sequence",
        "ratio",
    ]
    assert {where for where, bounded in found if bounded} >= {"lines.[].position", "total"}


@pytest.mark.parametrize(
    ("query", "bounded"),
    [
        (Query(gt=0), False),
        (Query(gt=0, le=Decimal("99999999999999")), True),
        (Query(gt=0, max_digits=18, decimal_places=4), True),
    ],
)
def test_the_number_guard_reads_query_parameter_bounds(query: Any, bounded: bool) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(Annotated[Decimal, query])

    assert request_numbers(adapter.core_schema, "query.quantity") == [("query.quantity", bounded)]
