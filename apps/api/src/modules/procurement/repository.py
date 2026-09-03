"""Queries for the procurement lifecycle.

Four shapes in this file exist for performance or correctness reasons rather
than convenience:

* **Everything the write paths need is batched.** ``PurchasableItemRepository``
  and ``VendorLookupRepository`` answer for a whole document in one statement.
  A twenty-line order validated with a lookup per line would be forty round
  trips on the write path, which is exactly the N+1 the rules forbid
  (master rules section 32).
* **Projections, not entities, wherever a wide table is read for a few
  columns.** ``items`` carries nine capability flags and a dozen planning
  parameters; order entry needs eight columns of it. Loading the row to use a
  third of it is the ``SELECT *`` the rules forbid.
* **The duplicate search narrows in SQL and decides in Python.** The statement
  restricts to open lines for one vendor, one warehouse and the requested items
  inside a coarse date envelope - a small, index-supported set. The per-item
  window and the action ladder are then applied by pure functions in the
  service, so section 16's rules can be exercised without a database and cannot
  drift between "what the query found" and "what the rule says".
* **The purchase order header is the serialisation point for receiving.**
  ``lock_for_update`` takes a row lock before any receipt reads or writes the
  running received quantities, so two deliveries arriving at once cannot each
  validate against the totals they read first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Select,
    String,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    null,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.core.types import GUID, Money, to_quantity
from src.modules.inventory.models import StockBatch
from src.modules.procurement.models import (
    OPEN_PO_LINE_STATUSES,
    OPEN_PO_STATUSES,
    OPEN_REQUISITION_STATUSES,
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    PurchaseReturn,
    PurchaseReturnLine,
    PurchaseSourceType,
    ReceiptLineQcStatus,
    RequisitionStatus,
    Rfq,
    RfqLine,
    VendorQuotation,
    VendorQuotationLine,
)
from src.modules.procurement.schemas import (
    GoodsReceiptFilters,
    PendingApproval,
    PendingApprovalFilters,
    PurchaseOrderFilters,
    PurchaseReturnFilters,
    QuotationFilters,
    RequisitionFilters,
    RfqFilters,
)
from src.modules.products.models import Item, ItemVendorLink
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse

# -----------------------------------------------------------------------------
# MRP netting: which orders count as inbound supply
# -----------------------------------------------------------------------------

#: Purchase-order statuses whose undelivered balance is *committed inbound
#: supply*, and therefore what MRP nets a shortage against.
#:
#: A strict subset of :data:`OPEN_PO_STATUSES`, which additionally carries
#: ``DRAFT`` and ``PENDING_APPROVAL`` because the duplicate matcher has to see
#: an order a buyer is still typing. Planning must not: approval is the
#: commitment, and it is the transition this module marks ``is_irreversible``
#: with the comment "past it the order can be sent to the vendor and the company
#: is on the hook for the money". A draft is an intention, and an intention that
#: is never approved would leave the plan short of a real requirement.
#:
#: Everything past ``PARTIALLY_RECEIVED`` is excluded for the opposite reason.
#: ``RECEIVED``, ``QC_PENDING``, ``QC_COMPLETED``, ``GRN_COMPLETED``,
#: ``INVOICED``, ``PAID`` and ``CLOSED`` are all reached by the goods physically
#: arriving, which posts them into ``inventory_balances`` - where the planner has
#: already counted them. Netting them here as well would count the same stock
#: twice, which is the direction that causes a stockout. ``REJECTED`` and
#: ``CANCELLED`` never arrive at all.
#:
#: Being a subset of ``OPEN_PO_STATUSES`` also keeps the partial index
#: ``ix_purchase_orders_open_vendor`` - built for exactly that predicate - usable
#: by this query.
NETTABLE_PO_STATUSES: frozenset[PurchaseOrderStatus] = frozenset(
    {
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.ACKNOWLEDGED,
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
    }
)

#: Line statuses that still expect a delivery. Derived from
#: :data:`OPEN_PO_LINE_STATUSES` rather than retyped, so the two cannot drift
#: apart and the partial index ``ix_purchase_order_lines_open_item`` - whose
#: predicate is that tuple - serves this query. ``RECEIVED`` has already landed,
#: ``CLOSED`` is a short-close whose balance will never arrive, and ``CANCELLED``
#: was withdrawn.
NETTABLE_PO_LINE_STATUSES: frozenset[PurchaseOrderLineStatus] = frozenset(OPEN_PO_LINE_STATUSES)


# -----------------------------------------------------------------------------
# Master-data projections
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PurchasableItem:
    """The item-master facts procurement needs, and nothing else."""

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    purchase_uom_id: uuid.UUID | None
    gst_rate: Decimal
    standard_cost: Decimal
    min_order_quantity: Decimal
    lead_time_days: int
    can_purchase: bool
    is_active: bool
    is_batch_tracked: bool
    is_serial_tracked: bool

    @property
    def order_uom_id(self) -> uuid.UUID:
        """The unit a purchase line defaults to: the purchase unit, else stock."""
        return self.purchase_uom_id or self.base_uom_id


@dataclass(frozen=True, slots=True)
class VendorFacts:
    """What procurement reads off a vendor: eligibility and commercial terms."""

    id: uuid.UUID
    code: str
    name: str
    rating: Decimal | None
    rating_sample_size: int
    lead_time_days: int
    payment_terms_days: int
    currency: str
    is_approved: bool
    is_active: bool
    is_archived: bool


@dataclass(frozen=True, slots=True)
class OpenOrderLine:
    """An open purchase order line, as the duplicate matcher sees it."""

    purchase_order_id: uuid.UUID
    purchase_order_code: str
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    line_id: uuid.UUID
    item_id: uuid.UUID
    quantity: Decimal
    received_quantity: Decimal
    #: The line's own date when it has one, otherwise the header's promise.
    required_date: date | None


class PurchasableItemRepository(BaseRepository[Item]):
    """Read-only access to the item master for procurement."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PurchasableItem]:
        """Batched id -> item facts. One statement for a whole document."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.purchase_uom_id,
            Item.gst_rate,
            Item.standard_cost,
            Item.min_order_quantity,
            Item.lead_time_days,
            Item.can_purchase,
            Item.is_active,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: PurchasableItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                purchase_uom_id=row.purchase_uom_id,
                gst_rate=Decimal(row.gst_rate),
                standard_cost=Decimal(row.standard_cost),
                min_order_quantity=Decimal(row.min_order_quantity),
                lead_time_days=int(row.lead_time_days),
                can_purchase=row.can_purchase,
                is_active=row.is_active,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
            )
            for row in rows
        }


class VendorLookupRepository(BaseRepository[Vendor]):
    """Read-only access to the vendor master."""

    model = Vendor

    async def load(self, vendor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, VendorFacts]:
        if not vendor_ids:
            return {}
        stmt = select(
            Vendor.id,
            Vendor.code,
            Vendor.name,
            Vendor.rating,
            Vendor.rating_sample_size,
            Vendor.lead_time_days,
            Vendor.payment_terms_days,
            Vendor.currency,
            Vendor.is_approved,
            Vendor.is_active,
            Vendor.deleted_at,
        ).where(
            Vendor.company_id == self.tenant.company_id,
            Vendor.id.in_(list(dict.fromkeys(vendor_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: VendorFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                rating=Decimal(row.rating) if row.rating is not None else None,
                rating_sample_size=int(row.rating_sample_size),
                lead_time_days=int(row.lead_time_days),
                payment_terms_days=int(row.payment_terms_days),
                currency=row.currency,
                is_approved=row.is_approved,
                is_active=row.is_active,
                is_archived=row.deleted_at is not None,
            )
            for row in rows
        }


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence probes for the destinations a procurement document names."""

    model = Warehouse

    async def exists_in_tenant(self, warehouse_id: uuid.UUID) -> bool:
        stmt = select(Warehouse.id).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.id == warehouse_id,
            Warehouse.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None


class BatchLookupRepository(BaseRepository[StockBatch]):
    """Batched resolution of vendor lot numbers to ``stock_batches`` rows.

    Read-only: batches are *created* through ``inventory.InventoryQueryService``
    so the audit entry and the duplicate check for a lot number stay in the
    module that owns the table. This exists so a receipt resolves every lot it
    already knows in one statement instead of one probe per line.
    """

    model = StockBatch

    async def by_numbers(
        self, pairs: Sequence[tuple[uuid.UUID, str]]
    ) -> dict[tuple[uuid.UUID, str], uuid.UUID]:
        if not pairs:
            return {}
        unique = list(dict.fromkeys(pairs))
        stmt = select(StockBatch.id, StockBatch.item_id, StockBatch.batch_number).where(
            StockBatch.company_id == self.tenant.company_id,
            or_(
                *[
                    and_(StockBatch.item_id == item_id, StockBatch.batch_number == number)
                    for item_id, number in unique
                ]
            ),
        )
        rows = (await self.session.execute(stmt)).all()
        return {(row.item_id, row.batch_number): row.id for row in rows}


class ItemVendorLinkRepository(BaseRepository[ItemVendorLink]):
    """Maintains the purchase-history columns ``products`` expects this module
    to write (see ``ItemVendorLink.last_purchase_price``)."""

    model = ItemVendorLink

    async def record_last_purchase(
        self,
        vendor_id: uuid.UUID,
        prices: dict[uuid.UUID, Decimal],
        purchased_on: date,
    ) -> int:
        """Stamp the last purchase price for several items in one statement.

        A CASE over the item id rather than a statement per item: a receipt can
        touch twenty items and twenty UPDATEs on the write path is the N+1 this
        codebase treats as a defect.
        """
        if not prices:
            return 0
        price_case = case(
            *[(ItemVendorLink.item_id == item_id, price) for item_id, price in prices.items()],
            else_=ItemVendorLink.last_purchase_price,
        )
        stmt = (
            update(ItemVendorLink)
            .where(
                ItemVendorLink.company_id == self.tenant.company_id,
                ItemVendorLink.vendor_id == vendor_id,
                ItemVendorLink.item_id.in_(list(prices)),
                ItemVendorLink.deleted_at.is_(None),
            )
            .values(last_purchase_price=price_case, last_purchase_date=purchased_on)
            .execution_options(synchronize_session=False)
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


# -----------------------------------------------------------------------------
# Purchase requisition
# -----------------------------------------------------------------------------


class RequisitionLineRepository(BaseRepository[PurchaseRequisitionLine]):
    model = PurchaseRequisitionLine

    async def list_for_requisition(
        self, requisition_id: uuid.UUID
    ) -> list[PurchaseRequisitionLine]:
        stmt = (
            self._scoped()
            .where(PurchaseRequisitionLine.requisition_id == requisition_id)
            .order_by(PurchaseRequisitionLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_requisitions(
        self, requisition_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[PurchaseRequisitionLine]]:
        """Batched: one statement for a page of requisitions."""
        if not requisition_ids:
            return {}
        stmt = (
            self._scoped()
            .where(PurchaseRequisitionLine.requisition_id.in_(list(requisition_ids)))
            .order_by(
                PurchaseRequisitionLine.requisition_id, PurchaseRequisitionLine.line_number
            )
        )
        grouped: dict[uuid.UUID, list[PurchaseRequisitionLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.requisition_id, []).append(line)
        return grouped

    async def load_many(self, line_ids: Sequence[uuid.UUID]) -> list[PurchaseRequisitionLine]:
        if not line_ids:
            return []
        stmt = self._scoped().where(PurchaseRequisitionLine.id.in_(list(set(line_ids))))
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_many(self, line_ids: Sequence[uuid.UUID]) -> list[PurchaseRequisitionLine]:
        """Lock requisition lines, in primary key order, before moving their
        ``ordered_quantity``.

        Two purchase orders raised against the same requisition line each read
        that running total and write it back; without the lock one of the two
        is lost and the line reads as less bought than it is. Ordered by id so
        two multi-line orders cannot deadlock.
        """
        if not line_ids:
            return []
        stmt = (
            self._scoped()
            .where(PurchaseRequisitionLine.id.in_(list(set(line_ids))))
            .order_by(PurchaseRequisitionLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_requisition(self, requisition_id: uuid.UUID) -> int:
        stmt = delete(PurchaseRequisitionLine).where(
            PurchaseRequisitionLine.company_id == self.tenant.company_id,
            PurchaseRequisitionLine.requisition_id == requisition_id,
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class RequisitionRepository(BaseRepository[PurchaseRequisition]):
    model = PurchaseRequisition
    searchable_fields = ("code", "purpose")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": PurchaseRequisition.code,
        "requisition_date": PurchaseRequisition.requisition_date,
        "required_date": PurchaseRequisition.required_date,
        "status": PurchaseRequisition.status,
        "priority": PurchaseRequisition.priority,
        "created_at": PurchaseRequisition.created_at,
    }

    async def lock_for_update(self, requisition_id: uuid.UUID) -> PurchaseRequisition | None:
        stmt = (
            self._scoped().where(PurchaseRequisition.id == requisition_id).with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: RequisitionFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(PurchaseRequisition.status == filters.status)
        if filters.open_only:
            stmt = stmt.where(PurchaseRequisition.status.in_(OPEN_REQUISITION_STATUSES))
        if filters.warehouse_id is not None:
            stmt = stmt.where(PurchaseRequisition.warehouse_id == filters.warehouse_id)
        if filters.department_id is not None:
            stmt = stmt.where(PurchaseRequisition.department_id == filters.department_id)
        if filters.required_from is not None:
            stmt = stmt.where(PurchaseRequisition.required_date >= filters.required_from)
        if filters.required_to is not None:
            stmt = stmt.where(PurchaseRequisition.required_date <= filters.required_to)
        if filters.item_id is not None:
            # EXISTS rather than a join: a join would multiply the header row by
            # its matching lines and force a DISTINCT over the whole page.
            stmt = stmt.where(
                select(PurchaseRequisitionLine.id)
                .where(
                    PurchaseRequisitionLine.company_id == self.tenant.company_id,
                    PurchaseRequisitionLine.requisition_id == PurchaseRequisition.id,
                    PurchaseRequisitionLine.item_id == filters.item_id,
                )
                .exists()
            )
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: RequisitionFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[PurchaseRequisition], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, PurchaseRequisition.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


# -----------------------------------------------------------------------------
# RFQ
# -----------------------------------------------------------------------------


class RfqLineRepository(BaseRepository[RfqLine]):
    model = RfqLine

    async def list_for_rfq(self, rfq_id: uuid.UUID) -> list[RfqLine]:
        stmt = self._scoped().where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.line_number)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_rfqs(
        self, rfq_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[RfqLine]]:
        if not rfq_ids:
            return {}
        stmt = (
            self._scoped()
            .where(RfqLine.rfq_id.in_(list(rfq_ids)))
            .order_by(RfqLine.rfq_id, RfqLine.line_number)
        )
        grouped: dict[uuid.UUID, list[RfqLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.rfq_id, []).append(line)
        return grouped

    async def delete_for_rfq(self, rfq_id: uuid.UUID) -> int:
        stmt = delete(RfqLine).where(
            RfqLine.company_id == self.tenant.company_id, RfqLine.rfq_id == rfq_id
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class RfqRepository(BaseRepository[Rfq]):
    model = Rfq
    searchable_fields = ("code",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Rfq.code,
        "rfq_date": Rfq.rfq_date,
        "response_due_date": Rfq.response_due_date,
        "status": Rfq.status,
        "created_at": Rfq.created_at,
    }

    async def lock_for_update(self, rfq_id: uuid.UUID) -> Rfq | None:
        stmt = self._scoped().where(Rfq.id == rfq_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: RfqFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(Rfq.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(Rfq.warehouse_id == filters.warehouse_id)
        if filters.due_before is not None:
            stmt = stmt.where(Rfq.response_due_date <= filters.due_before)
        if filters.vendor_id is not None:
            # Array containment, which PostgreSQL answers from a GIN index if
            # one is ever needed; the set of open RFQs is small either way.
            stmt = stmt.where(Rfq.vendor_ids.contains([filters.vendor_id]))
        if filters.item_id is not None:
            stmt = stmt.where(
                select(RfqLine.id)
                .where(
                    RfqLine.company_id == self.tenant.company_id,
                    RfqLine.rfq_id == Rfq.id,
                    RfqLine.item_id == filters.item_id,
                )
                .exists()
            )
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: RfqFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[Rfq], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, Rfq.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


# -----------------------------------------------------------------------------
# Vendor quotation
# -----------------------------------------------------------------------------


class QuotationLineRepository(BaseRepository[VendorQuotationLine]):
    model = VendorQuotationLine

    async def list_for_quotation(self, quotation_id: uuid.UUID) -> list[VendorQuotationLine]:
        stmt = (
            self._scoped()
            .where(VendorQuotationLine.quotation_id == quotation_id)
            .order_by(VendorQuotationLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_quotations(
        self, quotation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[VendorQuotationLine]]:
        """Batched: the comparison reads every line of every quotation at once."""
        if not quotation_ids:
            return {}
        stmt = (
            self._scoped()
            .where(VendorQuotationLine.quotation_id.in_(list(quotation_ids)))
            .order_by(VendorQuotationLine.quotation_id, VendorQuotationLine.line_number)
        )
        grouped: dict[uuid.UUID, list[VendorQuotationLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.quotation_id, []).append(line)
        return grouped

    async def delete_for_quotation(self, quotation_id: uuid.UUID) -> int:
        stmt = delete(VendorQuotationLine).where(
            VendorQuotationLine.company_id == self.tenant.company_id,
            VendorQuotationLine.quotation_id == quotation_id,
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class QuotationRepository(BaseRepository[VendorQuotation]):
    model = VendorQuotation
    searchable_fields = ("vendor_reference",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "quotation_date": VendorQuotation.quotation_date,
        "valid_until": VendorQuotation.valid_until,
        "total_amount": VendorQuotation.total_amount,
        "lead_time_days": VendorQuotation.lead_time_days,
        "status": VendorQuotation.status,
        "created_at": VendorQuotation.created_at,
    }

    async def for_rfq(self, rfq_id: uuid.UUID) -> list[VendorQuotation]:
        stmt = (
            self._scoped()
            .where(VendorQuotation.rfq_id == rfq_id)
            .order_by(VendorQuotation.total_amount.asc(), VendorQuotation.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def by_rfq_vendor(
        self, rfq_id: uuid.UUID, vendor_id: uuid.UUID
    ) -> VendorQuotation | None:
        stmt = self._scoped().where(
            VendorQuotation.rfq_id == rfq_id, VendorQuotation.vendor_id == vendor_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_update(self, quotation_id: uuid.UUID) -> VendorQuotation | None:
        stmt = self._scoped().where(VendorQuotation.id == quotation_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: QuotationFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.rfq_id is not None:
            stmt = stmt.where(VendorQuotation.rfq_id == filters.rfq_id)
        if filters.vendor_id is not None:
            stmt = stmt.where(VendorQuotation.vendor_id == filters.vendor_id)
        if filters.status is not None:
            stmt = stmt.where(VendorQuotation.status == filters.status)
        if filters.valid_on is not None:
            stmt = stmt.where(
                or_(
                    VendorQuotation.valid_until.is_(None),
                    VendorQuotation.valid_until >= filters.valid_on,
                )
            )
        # A vendor principal sees only its own offer. Applied here for the same
        # reason as on orders, receipts and returns - so no future caller can
        # forget - and it matters more here than anywhere else in the module:
        # the other quotations against an RFQ are competitors' prices.
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(VendorQuotation.vendor_id == self.tenant.vendor_id)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: QuotationFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[VendorQuotation], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, VendorQuotation.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


# -----------------------------------------------------------------------------
# Purchase order
# -----------------------------------------------------------------------------


class PurchaseOrderLineRepository(BaseRepository[PurchaseOrderLine]):
    model = PurchaseOrderLine

    async def list_for_order(self, order_id: uuid.UUID) -> list[PurchaseOrderLine]:
        stmt = (
            self._scoped()
            .where(PurchaseOrderLine.purchase_order_id == order_id)
            .order_by(PurchaseOrderLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_order(self, order_id: uuid.UUID) -> list[PurchaseOrderLine]:
        """Lock every line of an order, in a deterministic order.

        Ordered by primary key so two receipts against the same order acquire
        the same locks in the same sequence and cannot deadlock each other -
        the pattern database.md section 4.4 mandates for inventory, applied to
        the document whose running totals receiving updates.
        """
        stmt = (
            self._scoped()
            .where(PurchaseOrderLine.purchase_order_id == order_id)
            .order_by(PurchaseOrderLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_orders(
        self, order_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[PurchaseOrderLine]]:
        if not order_ids:
            return {}
        stmt = (
            self._scoped()
            .where(PurchaseOrderLine.purchase_order_id.in_(list(order_ids)))
            .order_by(PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.line_number)
        )
        grouped: dict[uuid.UUID, list[PurchaseOrderLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.purchase_order_id, []).append(line)
        return grouped

    async def load_many(self, line_ids: Sequence[uuid.UUID]) -> list[PurchaseOrderLine]:
        if not line_ids:
            return []
        stmt = self._scoped().where(PurchaseOrderLine.id.in_(list(set(line_ids))))
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_many(self, line_ids: Sequence[uuid.UUID]) -> list[PurchaseOrderLine]:
        """Lock specific order lines, in primary key order.

        Used by purchase-return posting, which reads a line's running returned
        quantity and writes it back. Two returns against two different receipts
        of the same order line touch different receipt lines and - when the two
        deliveries came in under different lots - different balance rows, so
        nothing else in the path serialises them: without this lock one of the
        two increments is silently lost. Ordered by id for the same reason as
        :meth:`lock_for_order`.
        """
        if not line_ids:
            return []
        stmt = (
            self._scoped()
            .where(PurchaseOrderLine.id.in_(list(set(line_ids))))
            .order_by(PurchaseOrderLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_order(self, order_id: uuid.UUID) -> int:
        stmt = delete(PurchaseOrderLine).where(
            PurchaseOrderLine.company_id == self.tenant.company_id,
            PurchaseOrderLine.purchase_order_id == order_id,
        )
        return int((await self.session.execute(stmt)).rowcount or 0)

    async def incoming_by_item(
        self,
        item_ids: Sequence[uuid.UUID],
        *,
        warehouse_ids: Sequence[uuid.UUID] | None = None,
        upto: date,
    ) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
        """Undelivered committed order balances per item, *dated*, up to ``upto``.

        One grouped statement for the whole item set, returning one entry per
        item per promised arrival date, in date order. MRP calls this once per
        round for every item it is planning; a statement per item would be the
        N+1 the rules forbid, and the planner's contract
        (:class:`~src.modules.mrp.engine.MrpDataSource`) has no per-item
        accessor for exactly that reason.

        **Why the date, and not one summed figure per item.** A summed figure
        cannot answer the only question the planner actually has: does this
        supply arrive before the demand needs it? Handed one lump for the whole
        horizon, a run netted a line needed on Thursday against an order landing
        six weeks later, reported no shortage and ordered nothing - a silent
        stockout. Grouping by the promised date lets the planner net each
        requirement against the tranches that arrive in time and report the rest
        (:func:`~src.modules.mrp.engine.split_supply`). It stays one statement:
        the grouping key gains a column, nothing gains a round trip.

        Three further decisions are embedded in the SQL:

        *Outstanding is floored inside the SUM, not after it.* An over-received
        line - the tolerance permits one - has ``received_quantity`` above
        ``quantity``, and a raw difference would be negative. Summed with a
        genuine shortfall on another line for the same item, that negative
        silently cancels part of the shortfall and the item is netted against
        supply that does not exist. ``GREATEST(..., 0)`` per line makes each
        line contribute its own balance or nothing.

        *The due date is the line's, then the header's.* A tranche-delivered
        order carries a date per line; ``required_date`` NULL means the
        header's ``expected_delivery_date`` applies, which is the same
        coalesce the duplicate matcher and the tracking view use. It is also
        the date each tranche is reported under, so an order called off in
        three deliveries nets against three different demands correctly.

        *Warehouse scope is the header's.* ``purchase_order_lines`` has no
        warehouse of its own - a purchase order is received into one store, and
        the header names it.
        """
        if not item_ids:
            return {}

        # The line's own promise, falling back to the header's. Both may be
        # NULL, and a NULL comparison is not TRUE, so such a line drops out of
        # the horizon filter below.
        #
        # ASSUMPTION: an order line with no date on either the line or the
        # header is NOT counted as supply inside any horizon. Nobody has
        # committed to when it arrives, so the honest answer to "does it land
        # before this date?" is "unknown", and the two ways of resolving an
        # unknown are not symmetric: counting it under-states the shortage and
        # the shortfall shows up as a stockout on the shop floor, while leaving
        # it out over-states the shortage and the worst case is a duplicate
        # order a buyer can see, question and cancel. Undated orders are also
        # the ones most likely to be stale. The conservative reading is the one
        # whose failure is visible, so an undated balance is left out.
        due_date = func.coalesce(
            PurchaseOrderLine.required_date, PurchaseOrder.expected_delivery_date
        )
        outstanding = func.greatest(
            PurchaseOrderLine.quantity - PurchaseOrderLine.received_quantity, 0
        )

        stmt = (
            select(
                PurchaseOrderLine.item_id.label("item_id"),
                due_date.label("due_date"),
                func.coalesce(func.sum(outstanding), 0).label("outstanding"),
            )
            .select_from(PurchaseOrderLine)
            # Joined on (id, company_id) so the tenant predicate applies to both
            # sides and PostgreSQL can use the tenant-leading indexes on each.
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrderLine.item_id.in_(list(dict.fromkeys(item_ids))),
                PurchaseOrderLine.status.in_(
                    sorted(str(status) for status in NETTABLE_PO_LINE_STATUSES)
                ),
                PurchaseOrder.status.in_(sorted(str(status) for status in NETTABLE_PO_STATUSES)),
                due_date.is_not(None),
                due_date <= upto,
            )
            .group_by(PurchaseOrderLine.item_id, due_date)
            # Ordered in SQL so the caller never has to sort the tranches, and so
            # two runs of the same plan read the same way.
            .order_by(PurchaseOrderLine.item_id, due_date)
        )
        if warehouse_ids:
            stmt = stmt.where(
                PurchaseOrder.warehouse_id.in_(list(dict.fromkeys(warehouse_ids)))
            )

        rows = (await self.session.execute(stmt)).all()
        # An item with nothing on order is simply absent: the planner reads this
        # with ``incoming.get(item_id, ())``, so absence and an empty schedule are
        # the same answer and a row of zeroes would be noise.
        schedule: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
        for row in rows:
            quantity = to_quantity(Decimal(str(row.outstanding)))
            if quantity <= 0:
                continue
            schedule.setdefault(row.item_id, []).append((row.due_date, quantity))
        return schedule


class PurchaseOrderRepository(BaseRepository[PurchaseOrder]):
    model = PurchaseOrder
    searchable_fields = ("code", "vendor_reference", "tracking_reference")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": PurchaseOrder.code,
        "po_date": PurchaseOrder.po_date,
        "expected_delivery_date": PurchaseOrder.expected_delivery_date,
        "status": PurchaseOrder.status,
        "priority": PurchaseOrder.priority,
        "total_amount": PurchaseOrder.total_amount,
        "created_at": PurchaseOrder.created_at,
    }

    async def get_by_code(self, code: str) -> PurchaseOrder | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, order_id: uuid.UUID) -> PurchaseOrder | None:
        """Take a row lock on the header before receiving against it.

        Receiving reads the running received quantities, decides whether the
        delivery fits inside the over-receipt tolerance, and writes them back.
        Two deliveries arriving at once would each validate against the totals
        they read first, and both could be accepted - so the header is the
        serialisation point for the whole document.
        """
        stmt = self._scoped().where(PurchaseOrder.id == order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: PurchaseOrderFilters, *, today: date) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(PurchaseOrder.status == filters.status)
        if filters.open_only:
            stmt = stmt.where(PurchaseOrder.status.in_(OPEN_PO_STATUSES))
        if filters.vendor_id is not None:
            stmt = stmt.where(PurchaseOrder.vendor_id == filters.vendor_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(PurchaseOrder.warehouse_id == filters.warehouse_id)
        if filters.source_type is not None:
            stmt = stmt.where(PurchaseOrder.source_type == filters.source_type)
        if filters.source_id is not None:
            stmt = stmt.where(PurchaseOrder.source_id == filters.source_id)
        if filters.expected_from is not None:
            stmt = stmt.where(PurchaseOrder.expected_delivery_date >= filters.expected_from)
        if filters.expected_to is not None:
            stmt = stmt.where(PurchaseOrder.expected_delivery_date <= filters.expected_to)
        if filters.overdue_only:
            stmt = stmt.where(
                PurchaseOrder.status.in_(OPEN_PO_STATUSES),
                PurchaseOrder.expected_delivery_date.is_not(None),
                PurchaseOrder.expected_delivery_date < today,
            )
        if filters.item_id is not None:
            stmt = stmt.where(
                select(PurchaseOrderLine.id)
                .where(
                    PurchaseOrderLine.company_id == self.tenant.company_id,
                    PurchaseOrderLine.purchase_order_id == PurchaseOrder.id,
                    PurchaseOrderLine.item_id == filters.item_id,
                )
                .exists()
            )
        # A vendor principal sees only its own orders. The repository applies
        # it rather than the route, so no future caller can forget (section 16).
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(PurchaseOrder.vendor_id == self.tenant.vendor_id)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: PurchaseOrderFilters, params: PageParams, sort: SortParams, *, today: date
    ) -> tuple[list[PurchaseOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, PurchaseOrder.created_at.desc())
        return await self.list_page(
            params, stmt=self._filtered(filters, today=today), order_by=order_by
        )

    # --- duplicate detection (master rules section 16) ----------------------

    async def open_lines_for_duplicate_check(
        self,
        *,
        vendor_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        item_ids: Sequence[uuid.UUID],
        date_from: date | None,
        date_to: date | None,
        source_type: PurchaseSourceType | None = None,
        source_id: uuid.UUID | None = None,
        exclude_order_id: uuid.UUID | None = None,
    ) -> list[OpenOrderLine]:
        """Open order lines that could be covering the same demand.

        Narrowed in SQL to one vendor, one warehouse, the requested items and a
        coarse date envelope - the predicate ``ix_purchase_orders_open_vendor``
        and ``ix_purchase_order_lines_open_item`` are built for. A line whose
        date is unknown, or whose order names the same source demand, is kept
        regardless of the envelope: those are the cases the per-item rules in
        the service still have to judge, and dropping them here would make the
        matcher silently miss the strongest signal it has.
        """
        if not item_ids:
            return []

        required_date = func.coalesce(
            PurchaseOrderLine.required_date, PurchaseOrder.expected_delivery_date
        )

        in_window = required_date.is_(None)
        if date_from is not None and date_to is not None:
            in_window = or_(in_window, required_date.between(date_from, date_to))

        same_source = literal(False)
        if source_type is not None and source_id is not None:
            same_source = and_(
                PurchaseOrder.source_type == source_type,
                PurchaseOrder.source_id == source_id,
            )

        stmt = (
            select(
                PurchaseOrder.id.label("purchase_order_id"),
                PurchaseOrder.code.label("purchase_order_code"),
                PurchaseOrder.status.label("status"),
                PurchaseOrder.vendor_id.label("vendor_id"),
                PurchaseOrder.warehouse_id.label("warehouse_id"),
                PurchaseOrder.source_type.label("source_type"),
                PurchaseOrder.source_id.label("source_id"),
                PurchaseOrderLine.id.label("line_id"),
                PurchaseOrderLine.item_id.label("item_id"),
                PurchaseOrderLine.quantity.label("quantity"),
                PurchaseOrderLine.received_quantity.label("received_quantity"),
                required_date.label("required_date"),
            )
            .select_from(PurchaseOrderLine)
            # Joined on (id, company_id) so the tenant predicate applies to both
            # sides and PostgreSQL can use the tenant-leading indexes on each.
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrder.vendor_id == vendor_id,
                PurchaseOrder.warehouse_id == warehouse_id,
                PurchaseOrder.status.in_(OPEN_PO_STATUSES),
                PurchaseOrderLine.status.in_(OPEN_PO_LINE_STATUSES),
                PurchaseOrderLine.item_id.in_(list(dict.fromkeys(item_ids))),
                or_(in_window, same_source),
            )
            .order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.id.desc())
            # An unbounded result set is not acceptable even on an internal
            # query; a hundred open lines for one vendor and one item is already
            # a data problem the buyer needs to see rather than page through.
            .limit(200)
        )
        if exclude_order_id is not None:
            stmt = stmt.where(PurchaseOrder.id != exclude_order_id)

        rows = (await self.session.execute(stmt)).all()
        return [
            OpenOrderLine(
                purchase_order_id=row.purchase_order_id,
                purchase_order_code=row.purchase_order_code,
                status=PurchaseOrderStatus(row.status),
                vendor_id=row.vendor_id,
                warehouse_id=row.warehouse_id,
                source_type=PurchaseSourceType(row.source_type),
                source_id=row.source_id,
                line_id=row.line_id,
                item_id=row.item_id,
                quantity=Decimal(row.quantity),
                received_quantity=Decimal(row.received_quantity),
                required_date=row.required_date,
            )
            for row in rows
        ]


# -----------------------------------------------------------------------------
# Goods receipt
# -----------------------------------------------------------------------------


class GoodsReceiptLineRepository(BaseRepository[GoodsReceiptLine]):
    model = GoodsReceiptLine

    async def list_for_receipt(self, receipt_id: uuid.UUID) -> list[GoodsReceiptLine]:
        stmt = (
            self._scoped()
            .where(GoodsReceiptLine.goods_receipt_id == receipt_id)
            .order_by(GoodsReceiptLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_receipt(self, receipt_id: uuid.UUID) -> list[GoodsReceiptLine]:
        stmt = (
            self._scoped()
            .where(GoodsReceiptLine.goods_receipt_id == receipt_id)
            .order_by(GoodsReceiptLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_receipts(
        self, receipt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[GoodsReceiptLine]]:
        if not receipt_ids:
            return {}
        stmt = (
            self._scoped()
            .where(GoodsReceiptLine.goods_receipt_id.in_(list(receipt_ids)))
            .order_by(GoodsReceiptLine.goods_receipt_id, GoodsReceiptLine.line_number)
        )
        grouped: dict[uuid.UUID, list[GoodsReceiptLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.goods_receipt_id, []).append(line)
        return grouped

    async def load_many(self, line_ids: Sequence[uuid.UUID]) -> list[GoodsReceiptLine]:
        if not line_ids:
            return []
        stmt = self._scoped().where(GoodsReceiptLine.id.in_(list(set(line_ids))))
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_many(self, line_ids: Sequence[uuid.UUID]) -> list[GoodsReceiptLine]:
        """Lock receipt lines by primary key order before a return writes to them."""
        if not line_ids:
            return []
        stmt = (
            self._scoped()
            .where(GoodsReceiptLine.id.in_(list(set(line_ids))))
            .order_by(GoodsReceiptLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def qc_pending_count(self, order_id: uuid.UUID) -> int:
        """How many *posted* received lines are still waiting on an inspection decision.

        Restricted to POSTED receipts on purpose. A draft has moved no stock and
        a cancelled one never will, so neither has anything to inspect - and
        counting them strands the purchase order in QC_PENDING, a state only a
        real inspection decision can leave.
        """
        stmt = (
            select(func.count())
            .select_from(GoodsReceiptLine)
            .join(
                GoodsReceipt,
                and_(
                    GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id,
                    GoodsReceipt.company_id == GoodsReceiptLine.company_id,
                ),
            )
            .where(
                GoodsReceiptLine.company_id == self.tenant.company_id,
                GoodsReceipt.purchase_order_id == order_id,
                GoodsReceipt.status == GoodsReceiptStatus.POSTED,
                GoodsReceiptLine.qc_status == ReceiptLineQcStatus.PENDING,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


class GoodsReceiptRepository(BaseRepository[GoodsReceipt]):
    model = GoodsReceipt
    searchable_fields = ("code", "vendor_delivery_note", "vendor_invoice_number")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": GoodsReceipt.code,
        "receipt_date": GoodsReceipt.receipt_date,
        "status": GoodsReceipt.status,
        "created_at": GoodsReceipt.created_at,
    }

    async def lock_for_update(self, receipt_id: uuid.UUID) -> GoodsReceipt | None:
        stmt = self._scoped().where(GoodsReceipt.id == receipt_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def for_order(self, order_id: uuid.UUID) -> list[GoodsReceipt]:
        stmt = (
            self._scoped()
            .where(GoodsReceipt.purchase_order_id == order_id)
            .order_by(GoodsReceipt.receipt_date.desc(), GoodsReceipt.id.desc())
            # The tracking view shows the delivery history of one order; an
            # order with more receipts than this is pathological and the buyer
            # needs the list endpoint, not a longer panel.
            .limit(50)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    def _filtered(self, filters: GoodsReceiptFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(GoodsReceipt.status == filters.status)
        if filters.purchase_order_id is not None:
            stmt = stmt.where(GoodsReceipt.purchase_order_id == filters.purchase_order_id)
        if filters.vendor_id is not None:
            stmt = stmt.where(GoodsReceipt.vendor_id == filters.vendor_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(GoodsReceipt.warehouse_id == filters.warehouse_id)
        if filters.received_from is not None:
            stmt = stmt.where(GoodsReceipt.receipt_date >= filters.received_from)
        if filters.received_to is not None:
            stmt = stmt.where(GoodsReceipt.receipt_date <= filters.received_to)
        if filters.item_id is not None or filters.qc_pending_only:
            exists_stmt = select(GoodsReceiptLine.id).where(
                GoodsReceiptLine.company_id == self.tenant.company_id,
                GoodsReceiptLine.goods_receipt_id == GoodsReceipt.id,
            )
            if filters.item_id is not None:
                exists_stmt = exists_stmt.where(GoodsReceiptLine.item_id == filters.item_id)
            if filters.qc_pending_only:
                exists_stmt = exists_stmt.where(
                    GoodsReceiptLine.qc_status == ReceiptLineQcStatus.PENDING
                )
            stmt = stmt.where(exists_stmt.exists())
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(GoodsReceipt.vendor_id == self.tenant.vendor_id)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: GoodsReceiptFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[GoodsReceipt], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, GoodsReceipt.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


# -----------------------------------------------------------------------------
# Purchase return
# -----------------------------------------------------------------------------


class PurchaseReturnLineRepository(BaseRepository[PurchaseReturnLine]):
    model = PurchaseReturnLine

    async def list_for_return(self, return_id: uuid.UUID) -> list[PurchaseReturnLine]:
        stmt = (
            self._scoped()
            .where(PurchaseReturnLine.purchase_return_id == return_id)
            .order_by(PurchaseReturnLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_returns(
        self, return_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[PurchaseReturnLine]]:
        if not return_ids:
            return {}
        stmt = (
            self._scoped()
            .where(PurchaseReturnLine.purchase_return_id.in_(list(return_ids)))
            .order_by(PurchaseReturnLine.purchase_return_id, PurchaseReturnLine.line_number)
        )
        grouped: dict[uuid.UUID, list[PurchaseReturnLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.purchase_return_id, []).append(line)
        return grouped


class PurchaseReturnRepository(BaseRepository[PurchaseReturn]):
    model = PurchaseReturn
    searchable_fields = ("code", "debit_note_number", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": PurchaseReturn.code,
        "return_date": PurchaseReturn.return_date,
        "status": PurchaseReturn.status,
        "total_amount": PurchaseReturn.total_amount,
        "created_at": PurchaseReturn.created_at,
    }

    async def lock_for_update(self, return_id: uuid.UUID) -> PurchaseReturn | None:
        stmt = self._scoped().where(PurchaseReturn.id == return_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: PurchaseReturnFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(PurchaseReturn.status == filters.status)
        if filters.vendor_id is not None:
            stmt = stmt.where(PurchaseReturn.vendor_id == filters.vendor_id)
        if filters.goods_receipt_id is not None:
            stmt = stmt.where(PurchaseReturn.goods_receipt_id == filters.goods_receipt_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(PurchaseReturn.warehouse_id == filters.warehouse_id)
        if filters.return_from is not None:
            stmt = stmt.where(PurchaseReturn.return_date >= filters.return_from)
        if filters.return_to is not None:
            stmt = stmt.where(PurchaseReturn.return_date <= filters.return_to)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(PurchaseReturn.vendor_id == self.tenant.vendor_id)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: PurchaseReturnFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[PurchaseReturn], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, PurchaseReturn.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


# -----------------------------------------------------------------------------
# Approvals queue
# -----------------------------------------------------------------------------


class PendingApprovalRepository(BaseRepository[PurchaseOrder]):
    """The one query behind ``GET /procurement/pending-approvals``.

    A ``UNION ALL`` of the two documents that wait on a procurement approval,
    paginated as one list. Two separate paginated endpoints would push the
    merge into the client, which then cannot page or sort the combined queue at
    all.
    """

    model = PurchaseOrder

    def _stmt(
        self,
        filters: PendingApprovalFilters,
        *,
        include_requisitions: bool,
        include_orders: bool,
    ) -> Select[Any] | None:
        branches = []

        if include_requisitions and filters.document_type in (None, "purchase_requisition"):
            requisitions = select(
                literal("purchase_requisition").label("document_type"),
                PurchaseRequisition.id.label("document_id"),
                PurchaseRequisition.code.label("code"),
                PurchaseRequisition.status.label("status"),
                cast(null(), Money).label("total_amount"),
                cast(null(), String(3)).label("currency"),
                cast(null(), GUID).label("counterparty_id"),
                PurchaseRequisition.warehouse_id.label("warehouse_id"),
                PurchaseRequisition.required_date.label("required_date"),
                PurchaseRequisition.priority.label("priority"),
                PurchaseRequisition.requested_by_id.label("requested_by_id"),
                PurchaseRequisition.created_at.label("created_at"),
            ).where(
                PurchaseRequisition.company_id == self.tenant.company_id,
                PurchaseRequisition.status == RequisitionStatus.PENDING_APPROVAL,
            )
            if filters.warehouse_id is not None:
                requisitions = requisitions.where(
                    PurchaseRequisition.warehouse_id == filters.warehouse_id
                )
            # A vendor filter cannot match a requisition: no vendor is chosen
            # yet. Asking for one means the caller wants purchase orders only.
            if filters.vendor_id is None:
                branches.append(requisitions)

        if include_orders and filters.document_type in (None, "purchase_order"):
            orders = select(
                literal("purchase_order").label("document_type"),
                PurchaseOrder.id.label("document_id"),
                PurchaseOrder.code.label("code"),
                PurchaseOrder.status.label("status"),
                PurchaseOrder.total_amount.label("total_amount"),
                PurchaseOrder.currency.label("currency"),
                PurchaseOrder.vendor_id.label("counterparty_id"),
                PurchaseOrder.warehouse_id.label("warehouse_id"),
                PurchaseOrder.expected_delivery_date.label("required_date"),
                PurchaseOrder.priority.label("priority"),
                PurchaseOrder.created_by_id.label("requested_by_id"),
                PurchaseOrder.created_at.label("created_at"),
            ).where(
                PurchaseOrder.company_id == self.tenant.company_id,
                PurchaseOrder.status == PurchaseOrderStatus.PENDING_APPROVAL,
            )
            if filters.warehouse_id is not None:
                orders = orders.where(PurchaseOrder.warehouse_id == filters.warehouse_id)
            if filters.vendor_id is not None:
                orders = orders.where(PurchaseOrder.vendor_id == filters.vendor_id)
            branches.append(orders)

        if not branches:
            return None

        merged = union_all(*branches).subquery("pending_approvals")
        return select(merged).order_by(
            # Most urgent first, then oldest: the queue a buyer should work
            # top-down. The id breaks ties so a page boundary cannot repeat or
            # drop a row.
            merged.c.priority.asc(),
            merged.c.created_at.asc(),
            merged.c.document_id.asc(),
        )

    async def list_pending(
        self,
        params: PageParams,
        *,
        filters: PendingApprovalFilters,
        include_requisitions: bool,
        include_orders: bool,
    ) -> tuple[list[PendingApproval], OffsetPagination]:
        """Paginate the union.

        ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``,
        which would keep only the first column of each row. The semantics are
        reproduced - one extra row to decide ``has_next``, and a bounded count
        only when the caller asked for a total.
        """
        stmt = self._stmt(
            filters, include_requisitions=include_requisitions, include_orders=include_orders
        )
        if stmt is None:
            # The caller can approve neither document type: an empty queue, not
            # an error, so the UI can still render the (empty) page.
            return [], OffsetPagination(
                page=params.page, limit=params.limit, total=0, total_pages=1
            )

        rows = (
            await self.session.execute(stmt.limit(params.limit + 1).offset(params.offset))
        ).all()
        has_next = len(rows) > params.limit
        rows = rows[: params.limit]

        total: int | None = None
        total_pages: int | None = None
        if params.with_total:
            count_stmt = select(func.count()).select_from(
                stmt.order_by(None).limit(COUNT_CEILING).subquery()
            )
            total = int((await self.session.execute(count_stmt)).scalar_one())
            total_pages = max(1, -(-total // params.limit))

        items = [
            PendingApproval(
                document_type=row.document_type,
                document_id=row.document_id,
                code=row.code,
                status=row.status,
                total_amount=Decimal(row.total_amount) if row.total_amount is not None else None,
                currency=row.currency,
                counterparty_id=row.counterparty_id,
                required_date=row.required_date,
                priority=int(row.priority),
                requested_by_id=row.requested_by_id,
                created_at=row.created_at,
            )
            for row in rows
        ]

        return items, OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )


__all__ = [
    "NETTABLE_PO_LINE_STATUSES",
    "NETTABLE_PO_STATUSES",
    "BatchLookupRepository",
    "GoodsReceiptLineRepository",
    "GoodsReceiptRepository",
    "ItemVendorLinkRepository",
    "OpenOrderLine",
    "PendingApprovalRepository",
    "PurchasableItem",
    "PurchasableItemRepository",
    "PurchaseOrderLineRepository",
    "PurchaseOrderRepository",
    "PurchaseReturnLineRepository",
    "PurchaseReturnRepository",
    "QuotationLineRepository",
    "QuotationRepository",
    "RequisitionLineRepository",
    "RequisitionRepository",
    "RfqLineRepository",
    "RfqRepository",
    "VendorFacts",
    "VendorLookupRepository",
    "WarehouseLookupRepository",
]
