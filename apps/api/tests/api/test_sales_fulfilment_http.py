"""Sales-order fulfilment over HTTP (WP E1a).

The regression this file exists for: a consignment raised through the API with
the body the dispatch screen sends - every line naming its sales-order line -
consumes the order's reservation, moves the order line's dispatched quantity and
walks the order's status. Before E1a the screen sent no order line, the
reservation stayed standing, free stock left instead, and the order still
claimed to be part-dispatched. Nothing caught it, because every test fed the
service a payload no screen ever sent.

Also settled at the boundary: the allocation read reports what the screen
pre-fills from, and the top-up is gated on ``sales_order.update``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrderLine, SalesOrderStatus
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
from src.modules.warehouses.models import WarehouseType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def _principal_without(base: Principal, *missing: Permission) -> Principal:
    """The same person holding every right except ``missing``."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission if p not in missing),
        role_codes=base.role_codes,
    )


@dataclass
class Order:
    id: uuid.UUID
    line_id: uuid.UUID
    item_id: uuid.UUID
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID


@pytest.fixture
async def partly_reserved(
    session: AsyncSession, tenant: TenantContext, principal: Principal, company: Any
) -> Order:
    """500 ordered with 200 in stock: confirmed, 200 reserved, 300 short."""
    company.state_code = "27"
    suffix = uuid.uuid4().hex[:8].upper()
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"FG-{suffix}",
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
        sale_price=Decimal("100"),
        gst_rate=Decimal("18"),
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=f"WH-{suffix}",
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    await inventory.post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("200"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code="OPENING",
        reason="Fulfilment fixture stock",
        unit_cost=Decimal("60"),
    )
    order, lines = await sales.create_order(
        session,
        tenant,
        principal,
        SalesOrderCreate(
            customer_id=customer.id,
            warehouse_id=warehouse.id,
            required_date=today() + timedelta(days=7),
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=item.id, quantity=Decimal("500"), uom_id=uom.id
                )
            ],
        ),
    )
    await sales.confirm_order(session, tenant, principal, order.id, reason="Accepted")
    return Order(order.id, lines[0].id, item.id, customer.id, warehouse.id)


async def test_the_allocation_read_reports_what_the_dispatch_screen_prefills(
    api_client: AsyncClient, partly_reserved: Order
) -> None:
    response = await api_client.get(f"/sales/{partly_reserved.id}/allocation-status")

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == SalesOrderStatus.CONFIRMED
    line = body["lines"][0]
    assert line["line_id"] == str(partly_reserved.line_id)
    assert _money(line["reserved_quantity"]) == Decimal("200")
    assert _money(line["dispatchable_quantity"]) == Decimal("200")
    assert _money(line["shortfall_quantity"]) == Decimal("300")


async def test_a_ui_shaped_consignment_consumes_the_reservation_and_moves_the_order(
    api_client: AsyncClient, session: AsyncSession, partly_reserved: Order
) -> None:
    o = partly_reserved
    reservation_id = (await session.get(SalesOrderLine, o.line_id)).reservation_id  # type: ignore[union-attr]
    # Field for field what CreateDispatchDialog posts for a pre-filled order line.
    created = await api_client.post(
        "/dispatch",
        json={
            "dispatch_type": "SALES",
            "warehouse_id": str(o.warehouse_id),
            "customer_id": str(o.customer_id),
            "vendor_id": None,
            "sales_order_id": str(o.id),
            "dispatch_date": today().isoformat(),
            "expected_delivery_date": None,
            "transporter_id": None,
            "courier_name": None,
            "transport_name": None,
            "tracking_id": None,
            "lr_number": "LR-4471",
            "vehicle_number": None,
            "driver_name": None,
            "driver_mobile": None,
            "eway_bill_number": None,
            "total_boxes": None,
            "total_weight_kg": None,
            "freight_amount": None,
            "freight_paid_by": None,
            "notes": None,
            "lines": [
                {
                    "item_id": str(o.item_id),
                    "sales_order_line_id": str(o.line_id),
                    "quantity": "200",
                    "serial_ids": [],
                    "carton_ids": [],
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    dispatch_id = created.json()["data"]["id"]
    ready = await api_client.post(f"/dispatch/{dispatch_id}/ready", json={"reason": "Loaded"})
    assert ready.status_code == 200, ready.text

    departed = await api_client.post(
        f"/dispatch/{dispatch_id}/dispatch", json={"reason": "Leaves the gate"}
    )

    assert departed.status_code == 200, departed.text
    result = departed.json()["data"]
    # The order's own promise was consumed - not free stock beside it.
    assert result["consumed_reservation_ids"] == [str(reservation_id)]
    assert result["sales_order_status"] == SalesOrderStatus.PARTIALLY_DISPATCHED
    reservation = (
        await session.execute(
            select(StockReservation)
            .where(StockReservation.id == reservation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reservation.status is ReservationStatus.CONSUMED

    view = (await api_client.get(f"/sales/{o.id}/allocation-status")).json()["data"]
    line = view["lines"][0]
    assert _money(line["dispatched_quantity"]) == Decimal("200")
    assert _money(line["reserved_quantity"]) == Decimal("0")
    assert _money(line["dispatchable_quantity"]) == Decimal("0")
    assert _money(line["shortfall_quantity"]) == Decimal("300")


async def test_a_line_that_names_no_order_line_is_refused_naming_the_line(
    api_client: AsyncClient, partly_reserved: Order
) -> None:
    o = partly_reserved
    response = await api_client.post(
        "/dispatch",
        json={
            "dispatch_type": "SALES",
            "warehouse_id": str(o.warehouse_id),
            "customer_id": str(o.customer_id),
            "sales_order_id": str(o.id),
            "dispatch_date": today().isoformat(),
            "lr_number": "LR-4472",
            "lines": [{"item_id": str(o.item_id), "quantity": "10"}],
        },
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "Line 1" in error["message"]


async def test_the_top_up_needs_the_right_to_change_sales_orders(
    api_client: AsyncClient, principal: Principal, partly_reserved: Order
) -> None:
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.SALES_ORDER_UPDATE
    )
    refused = await api_client.post(f"/sales/{partly_reserved.id}/allocate", json={})
    assert refused.status_code == 403, refused.text

    app.dependency_overrides[get_current_principal] = lambda: principal
    allowed = await api_client.post(
        f"/sales/{partly_reserved.id}/allocate", json={"reason": "Checked for new stock"}
    )
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["data"]["fully_allocated"] is False
    assert _money(body["data"]["lines"][0]["shortfall_quantity"]) == Decimal("300")
    assert "still short" in body["message"]
