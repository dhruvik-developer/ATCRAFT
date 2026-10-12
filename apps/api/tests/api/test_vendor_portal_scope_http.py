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
            po_date=today() - timedelta(days=3),
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


@pytest.mark.parametrize(
    "path",
    [
        "/procurement/purchase-orders",
        "/dispatch",
        "/finance/vendor-invoices",
        "/documents",
        "/job-work",
        "/quality/inspections",
        "/reports/catalogue",
        "/users",
    ],
)
async def test_real_auth_chain_blocks_vendor_staff_routes(
    api_client, session, user, principal, orders, path
):
    from src.core.deps import get_current_principal
    from src.core.security import create_access_token
    from src.main import app

    ours, _, _ = orders
    user.vendor_id = ours.id
    await session.flush()
    app.dependency_overrides.pop(get_current_principal, None)
    token, _ = create_access_token(user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


async def test_portal_hides_foreign_and_draft_orders(
    api_client, session, tenant, principal, orders
):
    ours, our_order, theirs = orders
    await _send(session, tenant, principal, theirs.id)
    _sign_in_as_vendor_owner(principal, ours)
    for row in (our_order, theirs):
        result = await api_client.get(f"/vendor-portal/purchase-orders/{row.id}")
        assert result.status_code == 404, result.text
    await _send(session, tenant, principal, our_order.id)
    listed = await api_client.get("/vendor-portal/purchase-orders")
    assert listed.status_code == 200, listed.text
    assert str(our_order.id) in {r["id"] for r in listed.json()["data"]}
    detail = await api_client.get(f"/vendor-portal/purchase-orders/{our_order.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["tracking"]["tracking_status"] == "NOT_DISPATCHED"


async def test_staff_cannot_impersonate_vendor(api_client, orders):
    result = await api_client.get("/vendor-portal/dashboard")
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


async def test_accept_date_ready_ship_deliver_and_board(
    api_client, session, tenant, principal, orders
):
    ours, order, _ = orders
    order_id = order.id
    planning_date = order.expected_delivery_date
    await _send(session, tenant, principal, order_id)
    _sign_in_as_vendor_owner(principal, ours)
    committed = today() + timedelta(days=20)
    result = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/accept",
        json={"committed_delivery_date": committed.isoformat()},
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["vendor_committed_date"] == committed.isoformat()
    assert result.json()["data"]["expected_delivery_date"] == planning_date.isoformat()
    result = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/ready-for-dispatch", json={"packages": 5}
    )
    assert result.status_code == 200, result.text
    result = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/shipments",
        json={
            "courier_name": "Blue Dart",
            "shipped_at": utcnow().isoformat(),
            "packages": 5,
            "weight_kg": "70",
            "tracking_reference": "BD-123",
        },
    )
    assert result.status_code == 201, result.text
    shipment_id = result.json()["data"]["id"]
    result = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/shipments/{shipment_id}/delivered", json={}
    )
    assert result.status_code == 200, result.text
    detail = await api_client.get(f"/vendor-portal/purchase-orders/{order_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["tracking"]["tracking_status"] == "DELIVERED"
    summary = await api_client.get("/vendor-portal/dashboard")
    assert summary.status_code == 200, summary.text


async def test_invoice_rejects_internal_cost_centres(api_client, principal, orders):
    ours, order, _ = orders
    _sign_in_as_vendor_owner(principal, ours)
    result = await api_client.post(
        "/vendor-portal/invoices",
        json={
            "purchase_order_id": str(order.id),
            "vendor_invoice_number": "X",
            "invoice_date": today().isoformat(),
            "cost_centre_id": str(uuid.uuid4()),
            "lines": [{"description": "Bill", "quantity": "1", "unit_price": "10"}],
        },
    )
    assert result.status_code == 422, result.text


async def test_consignments_documents_invoice_and_staff_filters(
    api_client, session, tenant, principal, orders, other_company, monkeypatch, tmp_path
):
    from src.core.config import settings
    from src.core.deps import get_current_principal
    from src.main import app
    from src.modules.procurement.models import PurchaseOrderLine
    from src.modules.vendors.models import Transporter
    from src.workers.tasks.reports import render_purchase_order_pdf

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    ours, order, foreign = orders
    await _send(session, tenant, principal, order.id)
    await _send(session, tenant, principal, foreign.id)
    _sign_in_as_vendor_owner(principal, ours)
    accepted = await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/accept", json={})
    assert accepted.status_code == 200
    changed = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/delivery-date",
        json={"date": (today() + timedelta(days=10)).isoformat(), "reason": "Capacity confirmed"},
    )
    assert changed.status_code == 200, changed.text
    await procurement_service.record_shipment(
        session,
        tenant,
        order.id,
        courier_name="Legacy carrier",
        tracking_reference="Old header tracking",
        shipped_at=utcnow() - timedelta(days=1),
    )
    legacy = await api_client.get("/vendor-portal/purchase-orders", params={"tab": "dispatched"})
    legacy_row = next(row for row in legacy.json()["data"] if row["id"] == str(order.id))
    assert legacy_row["tracking_status"] == "IN_TRANSIT"
    assert legacy_row["courier_name"] == "Legacy carrier" and legacy_row["latest_shipment"] is None
    bad_transporter = Transporter(
        company_id=other_company.id,
        code=_code("T"),
        name="Foreign carrier",
        transporter_type="COURIER",
    )
    session.add(bad_transporter)
    await session.flush()
    payload = {"transporter_id": str(bad_transporter.id), "shipped_at": utcnow().isoformat()}
    refused = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/shipments", json=payload
    )
    assert refused.status_code == 404, refused.text
    payload = {
        "courier_name": "Blue Dart",
        "tracking_reference": "BD-456",
        "packages": 2,
        "shipped_at": (utcnow() - timedelta(days=1)).isoformat(),
        "expected_delivery_date": today().isoformat(),
    }
    ids = []
    for number in (1, 2):
        sent = await api_client.post(
            f"/vendor-portal/purchase-orders/{order.id}/shipments", json=payload
        )
        assert sent.status_code == 201, sent.text
        assert sent.json()["data"]["shipment_number"] == number
        ids.append(sent.json()["data"]["id"])
    foreign_send = await api_client.post(
        f"/vendor-portal/purchase-orders/{foreign.id}/shipments", json=payload
    )
    assert foreign_send.status_code == 404
    pdf = render_purchase_order_pdf({"code": order.code, "lines": []})
    uploaded = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "LR_COPY", "shipment_id": ids[1]},
        files={"file": ("lorry-receipt.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    doc_id = uploaded.json()["data"]["id"]
    listed = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}/documents")
    assert listed.json()["data"][0]["kind"] == "LR_COPY"
    download = await api_client.get(f"/vendor-portal/documents/{doc_id}/download")
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    invoice_file = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "INVOICE"},
        files={"file": ("invoice.pdf", pdf, "application/pdf")},
    )
    assert invoice_file.status_code == 201, invoice_file.text
    line = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
        )
    ).scalar_one()
    bill_payload = {
        "purchase_order_id": str(order.id),
        "vendor_invoice_number": _code("BILL"),
        "invoice_document_id": invoice_file.json()["data"]["id"],
        "invoice_date": today().isoformat(),
        "lines": [
            {
                "item_id": str(line.item_id),
                "purchase_order_line_id": str(line.id),
                "quantity": "1",
                "unit_price": "40",
                "tax_rate": "18",
            }
        ],
    }
    billed = await api_client.post("/vendor-portal/invoices", json=bill_payload)
    assert billed.status_code == 201, billed.text
    assert billed.json()["data"]["vendor_id"] == str(ours.id)
    assert billed.json()["data"]["cost_centre_id"] is None
    assert billed.json()["data"]["status"] == "PENDING_APPROVAL"
    second_vendor = await _approved_vendor(session, tenant, "Other portal vendor")
    _sign_in_as_vendor_owner(principal, second_vendor)
    assert (await api_client.get(f"/vendor-portal/documents/{doc_id}/download")).status_code == 404
    assert (
        await api_client.get(f"/vendor-portal/invoices/{billed.json()['data']['id']}")
    ).status_code == 404
    bill_payload["purchase_order_id"] = str(foreign.id)
    assert (await api_client.post("/vendor-portal/invoices", json=bill_payload)).status_code == 404
    app.dependency_overrides[get_current_principal] = lambda: principal
    board = await api_client.get("/procurement/inbound-tracking", params={"in_transit": True})
    assert board.status_code == 200, board.text
    row = next(row for row in board.json()["data"] if row["id"] == str(order.id))
    assert row["tracking_status"] == "IN_TRANSIT" and row["has_lr_copy"]
    assert row["delivery_expected_date"] == today().isoformat()
    edited = await api_client.patch(
        f"/procurement/purchase-orders/{order.id}/shipments/{ids[1]}",
        json={**payload, "tracking_reference": "Corrected-456"},
    )
    assert edited.status_code == 200, edited.text
    from src.modules.finance import service as finance
    from src.modules.finance.schemas import AllocationRequest, InvoiceApproval, PaymentCreate
    from src.modules.procurement.schemas import GoodsReceiptCreate, GoodsReceiptLineIn

    receipt, _ = await procurement_service.create_goods_receipt(
        session,
        tenant,
        GoodsReceiptCreate(
            purchase_order_id=order.id,
            lines=[
                GoodsReceiptLineIn(
                    line_number=1,
                    purchase_order_line_id=line.id,
                    received_quantity=Decimal("1"),
                    accepted_quantity=Decimal("1"),
                    requires_qc=True,
                )
            ],
        ),
    )
    await procurement_service.post_goods_receipt(session, tenant, principal, receipt.id)
    invoice_id = uuid.UUID(billed.json()["data"]["id"])
    await finance.approve_invoice(
        session,
        tenant,
        principal,
        invoice_id,
        InvoiceApproval(override_reason="Agreed final vendor price after review"),
    )
    payment = await finance.create_payment(
        session,
        tenant,
        PaymentCreate(
            payment_type="PAYMENT",
            party_type="VENDOR",
            party_id=ours.id,
            payment_date=today(),
            amount=Decimal("47.20"),
            payment_method="CASH",
        ),
    )
    await finance.approve_payment(session, tenant, principal, payment.id)
    await finance.allocate_payment(
        session,
        tenant,
        payment.id,
        AllocationRequest(allocations=[{"vendor_invoice_id": invoice_id, "amount": "47.20"}]),
    )
    current_board = await api_client.get(
        "/procurement/inbound-tracking", params={"in_transit": True}
    )
    assert str(order.id) not in {row["id"] for row in current_board.json()["data"]}
    _sign_in_as_vendor_owner(principal, ours)
    receipts = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}/receipts")
    assert receipts.status_code == 200, receipts.text
    assert receipts.json()["data"][0]["lines"][0]["accepted_quantity"] == "1.0000"
    assert receipts.json()["data"][0]["inspections"]
    paid = await api_client.get("/vendor-portal/payments")
    assert paid.status_code == 200, paid.text
    assert paid.json()["data"][0]["id"] == str(payment.id)
    assert paid.json()["data"][0]["allocations"][0]["vendor_invoice_id"] == str(invoice_id)
    totals = await api_client.get("/vendor-portal/payments/summary")
    assert totals.status_code == 200, totals.text
    assert Decimal(totals.json()["data"][0]["paid_this_month"]) == Decimal("47.20")
    _sign_in_as_vendor_owner(principal, second_vendor)
    assert (await api_client.get("/vendor-portal/payments")).json()["data"] == []


async def test_decline_resend_cancel(api_client, session, tenant, principal, orders):
    ours, order, _ = orders
    await _send(session, tenant, principal, order.id)
    _sign_in_as_vendor_owner(principal, ours)
    declined = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/decline",
        json={"reason": "Unable to supply in this delivery week"},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["data"]["status"] == "VENDOR_DECLINED"
    await procurement_service.transition_purchase_order(
        session,
        tenant,
        principal,
        order.id,
        PurchaseOrderStatus.SENT,
        reason="Agreed a later delivery week",
    )
    declined = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/decline",
        json={"reason": "Capacity remains unavailable"},
    )
    assert declined.status_code == 200
    await procurement_service.transition_purchase_order(
        session,
        tenant,
        principal,
        order.id,
        PurchaseOrderStatus.CANCELLED,
        reason="Cancelled after vendor decline",
    )
    await session.refresh(order)
    assert order.status == "CANCELLED"


async def test_decline_cancel_releases_requisition(api_client, session, tenant, principal, orders):
    from src.modules.procurement.models import PurchaseOrderLine, RequisitionStatus
    from src.modules.procurement.schemas import RequisitionCreate, RequisitionLineIn

    ours, source, _ = orders
    source_line = (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == source.id)
        )
    ).scalar_one()
    requisition, lines = await procurement_service.create_requisition(
        session,
        tenant,
        principal,
        RequisitionCreate(
            warehouse_id=source.warehouse_id,
            required_date=today() + timedelta(days=30),
            lines=[
                RequisitionLineIn(
                    line_number=1, item_id=source_line.item_id, quantity=Decimal("100")
                )
            ],
        ),
    )
    await procurement_service.transition_requisition(
        session,
        tenant,
        principal,
        requisition.id,
        RequisitionStatus.PENDING_APPROVAL,
        reason="Required for next production batch",
    )
    await procurement_service.transition_requisition(
        session,
        tenant,
        _approver(principal),
        requisition.id,
        RequisitionStatus.APPROVED,
        reason="Approved planned demand",
    )
    order, _, _ = await procurement_service.create_purchase_order(
        session,
        tenant,
        principal,
        PurchaseOrderCreate(
            vendor_id=ours.id,
            warehouse_id=source.warehouse_id,
            expected_delivery_date=today() + timedelta(days=30),
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=source_line.item_id,
                    quantity=Decimal("100"),
                    unit_price=Decimal("50"),
                    requisition_line_id=lines[0].id,
                )
            ],
        ),
    )
    await _send(session, tenant, principal, order.id)
    _sign_in_as_vendor_owner(principal, ours)
    declined = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/decline",
        json={"reason": "Capacity unavailable"},
    )
    assert declined.status_code == 200, declined.text
    detail = await procurement_service.get_requisition_detail(session, tenant, requisition.id)
    assert detail.lines[0].ordered_quantity == Decimal("100")
    await procurement_service.transition_purchase_order(
        session,
        tenant,
        principal,
        order.id,
        PurchaseOrderStatus.CANCELLED,
        reason="Released demand after vendor decline",
    )
    detail = await procurement_service.get_requisition_detail(session, tenant, requisition.id)
    assert detail.lines[0].ordered_quantity == 0


async def test_po_sent_notifies_only_the_assignee(session, tenant, principal, orders):
    from src.modules.notifications.models import Notification, NotificationType
    from src.modules.users.models import Role, User

    ours, order, foreign = orders
    role = Role(
        company_id=tenant.company_id,
        code=_code("ROLE"),
        name="Vendor owner",
        permissions=[str(p) for p in ROLE_PRESETS["VENDOR_OWNER"]["permissions"]],
        is_active=True,
    )
    assignee = User(
        company_id=tenant.company_id,
        vendor_id=ours.id,
        email=f"{uuid.uuid4()}@test.local",
        full_name="Assigned owner",
        password_hash="unused",
        roles=[role],
    )
    second = User(
        company_id=tenant.company_id,
        vendor_id=ours.id,
        email=f"{uuid.uuid4()}@test.local",
        full_name="Other owner",
        password_hash="unused",
        roles=[role],
    )
    outsider = User(
        company_id=tenant.company_id,
        vendor_id=foreign.vendor_id,
        email=f"{uuid.uuid4()}@test.local",
        full_name="Other vendor",
        password_hash="unused",
        roles=[role],
    )
    session.add_all([assignee, second, outsider])
    await session.flush()
    for target in (PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.APPROVED):
        await procurement_service.transition_purchase_order(
            session, tenant, _approver(principal), order.id, target, reason="Approved vendor order"
        )
    await procurement_service.transition_purchase_order(
        session,
        tenant,
        principal,
        order.id,
        PurchaseOrderStatus.SENT,
        assigned_vendor_user_id=assignee.id,
        reason="Sent to assigned owner",
    )
    recipients = list(
        (
            await session.execute(
                select(Notification.user_id).where(
                    Notification.entity_id == order.id,
                    Notification.notification_type == NotificationType.PURCHASE_ORDER_SENT,
                )
            )
        ).scalars()
    )
    assert recipients == [assignee.id]
