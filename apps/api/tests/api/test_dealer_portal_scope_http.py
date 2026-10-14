"""Dealer isolation over HTTP (WP C3a, P0).

A dealer account reaches the ERP through exactly one door. The rules that keep
it there are settled before a handler runs, so they are tested where they live:

* **The route allow-list** (``core.deps.dealer_route_allowed``, applied in
  ``get_current_principal``): a dealer principal may call
  ``/dealer-portal/**``, its own ``/notifications/**`` and auth self-service -
  every other route is 403 ``SCOPE_DENIED``, whatever permissions its roles
  carry. Tested through the real dependency chain with a real token, because
  ``api_client`` overrides ``get_current_principal`` and would skip the gate.
* **The portal refuses staff** (``require_dealer_user``): an administrator holds
  every permission string and must not act "as" a dealer.
* **Ownership is "not found"**: another dealer's order, consignment, tracking
  or statement answers 404 - confirming it exists would itself be the
  disclosure.
* **The dealer reads no internal fields**: cost, approval evidence and other
  dealers' data stay internal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.permissions import ROLE_PRESETS
from tests.api.dealer_portal_support import (
    code,
    dealer_customer,
    dealer_principal,
    portal_user,
    sellable_item,
    sign_in,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
async def world(session: AsyncSession, tenant: TenantContext, company) -> Any:
    """Two dealers with no orders yet, plus one sellable item."""
    company.state = "Gujarat"
    company.state_code = "24"
    await session.flush()
    ours = await dealer_customer(session, tenant, "Our Hardware Mart")
    theirs = await dealer_customer(session, tenant, "Somebody Else's Mart")
    item = await sellable_item(session, tenant)
    return {"ours": ours, "theirs": theirs, "item": item}


# -----------------------------------------------------------------------------
# The route allow-list, through the real dependency chain
# -----------------------------------------------------------------------------


def _real_chain(app: Any) -> None:
    from src.core.deps import get_current_principal

    app.dependency_overrides.pop(get_current_principal, None)


@pytest.mark.parametrize(
    "path",
    [
        "/sales/orders",
        "/sales/demand-summary",
        "/products/items",
        "/customers",
        "/dispatch",
        "/procurement/purchase-orders",
        "/documents",
        "/users",
        "/roles",
        "/vendor-portal/me",
    ],
)
async def test_a_dealer_token_is_refused_on_every_staff_route(
    api_client, session, tenant, principal, world, path
):
    from src.core.security import create_access_token
    from src.main import app

    dealer_user = await portal_user(session, tenant, world["ours"].id)
    _real_chain(app)
    token, _ = create_access_token(dealer_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


async def test_a_dealer_token_is_refused_even_when_a_role_grants_staff_rights(
    api_client, session, tenant, principal, world
):
    """The gate is structural: a dealer role mistakenly granted sales rights changes nothing."""
    from src.core.security import create_access_token
    from src.main import app
    from tests.api.dealer_portal_support import role

    leaky = await role(
        session,
        tenant,
        role_code=code("LEAKY"),
        permissions=["sales_order.view", "dispatch.view", "dealer_portal.browse"],
    )
    dealer_user = await portal_user(session, tenant, world["ours"].id, with_role=leaky)
    _real_chain(app)
    token, _ = create_access_token(dealer_user.id, principal.company_id)
    for path in ("/sales/orders", "/dispatch"):
        result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 403, (path, result.text)
        assert result.json()["error"]["code"] == "SCOPE_DENIED"


@pytest.mark.parametrize(
    "path",
    [
        "/dealer-portal/me",
        "/dealer-portal/catalog/categories",
        "/dealer-portal/catalog/brands",
        "/dealer-portal/catalog/items",
        "/dealer-portal/cart",
        "/dealer-portal/wishlist",
        "/dealer-portal/orders",
        "/dealer-portal/statement",
        "/dealer-portal/outstanding",
        "/notifications/unread-count",
        "/auth/me",
    ],
)
async def test_a_dealer_token_reaches_the_portal(
    api_client, session, tenant, principal, world, path
):
    from src.core.security import create_access_token
    from src.main import app

    dealer_user = await portal_user(session, tenant, world["ours"].id)
    _real_chain(app)
    token, _ = create_access_token(dealer_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize(
    ("preset", "path", "allowed"),
    [
        ("DEALER_ACCOUNTS", "/dealer-portal/cart", False),
        ("DEALER_ACCOUNTS", "/dealer-portal/statement", True),
        ("DEALER_BUYER", "/dealer-portal/statement", False),
        ("DEALER_BUYER", "/dealer-portal/cart", True),
        ("DEALER_BUYER", "/dealer-portal/wishlist", True),
        ("DEALER_OWNER", "/dealer-portal/outstanding", True),
    ],
)
async def test_each_dealer_role_holds_only_its_portal_rights(
    api_client, session, tenant, principal, world, preset, path, allowed
):
    from src.core.security import create_access_token
    from src.main import app

    dealer_user = await portal_user(session, tenant, world["ours"].id, preset=preset)
    _real_chain(app)
    token, _ = create_access_token(dealer_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == (200 if allowed else 403), result.text


# -----------------------------------------------------------------------------
# The portal refuses staff
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/dealer-portal/me"),
        ("GET", "/dealer-portal/catalog/items"),
        ("GET", "/dealer-portal/cart"),
        ("GET", "/dealer-portal/orders"),
        ("POST", "/dealer-portal/checkout"),
    ],
)
async def test_staff_cannot_impersonate_a_dealer(api_client, method, path):
    result = await api_client.request(method, path, json={} if method == "POST" else None)
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


# -----------------------------------------------------------------------------
# Ownership is "not found"
# -----------------------------------------------------------------------------


async def test_cross_dealer_orders_dispatches_and_tracking_are_not_found(
    api_client, session, tenant, principal, world
):
    """Dealer B's order, consignments and tracking answer 404 for dealer A."""
    from datetime import timedelta

    from src.core.clock import today
    from src.modules.sales import service as sales_service
    from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
    from tests import seed_support

    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=code("WH")
    )
    tenant_b = TenantContext(
        company_id=tenant.company_id,
        user_id=principal.user_id,
        customer_id=world["theirs"].id,
    )
    order, _ = await sales_service.create_order(
        session,
        tenant_b,
        principal,
        SalesOrderCreate(
            customer_id=world["theirs"].id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[
                SalesOrderLineIn(line_number=1, item_id=world["item"].id, quantity=Decimal("1"))
            ],
        ),
        source="DEALER_PORTAL",
    )

    sign_in(dealer_principal(principal, world["ours"].id))
    assert (await api_client.get(f"/dealer-portal/orders/{order.id}")).status_code == 404
    listed = await api_client.get("/dealer-portal/orders")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []
    assert (await api_client.get(f"/dealer-portal/orders/{order.id}/dispatches")).status_code == 404
    refused = await api_client.post(
        f"/dealer-portal/orders/{order.id}/cancel", json={"reason": "Not mine to cancel"}
    )
    assert refused.status_code == 404, refused.text

    sign_in(dealer_principal(principal, world["theirs"].id))
    assert (await api_client.get(f"/dealer-portal/orders/{order.id}")).status_code == 200


async def test_the_dealer_reads_no_internal_fields(api_client, session, tenant, principal, world):
    sign_in(dealer_principal(principal, world["ours"].id))

    listed = (await api_client.get("/dealer-portal/catalog/items")).json()["data"]
    assert len(listed) == 1
    row = listed[0]
    for internal in ("standard_cost", "sale_price", "dealer_price", "available_quantity"):
        assert internal not in row, internal
    assert row["price"] == "100.000000"

    me = (await api_client.get("/dealer-portal/me")).json()["data"]
    assert me["customer_id"] == str(world["ours"].id)
    assert "outstanding_amount" in me["credit"]

    statement = await api_client.get("/dealer-portal/statement")
    assert statement.status_code == 200, statement.text
    assert statement.json()["data"]["party_id"] == str(world["ours"].id)

    outstanding = await api_client.get("/dealer-portal/outstanding")
    assert outstanding.status_code == 200, outstanding.text


def test_every_dealer_preset_is_exercised() -> None:
    """The role matrix above names every dealer preset there is."""
    assert {code for code, spec in ROLE_PRESETS.items() if spec.get("is_dealer_role")} == {
        "DEALER_OWNER",
        "DEALER_BUYER",
        "DEALER_ACCOUNTS",
    }
