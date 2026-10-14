"""Consignment and attachment rules used by staff and vendor routes.

One function per action, called by both doors: the buyer's procurement routes
and the vendor portal. Which orders a caller may touch is decided once, by
``PurchaseOrderRepository._scoped`` (vendor, SENT-and-later, category scope),
so the shared code never needs to know who is asking - a foreign order is
simply "not found".
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import UploadFile

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.documents.models import DocumentCategory
from src.modules.documents.schemas import DocumentRead, DocumentUploadRequest
from src.modules.documents.service import DocumentService
from src.modules.notifications.models import NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.notifications.service import notify
from src.modules.procurement.inbound_models import PurchaseOrderDocument, PurchaseOrderShipment
from src.modules.procurement.inbound_repository import (
    ORDER_DOCUMENT_ENTITY,
    InboundDocumentRepository,
    InboundRepository,
    ShipmentRepository,
    po_is_late,
)
from src.modules.procurement.inbound_schemas import (
    DOCUMENT_KIND_LABELS,
    DocumentKind,
    InboundDocumentRead,
    InboundShipmentCreate,
    InboundShipmentRead,
    InboundTrackingFilters,
    InboundTrackingRead,
    InboundTrackingStatus,
)
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.procurement.schemas import PurchaseOrderSummary

#: Statuses on which a consignment may be recorded or corrected.
#: ASSUMPTION: the vendor accepts first; recording dispatch against an order
#: nobody has accepted would let goods arrive against a refusal-in-waiting.
#: docs/assumptions.md §C1-2.
SHIPPING_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
)

#: Orders whose consignments are history: delivery can no longer be recorded.
SETTLED_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.CANCELLED,
    PurchaseOrderStatus.CLOSED,
)

#: How far a "delivered at" may run ahead of the server clock - a phone whose
#: clock is a little fast is not a false claim of a future delivery.
DELIVERY_CLOCK_SKEW = timedelta(minutes=5)

#: Internal roles told about every vendor action and shipment update, beside the
#: order's creator and everyone who may approve purchase orders.
#: ASSUMPTION: ADMIN watches inbound (proposal Add-on §07, "admin is notified
#: immediately"); PURCHASE_MANAGER owns the order book. docs/assumptions.md §C1-7.
BUYER_ALERT_ROLE_CODES: tuple[str, ...] = ("ADMIN", "PURCHASE_MANAGER")

#: The headline each buyer-facing event carries; the body says what changed.
BUYER_ALERT_TITLES: dict[NotificationType, str] = {
    NotificationType.PURCHASE_ORDER_ACKNOWLEDGED: "{code} accepted by the vendor",
    NotificationType.PURCHASE_ORDER_DECLINED: "{code} declined by the vendor",
    NotificationType.PURCHASE_ORDER_DATE_CHANGED: "{code}: vendor changed the delivery date",
    NotificationType.PURCHASE_ORDER_READY: "{code} is ready for dispatch",
    NotificationType.PURCHASE_ORDER_SHIPPED: "{code}: shipment update",
}


async def buyer_recipients(
    session: Any, tenant: TenantContext, order: PurchaseOrder
) -> list[uuid.UUID]:
    """Internal users told about a vendor action or a consignment change.

    The order's creator, every internal user who may approve purchase orders,
    and the ``BUYER_ALERT_ROLE_CODES`` roles. Vendor accounts are excluded by
    every resolver used here, and the person who made the change is not told
    about it.
    """
    repo = NotificationRepository(session, tenant)
    ids = [
        *await repo.user_ids_with_permission(str(Permission.PURCHASE_ORDER_APPROVE)),
        *await repo.user_ids_for_roles(BUYER_ALERT_ROLE_CODES),
    ]
    if order.created_by_id:
        ids += await repo.internal_user_ids([order.created_by_id])
    return [user_id for user_id in dict.fromkeys(ids) if user_id != tenant.user_id]


async def notify_buyer(
    session: Any,
    tenant: TenantContext,
    order: PurchaseOrder,
    kind: NotificationType,
    body: str,
    *,
    shipment_id: uuid.UUID | None = None,
) -> None:
    """Tell the buying side, in the caller's transaction.

    Everything about one consignment collapses onto one unread row per
    recipient (``po-shipment:{id}``), so the admin watches a single updating
    line rather than a new one per edit.
    """
    await notify(
        session,
        company_id=tenant.company_id,
        user_ids=await buyer_recipients(session, tenant, order),
        notification_type=kind,
        title=BUYER_ALERT_TITLES.get(kind, "{code}: vendor update").format(code=order.code),
        body=body,
        entity_type="purchase_order",
        entity_id=order.id,
        action_url=f"/procurement/purchase-orders/{order.id}",
        group_key=f"po-shipment:{shipment_id}" if shipment_id else None,
        actor_id=tenant.user_id,
    )


def _plain(value: Decimal) -> str:
    """``Decimal("70.0000")`` reads as ``70`` in a message, not as four decimals."""
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def shipment_message(order: PurchaseOrder, shipment: PurchaseOrderShipment, event: str) -> str:
    """The proposal's notification line: carrier, tracking, dates, boxes, weight, event."""
    parts = [
        f"Shipment {shipment.shipment_number} for {order.code}: "
        f"{shipment.courier_name or 'Transporter'}",
        shipment.tracking_reference or "tracking pending",
        f"dispatched {shipment.shipped_at.astimezone(UTC).date().isoformat()}",
        f"expected {shipment.expected_delivery_date.isoformat()}"
        if shipment.expected_delivery_date
        else None,
        f"{shipment.packages} boxes" if shipment.packages else None,
        f"{_plain(Decimal(shipment.weight_kg))} kg" if shipment.weight_kg is not None else None,
        event,
    ]
    return " · ".join(part for part in parts if part)


async def _lock_order(session: Any, tenant: TenantContext, order_id: uuid.UUID) -> PurchaseOrder:
    order = await PurchaseOrderRepository(session, tenant).lock_for_update(order_id)
    if order is None:
        raise NotFoundError("Purchase order", order_id)
    return order


async def _order_shipment(
    session: Any, tenant: TenantContext, order_id: uuid.UUID, shipment_id: uuid.UUID
) -> PurchaseOrderShipment:
    """A consignment of this order - another order's consignment is "not found"."""
    shipment = await ShipmentRepository(session, tenant).get(shipment_id)
    if shipment is None or shipment.purchase_order_id != order_id:
        raise NotFoundError("Purchase order shipment", shipment_id)
    return shipment


async def record_inbound_shipment(
    session: Any,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: InboundShipmentCreate,
    *,
    shipment_id: uuid.UUID | None = None,
) -> InboundShipmentRead:
    """Record a consignment, or (``shipment_id``) replace one's details."""
    repo = ShipmentRepository(session, tenant)
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        # ASSUMPTION: both staff and vendors record full consignments, and only
        # after acceptance. docs/assumptions.md §C1-2.
        if order.status not in SHIPPING_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; a shipment can be recorded "
                "only once the vendor has accepted it and while goods are still due.",
                details={"status": str(order.status)},
            )
        if payload.transporter_id:
            transporter = await repo.require_transporter(payload.transporter_id)
            if transporter is None:
                raise NotFoundError("Transporter", payload.transporter_id)
            if not payload.courier_name:
                payload = payload.model_copy(update={"courier_name": transporter.name})
        if payload.shipped_at.date() < order.po_date:
            raise ValidationError("Dispatch cannot precede the purchase order date.")
        if payload.shipped_at > utcnow() + DELIVERY_CLOCK_SKEW:
            raise ValidationError("A dispatch cannot be recorded in the future.")
        if shipment_id:
            shipment = await _order_shipment(session, tenant, order_id, shipment_id)
            if shipment.delivered_at and payload.shipped_at > shipment.delivered_at:
                raise ValidationError("Dispatch cannot follow recorded delivery.")
            before = InboundShipmentRead.model_validate(shipment).model_dump(mode="json")
            for key, value in payload.model_dump().items():
                setattr(shipment, key, value)
            repo.touch(shipment)
        else:
            before = None
            shipment = repo.add(
                PurchaseOrderShipment(
                    purchase_order_id=order_id,
                    shipment_number=await repo.next_number(order_id),
                    **payload.model_dump(),
                )
            )
        await session.flush()
        latest = await repo.latest(order_id)
        if latest:
            # Back-compat: the header's single-shipment fields mirror the latest
            # consignment, so screens and reports built on them keep working.
            order.courier_name = latest.courier_name
            order.tracking_reference = latest.tracking_reference
            order.shipped_at = latest.shipped_at
            PurchaseOrderRepository(session, tenant).touch(order)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE if shipment_id else AuditAction.CREATE,
            entity_type="purchase_order_shipment",
            entity_id=shipment.id,
            entity_code=order.code,
            summary=(
                f"{'Corrected' if shipment_id else 'Recorded'} shipment "
                f"{shipment.shipment_number} for {order.code}"
            ),
            user_id=tenant.user_id,
            old_values=before,
            new_values=payload.model_dump(mode="json"),
        )
        await notify_buyer(
            session,
            tenant,
            order,
            NotificationType.PURCHASE_ORDER_SHIPPED,
            shipment_message(
                order, shipment, "tracking updated" if shipment_id else "shipment recorded"
            ),
            shipment_id=shipment.id,
        )
        return InboundShipmentRead.model_validate(shipment)


async def mark_delivered(
    session: Any,
    tenant: TenantContext,
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    delivered_at: datetime | None = None,
) -> InboundShipmentRead:
    """Record that a consignment reached the buyer's door (before or without a GRN)."""
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if order.status in SETTLED_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; its consignments can no "
                "longer change.",
                details={"status": str(order.status)},
            )
        shipment = await _order_shipment(session, tenant, order_id, shipment_id)
        repo = ShipmentRepository(session, tenant)
        moment = delivered_at or utcnow()
        if moment.tzinfo is None:
            raise ValidationError("delivered_at must include a timezone offset.")
        moment = moment.astimezone(UTC)
        if moment < shipment.shipped_at:
            raise ValidationError("Delivery cannot precede dispatch.")
        if moment > utcnow() + DELIVERY_CLOCK_SKEW:
            raise ValidationError("A delivery cannot be recorded in the future.")
        before = shipment.delivered_at
        shipment.delivered_at = moment
        repo.touch(shipment)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="purchase_order_shipment",
            entity_id=shipment.id,
            entity_code=order.code,
            summary=f"Marked shipment {shipment.shipment_number} of {order.code} delivered",
            user_id=tenant.user_id,
            old_values={"delivered_at": before},
            new_values={"delivered_at": moment},
        )
        await notify_buyer(
            session,
            tenant,
            order,
            NotificationType.PURCHASE_ORDER_SHIPPED,
            shipment_message(order, shipment, "delivered"),
            shipment_id=shipment.id,
        )
        return InboundShipmentRead.model_validate(shipment)


async def list_shipments(
    session: Any, tenant: TenantContext, order_id: uuid.UUID, params: PageParams
) -> tuple[list[InboundShipmentRead], OffsetPagination]:
    await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
    repo = ShipmentRepository(session, tenant)
    rows, page = await repo.list_page(
        params, stmt=repo.for_order(order_id), order_by=PurchaseOrderShipment.shipment_number.desc()
    )
    return [InboundShipmentRead.model_validate(r) for r in rows], page


async def upload_document(
    session: Any,
    tenant: TenantContext,
    order_id: uuid.UUID,
    upload: UploadFile,
    kind: DocumentKind,
    shipment_id: uuid.UUID | None = None,
) -> InboundDocumentRead:
    """File a typed document against an order, and optionally one of its consignments.

    Files attached here are the ones shared between the buyer and the vendor
    of the order (docs/assumptions.md §C1-3).
    """
    async with transaction(session):
        order = await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
        shipment = (
            await _order_shipment(session, tenant, order.id, shipment_id) if shipment_id else None
        )
        doc = await DocumentService(session, tenant).upload(
            upload,
            DocumentUploadRequest(
                category=DocumentCategory.DOCUMENT,
                entity_type=ORDER_DOCUMENT_ENTITY,
                entity_id=order.id,
            ),
        )
        repo = InboundDocumentRepository(session, tenant)
        link = repo.add(
            PurchaseOrderDocument(
                purchase_order_id=order.id, document_id=doc.id, kind=kind, shipment_id=shipment_id
            )
        )
        await session.flush()
        label = DOCUMENT_KIND_LABELS[kind]
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="purchase_order_document",
            entity_id=link.id,
            entity_code=order.code,
            summary=f"Attached {label.lower()} {doc.file_name} to {order.code}",
            user_id=tenant.user_id,
            new_values={"kind": kind, "document_id": doc.id, "shipment_id": shipment_id},
        )
        if kind in ("LR_COPY", "DISPATCH_PHOTO"):
            event = f"{label} uploaded"
            await notify_buyer(
                session,
                tenant,
                order,
                NotificationType.PURCHASE_ORDER_SHIPPED,
                shipment_message(order, shipment, event) if shipment else f"{order.code}: {event}",
                shipment_id=shipment_id,
            )
        return InboundDocumentRead(**doc.model_dump(), kind=kind, shipment_id=shipment_id)


async def list_documents(
    session: Any, tenant: TenantContext, order_id: uuid.UUID, params: PageParams
) -> tuple[list[InboundDocumentRead], OffsetPagination]:
    """The order's files the caller may see (all of them for staff; shared ones for a vendor)."""
    await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
    rows = await InboundDocumentRepository(session, tenant).page_documents(order_id, params)
    return [
        InboundDocumentRead(
            **DocumentRead.model_validate(doc).model_dump(),
            kind=link.kind if link else "OTHER",
            shipment_id=link.shipment_id if link else None,
        )
        for doc, link in rows[: params.limit]
    ], OffsetPagination(
        page=params.page,
        limit=params.limit,
        has_next=len(rows) > params.limit,
        has_previous=params.page > 1,
    )


#: Two decimals for a percentage of goods received - a ratio, not a quantity.
PERCENT_QUANTUM = Decimal("0.01")


def tracking_read(row: Any, as_of: date) -> InboundTrackingRead:
    """One board/list row from one projection row.

    The tracking status is the one the database computed for the row - the
    same CASE the filters and the dashboard counted by - never re-derived here.
    """
    po, shipment = row[0], row[1]
    shipped_at = shipment.shipped_at if shipment else po.shipped_at
    expected = (
        (shipment.expected_delivery_date if shipment else None)
        or po.vendor_committed_date
        or po.expected_delivery_date
    )
    late = po_is_late(
        status=po.status,
        open_quantity=row.open_quantity,
        expected_date=po.expected_delivery_date,
        as_of=as_of,
    )
    ordered = Decimal(row.ordered or 0)
    received = Decimal(row.received or 0)
    percentage = (
        min(Decimal("100"), received / ordered * 100).quantize(PERCENT_QUANTUM)
        if ordered > 0
        else Decimal("0")
    )
    return InboundTrackingRead(
        id=po.id,
        code=po.code,
        po_date=po.po_date,
        status=po.status,
        vendor_id=po.vendor_id,
        vendor_name=row.vendor_name,
        warehouse_id=po.warehouse_id,
        item_category_id=po.item_category_id,
        item_category_code=row.category_code,
        item_category_name=row.category_name,
        assigned_vendor_user_id=po.assigned_vendor_user_id,
        assignee_name=row.assignee_name,
        currency=po.currency,
        total_amount=po.total_amount,
        item_count=row.item_count,
        expected_delivery_date=po.expected_delivery_date,
        vendor_committed_date=po.vendor_committed_date,
        delivery_expected_date=expected,
        sent_at=po.sent_at,
        acknowledged_at=po.acknowledged_at,
        ready_for_dispatch_at=po.ready_for_dispatch_at,
        ready_packages=po.ready_packages,
        vendor_decline_reason=po.vendor_decline_reason,
        tracking_status=InboundTrackingStatus(row.derived_tracking_status),
        shipment_count=row.shipment_count,
        latest_shipment=InboundShipmentRead.model_validate(shipment) if shipment else None,
        courier_name=shipment.courier_name if shipment else po.courier_name,
        tracking_reference=shipment.tracking_reference if shipment else po.tracking_reference,
        shipped_at=shipped_at,
        days_late=(as_of - po.expected_delivery_date).days if late else 0,
        received_percentage=percentage,
        grn_pending=shipped_at is not None and row.receipts_since_dispatch == 0,
        qc_pending=row.qc_count > 0,
        documents_count=row.documents_count,
        has_lr_copy=bool(row.has_lr_copy),
        created_at=po.created_at,
    )


async def tracking_board(
    session: Any, tenant: TenantContext, filters: InboundTrackingFilters, params: PageParams
) -> tuple[list[InboundTrackingRead], OffsetPagination]:
    as_of = today()
    rows, page = await InboundRepository(session, tenant).page_projection(filters, params, as_of)
    return [tracking_read(r, as_of) for r in rows], page


async def tracking_row(
    session: Any, tenant: TenantContext, order_id: uuid.UUID
) -> InboundTrackingRead:
    """One order's tracking row - "not found" when the caller cannot see the order."""
    as_of = today()
    row = await InboundRepository(session, tenant).order_row(order_id, as_of)
    if row is None:
        raise NotFoundError("Purchase order", order_id)
    return tracking_read(row, as_of)


async def accept_vendor_date(
    session: Any, tenant: TenantContext, order_id: uuid.UUID
) -> PurchaseOrderSummary:
    """Adopt the vendor's committed date as the planning date (MRP nets on it).

    Explicit and audited because the two dates are kept apart on purpose
    (docs/assumptions.md §C1-1). Idempotent: when they already agree nothing
    is written.
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if order.status not in SHIPPING_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; the planning date can follow "
                "the vendor's commitment only while goods are still due.",
                details={"status": str(order.status)},
            )
        if order.vendor_committed_date is None:
            raise BusinessRuleError("The vendor has not committed a delivery date.")
        if order.expected_delivery_date != order.vendor_committed_date:
            before = order.expected_delivery_date
            order.expected_delivery_date = order.vendor_committed_date
            PurchaseOrderRepository(session, tenant).touch(order)
            await session.flush()
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type="purchase_order",
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Accepted the vendor's delivery date for {order.code}",
                user_id=tenant.user_id,
                old_values={"expected_delivery_date": before},
                new_values={"expected_delivery_date": order.expected_delivery_date},
            )
        return PurchaseOrderSummary.model_validate(order)


__all__ = [
    "BUYER_ALERT_ROLE_CODES",
    "DELIVERY_CLOCK_SKEW",
    "SETTLED_PO_STATUSES",
    "SHIPPING_PO_STATUSES",
    "accept_vendor_date",
    "buyer_recipients",
    "list_documents",
    "list_shipments",
    "mark_delivered",
    "notify_buyer",
    "record_inbound_shipment",
    "shipment_message",
    "tracking_board",
    "tracking_read",
    "tracking_row",
    "upload_document",
]
