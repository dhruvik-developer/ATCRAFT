"""Consignment and attachment rules used by staff and vendor routes."""

from __future__ import annotations

import uuid
from datetime import UTC, date
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
    InboundDocumentRepository,
    InboundRepository,
    ShipmentRepository,
    inbound_tracking_status,
    po_is_late,
)
from src.modules.procurement.inbound_schemas import (
    DocumentKind,
    InboundDocumentRead,
    InboundShipmentCreate,
    InboundShipmentRead,
    InboundTrackingFilters,
    InboundTrackingRead,
)
from src.modules.procurement.models import PurchaseOrder
from src.modules.procurement.repository import PurchaseOrderRepository


async def notify_buyer(
    session: Any,
    tenant: TenantContext,
    order: PurchaseOrder,
    kind: NotificationType,
    body: str,
    *,
    shipment_id: uuid.UUID | None = None,
) -> None:
    repo = NotificationRepository(session, tenant)
    ids = await repo.user_ids_with_permission(str(Permission.PURCHASE_ORDER_APPROVE))
    ids += await repo.user_ids_for_roles(("ADMIN", "PURCHASE_MANAGER"))
    if order.created_by_id:
        ids += await repo.internal_user_ids([order.created_by_id])
    await notify(
        session,
        company_id=tenant.company_id,
        user_ids=ids,
        notification_type=kind,
        title=f"{order.code}: vendor update",
        body=body,
        entity_type="purchase_order",
        entity_id=order.id,
        action_url=f"/procurement/purchase-orders/{order.id}",
        group_key=f"po-shipment:{shipment_id}" if shipment_id else None,
        actor_id=tenant.user_id,
    )


def shipment_message(order: PurchaseOrder, shipment: PurchaseOrderShipment, event: str) -> str:
    carrier = shipment.courier_name or "Transporter"
    tracking = shipment.tracking_reference or "tracking pending"
    expected = shipment.expected_delivery_date or "not specified"
    return (
        f"Shipment {shipment.shipment_number} for {order.code}: {carrier} · "
        f"{tracking} · dispatch {shipment.shipped_at.date()} · "
        f"expected {expected} · {shipment.packages or 0} boxes · "
        f"{shipment.weight_kg or 0} kg · {event}"
    )


async def record_inbound_shipment(
    session: Any,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: InboundShipmentCreate,
    *,
    shipment_id: uuid.UUID | None = None,
) -> InboundShipmentRead:
    repo = ShipmentRepository(session, tenant)
    async with transaction(session):
        order = await PurchaseOrderRepository(session, tenant).lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        # ASSUMPTION: both staff and vendors record full consignments after acceptance.
        # docs/assumptions.md §C1-2.
        if order.status not in ("ACKNOWLEDGED", "PARTIALLY_RECEIVED"):
            raise BusinessRuleError(
                "The vendor must accept the purchase order before recording a shipment."
            )
        if payload.transporter_id:
            transporter = await repo.require_transporter(payload.transporter_id)
            if transporter is None:
                raise NotFoundError("Transporter", payload.transporter_id)
            if not payload.courier_name:
                payload = payload.model_copy(update={"courier_name": transporter.name})
        if payload.shipped_at.date() < order.po_date:
            raise ValidationError("Dispatch cannot precede the purchase order date.")
        if shipment_id:
            shipment = await repo.get_or_404(shipment_id)
            if shipment.purchase_order_id != order_id:
                raise NotFoundError("Purchase order shipment", shipment_id)
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
            summary=f"Recorded shipment {shipment.shipment_number} for {order.code}",
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
    delivered_at: Any = None,
) -> InboundShipmentRead:
    async with transaction(session):
        order = await PurchaseOrderRepository(session, tenant).lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        repo = ShipmentRepository(session, tenant)
        shipment = await repo.get_or_404(shipment_id)
        if shipment.purchase_order_id != order_id:
            raise NotFoundError("Purchase order shipment", shipment_id)
        moment = delivered_at or utcnow()
        if moment.tzinfo is None or moment < shipment.shipped_at:
            raise ValidationError("Delivery must include a timezone and cannot precede dispatch.")
        moment = moment.astimezone(UTC)
        before = shipment.delivered_at
        shipment.delivered_at = moment
        repo.touch(shipment)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="purchase_order_shipment",
            entity_id=shipment.id,
            entity_code=order.code,
            summary=f"Marked shipment {shipment.shipment_number} delivered",
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
        await session.flush()
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
    async with transaction(session):
        order = await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
        shipment = None
        if shipment_id:
            shipment = await ShipmentRepository(session, tenant).get_or_404(shipment_id)
            if shipment.purchase_order_id != order.id:
                raise NotFoundError("Purchase order shipment", shipment_id)
        doc = await DocumentService(session, tenant).upload(
            upload,
            DocumentUploadRequest(
                category=DocumentCategory.DOCUMENT, entity_type="purchase_order", entity_id=order.id
            ),
        )
        repo = InboundDocumentRepository(session, tenant)
        link = repo.add(
            PurchaseOrderDocument(
                purchase_order_id=order.id, document_id=doc.id, kind=kind, shipment_id=shipment_id
            )
        )
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="purchase_order_document",
            entity_id=link.id,
            entity_code=order.code,
            summary=f"Attached {kind} to {order.code}",
            user_id=tenant.user_id,
            new_values={"kind": kind, "document_id": doc.id, "shipment_id": shipment_id},
        )
        if kind in ("LR_COPY", "DISPATCH_PHOTO"):
            body = (
                shipment_message(order, shipment, f"{kind.replace('_', ' ').title()} uploaded")
                if shipment
                else f"{kind.replace('_', ' ').title()} uploaded"
            )
            await notify_buyer(
                session,
                tenant,
                order,
                NotificationType.PURCHASE_ORDER_SHIPPED,
                body,
                shipment_id=shipment_id,
            )
        return InboundDocumentRead(**doc.model_dump(), kind=kind, shipment_id=shipment_id)


async def list_documents(
    session: Any, tenant: TenantContext, order_id: uuid.UUID, params: PageParams
) -> tuple[list[InboundDocumentRead], OffsetPagination]:
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


def tracking_read(row: Any, as_of: date) -> InboundTrackingRead:
    po, shipment = row[0], row[1]
    shipped_at = shipment.shipped_at if shipment else po.shipped_at
    status = inbound_tracking_status(
        fully_received=row.open_quantity <= 0 or po.status == "CLOSED",
        receipt_exists=row.receipt_count > 0,
        qc_pending=row.qc_count > 0,
        shipment_exists=shipped_at is not None,
        delivered=bool(shipment and shipment.delivered_at),
        shipped_today=bool(shipped_at and shipped_at.date() == as_of),
        ready=po.ready_for_dispatch_at is not None,
    )
    expected = (
        (shipment.expected_delivery_date if shipment else None)
        or po.vendor_committed_date
        or po.expected_delivery_date
    )
    late = po_is_late(
        status=str(po.status),
        open_quantity=row.open_quantity,
        expected_date=po.expected_delivery_date,
        as_of=as_of,
    )
    from src.modules.procurement.schemas import PurchaseOrderSummary

    return InboundTrackingRead(
        **PurchaseOrderSummary.model_validate(po).model_dump(),
        **{
            "vendor_name": row.vendor_name,
            "assignee_name": row.assignee_name,
            "tracking_status": status,
            "latest_shipment": InboundShipmentRead.model_validate(shipment) if shipment else None,
            "delivery_expected_date": expected,
            "days_late": (as_of - po.expected_delivery_date).days if late else 0,
            "received_percentage": min(Decimal("100"), row.received / row.ordered * 100)
            if row.ordered
            else Decimal("0"),
            "item_count": row.item_count,
            "qc_pending": row.qc_count > 0,
            "grn_pending": shipped_at is not None and row.receipt_count == 0,
            "documents_count": row.documents_count,
            "has_lr_copy": row.has_lr_copy,
            "courier_name": shipment.courier_name if shipment else po.courier_name,
            "tracking_reference": shipment.tracking_reference
            if shipment
            else po.tracking_reference,
            "shipped_at": shipped_at,
        },
    )


async def tracking_board(
    session: Any, tenant: TenantContext, filters: InboundTrackingFilters, params: PageParams
) -> tuple[list[InboundTrackingRead], OffsetPagination]:
    rows, page = await InboundRepository(session, tenant).page_projection(filters, params, today())
    return [tracking_read(r, today()) for r in rows], page


async def accept_vendor_date(session: Any, tenant: TenantContext, order_id: uuid.UUID) -> None:
    async with transaction(session):
        order = await PurchaseOrderRepository(session, tenant).lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        if order.vendor_committed_date is None:
            raise BusinessRuleError("The vendor has not committed a delivery date.")
        before = order.expected_delivery_date
        order.expected_delivery_date = order.vendor_committed_date
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="purchase_order",
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Accepted vendor delivery date for {order.code}",
            user_id=tenant.user_id,
            old_values={"expected_delivery_date": before},
            new_values={"expected_delivery_date": order.expected_delivery_date},
        )
