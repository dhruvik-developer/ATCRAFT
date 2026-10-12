"""Batched inbound projections shared by the vendor portal and the buyer's board.

One statement per page, whoever asks. Every per-order figure - lines, receipts,
open inspections, filed documents, the consignment count - is a subquery
correlated to the order row, so its cost is proportional to the orders the
caller can see (one vendor's, one page's worth) rather than to the whole
company's purchasing history, which is what a grouped subquery joined back on
``purchase_order_id`` costs on every request.

The quality figure is reached through a guarded import
(:func:`resolve_open_inspection_counter`). Procurement sits above quality in the
dependency order (docs/architecture.md), so it never imports quality's types;
it asks for a correlatable count the way it asks for every other quality fact.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select, true
from sqlalchemy.orm import aliased

from src.common.repository import BaseRepository
from src.core.clock import utcnow
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.documents.models import Document
from src.modules.procurement.inbound_models import PurchaseOrderDocument, PurchaseOrderShipment
from src.modules.procurement.inbound_schemas import InboundTrackingFilters, InboundTrackingStatus
from src.modules.procurement.models import (
    RECEIVABLE_PO_STATUSES,
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.products.models import ItemCategory
from src.modules.users.models import User
from src.modules.vendors.models import Transporter, Vendor

#: The order's category, joined for its label under its own name: the vendor
#: scope's subtree probe (``category_within``) reads ``item_categories`` too.
OrderCategory = aliased(ItemCategory, name="order_category")

#: The reference type incoming inspections are filed under - the goods receipt.
#: Mirrors ``service.MOVEMENT_REFERENCE_GRN``; spelled here so this repository
#: does not import the service module that imports it.
RECEIPT_REFERENCE = "goods_receipt"

#: The entity type every purchase-order file is stored under in ``documents``.
ORDER_DOCUMENT_ENTITY = "purchase_order"

#: The buyer's working copy. Never on the board, never in the vendor's list
#: (``PurchaseOrderRepository._scoped`` already hides it from vendors).
UNSENT_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.REJECTED,
)

# ASSUMPTION: the buyer's inbound board is a work queue, so by default it shows
# only orders still in motion - with the vendor (SENT, VENDOR_DECLINED awaiting
# the buyer's decision, ACKNOWLEDGED, PARTIALLY_RECEIVED) or arrived but not yet
# through QC/GRN. Completed, closed and cancelled orders appear only with
# ``include_completed``. docs/assumptions.md §C1-6.
BOARD_OPEN_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.VENDOR_DECLINED,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
    PurchaseOrderStatus.RECEIVED,
    PurchaseOrderStatus.QC_PENDING,
    PurchaseOrderStatus.QC_COMPLETED,
)

#: The vendor app's order tabs and dashboard tiles that are read off the
#: derived tracking status. ``to_accept`` (SENT) and ``accepted`` (ACKNOWLEDGED,
#: nothing dispatched yet) also read the order status - see :func:`order_tab`.
TAB_TRACKING_STATUSES: dict[str, tuple[InboundTrackingStatus, ...]] = {
    "ready": (InboundTrackingStatus.READY_FOR_DISPATCH,),
    "dispatched": (InboundTrackingStatus.DISPATCHED, InboundTrackingStatus.IN_TRANSIT),
    "delivered": (
        InboundTrackingStatus.DELIVERED,
        InboundTrackingStatus.RECEIVED,
        InboundTrackingStatus.QC_PENDING,
        InboundTrackingStatus.GRN_COMPLETED,
    ),
}


def inbound_tracking_status(
    *,
    fully_received: Any,
    any_receipt: Any,
    receipt_since_dispatch: Any,
    qc_pending: Any,
    shipment_exists: Any,
    delivered: Any,
    shipped_today: Any,
    ready: Any,
    sql: bool = False,
) -> Any:
    """The one ordered rule behind every tracking status the ERP shows.

    Called with booleans it answers for one row (``tracking_read``); called
    with SQL expressions and ``sql=True`` it renders the same rules as a CASE,
    which is what the board's filters and the vendor dashboard count by - so
    the three can never disagree.

    ``receipt_since_dispatch`` is a goods receipt raised at or after the latest
    consignment left (or any receipt, when nothing was dispatched). A receipt
    for an *earlier* consignment does not mask the one now on the road.
    """
    # ASSUMPTION: receipt evidence outranks carrier evidence, and the latest
    # consignment is what the status describes; a dispatch dated today is
    # DISPATCHED and an older undelivered one IN_TRANSIT. A fully received or
    # short-closed order is GRN_COMPLETED once no inspection is open.
    # docs/assumptions.md §C1-5.
    rules = (
        (
            any_receipt & qc_pending & (fully_received | receipt_since_dispatch),
            InboundTrackingStatus.QC_PENDING,
        ),
        (any_receipt & fully_received, InboundTrackingStatus.GRN_COMPLETED),
        (receipt_since_dispatch, InboundTrackingStatus.RECEIVED),
        (delivered, InboundTrackingStatus.DELIVERED),
        (shipment_exists & shipped_today, InboundTrackingStatus.DISPATCHED),
        (shipment_exists, InboundTrackingStatus.IN_TRANSIT),
        (ready, InboundTrackingStatus.READY_FOR_DISPATCH),
    )
    if sql:
        return case(
            *((condition, value.value) for condition, value in rules),
            else_=InboundTrackingStatus.NOT_DISPATCHED.value,
        )
    return next(
        (value for condition, value in rules if condition), InboundTrackingStatus.NOT_DISPATCHED
    )


def order_tab(status: str, tracking: InboundTrackingStatus) -> str | None:
    """The vendor-app tab (and dashboard tile) one order belongs to, if any.

    The pure statement of :meth:`InboundRepository.tab_clause`, which is the
    SQL the list and the dashboard actually run.
    """
    if status == PurchaseOrderStatus.SENT:
        return "to_accept"
    if (
        status == PurchaseOrderStatus.ACKNOWLEDGED
        and tracking is InboundTrackingStatus.NOT_DISPATCHED
    ):
        return "accepted"
    for tab, members in TAB_TRACKING_STATUSES.items():
        if tracking in members:
            return tab
    return None


def po_is_late(
    *, status: Any, open_quantity: Any, expected_date: Any, as_of: date, sql: bool = False
) -> Any:
    """The one "PO late" predicate: receivable, goods still due, planning date passed.

    ``order_tracking``'s ``is_delayed``, the board's ``overdue`` filter and its
    ``days_late`` all read this; the delay alerts are to reuse it rather than
    restate it.
    """
    receivable = tuple(str(value) for value in RECEIVABLE_PO_STATUSES)
    if sql:
        return and_(
            status.in_(receivable),
            open_quantity > 0,
            expected_date.is_not(None),
            expected_date < as_of,
        )
    return (
        str(status) in receivable
        and open_quantity > 0
        and expected_date is not None
        and expected_date < as_of
    )


def resolve_open_inspection_counter() -> Callable[..., ColumnElement[int]] | None:
    """Quality's correlatable open-inspection count, if the module is deployed.

    Only the module's absence is tolerated, exactly as for the other quality
    seams in ``procurement.service``: without it no inspection can be open.
    """
    try:
        from src.modules.quality import repository as quality_repository
    except ImportError:
        return None
    counter = getattr(quality_repository, "open_inspection_count", None)
    return counter if callable(counter) else None


def document_is_live(now: datetime) -> ColumnElement[bool]:
    """A file past ``expires_at`` behaves as if it were not there (documents module rule)."""
    return or_(Document.expires_at.is_(None), Document.expires_at > now)


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
        """An active transporter of this tenant - a bare FK checks neither."""
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

    def visibility(self) -> ColumnElement[bool]:
        """Which of an order's files the caller may see.

        Staff see every file filed against the order. A vendor sees only what
        was shared with it: files attached through the purchase-order documents
        route (they carry a typed link) and the generated PO PDF. A file a buyer
        filed against the order from the generic Documents screen - a quote
        comparison, an internal memo - stays internal. Built over a statement
        that outer-joins the link table.
        """
        if self.tenant.vendor_id is None:
            return true()
        # ASSUMPTION: the PO documents route is the shared channel; the generic
        # Documents module stays internal. docs/assumptions.md §C1-3.
        from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot

        generated_order_pdf = (
            select(ReportSnapshot.id)
            .where(
                ReportSnapshot.company_id == Document.company_id,
                ReportSnapshot.document_id == Document.id,
                ReportSnapshot.report_key == ReportKey.PURCHASE_ORDER,
                ReportSnapshot.status == ReportJobStatus.COMPLETED,
            )
            .correlate(Document)
            .exists()
        )
        return or_(
            PurchaseOrderDocument.id.is_not(None),
            generated_order_pdf,
        )

    def documents(self, order_ids: list[uuid.UUID], now: datetime) -> Select[Any]:
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
                Document.entity_type == ORDER_DOCUMENT_ENTITY,
                Document.entity_id.in_(order_ids),
                document_is_live(now),
                self.visibility(),
            )
        )

    async def page_documents(self, order_id: uuid.UUID, params: PageParams) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    self.documents([order_id], utcnow())
                    .order_by(Document.created_at.desc(), Document.id.desc())
                    .offset(params.offset)
                    .limit(params.limit + 1)
                )
            ).all()
        )

    async def visible_document(self, document_id: uuid.UUID, order_id: uuid.UUID) -> Any:
        """The file, if it is one of ``order_id``'s files the caller may see."""
        return (
            await self.session.execute(
                self.documents([order_id], utcnow()).where(Document.id == document_id)
            )
        ).first()

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


class InboundRepository(PurchaseOrderRepository):
    """One row per PO, including all tracking inputs; no relationship loads."""

    def projection(self, as_of: date) -> Select[Any]:
        cid = self.tenant.company_id
        latest_id = (
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
        shipment_count = (
            select(func.count())
            .select_from(PurchaseOrderShipment)
            .where(
                PurchaseOrderShipment.company_id == cid,
                PurchaseOrderShipment.purchase_order_id == PurchaseOrder.id,
            )
            .correlate(PurchaseOrder)
            .scalar_subquery()
        )
        lines = (
            select(
                func.count().label("item_count"),
                func.coalesce(func.sum(PurchaseOrderLine.quantity), 0).label("ordered"),
                func.coalesce(func.sum(PurchaseOrderLine.received_quantity), 0).label("received"),
                func.coalesce(
                    func.sum(
                        func.greatest(
                            PurchaseOrderLine.quantity - PurchaseOrderLine.received_quantity, 0
                        )
                    ),
                    0,
                ).label("open_quantity"),
            )
            .where(
                PurchaseOrderLine.company_id == cid,
                PurchaseOrderLine.purchase_order_id == PurchaseOrder.id,
            )
            .correlate(PurchaseOrder)
            .lateral("po_lines")
        )
        # The latest consignment's dispatch, or the legacy header shipment when no
        # numbered consignment exists (docs/assumptions.md §C1-9).
        dispatched_at = func.coalesce(PurchaseOrderShipment.shipped_at, PurchaseOrder.shipped_at)
        receipts = (
            select(
                func.count().label("receipt_count"),
                func.count()
                .filter(or_(dispatched_at.is_(None), GoodsReceipt.created_at >= dispatched_at))
                .label("receipts_since_dispatch"),
            )
            .where(
                GoodsReceipt.company_id == cid,
                GoodsReceipt.purchase_order_id == PurchaseOrder.id,
                GoodsReceipt.status != GoodsReceiptStatus.CANCELLED,
            )
            .correlate(PurchaseOrder, PurchaseOrderShipment)
            .lateral("po_receipts")
        )
        counter = resolve_open_inspection_counter()
        posted_receipts = (
            select(GoodsReceipt.id)
            .where(
                GoodsReceipt.company_id == cid,
                GoodsReceipt.purchase_order_id == PurchaseOrder.id,
                GoodsReceipt.status == GoodsReceiptStatus.POSTED,
            )
            .correlate(PurchaseOrder)
        )
        qc_count = (
            counter(cid, RECEIPT_REFERENCE, posted_receipts) if counter is not None else literal(0)
        )
        documents = InboundDocumentRepository(self.session, self.tenant)
        docs = (
            select(
                func.count().label("documents_count"),
                func.coalesce(func.bool_or(PurchaseOrderDocument.kind == "LR_COPY"), False).label(
                    "has_lr_copy"
                ),
            )
            .select_from(Document)
            .outerjoin(
                PurchaseOrderDocument,
                and_(
                    PurchaseOrderDocument.document_id == Document.id,
                    PurchaseOrderDocument.company_id == Document.company_id,
                ),
            )
            .where(
                Document.company_id == cid,
                Document.entity_type == ORDER_DOCUMENT_ENTITY,
                Document.entity_id == PurchaseOrder.id,
                document_is_live(utcnow()),
                documents.visibility(),
            )
            .correlate(PurchaseOrder)
            .lateral("po_documents")
        )
        fully_received = or_(
            lines.c.open_quantity <= 0, PurchaseOrder.status == PurchaseOrderStatus.CLOSED
        )
        return (
            self._scoped()
            .add_columns(
                PurchaseOrderShipment,
                Vendor.name.label("vendor_name"),
                User.full_name.label("assignee_name"),
                OrderCategory.code.label("category_code"),
                OrderCategory.name.label("category_name"),
                lines.c.item_count,
                lines.c.ordered,
                lines.c.received,
                lines.c.open_quantity,
                shipment_count.label("shipment_count"),
                receipts.c.receipt_count,
                receipts.c.receipts_since_dispatch,
                qc_count.label("qc_count"),
                docs.c.documents_count,
                docs.c.has_lr_copy,
                inbound_tracking_status(
                    fully_received=fully_received,
                    any_receipt=receipts.c.receipt_count > 0,
                    receipt_since_dispatch=receipts.c.receipts_since_dispatch > 0,
                    qc_pending=qc_count > 0,
                    shipment_exists=dispatched_at.is_not(None),
                    delivered=PurchaseOrderShipment.delivered_at.is_not(None),
                    shipped_today=func.date(func.timezone("UTC", dispatched_at)) == as_of,
                    ready=PurchaseOrder.ready_for_dispatch_at.is_not(None),
                    sql=True,
                ).label("derived_tracking_status"),
            )
            .join(Vendor, and_(Vendor.id == PurchaseOrder.vendor_id, Vendor.company_id == cid))
            .outerjoin(
                PurchaseOrderShipment,
                and_(
                    PurchaseOrderShipment.id == latest_id,
                    PurchaseOrderShipment.company_id == cid,
                ),
            )
            .outerjoin(
                User, and_(User.id == PurchaseOrder.assigned_vendor_user_id, User.company_id == cid)
            )
            .outerjoin(
                OrderCategory,
                and_(
                    OrderCategory.id == PurchaseOrder.item_category_id,
                    OrderCategory.company_id == cid,
                ),
            )
            .join(lines, true())
            .join(receipts, true())
            .join(docs, true())
        )

    def tab_clause(self, stmt: Select[Any], tab: str) -> ColumnElement[bool]:
        """The SQL of :func:`order_tab` for one tab, over :meth:`projection`."""
        tracking = stmt.selected_columns["derived_tracking_status"]
        if tab == "to_accept":
            return PurchaseOrder.status == PurchaseOrderStatus.SENT
        if tab == "accepted":
            return and_(
                PurchaseOrder.status == PurchaseOrderStatus.ACKNOWLEDGED,
                tracking == InboundTrackingStatus.NOT_DISPATCHED.value,
            )
        return tracking.in_([value.value for value in TAB_TRACKING_STATUSES[tab]])

    def filtered_projection(self, filters: InboundTrackingFilters, as_of: date) -> Select[Any]:
        stmt = self.projection(as_of)
        stmt = stmt.where(PurchaseOrder.status.not_in(UNSENT_PO_STATUSES))
        tracking = stmt.selected_columns["derived_tracking_status"]
        if self.tenant.vendor_id is None and not filters.include_completed:
            stmt = stmt.where(
                PurchaseOrder.status.in_(BOARD_OPEN_PO_STATUSES),
                tracking != InboundTrackingStatus.GRN_COMPLETED.value,
            )
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
        if filters.not_acknowledged:
            stmt = stmt.where(self.tab_clause(stmt, "to_accept"))
        if filters.in_transit:
            stmt = stmt.where(self.tab_clause(stmt, "dispatched"))
        if filters.tab != "all":
            stmt = stmt.where(self.tab_clause(stmt, filters.tab))
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

    async def order_row(self, order_id: uuid.UUID, as_of: date) -> Any:
        """One order's projection row, or ``None`` when the caller cannot see it."""
        return (
            await self.session.execute(self.projection(as_of).where(PurchaseOrder.id == order_id))
        ).first()

    def tab_counts(self, as_of: date) -> tuple[Any, list[Any]]:
        """The vendor-app tab counts as labelled aggregates over :meth:`projection`.

        Returns the subquery and the columns, so the vendor dashboard can add its
        own finance and QC figures and still answer in one statement. The tile
        names are the dashboard's: ``dispatched`` is shown as "in transit".
        """
        source = (
            self.projection(as_of).where(PurchaseOrder.status.not_in(UNSENT_PO_STATUSES)).subquery()
        )
        tracking = source.c.derived_tracking_status

        def count(where: Any) -> Any:
            return func.count().filter(where)

        columns = [
            count(source.c.status == PurchaseOrderStatus.SENT).label("to_accept"),
            count(
                and_(
                    source.c.status == PurchaseOrderStatus.ACKNOWLEDGED,
                    tracking == InboundTrackingStatus.NOT_DISPATCHED.value,
                )
            ).label("accepted"),
            *(
                count(tracking.in_([value.value for value in TAB_TRACKING_STATUSES[tab]])).label(
                    label
                )
                for tab, label in (
                    ("ready", "ready"),
                    ("dispatched", "in_transit"),
                    ("delivered", "delivered"),
                )
            ),
        ]
        return source, columns

    def visible_order_ids(self) -> Select[Any]:
        """The ids of every order the caller may see - for scoping other tables."""
        return self._scoped().with_only_columns(PurchaseOrder.id)


__all__ = [
    "BOARD_OPEN_PO_STATUSES",
    "ORDER_DOCUMENT_ENTITY",
    "RECEIPT_REFERENCE",
    "TAB_TRACKING_STATUSES",
    "UNSENT_PO_STATUSES",
    "InboundDocumentRepository",
    "InboundRepository",
    "ShipmentRepository",
    "document_is_live",
    "inbound_tracking_status",
    "order_tab",
    "po_is_late",
    "resolve_open_inspection_counter",
]
