"""Vendor-portal principals over HTTP, on the purchase-order routes.

A vendor account holds ``purchase_order.update`` so it can acknowledge an order
and record the courier on it - and that same permission gates the buyer-side
PATCH and submit. Two rules keep the portal in its lane, and both are settled
before a handler runs, so they are tested at the HTTP boundary where they live:

* another vendor's order is *not found*, never *forbidden*. Confirming that it
  exists is itself a disclosure, so the repository's ``_scoped`` filters every
  read and lock by the principal's vendor (master rules section 16);
* the buyer-side edit and submit are closed to every vendor account, whichever
  order it names, with ``SCOPE_DENIED`` (``forbid_vendor_users``). That gate
  runs before any lookup, so a vendor's PATCH on *another* vendor's order is
  also 403 rather than 404 - which discloses nothing, because no vendor can
  PATCH any order at all.

The orders are raised through the service as the buyer; only the vendor's
requests go over HTTP, because the vendor's requests are what is under test.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.procurement.schemas import PurchaseOrderCreate, PurchaseOrderLineIn
from src.modules.products.models import ProductionType
from src.modules.vendors.models import Vendor, VendorType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def _approved_vendor(session: AsyncSession, tenant: TenantContext, name: str) -> Vendor:
    vendor = Vendor(
        company_id=tenant.company_id,
        code=_code("V"),
        name=name,
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER.value],
        payment_terms_days=30,
        lead_time_days=7,
        currency="INR",
        is_approved=True,
        # The database refuses an approved vendor with no approval timestamp.
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
    )
    session.add(vendor)
    await session.flush()
    return vendor


@pytest.fixture
async def orders(session: AsyncSession, tenant: TenantContext, principal: Principal):
    """One draft order for each of two approved vendors, raised by the buyer.

    Returns ``(our_vendor, our_order, their_order)``: the portal principal under
    test belongs to ``our_vendor``. Different items on the two orders, so the
    duplicate-order matcher has nothing to say about the second.
    """
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    ours = await _approved_vendor(session, tenant, "Our Fasteners")
    theirs = await _approved_vendor(session, tenant, "Somebody Else's Fasteners")

    raised: list[PurchaseOrder] = []
    for vendor in (ours, theirs):
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            base_uom_id=uom.id,
            purchase_uom_id=uom.id,
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
            production_type=ProductionType.BUY,
            can_purchase=True,
        )
        order, _, _ = await procurement_service.create_purchase_order(
            session,
            tenant,
            principal,
            PurchaseOrderCreate(
                vendor_id=vendor.id,
                warehouse_id=warehouse.id,
                expected_delivery_date=today() + timedelta(days=10),
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=item.id,
                        quantity=Decimal("10"),
                        unit_price=Decimal("50"),
                    )
                ],
            ),
        )
        raised.append(order)
    return ours, raised[0], raised[1]


def _as_vendor_owner(base: Principal, vendor_id: uuid.UUID) -> Principal:
    """The VENDOR_OWNER preset, bound to one vendor - what the portal token resolves to."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS["VENDOR_OWNER"]["permissions"])
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email="owner@vendor.test",
        full_name="Vendor Owner",
        is_superuser=False,
        permissions=frozenset(str(p) for p in granted),
        role_codes=("VENDOR_OWNER",),
        vendor_id=vendor_id,
    )


def _sign_in_as_vendor_owner(principal: Principal, vendor: Vendor) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _as_vendor_owner(principal, vendor.id)


class TestAnotherVendorsOrder:
    async def test_it_is_not_found_on_every_route_the_vendor_may_otherwise_use(
        self, api_client, principal: Principal, orders
    ) -> None:
        ours, _, theirs_order = orders
        _sign_in_as_vendor_owner(principal, ours)
        path = f"/procurement/purchase-orders/{theirs_order.id}"

        assert (await api_client.get(path)).status_code == 404
        acknowledged = await api_client.post(f"{path}/acknowledge", json={"reason": "Accepted"})
        assert acknowledged.status_code == 404, acknowledged.text
        shipped = await api_client.post(
            f"{path}/shipment", json={"courier_name": "Blue Dart", "tracking_reference": "BD-1"}
        )
        assert shipped.status_code == 404, shipped.text

    async def test_the_buyer_side_edit_is_refused_before_the_order_is_even_looked_up(
        self, api_client, principal: Principal, orders
    ) -> None:
        ours, _, theirs_order = orders
        _sign_in_as_vendor_owner(principal, ours)

        response = await api_client.patch(
            f"/procurement/purchase-orders/{theirs_order.id}", json={"notes": "Not mine"}
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "SCOPE_DENIED"


class TestTheVendorsOwnOrder:
    async def test_it_is_visible_but_the_buyer_side_edit_and_submit_are_scope_denied(
        self, api_client, session: AsyncSession, principal: Principal, orders
    ) -> None:
        ours, our_order, _ = orders
        _sign_in_as_vendor_owner(principal, ours)
        path = f"/procurement/purchase-orders/{our_order.id}"

        visible = await api_client.get(path)
        assert visible.status_code == 200, visible.text
        assert visible.json()["data"]["id"] == str(our_order.id)

        edited = await api_client.patch(path, json={"notes": "Vendor rewriting the order"})
        assert edited.status_code == 403, edited.text
        assert edited.json()["error"]["code"] == "SCOPE_DENIED"

        submitted = await api_client.post(f"{path}/submit", json={"reason": "Vendor submitting"})
        assert submitted.status_code == 403, submitted.text
        assert submitted.json()["error"]["code"] == "SCOPE_DENIED"

        status, notes = (
            await session.execute(
                select(PurchaseOrder.status, PurchaseOrder.notes).where(
                    PurchaseOrder.id == our_order.id
                )
            )
        ).one()
        assert PurchaseOrderStatus(status) is PurchaseOrderStatus.DRAFT
        assert notes is None
