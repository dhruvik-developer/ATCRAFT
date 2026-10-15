"""Vendor-portal behaviour over HTTP (WP C1a).

The vendor's actions - accept, decline, date change, ready, consignments,
delivery, typed files, bills - and the buyer's companions: the inbound board,
consignments recorded on a vendor's behalf, adopting the vendor's date and the
PO PDF. Notifications are asserted on the rows ``notify`` wrote, in the same
transaction as the event.

A note on time: the ``session`` fixture runs a whole test in one transaction,
so every row's ``created_at`` is that transaction's start. A goods receipt
"after" a consignment therefore needs the consignment dated before the test
began - which is why dispatches here are dated in the past.
"""

from __future__ import annotations

import uuid
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import AuditLog
from src.modules.notifications.models import Notification, NotificationType
from src.modules.procurement import service as procurement
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.vendors.models import Transporter, Vendor
from src.workers.tasks.reports import render_purchase_order_pdf
from tests import seed_support
from tests.api.vendor_portal_support import (
    approved_vendor,
    code,
    order_line,
    portal_user,
    raise_order,
    receive,
    role,
    send,
    sign_in,
    staff_user,
    vendor_principal,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

PDF = render_purchase_order_pdf({"code": "TEST", "lines": []})


@pytest.fixture
async def world(session: AsyncSession, tenant: TenantContext, principal: Principal) -> Any:
    """Two approved vendors, a store, a vendor account each, and the buying office.

    The buying office is the order's creator (the ``principal`` fixture's user),
    an approver holding ``purchase_order.approve`` and an ADMIN - the three
    groups a shipment update must reach.
    """
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=code("WH"), address_line1="Plot 7, MIDC"
    )
    ours = await approved_vendor(session, tenant, "Our Fasteners")
    theirs = await approved_vendor(session, tenant, "Somebody Else's Fasteners")
    approvers = await role(
        session,
        tenant,
        role_code=code("APPROVER"),
        permissions=[Permission.PURCHASE_ORDER_VIEW, Permission.PURCHASE_ORDER_APPROVE],
    )
    admins = await role(session, tenant, role_code="ADMIN", permissions=[])
    owner = await portal_user(session, tenant, ours)

    async def order_for(vendor: Vendor, **kwargs: Any) -> PurchaseOrder:
        return await raise_order(
            session,
            tenant,
            principal,
            vendor=vendor,
            warehouse_id=warehouse.id,
            uom_id=uom.id,
            **kwargs,
        )

    return {
        "ours": ours,
        "theirs": theirs,
        "owner": owner,
        "approver": await staff_user(session, tenant, with_role=approvers, name="Approver"),
        "admin": await staff_user(session, tenant, with_role=admins, name="Admin"),
        "warehouse": warehouse,
        "uom": uom,
        "order_for": order_for,
    }


def as_owner(principal: Principal, world: Any) -> Principal:
    vendor = vendor_principal(principal, world["ours"].id, user_id=world["owner"].id)
    sign_in(vendor)
    return vendor


async def sent_order(
    session: AsyncSession, tenant: TenantContext, principal: Principal, world: Any, **kwargs: Any
) -> PurchaseOrder:
    order = await world["order_for"](world["ours"], **kwargs)
    await send(session, tenant, principal, order.id)
    return order


async def accepted_order(
    api_client: Any,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    world: Any,
    **kwargs: Any,
) -> PurchaseOrder:
    order = await sent_order(session, tenant, principal, world, **kwargs)
    as_owner(principal, world)
    accepted = await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    return order


async def recipients(
    session: AsyncSession, entity_id: uuid.UUID, kind: NotificationType
) -> set[uuid.UUID]:
    rows = await session.execute(
        select(Notification.user_id).where(
            Notification.entity_id == entity_id, Notification.notification_type == kind
        )
    )
    return set(rows.scalars())


def shipment_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "courier_name": "Blue Dart",
        "tracking_reference": "BD-123",
        "packages": 5,
        "weight_kg": "70",
        "shipped_at": (utcnow() - timedelta(days=1)).isoformat(),
        **overrides,
    }


# -----------------------------------------------------------------------------
# Accept, decline, date, ready
# -----------------------------------------------------------------------------


async def test_accepting_keeps_the_planning_date_and_tells_the_buyer(
    api_client, session, tenant, principal, user, world
):
    order = await sent_order(session, tenant, principal, world)
    planning = order.expected_delivery_date
    as_owner(principal, world)
    too_early = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/accept",
        json={"committed_delivery_date": (order.po_date - timedelta(days=1)).isoformat()},
    )
    assert too_early.status_code == 422, too_early.text

    committed = today() + timedelta(days=20)
    result = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/accept",
        json={
            "committed_delivery_date": committed.isoformat(),
            "vendor_reference": "SO-889",
            "note": "Confirmed",
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()["data"]
    assert body["status"] == "ACKNOWLEDGED"
    assert body["vendor_committed_date"] == committed.isoformat()
    assert body["expected_delivery_date"] == planning.isoformat()
    assert not {"source_type", "source_id", "approved_at", "approved_by_id", "notes"} & body.keys()

    await session.refresh(order)
    assert order.vendor_reference == "SO-889"
    audited = (
        await session.execute(
            select(AuditLog.new_values).where(
                AuditLog.entity_id == order.id, AuditLog.action == "UPDATE"
            )
        )
    ).scalars()
    assert {"vendor_committed_date": committed.isoformat()} in list(audited)
    told = await recipients(session, order.id, NotificationType.PURCHASE_ORDER_ACKNOWLEDGED)
    assert told == {user.id, world["approver"].id, world["admin"].id}

    again = await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/accept", json={})
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


async def test_the_committed_date_moves_only_after_acceptance_and_never_the_plan(
    api_client, session, tenant, principal, world
):
    order = await sent_order(session, tenant, principal, world)
    as_owner(principal, world)
    body = {"date": (today() + timedelta(days=30)).isoformat(), "reason": "Capacity confirmed"}
    refused = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/delivery-date", json=body
    )
    assert refused.status_code == 422, refused.text

    await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/accept", json={})
    moved = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/delivery-date", json=body
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["vendor_committed_date"] == body["date"]
    assert (
        moved.json()["data"]["expected_delivery_date"] == order.expected_delivery_date.isoformat()
    )
    assert await recipients(session, order.id, NotificationType.PURCHASE_ORDER_DATE_CHANGED)


async def test_ready_for_dispatch_is_a_flag_not_a_status(
    api_client, session, tenant, principal, world
):
    order = await accepted_order(api_client, session, tenant, principal, world)
    naive = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/ready-for-dispatch",
        json={"ready_on": "2026-01-01T10:00:00"},
    )
    assert naive.status_code == 422
    ready = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/ready-for-dispatch",
        json={"packages": 3, "note": "Packed on two pallets"},
    )
    assert ready.status_code == 200, ready.text
    assert ready.json()["data"]["status"] == "ACKNOWLEDGED"
    assert ready.json()["data"]["ready_packages"] == 3

    detail = (await api_client.get(f"/vendor-portal/purchase-orders/{order.id}")).json()["data"]
    assert detail["ready_note"] == "Packed on two pallets"
    assert detail["tracking"]["tracking_status"] == "READY_FOR_DISPATCH"
    listed = await api_client.get("/vendor-portal/purchase-orders", params={"tab": "ready"})
    assert [row["id"] for row in listed.json()["data"]] == [str(order.id)]
    assert await recipients(session, order.id, NotificationType.PURCHASE_ORDER_READY)


async def test_a_decline_carries_its_reason_until_the_buyer_sends_again(
    api_client, session, tenant, principal, user, world
):
    order = await sent_order(session, tenant, principal, world)
    # Held in a plain variable: an expected refusal below rolls a savepoint
    # back, which expires the ORM object (CLAUDE.md, rolled-back savepoints).
    order_id = order.id
    as_owner(principal, world)
    short = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/decline", json={"reason": "no"}
    )
    assert short.status_code == 422
    declined = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/decline",
        json={"reason": "Unable to supply in this delivery week"},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["data"]["status"] == "VENDOR_DECLINED"
    assert declined.json()["data"]["vendor_decline_reason"] == (
        "Unable to supply in this delivery week"
    )
    assert user.id in await recipients(session, order_id, NotificationType.PURCHASE_ORDER_DECLINED)
    # Nothing a declined order can do on the vendor side.
    for action, body in (("accept", {}), ("ready-for-dispatch", {})):
        result = await api_client.post(
            f"/vendor-portal/purchase-orders/{order_id}/{action}", json=body
        )
        assert result.status_code == 422, (action, result.text)

    resent = await procurement.transition_purchase_order(
        session, tenant, principal, order_id, PurchaseOrderStatus.SENT, reason="Later week agreed"
    )
    assert resent.status == "SENT" and resent.vendor_decline_reason is None


async def test_a_declined_order_cannot_be_resent_to_a_suspended_vendor(
    api_client, session, tenant, principal, world
):
    order = await sent_order(session, tenant, principal, world)
    as_owner(principal, world)
    await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/decline", json={"reason": "Factory shut"}
    )
    world["ours"].is_active = False
    await session.flush()
    from src.core.errors import BusinessRuleError

    with pytest.raises(BusinessRuleError):
        await procurement.transition_purchase_order(
            session, tenant, principal, order.id, PurchaseOrderStatus.SENT, reason="Try again"
        )


async def test_cancelling_a_declined_order_releases_the_requisition(
    api_client, session, tenant, principal, world
):
    from src.modules.procurement.models import RequisitionStatus
    from src.modules.procurement.schemas import (
        PurchaseOrderCreate,
        PurchaseOrderLineIn,
        RequisitionCreate,
        RequisitionLineIn,
    )
    from tests.api.vendor_portal_support import approver

    source = await world["order_for"](world["ours"])
    item_id = (await order_line(session, source.id)).item_id
    requisition, lines = await procurement.create_requisition(
        session,
        tenant,
        principal,
        RequisitionCreate(
            warehouse_id=world["warehouse"].id,
            required_date=today() + timedelta(days=30),
            lines=[RequisitionLineIn(line_number=1, item_id=item_id, quantity=Decimal("100"))],
        ),
    )
    for target, actor in (
        (RequisitionStatus.PENDING_APPROVAL, principal),
        (RequisitionStatus.APPROVED, approver(principal)),
    ):
        await procurement.transition_requisition(
            session, tenant, actor, requisition.id, target, reason="Planned demand"
        )
    order, _, _ = await procurement.create_purchase_order(
        session,
        tenant,
        principal,
        PurchaseOrderCreate(
            vendor_id=world["ours"].id,
            warehouse_id=world["warehouse"].id,
            expected_delivery_date=today() + timedelta(days=30),
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=item_id,
                    quantity=Decimal("100"),
                    unit_price=Decimal("50"),
                    requisition_line_id=lines[0].id,
                )
            ],
        ),
    )
    await send(session, tenant, principal, order.id)
    as_owner(principal, world)
    declined = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/decline", json={"reason": "Capacity full"}
    )
    assert declined.status_code == 200, declined.text
    # Declining keeps the requisition committed: the buyer has not decided yet.
    detail = await procurement.get_requisition_detail(session, tenant, requisition.id)
    assert detail.lines[0].ordered_quantity == Decimal("100")
    await procurement.transition_purchase_order(
        session, tenant, principal, order.id, PurchaseOrderStatus.CANCELLED, reason="Re-source"
    )
    detail = await procurement.get_requisition_detail(session, tenant, requisition.id)
    assert detail.lines[0].ordered_quantity == 0


# -----------------------------------------------------------------------------
# Consignments
# -----------------------------------------------------------------------------


async def test_consignments_are_numbered_checked_and_mirrored(
    api_client, session, tenant, principal, other_company, world
):
    order = await sent_order(session, tenant, principal, world)
    as_owner(principal, world)
    url = f"/vendor-portal/purchase-orders/{order.id}/shipments"
    early = await api_client.post(url, json=shipment_payload())
    assert early.status_code == 422, early.text  # the vendor must accept first

    await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/accept", json={})
    foreign = Transporter(
        company_id=other_company.id, code=code("T"), name="Elsewhere", transporter_type="COURIER"
    )
    retired = Transporter(
        company_id=tenant.company_id,
        code=code("T"),
        name="Retired Lines",
        transporter_type="COURIER",
        is_active=False,
    )
    ours = Transporter(
        company_id=tenant.company_id, code=code("T"), name="Gati", transporter_type="COURIER"
    )
    session.add_all([foreign, retired, ours])
    await session.flush()
    for transporter in (foreign, retired):
        refused = await api_client.post(
            url,
            json=shipment_payload(courier_name=None, transporter_id=str(transporter.id)),
        )
        assert refused.status_code == 404, refused.text
    for bad in (
        {"shipped_at": (utcnow() + timedelta(days=2)).isoformat()},
        {"shipped_at": (utcnow() - timedelta(days=30)).isoformat()},
        {"shipped_at": "2026-01-01T10:00:00"},
        {"courier_name": None},
    ):
        result = await api_client.post(url, json=shipment_payload(**bad))
        assert result.status_code == 422, (bad, result.text)

    first = await api_client.post(
        url, json=shipment_payload(courier_name=None, transporter_id=str(ours.id))
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["shipment_number"] == 1
    assert first.json()["data"]["courier_name"] == "Gati"
    second = await api_client.post(url, json=shipment_payload(tracking_reference="BD-456"))
    assert second.json()["data"]["shipment_number"] == 2

    foreign_order = await world["order_for"](world["theirs"])
    await send(session, tenant, principal, foreign_order.id)
    assert (
        await api_client.post(
            f"/vendor-portal/purchase-orders/{foreign_order.id}/shipments", json=shipment_payload()
        )
    ).status_code == 404

    # The buyer records a third on the vendor's behalf and corrects the first.
    sign_in(principal)
    third = await api_client.post(
        f"/procurement/purchase-orders/{order.id}/shipments",
        json=shipment_payload(courier_name="DTDC", tracking_reference="DT-9"),
    )
    assert third.status_code == 201, third.text
    assert third.json()["data"]["shipment_number"] == 3
    corrected = await api_client.patch(
        f"/procurement/purchase-orders/{order.id}/shipments/{first.json()['data']['id']}",
        json=shipment_payload(courier_name="Gati", tracking_reference="GT-1"),
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["tracking_reference"] == "GT-1"
    elsewhere = await api_client.patch(
        f"/procurement/purchase-orders/{foreign_order.id}/shipments/{first.json()['data']['id']}",
        json=shipment_payload(),
    )
    assert elsewhere.status_code == 422  # that order is not with the vendor's acceptance

    header = (await api_client.get(f"/procurement/purchase-orders/{order.id}")).json()["data"]
    assert (header["courier_name"], header["tracking_reference"]) == ("DTDC", "DT-9")
    listed = await api_client.get(f"/procurement/purchase-orders/{order.id}/shipments")
    assert [row["shipment_number"] for row in listed.json()["data"]] == [3, 2, 1]


async def test_delivery_is_marked_once_the_goods_arrive_and_never_in_the_future(
    api_client, session, tenant, principal, world
):
    order = await accepted_order(api_client, session, tenant, principal, world)
    shipped = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/shipments", json=shipment_payload()
    )
    shipment_id = shipped.json()["data"]["id"]
    url = f"/vendor-portal/purchase-orders/{order.id}/shipments/{shipment_id}/delivered"
    for bad in (
        {"delivered_at": (utcnow() + timedelta(days=1)).isoformat()},
        {"delivered_at": (utcnow() - timedelta(days=5)).isoformat()},
        {"delivered_at": "2026-01-01T10:00:00"},
    ):
        result = await api_client.post(url, json=bad)
        assert result.status_code == 422, (bad, result.text)
    delivered = await api_client.post(url, json={})
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["data"]["delivered_at"] is not None
    detail = (await api_client.get(f"/vendor-portal/purchase-orders/{order.id}")).json()["data"]
    assert detail["tracking"]["tracking_status"] == "DELIVERED"

    sign_in(principal)
    gate = await api_client.post(
        f"/procurement/purchase-orders/{order.id}/shipments/{shipment_id}/delivered", json={}
    )
    assert gate.status_code == 200, gate.text
    await procurement.transition_purchase_order(
        session, tenant, principal, order.id, PurchaseOrderStatus.CANCELLED, reason="Rejected lot"
    )
    settled = await api_client.post(
        f"/procurement/purchase-orders/{order.id}/shipments/{shipment_id}/delivered", json={}
    )
    assert settled.status_code == 422, settled.text


async def test_an_earlier_receipt_does_not_hide_the_next_consignment(
    api_client, session, tenant, principal, world
):
    order = await accepted_order(
        api_client, session, tenant, principal, world, quantity=Decimal("10")
    )
    url = f"/vendor-portal/purchase-orders/{order.id}/shipments"
    await api_client.post(
        url, json=shipment_payload(shipped_at=(utcnow() - timedelta(days=2)).isoformat())
    )
    await receive(session, tenant, principal, order.id, received=Decimal("4"))

    async def status() -> str:
        detail = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}")
        return str(detail.json()["data"]["tracking"]["tracking_status"])

    assert await status() == "RECEIVED"
    second = await api_client.post(url, json=shipment_payload(shipped_at=utcnow().isoformat()))
    assert second.status_code == 201, second.text
    assert await status() in {"DISPATCHED", "IN_TRANSIT"}
    listed = await api_client.get("/vendor-portal/purchase-orders", params={"tab": "dispatched"})
    assert str(order.id) in {row["id"] for row in listed.json()["data"]}

    await api_client.post(f"{url}/{second.json()['data']['id']}/delivered", json={})
    assert await status() == "DELIVERED"
    await receive(session, tenant, principal, order.id, received=Decimal("6"))
    assert await status() == "GRN_COMPLETED"


# -----------------------------------------------------------------------------
# Files, and the buyer hearing about them
# -----------------------------------------------------------------------------


async def test_typed_files_are_shared_and_one_consignment_is_one_notification(
    api_client, session, tenant, principal, user, world, monkeypatch, tmp_path
):
    from src.core.config import settings

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    order = await accepted_order(api_client, session, tenant, principal, world)
    shipped = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/shipments", json=shipment_payload()
    )
    shipment_id = shipped.json()["data"]["id"]
    uploaded = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "LR_COPY", "shipment_id": shipment_id},
        files={"file": ("lorry-receipt.pdf", PDF, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["data"]["kind"] == "LR_COPY"
    download = await api_client.get(
        f"/vendor-portal/documents/{uploaded.json()['data']['id']}/download"
    )
    assert download.status_code == 200 and download.content.startswith(b"%PDF")

    # One unread row per recipient for the whole consignment, with the latest news.
    rows = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.entity_id == order.id,
                    Notification.notification_type == NotificationType.PURCHASE_ORDER_SHIPPED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.user_id for row in rows} == {user.id, world["approver"].id, world["admin"].id}
    assert all(row.occurrence_count == 2 for row in rows)
    assert all(row.group_key == f"po-shipment:{shipment_id}" for row in rows)
    assert rows[0].body == (
        f"Shipment 1 for {order.code}: Blue Dart · BD-123 · "
        f"dispatched {(utcnow() - timedelta(days=1)).astimezone(UTC).date().isoformat()} · "
        "5 boxes · 70 kg · LR copy uploaded"
    )
    assert world["owner"].id not in {row.user_id for row in rows}

    # The kind decides the right: dispatch staff cannot file an invoice ...
    sign_in(vendor_principal(principal, world["ours"].id, preset="VENDOR_DISPATCH_MANAGER"))
    refused = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "INVOICE"},
        files={"file": ("bill.pdf", PDF, "application/pdf")},
    )
    assert refused.status_code == 403, refused.text
    # ... and accounts cannot file a lorry receipt.
    sign_in(vendor_principal(principal, world["ours"].id, preset="VENDOR_ACCOUNT_MANAGER"))
    refused = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "LR_COPY"},
        files={"file": ("lr.pdf", PDF, "application/pdf")},
    )
    assert refused.status_code == 403, refused.text

    # A buyer's typed upload is shared with the vendor too, and staff see everything.
    sign_in(principal)
    filed = await api_client.post(
        f"/procurement/purchase-orders/{order.id}/documents",
        data={"kind": "DELIVERY_CHALLAN"},
        files={"file": ("challan.pdf", PDF, "application/pdf")},
    )
    assert filed.status_code == 201, filed.text
    other = await world["order_for"](world["ours"])
    misfiled = await api_client.post(
        f"/procurement/purchase-orders/{other.id}/documents",
        data={"kind": "LR_COPY", "shipment_id": shipment_id},
        files={"file": ("lr.pdf", PDF, "application/pdf")},
    )
    assert misfiled.status_code == 404, misfiled.text
    as_owner(principal, world)
    kinds = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}/documents")
    assert sorted(row["kind"] for row in kinds.json()["data"]) == ["DELIVERY_CHALLAN", "LR_COPY"]
    board_row = (await api_client.get("/vendor-portal/purchase-orders")).json()["data"][0]
    assert board_row["has_lr_copy"] is True and board_row["documents_count"] == 2


# -----------------------------------------------------------------------------
# Receipts, bills, payments
# -----------------------------------------------------------------------------


async def test_receipts_bills_and_payments_are_the_vendors_own(
    api_client, session, tenant, principal, world, monkeypatch, tmp_path
):
    from src.core.config import settings
    from src.modules.finance import service as finance
    from src.modules.finance.schemas import AllocationRequest, InvoiceApproval, PaymentCreate

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    order = await accepted_order(api_client, session, tenant, principal, world)
    receipt = await receive(
        session, tenant, principal, order.id, received=Decimal("3"), requires_qc=True
    )
    told = await recipients(session, receipt.id, NotificationType.GOODS_RECEIPT_POSTED)
    assert told == {world["owner"].id}

    listed = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}/receipts")
    assert listed.status_code == 200, listed.text
    row = listed.json()["data"][0]
    assert row["purchase_order_code"] == order.code
    assert row["lines"][0]["accepted_quantity"] == "3.0000"
    assert row["inspections"][0]["status"] in {"PENDING", "IN_PROGRESS"}
    everywhere = await api_client.get("/vendor-portal/receipts")
    assert [r["id"] for r in everywhere.json()["data"]] == [str(receipt.id)]

    uploaded = await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/documents",
        data={"kind": "INVOICE"},
        files={"file": ("bill.pdf", PDF, "application/pdf")},
    )
    line = await order_line(session, order.id)
    bill = {
        "purchase_order_id": str(order.id),
        "goods_receipt_id": str(receipt.id),
        "vendor_invoice_number": code("BILL"),
        "invoice_document_id": uploaded.json()["data"]["id"],
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
    billed = await api_client.post("/vendor-portal/invoices", json=bill)
    assert billed.status_code == 201, billed.text
    reused = await api_client.post(
        "/vendor-portal/invoices", json={**bill, "vendor_invoice_number": code("BILL")}
    )
    assert reused.status_code == 422, reused.text  # one file, one bill
    invoice_id = uuid.UUID(billed.json()["data"]["id"])

    await finance.approve_invoice(
        session,
        tenant,
        principal,
        invoice_id,
        InvoiceApproval(override_reason="Internal: price agreed after a phone call"),
    )
    approved = (
        await session.execute(
            select(Notification).where(
                Notification.entity_id == invoice_id,
                Notification.notification_type == NotificationType.VENDOR_INVOICE_STATUS,
            )
        )
    ).scalar_one()
    assert approved.user_id == world["owner"].id
    assert approved.action_url == "/vendor/invoices"
    assert "Internal" not in approved.body  # the approver's note is not the vendor's

    payment = await finance.create_payment(
        session,
        tenant,
        PaymentCreate(
            payment_type="PAYMENT",
            party_type="VENDOR",
            party_id=world["ours"].id,
            payment_date=today(),
            amount=Decimal("47.20"),
            payment_method="BANK_TRANSFER",
            reference_number="UTR778",
        ),
    )
    await finance.approve_payment(session, tenant, principal, payment.id)
    await finance.allocate_payment(
        session,
        tenant,
        payment.id,
        AllocationRequest(allocations=[{"vendor_invoice_id": invoice_id, "amount": "47.20"}]),
    )
    paid_rows = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.entity_id == payment.id,
                    Notification.notification_type == NotificationType.VENDOR_PAYMENT_RECORDED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(row.user_id, row.occurrence_count) for row in paid_rows] == [(world["owner"].id, 2)]

    paid = await api_client.get("/vendor-portal/payments")
    assert paid.status_code == 200, paid.text
    payment_row = paid.json()["data"][0]
    assert payment_row["reference_number"] == "UTR778"
    assert payment_row["allocations"][0]["vendor_invoice_id"] == str(invoice_id)
    assert "notes" not in payment_row and "approved_by_id" not in payment_row
    totals = await api_client.get("/vendor-portal/payments/summary")
    assert totals.status_code == 200, totals.text
    assert Decimal(totals.json()["data"][0]["paid_this_month"]) == Decimal("47.20")
    bills = await api_client.get("/vendor-portal/invoices")
    assert [row["id"] for row in bills.json()["data"]] == [str(invoice_id)]
    assert bills.json()["data"][0]["status"] == "PAID"

    sign_in(vendor_principal(principal, world["theirs"].id))
    assert (await api_client.get("/vendor-portal/payments")).json()["data"] == []
    assert (await api_client.get("/vendor-portal/receipts")).json()["data"] == []
    assert (await api_client.get("/vendor-portal/payments/summary")).json()["data"] == []


async def test_a_rejection_at_inspection_reaches_the_vendors_quality_contact(
    api_client, session, tenant, principal, world
):
    from src.modules.quality.models import QcInspection
    from src.modules.quality.schemas import DecideRequest
    from src.modules.quality.service import decide_inspection

    qc_contact = await portal_user(session, tenant, world["ours"], preset="VENDOR_QC_CONTACT")
    accounts = await portal_user(session, tenant, world["ours"], preset="VENDOR_ACCOUNT_MANAGER")
    order = await accepted_order(api_client, session, tenant, principal, world)
    receipt = await receive(
        session, tenant, principal, order.id, received=Decimal("4"), requires_qc=True
    )
    inspection = (
        await session.execute(select(QcInspection).where(QcInspection.reference_id == receipt.id))
    ).scalar_one()
    await decide_inspection(
        session,
        tenant,
        inspection.id,
        DecideRequest(
            accepted_quantity=Decimal("3"),
            rejected_quantity=Decimal("1"),
            reason="Visual inspection",
            rejection_reason="Thread damage",
        ),
    )
    vendor_rows = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.entity_id == inspection.id,
                    Notification.group_key == f"vendor-qc:{inspection.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    # Owner and QC contact hold vendor_portal.quality; accounts does not.
    assert {row.user_id for row in vendor_rows} == {world["owner"].id, qc_contact.id}
    assert accounts.id not in {row.user_id for row in vendor_rows}
    assert all(row.action_url == f"/vendor/orders/{order.id}" for row in vendor_rows)
    assert "Thread damage" in vendor_rows[0].body


# -----------------------------------------------------------------------------
# Dashboard, tabs and the buyer's board
# -----------------------------------------------------------------------------


async def test_the_dashboard_counts_what_the_tabs_list(
    api_client, session, tenant, principal, world
):
    category = await seed_support.seed_category(
        session, company_id=tenant.company_id, code=code("C")
    )
    to_accept = await sent_order(session, tenant, principal, world)
    accepted = await accepted_order(
        api_client, session, tenant, principal, world, expected=today() - timedelta(days=1)
    )
    ready = await accepted_order(api_client, session, tenant, principal, world, category=category)
    await api_client.post(f"/vendor-portal/purchase-orders/{ready.id}/ready-for-dispatch", json={})
    moving = await accepted_order(api_client, session, tenant, principal, world)
    await api_client.post(
        f"/vendor-portal/purchase-orders/{moving.id}/shipments",
        json=shipment_payload(expected_delivery_date=today().isoformat()),
    )
    done = await accepted_order(api_client, session, tenant, principal, world)
    await receive(
        session, tenant, principal, done.id, received=Decimal("10"), rejected=Decimal("2")
    )
    declined = await sent_order(session, tenant, principal, world)
    await api_client.post(
        f"/vendor-portal/purchase-orders/{declined.id}/decline", json={"reason": "Capacity full"}
    )
    line = await order_line(session, done.id)
    billed = await api_client.post(
        "/vendor-portal/invoices",
        json={
            "purchase_order_id": str(done.id),
            "vendor_invoice_number": code("BILL"),
            "invoice_date": today().isoformat(),
            "lines": [
                {
                    "purchase_order_line_id": str(line.id),
                    "item_id": str(line.item_id),
                    "quantity": "8",
                    "unit_price": "50",
                }
            ],
        },
    )
    assert billed.status_code == 201, billed.text
    # Noise the vendor must not count: another vendor's orders in the same states.
    for _ in range(2):
        foreign = await world["order_for"](world["theirs"])
        await send(session, tenant, principal, foreign.id)

    dashboard = await api_client.get("/vendor-portal/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["data"] == {
        "to_accept": 1,
        "accepted": 1,
        "ready": 1,
        "in_transit": 1,
        "delivered": 1,
        "payment_pending": 1,
        "qc_rejected": 1,
        "qc_lookback_days": 30,
    }
    expected_tabs = {
        "to_accept": {to_accept.id},
        "accepted": {accepted.id},
        "ready": {ready.id},
        "dispatched": {moving.id},
        "delivered": {done.id},
        "all": {to_accept.id, accepted.id, ready.id, moving.id, done.id, declined.id},
    }
    for tab, ids in expected_tabs.items():
        listed = await api_client.get("/vendor-portal/purchase-orders", params={"tab": tab})
        assert {row["id"] for row in listed.json()["data"]} == {str(i) for i in ids}, tab
    rows = {
        row["id"]: row
        for row in (await api_client.get("/vendor-portal/purchase-orders")).json()["data"]
    }
    assert rows[str(accepted.id)]["days_late"] == 1
    assert rows[str(moving.id)]["delivery_expected_date"] == today().isoformat()
    assert rows[str(done.id)]["received_percentage"] == "100.00"

    # The buyer's board over the same orders.
    sign_in(principal)

    async def board(**params: Any) -> set[str]:
        result = await api_client.get(
            "/procurement/inbound-tracking", params={"vendor_id": str(world["ours"].id), **params}
        )
        assert result.status_code == 200, result.text
        return {row["id"] for row in result.json()["data"]}

    open_work = {str(i) for i in (to_accept.id, accepted.id, ready.id, moving.id, declined.id)}
    assert await board() == open_work
    assert await board(include_completed=True) == open_work | {str(done.id)}
    assert await board(overdue=True) == {str(accepted.id)}
    assert await board(expected_today=True) == {str(moving.id)}
    assert await board(in_transit=True) == {str(moving.id)}
    assert await board(not_acknowledged=True) == {str(to_accept.id)}
    assert await board(category_id=str(category.id)) == {str(ready.id)}
    assert await board(search=ready.code) == {str(ready.id)}
    every_vendor = await api_client.get("/procurement/inbound-tracking")
    assert len(every_vendor.json()["data"]) == len(open_work) + 2


async def test_the_buyer_adopts_the_vendors_date_explicitly(
    api_client, session, tenant, principal, world
):
    order = await sent_order(session, tenant, principal, world)
    sign_in(principal)
    url = f"/procurement/purchase-orders/{order.id}/accept-vendor-date"
    assert (await api_client.post(url)).status_code == 422  # not accepted yet

    committed = today() + timedelta(days=25)
    as_owner(principal, world)
    await api_client.post(
        f"/vendor-portal/purchase-orders/{order.id}/accept",
        json={"committed_delivery_date": committed.isoformat()},
    )
    sign_in(principal)
    adopted = await api_client.post(url)
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["data"]["expected_delivery_date"] == committed.isoformat()
    again = await api_client.post(url)
    assert again.status_code == 200
    audits = (
        await session.execute(
            select(AuditLog.id).where(
                AuditLog.entity_id == order.id,
                AuditLog.summary.like("Accepted the vendor's delivery date%"),
            )
        )
    ).all()
    assert len(audits) == 1  # the second call changed nothing and recorded nothing


# -----------------------------------------------------------------------------
# The PO PDF and the small reads
# -----------------------------------------------------------------------------


async def test_the_po_pdf_is_queued_from_both_doors(
    api_client, session, tenant, principal, world, monkeypatch
):
    from src.modules.procurement.purchase_order_pdf_repository import purchase_order_pdf_data
    from src.workers import celery_app as celery_module

    sent: list[str] = []

    def accepted_by_the_broker(name: str, **_: Any) -> Any:
        sent.append(name)
        return type("Task", (), {"id": uuid.uuid4().hex})()

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted_by_the_broker)
    order = await sent_order(session, tenant, principal, world)
    as_owner(principal, world)
    queued = await api_client.post(f"/vendor-portal/purchase-orders/{order.id}/pdf")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["data"]["job_id"]
    assert queued.json()["data"]["status_url"] == f"/api/v1/vendor-portal/jobs/{job_id}"
    job = await api_client.get(f"/vendor-portal/jobs/{job_id}")
    assert job.status_code == 200, job.text

    sign_in(principal)
    staff = await api_client.post(f"/procurement/purchase-orders/{order.id}/pdf")
    assert staff.status_code == 202, staff.text
    assert "/reports/jobs/" in staff.json()["data"]["status_url"]
    assert sent == ["reports.generate_purchase_order"] * 2

    data = await purchase_order_pdf_data(session, tenant, order.id)
    assert data["code"] == order.code
    assert "Plot 7, MIDC" in data["delivery_address"]
    assert data["lines"][0]["code"].startswith("RM-")
    assert render_purchase_order_pdf(data).startswith(b"%PDF")


async def test_profile_rating_and_transporters(api_client, session, tenant, principal, world):
    lock_bodies = await seed_support.seed_category(
        session, company_id=tenant.company_id, code=code("LB")
    )
    from src.modules.vendors.models import VendorCategory

    session.add(
        VendorCategory(
            company_id=tenant.company_id,
            vendor_id=world["ours"].id,
            item_category_id=lock_bodies.id,
        )
    )
    session.add(
        Transporter(
            company_id=tenant.company_id, code=code("T"), name="Gati", transporter_type="COURIER"
        )
    )
    await session.flush()
    as_owner(principal, world)
    me = (await api_client.get("/vendor-portal/me")).json()["data"]
    assert me["code"] == world["ours"].code
    assert me["roles"] == ["VENDOR_OWNER"]
    assert [c["code"] for c in me["categories"]] == [lock_bodies.code]
    rating = await api_client.get("/vendor-portal/rating")
    assert rating.status_code == 200, rating.text
    assert rating.json()["data"]["vendor_id"] == str(world["ours"].id)
    options = await api_client.get("/vendor-portal/transporters/options")
    assert "Gati" in {row["name"] for row in options.json()["data"]}


# -----------------------------------------------------------------------------
# Who is told when an order is sent
# -----------------------------------------------------------------------------


async def test_a_sent_order_reaches_its_assignee_and_nobody_else(session, tenant, principal, world):
    second = await portal_user(session, tenant, world["ours"])
    outsider = await portal_user(session, tenant, world["theirs"])
    order = await world["order_for"](world["ours"])
    await send(session, tenant, principal, order.id, assigned_vendor_user_id=second.id)
    assert await recipients(session, order.id, NotificationType.PURCHASE_ORDER_SENT) == {second.id}
    assert outsider.id not in await recipients(
        session, order.id, NotificationType.PURCHASE_ORDER_SENT
    )


async def test_an_unassigned_order_reaches_only_responders_who_can_see_it(
    session, tenant, principal, world
):
    handles = await seed_support.seed_category(
        session, company_id=tenant.company_id, code=code("H")
    )
    locks = await seed_support.seed_category(session, company_id=tenant.company_id, code=code("L"))
    # Two managers scoped to Handles: the assignee rule cannot choose, so the
    # order goes out unassigned (docs/assumptions.md §55).
    first = await portal_user(
        session, tenant, world["ours"], preset="VENDOR_MANAGER", allowed_category_ids=[handles.id]
    )
    second = await portal_user(
        session, tenant, world["ours"], preset="VENDOR_MANAGER", allowed_category_ids=[handles.id]
    )
    lock_manager = await portal_user(
        session, tenant, world["ours"], preset="VENDOR_MANAGER", allowed_category_ids=[locks.id]
    )
    qc_contact = await portal_user(session, tenant, world["ours"], preset="VENDOR_QC_CONTACT")
    order = await world["order_for"](world["ours"], category=handles)
    await send(session, tenant, principal, order.id)
    await session.refresh(order)
    assert order.assigned_vendor_user_id is None
    told = await recipients(session, order.id, NotificationType.PURCHASE_ORDER_SENT)
    # The unrestricted owner and both Handles managers; not the Lock Bodies
    # manager (their portal would answer 404) nor QC (cannot respond).
    assert told == {world["owner"].id, first.id, second.id}
    assert lock_manager.id not in told and qc_contact.id not in told
