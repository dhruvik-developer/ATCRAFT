"""Shortfall planning, sales settings and the fulfilment trail over HTTP (WP E1b).

The routes settle three things the services cannot prove on their own:

* ``/sales/settings`` is declared before ``/sales/{order_id}`` and is therefore a
  settings read, not a malformed order id;
* the plan and the trail carry different rights - changing an order is not the
  same permission as looking at one - and an automation endpoint is not a way
  around them;
* the response bodies are the shapes the screens consume: a plan with document
  rows, and the timeline with one empty list per family rather than a missing
  key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.sales import service as sales
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
from src.modules.warehouses.models import WarehouseType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def _principal_without(base: Principal, *missing: Permission) -> Principal:
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
class ShortOrder:
    id: uuid.UUID
    line_id: uuid.UUID


@pytest.fixture
async def short_buy_order(
    session: AsyncSession, tenant: TenantContext, principal: Principal, company: Any
) -> ShortOrder:
    """40 wanted, none in stock: BUY, so the plan is one requisition."""
    company.state_code = "27"
    suffix = uuid.uuid4().hex[:8].upper()
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"BUY-{suffix}",
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.BUY,
        can_purchase=True,
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
    order, lines = await sales.create_order(
        session,
        tenant,
        principal,
        SalesOrderCreate(
            customer_id=customer.id,
            warehouse_id=warehouse.id,
            required_date=today() + timedelta(days=10),
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=item.id, quantity=Decimal("40"), uom_id=uom.id
                )
            ],
        ),
    )
    await sales.confirm_order(session, tenant, principal, order.id, reason="Accepted")
    return ShortOrder(order.id, lines[0].id)


async def test_the_settings_route_is_not_read_as_an_order_id(api_client: AsyncClient) -> None:
    response = await api_client.get("/sales/settings")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["auto_plan_on_confirm"] is True
    assert data["is_default"] is True


async def test_settings_are_read_and_written_under_different_rights(
    api_client: AsyncClient, principal: Principal
) -> None:
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.SETTINGS_VIEW
    )
    refused_read = await api_client.get("/sales/settings")
    assert refused_read.status_code == 403, refused_read.text

    app.dependency_overrides[get_current_principal] = lambda: principal
    saved = await api_client.put("/sales/settings", json={"auto_plan_on_confirm": False})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"] == {
        "auto_plan_on_confirm": False,
        "is_default": False,
    }

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.SETTINGS_MANAGE
    )
    refused_write = await api_client.put("/sales/settings", json={"auto_plan_on_confirm": True})
    assert refused_write.status_code == 403, refused_write.text

    # Explicit null restores the code default; the row stays sparse.
    app.dependency_overrides[get_current_principal] = lambda: principal
    restored = await api_client.put("/sales/settings", json={"auto_plan_on_confirm": None})
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"] == {"auto_plan_on_confirm": True, "is_default": True}


async def test_the_plan_route_raises_the_draft_supply_the_trail_then_reports(
    api_client: AsyncClient, short_buy_order: ShortOrder
) -> None:
    response = await api_client.post(
        f"/sales/{short_buy_order.id}/plan-shortfall",
        json={"reason": "Planner asked for the balance"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    documents = body["data"]["documents"]
    assert len(documents) == 1
    assert documents[0]["kind"] == "PURCHASE_REQUISITION"
    assert documents[0]["line_id"] == str(short_buy_order.line_id)
    assert _money(documents[0]["quantity"]) == Decimal("40")
    assert "draft document" in body["message"]

    trail = await api_client.get(f"/sales/{short_buy_order.id}/fulfilment")
    assert trail.status_code == 200, trail.text
    data = trail.json()["data"]
    assert data["order_id"] == str(short_buy_order.id)
    assert len(data["lines"]) == 1
    assert data["lines"][0]["line_id"] == str(short_buy_order.line_id)
    assert _money(data["lines"][0]["shortfall_quantity"]) == Decimal("40")
    assert len(data["requisitions"]) == 1
    assert data["requisitions"][0]["id"] == documents[0]["id"]
    assert data["production_orders"] == []
    assert data["dispatches"] == []


async def test_planning_and_reading_the_trail_need_their_own_rights(
    api_client: AsyncClient, principal: Principal, short_buy_order: ShortOrder
) -> None:
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.SALES_ORDER_UPDATE
    )
    refused_plan = await api_client.post(
        f"/sales/{short_buy_order.id}/plan-shortfall", json={"reason": "Not allowed"}
    )
    assert refused_plan.status_code == 403, refused_plan.text

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.SALES_ORDER_VIEW
    )
    refused_trail = await api_client.get(f"/sales/{short_buy_order.id}/fulfilment")
    assert refused_trail.status_code == 403, refused_trail.text

    app.dependency_overrides[get_current_principal] = lambda: principal
    allowed = await api_client.get(f"/sales/{short_buy_order.id}/fulfilment")
    assert allowed.status_code == 200, allowed.text
