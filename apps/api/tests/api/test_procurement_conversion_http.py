"""The requisition split over HTTP: routes, status codes, permissions and envelopes.

The rules themselves are pinned by the unit and integration tests; this file
proves what only the HTTP boundary decides - which right each route demands,
that a preview is a 200 and a conversion a 201, that a duplicate comes back as
409 with its candidates, and that the shapes the frontend reads are the shapes
the routes return.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import PurchaseOrderStatus, RequisitionStatus
from src.modules.procurement.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderLineIn,
    RequisitionCreate,
    RequisitionLineIn,
)
from src.modules.products.models import ItemGroup, ItemVendorLink
from src.modules.users.models import Role
from src.modules.vendors.models import Vendor, VendorCategory, VendorType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


def _as(base: Principal, *permissions: Permission, role: str = "BUYER") -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=(role,),
    )


def _sign_in(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


@pytest.fixture
async def setting(session: AsyncSession, tenant: TenantContext, principal: Principal):
    """A vendor qualified for LOCKS, its LOCKS manager, a lock body and a box item,
    and an approved requisition for both."""
    company_id = tenant.company_id
    uom = await seed_support.seed_uom(session, company_id=company_id)
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=_code("WH"))
    locks = await seed_support.seed_category(session, company_id=company_id)
    packing = await seed_support.seed_category(session, company_id=company_id)
    vendor = await seed_support.seed_vendor(
        session,
        company_id=company_id,
        vendor_types=(VendorType.COMPONENT_SUPPLIER,),
        is_approved=True,
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
    )
    session.add(
        VendorCategory(company_id=company_id, vendor_id=vendor.id, item_category_id=locks.id)
    )
    vendor_role = Role(
        company_id=company_id,
        code=_code("VMGR"),
        name="Vendor Manager",
        permissions=[str(Permission.PURCHASE_ORDER_VIEW), str(Permission.PURCHASE_ORDER_UPDATE)],
        is_system=False,
        is_vendor_role=True,
        is_active=True,
    )
    session.add(vendor_role)
    await session.flush()
    manager = await seed_support.seed_user(
        session,
        company_id=company_id,
        full_name="Manager One",
        vendor_id=vendor.id,
        allowed_category_ids=[locks.id],
        roles=[vendor_role],
    )

    async def item(category_id: uuid.UUID) -> Any:
        record = await seed_support.seed_item(
            session,
            company_id=company_id,
            code=_code("IT"),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            category_id=category_id,
            gst_rate=Decimal("18"),
            can_purchase=True,
        )
        session.add(
            ItemVendorLink(
                company_id=company_id,
                item_id=record.id,
                vendor_id=vendor.id,
                is_preferred=True,
                last_purchase_price=Decimal("50"),
            )
        )
        await session.flush()
        return record

    lock_body = await item(locks.id)
    box = await item(packing.id)

    requisition, _ = await procurement_service.create_requisition(
        session,
        tenant,
        principal,
        RequisitionCreate(
            required_date=today() + timedelta(days=14),
            warehouse_id=warehouse.id,
            lines=[
                RequisitionLineIn(line_number=1, item_id=lock_body.id, quantity=Decimal("100")),
                RequisitionLineIn(line_number=2, item_id=box.id, quantity=Decimal("500")),
            ],
        ),
    )
    approver = _as(principal, *Permission, role="SUPER_ADMIN")
    for target in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await procurement_service.transition_requisition(
            session, tenant, approver, requisition.id, target, reason=str(target)
        )
    return {
        "warehouse": warehouse,
        "vendor": vendor,
        "locks": locks,
        "packing": packing,
        "manager": manager,
        "lock_body": lock_body,
        "box": box,
        "requisition_id": requisition.id,
    }


class TestConversion:
    async def test_preview_is_a_200_and_writes_nothing(self, api_client, setting) -> None:
        path = f"/procurement/purchase-requisitions/{setting['requisition_id']}"
        response = await api_client.post(f"{path}/conversion-preview", json={})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["converted"] is False
        assert len(data["groups"]) == 2
        locks = next(g for g in data["groups"] if g["item_category_id"] == str(setting["locks"].id))
        assert locks["assigned_vendor_user_id"] == str(setting["manager"].id)
        assert locks["assignee_reason"] == "CATEGORY_MATCH"
        assert locks["lines"][0]["price_source"] == "LAST_PURCHASE"
        assert locks["lines"][0]["vendor_source"] == "ITEM_PREFERRED"

        detail = await api_client.get(path)
        assert detail.json()["data"]["status"] == "APPROVED"

    async def test_convert_is_a_201_with_the_raised_codes(self, api_client, setting) -> None:
        path = f"/procurement/purchase-requisitions/{setting['requisition_id']}"
        response = await api_client.post(f"{path}/convert-to-orders", json={})
        assert response.status_code == 201, response.text
        body = response.json()
        codes = [g["purchase_order_code"] for g in body["data"]["groups"]]
        assert all(codes)
        for code in codes:
            assert code in body["message"]
        assert body["data"]["requisitions"][0]["status"] == "CONVERTED"

        orders = await api_client.get(
            "/procurement/purchase-orders",
            params={"item_category_id": str(setting["locks"].id)},
        )
        listed = orders.json()["data"]
        assert [row["item_category_id"] for row in listed] == [str(setting["locks"].id)]

    async def test_the_batch_form_converts_several(self, api_client, setting) -> None:
        response = await api_client.post(
            "/procurement/purchase-requisitions/convert-to-orders",
            json={"requisition_ids": [str(setting["requisition_id"])]},
        )
        assert response.status_code == 201, response.text
        assert len(response.json()["data"]["groups"]) == 2

    async def test_a_duplicate_is_a_409_and_the_acknowledged_resubmit_a_201(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        await procurement_service.create_purchase_order(
            session,
            tenant,
            principal,
            PurchaseOrderCreate(
                vendor_id=setting["vendor"].id,
                warehouse_id=setting["warehouse"].id,
                expected_delivery_date=today() + timedelta(days=14),
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=setting["lock_body"].id,
                        quantity=Decimal("10"),
                        unit_price=Decimal("50"),
                    )
                ],
            ),
        )
        path = f"/procurement/purchase-requisitions/{setting['requisition_id']}/convert-to-orders"
        refused = await api_client.post(path, json={})
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "DUPLICATE_DOCUMENT"
        assert error["details"]["candidates"]

        missing_reason = await api_client.post(path, json={"acknowledge_duplicates": True})
        assert missing_reason.status_code == 422

        accepted = await api_client.post(
            path,
            json={
                "acknowledge_duplicates": True,
                "duplicate_acknowledgement_reason": "A second build lot needs these too.",
            },
        )
        assert accepted.status_code == 201, accepted.text

    async def test_an_unresolved_line_is_a_422_naming_it(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        orphan = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=_code("OR"), can_purchase=True
        )
        requisition, _ = await procurement_service.create_requisition(
            session,
            tenant,
            principal,
            RequisitionCreate(
                required_date=today() + timedelta(days=14),
                warehouse_id=setting["warehouse"].id,
                lines=[RequisitionLineIn(line_number=1, item_id=orphan.id, quantity=Decimal("1"))],
            ),
        )
        approver = _as(principal, *Permission, role="SUPER_ADMIN")
        for target in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
            await procurement_service.transition_requisition(
                session, tenant, approver, requisition.id, target, reason=str(target)
            )
        response = await api_client.post(
            f"/procurement/purchase-requisitions/{requisition.id}/convert-to-orders", json={}
        )
        assert response.status_code == 422, response.text
        unresolved = response.json()["error"]["details"]["unresolved_lines"]
        assert unresolved[0]["item_id"] == str(orphan.id)
        assert len(unresolved[0]["reasons"]) == 2

    @pytest.mark.parametrize("action", ["conversion-preview", "convert-to-orders"])
    async def test_converting_needs_the_create_right(
        self, api_client, principal, setting, action
    ) -> None:
        _sign_in(
            _as(principal, Permission.PURCHASE_REQUISITION_VIEW, Permission.PURCHASE_ORDER_VIEW)
        )
        response = await api_client.post(
            f"/procurement/purchase-requisitions/{setting['requisition_id']}/{action}", json={}
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"


class TestClose:
    async def test_close_needs_the_approve_right_and_records_the_reason(
        self, api_client, principal, setting
    ) -> None:
        path = f"/procurement/purchase-requisitions/{setting['requisition_id']}/close"
        _sign_in(_as(principal, Permission.PURCHASE_REQUISITION_CREATE))
        refused = await api_client.post(path, json={"reason": "Not needed any more"})
        assert refused.status_code == 403, refused.text

        _sign_in(principal)
        closed = await api_client.post(path, json={"reason": "Not needed any more"})
        assert closed.status_code == 200, closed.text
        assert closed.json()["data"]["status"] == "CLOSED"

    async def test_close_needs_a_reason(self, api_client, setting) -> None:
        path = f"/procurement/purchase-requisitions/{setting['requisition_id']}/close"
        assert (await api_client.post(path, json={})).status_code == 422


class TestRequisitionList:
    async def test_rows_carry_the_open_line_count_and_filter_on_it(
        self, api_client, setting
    ) -> None:
        listed = await api_client.get("/procurement/purchase-requisitions")
        assert listed.status_code == 200
        row = next(r for r in listed.json()["data"] if r["id"] == str(setting["requisition_id"]))
        assert row["open_line_count"] == 2

        await api_client.post(
            f"/procurement/purchase-requisitions/{setting['requisition_id']}/convert-to-orders",
            json={},
        )
        convertible = await api_client.get(
            "/procurement/purchase-requisitions", params={"convertible_only": "true"}
        )
        ids = {r["id"] for r in convertible.json()["data"]}
        assert str(setting["requisition_id"]) not in ids


class TestVendorAssignees:
    async def _order(self, session, tenant, principal, setting) -> Any:
        order, _, _ = await procurement_service.create_purchase_order(
            session,
            tenant,
            principal,
            PurchaseOrderCreate(
                vendor_id=setting["vendor"].id,
                warehouse_id=setting["warehouse"].id,
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=setting["lock_body"].id,
                        quantity=Decimal("3"),
                        unit_price=Decimal("50"),
                    )
                ],
            ),
        )
        return order

    async def test_the_sender_sees_the_eligible_users_with_the_suggestion_marked(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        order = await self._order(session, tenant, principal, setting)
        _sign_in(_as(principal, Permission.PURCHASE_ORDER_SEND))
        response = await api_client.get(f"/procurement/purchase-orders/{order.id}/vendor-assignees")
        assert response.status_code == 200, response.text
        rows = response.json()["data"]
        assert [(r["id"], r["is_suggested"]) for r in rows] == [(str(setting["manager"].id), True)]
        assert rows[0]["allowed_category_codes"] == [setting["locks"].code]

    async def test_without_the_send_right_it_is_refused(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        order = await self._order(session, tenant, principal, setting)
        _sign_in(_as(principal, Permission.PURCHASE_ORDER_VIEW, Permission.USER_VIEW))
        response = await api_client.get(f"/procurement/purchase-orders/{order.id}/vendor-assignees")
        assert response.status_code == 403

    async def test_send_records_the_chosen_assignee(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        order = await self._order(session, tenant, principal, setting)
        approver = _as(principal, *Permission, role="SUPER_ADMIN")
        for target in (PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.APPROVED):
            await procurement_service.transition_purchase_order(
                session, tenant, approver, order.id, target, reason=str(target)
            )
        sent = await api_client.post(
            f"/procurement/purchase-orders/{order.id}/send",
            json={"assigned_vendor_user_id": str(setting["manager"].id)},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["data"]["assigned_vendor_user_id"] == str(setting["manager"].id)

        detail = await api_client.get(f"/procurement/purchase-orders/{order.id}")
        data = detail.json()["data"]
        assert data["assigned_vendor_user_name"] == "Manager One"
        assert data["item_category_code"] == setting["locks"].code

    async def test_send_refuses_a_user_of_another_vendor(
        self, api_client, session, tenant, principal, setting
    ) -> None:
        order = await self._order(session, tenant, principal, setting)
        approver = _as(principal, *Permission, role="SUPER_ADMIN")
        for target in (PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.APPROVED):
            await procurement_service.transition_purchase_order(
                session, tenant, approver, order.id, target, reason=str(target)
            )
        stranger_vendor: Vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, is_active=True
        )
        stranger = await seed_support.seed_user(
            session, company_id=tenant.company_id, vendor_id=stranger_vendor.id
        )
        sent = await api_client.post(
            f"/procurement/purchase-orders/{order.id}/send",
            json={"assigned_vendor_user_id": str(stranger.id)},
        )
        assert sent.status_code == 422, sent.text
        fields = sent.json()["error"]["details"]["fields"]
        assert "assigned_vendor_user_id" in fields
