"""Vendor-portal principals over HTTP, on the purchase-order routes.

A vendor account holds ``purchase_order.update`` so it can acknowledge an order
and record the courier on it - and that same permission gates the buyer-side
PATCH and submit. The rules that keep the portal in its lane are settled before
a handler runs, so they are tested at the HTTP boundary where they live:

* another vendor's order is *not found*, never *forbidden*. Confirming that it
  exists is itself a disclosure, so the repository's ``_scoped`` filters every
  read and lock by the principal's vendor (master rules section 16);
* a vendor sees only orders it has been sent - SENT and later, and a cancelled
  one only if it had been sent. Its own DRAFT is "not found" too: an order the
  buyer is still typing, with prices nobody has approved
  (docs/assumptions.md §56);
* a vendor user restricted to item categories sees the orders raised for those
  categories (or beneath them), plus any order assigned to it by name;
* the buyer-side edit and submit, the duplicate check and the requisition
  conversion are closed to every vendor account with ``SCOPE_DENIED``
  (``forbid_vendor_users``). That gate runs before any lookup, so a vendor's
  PATCH on *another* vendor's order is also 403 rather than 404 - which
  discloses nothing, because no vendor can PATCH any order at all.

The orders are raised and sent through the service as the buyer; only the
vendor's requests go over HTTP, because the vendor's requests are what is under
test.
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
from src.modules.products.models import ItemCategory, ProductionType
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


async def _raise_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    *,
    vendor: Vendor,
    warehouse_id: uuid.UUID,
    uom_id: uuid.UUID,
    category: ItemCategory | None = None,
) -> PurchaseOrder:
    """A DRAFT order for one fresh item, in ``category`` when one is given."""
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("RM"),
        base_uom_id=uom_id,
        purchase_uom_id=uom_id,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("40"),
        production_type=ProductionType.BUY,
        can_purchase=True,
        category_id=category.id if category is not None else None,
    )
    order, _, _ = await procurement_service.create_purchase_order(
        session,
        tenant,
        principal,
        PurchaseOrderCreate(
            vendor_id=vendor.id,
            warehouse_id=warehouse_id,
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
    return order


def _approver(base: Principal) -> Principal:
    """The buyer's own user holding SUPER_ADMIN - the one who may approve their own order."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=base.permissions,
        role_codes=("SUPER_ADMIN",),
    )


async def _send(session: AsyncSession, tenant: TenantContext, principal: Principal, order_id):
    for target, actor in (
        (PurchaseOrderStatus.PENDING_APPROVAL, principal),
        (PurchaseOrderStatus.APPROVED, _approver(principal)),
        (PurchaseOrderStatus.SENT, principal),
    ):
        await procurement_service.transition_purchase_order(
            session, tenant, actor, order_id, target, reason=str(target)
        )


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

    raised = [
        await _raise_order(
            session, tenant, principal, vendor=vendor, warehouse_id=warehouse.id, uom_id=uom.id
        )
        for vendor in (ours, theirs)
    ]
    return ours, raised[0], raised[1]


def _as_vendor_owner(
    base: Principal,
    vendor_id: uuid.UUID,
    *,
    allowed_category_ids: tuple[uuid.UUID, ...] = (),
) -> Principal:
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
        allowed_category_ids=allowed_category_ids,
    )


def _sign_in_as_vendor_owner(
    principal: Principal, vendor: Vendor, *, allowed_category_ids: tuple[uuid.UUID, ...] = ()
) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    vendor_principal = _as_vendor_owner(
        principal, vendor.id, allowed_category_ids=allowed_category_ids
    )
    app.dependency_overrides[get_current_principal] = lambda: vendor_principal


class TestAnotherVendorsOrder:
    async def test_it_is_not_found_on_every_route_the_vendor_may_otherwise_use(
        self, api_client, session: AsyncSession, tenant, principal: Principal, orders
    ) -> None:
        ours, _, theirs_order = orders
        # Sent, so only the vendor scope can be what hides it.
        await _send(session, tenant, principal, theirs_order.id)
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
    async def test_a_draft_is_not_found(self, api_client, principal: Principal, orders) -> None:
        """Changed deliberately (§56): before WP C0 a vendor could read its own
        DRAFT and PENDING_APPROVAL orders, unapproved prices included."""
        ours, our_order, _ = orders
        _sign_in_as_vendor_owner(principal, ours)

        draft = await api_client.get(f"/procurement/purchase-orders/{our_order.id}")
        assert draft.status_code == 404, draft.text
        listed = await api_client.get("/procurement/purchase-orders")
        assert listed.status_code == 200
        assert str(our_order.id) not in {row["id"] for row in listed.json()["data"]}

    async def test_once_sent_it_is_visible_but_the_buyer_side_edit_and_submit_are_scope_denied(
        self, api_client, session: AsyncSession, tenant, principal: Principal, orders
    ) -> None:
        ours, our_order, _ = orders
        await _send(session, tenant, principal, our_order.id)
        _sign_in_as_vendor_owner(principal, ours)
        path = f"/procurement/purchase-orders/{our_order.id}"

        visible = await api_client.get(path)
        assert visible.status_code == 200, visible.text
        assert visible.json()["data"]["id"] == str(our_order.id)
        listed = await api_client.get("/procurement/purchase-orders")
        assert str(our_order.id) in {row["id"] for row in listed.json()["data"]}

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
        assert PurchaseOrderStatus(status) is PurchaseOrderStatus.SENT
        assert notes is None

    async def test_an_order_cancelled_before_it_was_sent_stays_hidden(
        self, api_client, session: AsyncSession, tenant, principal: Principal, orders
    ) -> None:
        ours, our_order, _ = orders
        await procurement_service.transition_purchase_order(
            session, tenant, principal, our_order.id, PurchaseOrderStatus.CANCELLED, reason="No"
        )
        _sign_in_as_vendor_owner(principal, ours)
        response = await api_client.get(f"/procurement/purchase-orders/{our_order.id}")
        assert response.status_code == 404


class TestCategoryScope:
    async def test_a_category_manager_sees_its_categories_and_what_is_assigned_to_it(
        self, api_client, session: AsyncSession, tenant, principal: Principal
    ) -> None:
        uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
        warehouse = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WH")
        )
        vendor = await _approved_vendor(session, tenant, "Precision Fasteners")
        locks = await seed_support.seed_category(session, company_id=tenant.company_id)
        lock_body = await seed_support.seed_category(
            session, company_id=tenant.company_id, parent=locks
        )
        handles = await seed_support.seed_category(session, company_id=tenant.company_id)

        def order_in(category: ItemCategory):
            return _raise_order(
                session,
                tenant,
                principal,
                vendor=vendor,
                warehouse_id=warehouse.id,
                uom_id=uom.id,
                category=category,
            )

        in_scope = await order_in(locks)
        beneath_scope = await order_in(lock_body)
        out_of_scope = await order_in(handles)
        assigned_out_of_scope = await order_in(handles)
        for order in (in_scope, beneath_scope, out_of_scope, assigned_out_of_scope):
            await _send(session, tenant, principal, order.id)
        # Assigned by name - as when a manager's scope is narrowed after the
        # order was sent to them. The name keeps the order in view.
        assigned_out_of_scope.assigned_vendor_user_id = principal.user_id
        await session.flush()

        _sign_in_as_vendor_owner(principal, vendor, allowed_category_ids=(locks.id,))

        def get(order: PurchaseOrder):
            return api_client.get(f"/procurement/purchase-orders/{order.id}")

        assert (await get(in_scope)).status_code == 200
        assert (await get(beneath_scope)).status_code == 200
        assert (await get(out_of_scope)).status_code == 404
        assert (await get(assigned_out_of_scope)).status_code == 200

        listed = await api_client.get("/procurement/purchase-orders")
        ids = {row["id"] for row in listed.json()["data"]}
        assert {str(in_scope.id), str(beneath_scope.id), str(assigned_out_of_scope.id)} <= ids
        assert str(out_of_scope.id) not in ids


class TestBuyerOnlyRoutes:
    async def test_the_duplicate_check_is_closed_to_vendors(
        self, api_client, principal: Principal, orders
    ) -> None:
        """It answers with other vendors' open orders for a vendor the caller names."""
        ours, _, theirs_order = orders
        _sign_in_as_vendor_owner(principal, ours)
        response = await api_client.post(
            "/procurement/purchase-orders/check-duplicates",
            json={
                "vendor_id": str(theirs_order.vendor_id),
                "warehouse_id": str(theirs_order.warehouse_id),
                "lines": [{"item_id": str(uuid.uuid4())}],
            },
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "SCOPE_DENIED"

    async def test_the_conversion_and_the_assignee_list_are_closed_to_vendors(
        self, api_client, principal: Principal, orders
    ) -> None:
        ours, our_order, _ = orders
        _sign_in_as_vendor_owner(principal, ours)
        for method, path, body in (
            ("post", f"/procurement/purchase-requisitions/{uuid.uuid4()}/conversion-preview", {}),
            ("post", f"/procurement/purchase-requisitions/{uuid.uuid4()}/convert-to-orders", {}),
            (
                "post",
                "/procurement/purchase-requisitions/convert-to-orders",
                {"requisition_ids": [str(uuid.uuid4())]},
            ),
            ("get", f"/procurement/purchase-orders/{our_order.id}/vendor-assignees", None),
        ):
            if method == "get":
                response = await api_client.get(path)
            else:
                response = await api_client.post(path, json=body)
            # The vendor preset lacks purchase_order.create/send, so the
            # permission answers first; either way a vendor never gets in.
            assert response.status_code == 403, (path, response.text)
