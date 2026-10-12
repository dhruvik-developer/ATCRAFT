"""Batched inbound projections shared by the portal and buyer board."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select

from src.common.repository import BaseRepository
from src.core.clock import today
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.documents.models import Document
from src.modules.procurement.inbound_models import PurchaseOrderDocument, PurchaseOrderShipment
from src.modules.procurement.inbound_schemas import InboundTrackingFilters, InboundTrackingStatus
from src.modules.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
)
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.products.models import ItemCategory
from src.modules.quality.models import OPEN_STATUSES
from src.modules.quality.models import QcInspection as Inspection
from src.modules.users.models import User
from src.modules.vendors.models import Transporter, Vendor


def inbound_tracking_status(
    *,
    fully_received: Any,
    receipt_exists: Any,
    qc_pending: Any,
    shipment_exists: Any,
    delivered: Any,
    shipped_today: Any,
    ready: Any,
    sql: bool = False,
) -> Any:
    # ASSUMPTION: receipts outrank courier evidence; today's dispatch is DISPATCHED
    # and older undelivered consignments are IN_TRANSIT. docs/assumptions.md §C1-4.
    rules = (
        (receipt_exists & qc_pending, InboundTrackingStatus.QC_PENDING),
        (receipt_exists & fully_received, InboundTrackingStatus.GRN_COMPLETED),
        (receipt_exists, InboundTrackingStatus.RECEIVED),
        (delivered, InboundTrackingStatus.DELIVERED),
        (shipment_exists & shipped_today, InboundTrackingStatus.DISPATCHED),
        (shipment_exists, InboundTrackingStatus.IN_TRANSIT),
        (ready, InboundTrackingStatus.READY_FOR_DISPATCH),
    )
    if sql:
        return case(*rules, else_=InboundTrackingStatus.NOT_DISPATCHED.value)
    return next(
        (value for condition, value in rules if condition), InboundTrackingStatus.NOT_DISPATCHED
    )


def po_is_late(
    *, status: Any, open_quantity: Any, expected_date: Any, as_of: date, sql: bool = False
) -> Any:
    receivable = ("SENT", "ACKNOWLEDGED", "PARTIALLY_RECEIVED")
    if sql:
        return and_(
            status.in_(receivable),
            open_quantity > 0,
            expected_date.is_not(None),
            expected_date < as_of,
        )
    return (
        status in receivable
        and open_quantity > 0
        and expected_date is not None
        and expected_date < as_of
    )


class ShipmentRepository(BaseRepository[PurchaseOrderShipment]):
    model = PurchaseOrderShipment

    def for_order(self, order_id: uuid.UUID) -> Select[Any]:
        return self._scoped().where(PurchaseOrderShipment.purchase_order_id == order_id)

    async def latest(self, order_id: uuid.UUID) -> PurchaseOrderShipment | None:
        return await self.session.scalar(
            self.for_order(order_id).order_by(PurchaseOrderShipment.shipment_number.desc()).limit(1)
        )

    async def next_number(self, order_id: uuid.UUID) -> int:
        # Caller holds the parent PO lock, so concurrent first consignments serialize.
        value = await self.session.scalar(
            select(func.max(PurchaseOrderShipment.shipment_number)).where(
                PurchaseOrderShipment.company_id == self.tenant.company_id,
                PurchaseOrderShipment.purchase_order_id == order_id,
            )
        )
        return int(value or 0) + 1

    async def require_transporter(self, transporter_id: uuid.UUID) -> Transporter | None:
        return await self.session.scalar(
            select(Transporter).where(
                Transporter.company_id == self.tenant.company_id,
                Transporter.id == transporter_id,
                Transporter.is_active.is_(True),
                Transporter.deleted_at.is_(None),
            )
        )


class InboundDocumentRepository(BaseRepository[PurchaseOrderDocument]):
    model = PurchaseOrderDocument

    async def invoice_document(
        self, document_id: uuid.UUID, order_id: uuid.UUID
    ) -> PurchaseOrderDocument | None:
        return await self.session.scalar(
            self._scoped()
            .where(
                PurchaseOrderDocument.document_id == document_id,
                PurchaseOrderDocument.purchase_order_id == order_id,
                PurchaseOrderDocument.kind == "INVOICE",
            )
            .with_for_update()
        )

    async def page_documents(self, order_id: uuid.UUID, params: PageParams) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    self.documents([order_id])
                    .order_by(Document.created_at.desc(), Document.id.desc())
                    .offset(params.offset)
                    .limit(params.limit + 1)
                )
            ).all()
        )

    def documents(self, order_ids: list[uuid.UUID]) -> Select[Any]:
        return (
            select(Document, PurchaseOrderDocument)
            .outerjoin(
                PurchaseOrderDocument,
                and_(
                    PurchaseOrderDocument.document_id == Document.id,
                    PurchaseOrderDocument.company_id == Document.company_id,
                ),
            )
            .where(
                Document.company_id == self.tenant.company_id,
                Document.entity_type == "purchase_order",
                Document.entity_id.in_(order_ids),
            )
        )


class InboundRepository(PurchaseOrderRepository):
    """One row per PO, including all tracking inputs; no relationship loads."""

    def projection(self) -> Select[Any]:
        cid = self.tenant.company_id
        lines = (
            select(
                PurchaseOrderLine.purchase_order_id.label("po_id"),
                func.count().label("item_count"),
                func.sum(PurchaseOrderLine.quantity).label("ordered"),
                func.sum(PurchaseOrderLine.received_quantity).label("received"),
                func.sum(
                    func.greatest(
                        PurchaseOrderLine.quantity - PurchaseOrderLine.received_quantity, 0
                    )
                ).label("open_quantity"),
            )
            .where(PurchaseOrderLine.company_id == cid)
            .group_by(PurchaseOrderLine.purchase_order_id)
            .subquery()
        )
        latest = (
            select(PurchaseOrderShipment.id)
            .where(
                PurchaseOrderShipment.company_id == cid,
                PurchaseOrderShipment.purchase_order_id == PurchaseOrder.id,
            )
            .order_by(PurchaseOrderShipment.shipment_number.desc())
            .limit(1)
            .correlate(PurchaseOrder)
            .scalar_subquery()
        )
        receipts = (
            select(
                GoodsReceipt.purchase_order_id.label("po_id"), func.count().label("receipt_count")
            )
            .where(GoodsReceipt.company_id == cid, GoodsReceipt.status != "CANCELLED")
            .group_by(GoodsReceipt.purchase_order_id)
            .subquery()
        )
        inspection_stmt = select(
            GoodsReceipt.purchase_order_id.label("po_id"), func.count().label("qc_count")
        ).join(
            Inspection,
            and_(
                Inspection.company_id == GoodsReceipt.company_id,
                Inspection.reference_id == GoodsReceipt.id,
                Inspection.reference_type == "goods_receipt",
            ),
        )
        inspections = (
            inspection_stmt.where(
                GoodsReceipt.company_id == cid,
                GoodsReceipt.status == "POSTED",
                Inspection.status.in_(OPEN_STATUSES),
            )
            .group_by(GoodsReceipt.purchase_order_id)
            .subquery()
        )
        docs = (
            select(
                Document.entity_id.label("po_id"),
                func.count().label("documents_count"),
                func.bool_or(PurchaseOrderDocument.kind == "LR_COPY").label("has_lr_copy"),
            )
            .outerjoin(PurchaseOrderDocument, PurchaseOrderDocument.document_id == Document.id)
            .where(Document.company_id == cid, Document.entity_type == "purchase_order")
            .group_by(Document.entity_id)
            .subquery()
        )
        return (
            self._scoped()
            .add_columns(
                PurchaseOrderShipment,
                Vendor.name.label("vendor_name"),
                User.full_name.label("assignee_name"),
                ItemCategory.code.label("category_code"),
                ItemCategory.name.label("category_name"),
                func.coalesce(lines.c.item_count, 0).label("item_count"),
                func.coalesce(lines.c.ordered, 0).label("ordered"),
                func.coalesce(lines.c.received, 0).label("received"),
                func.coalesce(lines.c.open_quantity, 0).label("open_quantity"),
                func.coalesce(receipts.c.receipt_count, 0).label("receipt_count"),
                func.coalesce(inspections.c.qc_count, 0).label("qc_count"),
                func.coalesce(docs.c.documents_count, 0).label("documents_count"),
                func.coalesce(docs.c.has_lr_copy, False).label("has_lr_copy"),
                inbound_tracking_status(
                    fully_received=or_(
                        func.coalesce(lines.c.open_quantity, 0) <= 0,
                        PurchaseOrder.status == "CLOSED",
                    ),
                    receipt_exists=func.coalesce(receipts.c.receipt_count, 0) > 0,
                    qc_pending=func.coalesce(inspections.c.qc_count, 0) > 0,
                    shipment_exists=or_(
                        PurchaseOrderShipment.id.is_not(None), PurchaseOrder.shipped_at.is_not(None)
                    ),
                    delivered=PurchaseOrderShipment.delivered_at.is_not(None),
                    shipped_today=func.date(
                        func.coalesce(PurchaseOrderShipment.shipped_at, PurchaseOrder.shipped_at)
                    )
                    == today(),
                    ready=PurchaseOrder.ready_for_dispatch_at.is_not(None),
                    sql=True,
                ).label("derived_tracking_status"),
            )
            .join(Vendor, and_(Vendor.id == PurchaseOrder.vendor_id, Vendor.company_id == cid))
            .outerjoin(PurchaseOrderShipment, PurchaseOrderShipment.id == latest)
            .outerjoin(
                User, and_(User.id == PurchaseOrder.assigned_vendor_user_id, User.company_id == cid)
            )
            .outerjoin(
                ItemCategory,
                and_(
                    ItemCategory.id == PurchaseOrder.item_category_id,
                    ItemCategory.company_id == cid,
                ),
            )
            .outerjoin(lines, lines.c.po_id == PurchaseOrder.id)
            .outerjoin(receipts, receipts.c.po_id == PurchaseOrder.id)
            .outerjoin(inspections, inspections.c.po_id == PurchaseOrder.id)
            .outerjoin(docs, docs.c.po_id == PurchaseOrder.id)
        )

    def filtered_projection(self, filters: InboundTrackingFilters, as_of: date) -> Select[Any]:
        stmt = self.projection()
        stmt = stmt.where(
            PurchaseOrder.status.not_in(("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED"))
        )
        tracking = stmt.selected_columns["derived_tracking_status"]
        if filters.search:
            term = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(PurchaseOrder.code.ilike(term), Vendor.name.ilike(term)))
        if filters.vendor_id:
            stmt = stmt.where(PurchaseOrder.vendor_id == filters.vendor_id)
        if filters.category_id:
            stmt = stmt.where(PurchaseOrder.item_category_id == filters.category_id)
        if filters.assigned_to_me:
            stmt = stmt.where(PurchaseOrder.assigned_vendor_user_id == self.tenant.user_id)
        if filters.overdue:
            stmt = stmt.where(
                po_is_late(
                    status=PurchaseOrder.status,
                    open_quantity=stmt.selected_columns["open_quantity"],
                    expected_date=PurchaseOrder.expected_delivery_date,
                    as_of=as_of,
                    sql=True,
                )
            )
        if filters.expected_today:
            stmt = stmt.where(
                func.coalesce(
                    PurchaseOrderShipment.expected_delivery_date,
                    PurchaseOrder.vendor_committed_date,
                    PurchaseOrder.expected_delivery_date,
                )
                == as_of
            )
        if filters.not_acknowledged or filters.tab == "to_accept":
            stmt = stmt.where(PurchaseOrder.status == "SENT")
        if filters.tab == "accepted":
            stmt = stmt.where(
                PurchaseOrder.status == "ACKNOWLEDGED",
                tracking == "NOT_DISPATCHED",
            )
        if filters.tab == "ready":
            stmt = stmt.where(tracking == "READY_FOR_DISPATCH")
        if filters.in_transit or filters.tab == "dispatched":
            stmt = stmt.where(tracking.in_(("DISPATCHED", "IN_TRANSIT")))
        if filters.tab == "delivered":
            stmt = stmt.where(
                tracking.in_(("DELIVERED", "RECEIVED", "QC_PENDING", "GRN_COMPLETED"))
            )
        return stmt

    async def page_projection(
        self, filters: InboundTrackingFilters, params: PageParams, as_of: date
    ) -> tuple[list[Any], OffsetPagination]:
        stmt = self.filtered_projection(filters, as_of)
        total = (
            int(await self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            if params.with_total
            else None
        )
        rows = (
            await self.session.execute(
                stmt.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
                .offset(params.offset)
                .limit(params.limit + 1)
            )
        ).all()
        return list(rows[: params.limit]), OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=(total + params.limit - 1) // params.limit if total is not None else None,
            has_next=len(rows) > params.limit,
            has_previous=params.page > 1,
        )
