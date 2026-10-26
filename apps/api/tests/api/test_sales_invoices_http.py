"""Invoice permissions and UI-shaped fulfilment requests over HTTP."""

from __future__ import annotations

import uuid

import pytest

from src.core.deps import Principal, get_current_principal
from src.modules.sales.models import SalesOrderStatus
from tests.integration.test_sales_invoice_service import bench as bench  # fixture re-export

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/finance/sales-invoices", None),
        ("GET", "/finance/sales-invoices/preview?dispatch_id={id}", None),
        ("GET", "/finance/sales-invoices/{id}", None),
        ("POST", "/finance/sales-invoices", {"dispatch_id": "{id}"}),
        ("POST", "/finance/sales-invoices/{id}/cancel", {"reason": "Wrong issue"}),
        ("POST", "/finance/sales-invoices/{id}/pdf", None),
    ],
)
async def test_each_invoice_route_enforces_its_permission(
    api_client, principal, method, path, body
):
    from src.main import app

    empty = Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset(),
    )
    app.dependency_overrides[get_current_principal] = lambda: empty
    identifier = str(uuid.uuid4())
    payload = (
        {key: value.replace("{id}", identifier) for key, value in body.items()} if body else None
    )
    response = await api_client.request(method, path.replace("{id}", identifier), json=payload)
    assert response.status_code == 403, response.text


async def test_ui_payload_consumes_reservation_and_moves_order(api_client, bench):
    b = bench
    payload = {
        "dispatch_type": "SALES",
        "dispatch_date": b.order.order_date.isoformat(),
        "customer_id": str(b.customer.id),
        "warehouse_id": str(b.warehouse.id),
        "sales_order_id": str(b.order.id),
        "lines": [
            {
                "item_id": str(b.item.id),
                "sales_order_line_id": str(b.line.id),
                "quantity": "200",
                "serial_ids": [],
                "carton_ids": [],
            }
        ],
    }
    created = await api_client.post("/dispatch", json=payload)
    assert created.status_code == 201, created.text
    dispatch_id = created.json()["data"]["id"]
    ready = await api_client.post(
        f"/dispatch/{dispatch_id}/ready", json={"reason": "Loaded for dispatch"}
    )
    assert ready.status_code == 200, ready.text
    departed = await api_client.post(
        f"/dispatch/{dispatch_id}/dispatch", json={"reason": "Leaves the gate"}
    )
    assert departed.status_code == 200, departed.text
    result = departed.json()["data"]
    assert result["consumed_reservation_ids"]
    assert result["sales_order_status"] == SalesOrderStatus.PARTIALLY_DISPATCHED
    view = await api_client.get(f"/sales/{b.order.id}/allocation-status")
    line = view.json()["data"]["lines"][0]
    assert line["dispatched_quantity"] == "200.0000"
    assert line["shortfall_quantity"] == "300.0000"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/finance/credit-notes"),
        ("GET", "/finance/credit-notes/{id}"),
        ("POST", "/finance/credit-notes"),
        ("POST", "/finance/credit-notes/{id}/cancel"),
        ("POST", "/finance/credit-notes/{id}/pdf"),
    ],
)
async def test_credit_note_routes_enforce_their_permissions(api_client, principal, method, path):
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset(),
    )
    identifier = str(uuid.uuid4())
    response = await api_client.request(
        method, path.replace("{id}", identifier), json={"reason": "Wrong issue"}
    )
    assert response.status_code == 403, response.text


async def test_duplicate_credit_lines_return_field_validation_errors(api_client):
    line = {"sales_invoice_line_id": str(uuid.uuid4()), "quantity": "1"}
    response = await api_client.post(
        "/finance/credit-notes",
        json={
            "sales_invoice_id": str(uuid.uuid4()),
            "note_date": "2026-10-24",
            "reason_type": "PRICE_ADJUSTMENT",
            "lines": [line, line],
        },
    )
    assert response.status_code == 422, response.text
    assert "only once" in response.text
