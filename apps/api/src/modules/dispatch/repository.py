"""Queries for dispatches, their lines and their tracking events.

Five shapes here exist for performance or correctness reasons rather than
convenience:

* **The tracking board is one statement.** Master rules section 23 wants the
  counterparty, the order it fulfils, the carrier, the reference, the status, the
  promised date and three derived flags on every row. Fetching the consignments
  and then resolving each party would be the N+1 section 32 forbids, wearing a
  service call's clothes - so ``tracking_board_stmt`` joins ``customers``,
  ``vendors``, ``transporters`` and ``sales_orders`` and projects the columns the
  board prints, nothing wider.
* **Every lookup a write path needs is batched.** Items, cartons, serials and
  batches are validated a whole line-set at a time. A twenty-line consignment
  checked with a lookup per line would be eighty round trips on the write path.
* **Narrow projections.** ``items``, ``customers`` and ``vendors`` are wide
  tables and the validation needs four columns of each. Pulling whole rows to
  read four fields is the ``SELECT *`` the rules forbid.
* **The header is the serialisation point.** ``lock_for_update`` is what makes
  the dispatch action safe: it reads the lines, decides the balance arithmetic on
  every sales-order line and writes back, and two dispatchers on one consignment
  would otherwise each act on what they read first.
* **The delay sweep is deliberately not tenant-scoped.** It runs from Celery
  beat, where there is no principal and no company. It is the one query in this
  module that does not start from ``BaseRepository._scoped``, and it is bounded,
  ordered and grouped by company so a caller cannot use it as a cross-tenant read
  path - see :func:`scan_delayed_across_tenants`.

Reads that cross into ``items``, ``customers``, ``vendors``, ``warehouses``,
``sales_orders``, ``packing_orders``, ``cartons``, ``stock_batches`` and
``stock_serials`` go through ``BaseRepository`` so the tenant filter - and the
soft-delete filter where the table has one - stays automatic rather than
something each statement has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.clock import utcnow
from src.core.pagination import COUNT_CEILING, CursorParams, PageParams, SortParams
from src.core.responses import CursorPagination, OffsetPagination
from src.modules.customers.models import Customer
from src.modules.dispatch.models import (
    DELIVERED_STATUSES,
    IN_FLIGHT_STATUSES,
    Dispatch,
    DispatchLine,
    DispatchStatus,
    DispatchTrackingEvent,
)
from src.modules.dispatch.schemas import (
    DelayedDispatchRow,
    DispatchFilters,
    TrackingBoardRow,
)
from src.modules.documents.models import Document
from src.modules.inventory.models import (
    InventoryTransaction,
    SerialStatus,
    StockBatch,
    StockSerial,
)
from src.modules.packing.models import Carton, PackingOrder, PackingOrderStatus
from src.modules.products.models import Item
from src.modules.sales.models import SalesOrder, SalesOrderLine
from src.modules.vendors.models import Transporter, Vendor
from src.modules.warehouses.models import Warehouse

#: ASSUMPTION: the board's "QC pending" and "GRN pending" columns are read off
#: the consignment's own status, which is what the section 7 dispatch flow
#: models: DELIVERED -> QC_PENDING -> RECEIVED -> QC_COMPLETED -> GRN_COMPLETED
#: is the receiver's booking-in sequence. A consignment that has arrived and has
#: not reached GRN_COMPLETED still owes a goods receipt at the far end; one
#: sitting in QC_PENDING still owes an inspection. Deriving these from the
#: quality and procurement tables instead would need a link from a dispatch to
#: an inspection that the contract does not define.
GRN_OUTSTANDING_STATUSES: tuple[DispatchStatus, ...] = tuple(
    status for status in DELIVERED_STATUSES if status is not DispatchStatus.GRN_COMPLETED
)


# -----------------------------------------------------------------------------
# Projections - what the write paths validate against
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchItem:
    """The item-master facts a dispatch line is validated against.

    A projection, not the ``Item`` entity - see the module docstring. The two
    tracking flags decide what a line must carry; ``code`` and ``name`` are
    printed on the challan and quoted in every error message about the line.
    """

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    is_batch_tracked: bool
    is_serial_tracked: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class PartyFacts:
    """A customer or vendor, and whether we may still ship to them."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    is_blocked: bool


@dataclass(frozen=True, slots=True)
class SalesLineFacts:
    """What the partial-dispatch arithmetic needs from one sales-order line.

    Read under a row lock by the dispatch path: ``dispatched_quantity`` is a
    read-modify-write, and two consignments drawing on the same line without the
    lock would each add their quantity to the value they read first.
    """

    id: uuid.UUID
    sales_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity: Any
    allocated_quantity: Any
    dispatched_quantity: Any
    reservation_id: uuid.UUID | None
    status: str


@dataclass(frozen=True, slots=True)
class SerialFacts:
    """Where one serialised unit is, and whether it is still in stock."""

    id: uuid.UUID
    item_id: uuid.UUID
    serial_number: str
    warehouse_id: uuid.UUID | None
    status: SerialStatus


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """A batch and the item it belongs to, for validating a dispatch line."""

    id: uuid.UUID
    item_id: uuid.UUID
    batch_number: str


# -----------------------------------------------------------------------------
# Batched lookups into other modules' tables
# -----------------------------------------------------------------------------


class ItemLookupRepository(BaseRepository[Item]):
    """Item-master facts for a whole line set in one statement."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, DispatchItem]:
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.is_active,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(set(item_ids)),
        )
        return {
            row.id: DispatchItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt)).all()
        }


class CustomerLookupRepository(BaseRepository[Customer]):
    model = Customer

    async def get_facts(self, customer_id: uuid.UUID) -> PartyFacts | None:
        stmt = select(
            Customer.id, Customer.code, Customer.name, Customer.is_active, Customer.is_blocked
        ).where(
            Customer.company_id == self.tenant.company_id,
            Customer.deleted_at.is_(None),
            Customer.id == customer_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return PartyFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            is_blocked=row.is_blocked,
        )


class VendorLookupRepository(BaseRepository[Vendor]):
    model = Vendor

    async def get_facts(self, vendor_id: uuid.UUID) -> PartyFacts | None:
        stmt = select(
            Vendor.id, Vendor.code, Vendor.name, Vendor.is_active, Vendor.is_approved
        ).where(
            Vendor.company_id == self.tenant.company_id,
            Vendor.deleted_at.is_(None),
            Vendor.id == vendor_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        # ASSUMPTION: ``vendors`` has no block flag of its own, so an unapproved
        # vendor is treated as blocked for the purpose of sending them goods.
        # Approval is precisely the control that says "we do business with these
        # people"; shipping a purchase return or job-work material to a vendor
        # nobody has vetted is the conservative thing to refuse.
        return PartyFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            is_blocked=not row.is_approved,
        )


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    model = Warehouse

    async def get_facts(self, warehouse_id: uuid.UUID) -> tuple[str, bool] | None:
        stmt = select(Warehouse.code, Warehouse.is_active).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.deleted_at.is_(None),
            Warehouse.id == warehouse_id,
        )
        row = (await self.session.execute(stmt)).first()
        return (row.code, row.is_active) if row is not None else None


class TransporterLookupRepository(BaseRepository[Transporter]):
    model = Transporter

    async def get_facts(self, transporter_id: uuid.UUID) -> tuple[str, str, bool] | None:
        stmt = select(Transporter.code, Transporter.name, Transporter.is_active).where(
            Transporter.company_id == self.tenant.company_id,
            Transporter.deleted_at.is_(None),
            Transporter.id == transporter_id,
        )
        row = (await self.session.execute(stmt)).first()
        return (row.code, row.name, row.is_active) if row is not None else None


class DocumentLookupRepository(BaseRepository[Document]):
    model = Document

    async def exists_in_tenant(self, document_id: uuid.UUID) -> bool:
        stmt = select(Document.id).where(
            Document.company_id == self.tenant.company_id, Document.id == document_id
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None


class PackingOrderLookupRepository(BaseRepository[PackingOrder]):
    model = PackingOrder

    async def get_facts(
        self, packing_order_id: uuid.UUID
    ) -> tuple[str, PackingOrderStatus, uuid.UUID, uuid.UUID | None, uuid.UUID | None] | None:
        """Code, status, customer, sales order and the dispatch that claimed it."""
        stmt = select(
            PackingOrder.code,
            PackingOrder.status,
            PackingOrder.customer_id,
            PackingOrder.sales_order_id,
            PackingOrder.dispatch_id,
        ).where(
            PackingOrder.company_id == self.tenant.company_id,
            PackingOrder.id == packing_order_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return (
            row.code,
            PackingOrderStatus(row.status),
            row.customer_id,
            row.sales_order_id,
            row.dispatch_id,
        )


class CartonLookupRepository(BaseRepository[Carton]):
    model = Carton

    async def sealed_cartons_of(
        self, packing_order_id: uuid.UUID, carton_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of ``carton_ids`` are sealed cartons of that packing order.

        One statement for the whole line set: the caller compares the returned
        set against what it asked for and names the difference, rather than
        probing each carton.
        """
        if not carton_ids:
            return set()
        stmt = select(Carton.id).where(
            Carton.company_id == self.tenant.company_id,
            Carton.packing_order_id == packing_order_id,
            Carton.id.in_(set(carton_ids)),
            Carton.is_sealed.is_(True),
        )
        return {row.id for row in (await self.session.execute(stmt)).all()}


class SerialLookupRepository(BaseRepository[StockSerial]):
    model = StockSerial

    async def load(self, serial_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SerialFacts]:
        if not serial_ids:
            return {}
        stmt = select(
            StockSerial.id,
            StockSerial.item_id,
            StockSerial.serial_number,
            StockSerial.warehouse_id,
            StockSerial.status,
        ).where(
            StockSerial.company_id == self.tenant.company_id,
            StockSerial.id.in_(set(serial_ids)),
        )
        return {
            row.id: SerialFacts(
                id=row.id,
                item_id=row.item_id,
                serial_number=row.serial_number,
                warehouse_id=row.warehouse_id,
                status=SerialStatus(row.status),
            )
            for row in (await self.session.execute(stmt)).all()
        }


class BatchLookupRepository(BaseRepository[StockBatch]):
    model = StockBatch

    async def load(self, batch_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, BatchFacts]:
        if not batch_ids:
            return {}
        stmt = select(StockBatch.id, StockBatch.item_id, StockBatch.batch_number).where(
            StockBatch.company_id == self.tenant.company_id,
            StockBatch.id.in_(set(batch_ids)),
        )
        return {
            row.id: BatchFacts(id=row.id, item_id=row.item_id, batch_number=row.batch_number)
            for row in (await self.session.execute(stmt)).all()
        }


class LedgerLookupRepository(BaseRepository[InventoryTransaction]):
    """Which ledger rows one document wrote.

    Reads through the ``(company_id, reference_type, reference_id)`` index the
    ledger carries for exactly this question (database.md section 4.2). It exists
    because a consignment's movements are written by two different mechanisms -
    ``allocation.consume`` for reserved lines and ``inventory.post_movements``
    for the rest - and neither returns a set the other knows about. The reference
    is what makes them one answer.
    """

    model = InventoryTransaction

    async def posted_for(
        self, reference_type: str, reference_id: uuid.UUID
    ) -> list[uuid.UUID]:
        stmt = (
            select(InventoryTransaction.id)
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.reference_type == reference_type,
                InventoryTransaction.reference_id == reference_id,
            )
            .order_by(InventoryTransaction.id)
        )
        return [row.id for row in (await self.session.execute(stmt)).all()]


class SalesOrderRepository(BaseRepository[SalesOrder]):
    """The sales order this consignment fulfils.

    The dispatch module writes ``sales_order_lines.dispatched_quantity`` and
    advances the order's status through the sales module's own state machine, so
    it needs both the header under a lock and its lines under one.
    """

    model = SalesOrder

    async def lock_header(self, sales_order_id: uuid.UUID) -> SalesOrder | None:
        stmt = self._scoped().where(SalesOrder.id == sales_order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_code(self, sales_order_id: uuid.UUID) -> tuple[str, uuid.UUID, str] | None:
        """Code, customer and status - enough to validate an attachment cheaply."""
        stmt = select(SalesOrder.code, SalesOrder.customer_id, SalesOrder.status).where(
            SalesOrder.company_id == self.tenant.company_id, SalesOrder.id == sales_order_id
        )
        row = (await self.session.execute(stmt)).first()
        return (row.code, row.customer_id, row.status) if row is not None else None


class SalesOrderLineRepository(BaseRepository[SalesOrderLine]):
    model = SalesOrderLine

    async def lock_lines(self, line_ids: Sequence[uuid.UUID]) -> list[SalesOrderLine]:
        """Lock a whole line set, ordered by primary key.

        The ordering is the deadlock argument, not a style choice: two
        consignments touching an overlapping set of order lines take the locks in
        the same sequence and therefore queue instead of deadlocking
        (database.md section 4.4).
        """
        if not line_ids:
            return []
        stmt = (
            self._scoped()
            .where(SalesOrderLine.id.in_(set(line_ids)))
            .order_by(SalesOrderLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def open_line_count(self, sales_order_id: uuid.UUID) -> int:
        """How many lines of the order still owe goods.

        The predicate matches ``ix_sales_order_lines_open_item`` so the count is
        an index scan rather than a filter over the order's whole history.
        """
        stmt = (
            select(func.count())
            .select_from(SalesOrderLine)
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrderLine.sales_order_id == sales_order_id,
                SalesOrderLine.status != "CANCELLED",
                SalesOrderLine.dispatched_quantity < SalesOrderLine.quantity,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


# -----------------------------------------------------------------------------
# The module's own tables
# -----------------------------------------------------------------------------


class DispatchLineRepository(BaseRepository[DispatchLine]):
    model = DispatchLine

    async def for_dispatch(self, dispatch_id: uuid.UUID) -> list[DispatchLine]:
        stmt = (
            self._scoped()
            .where(DispatchLine.dispatch_id == dispatch_id)
            .order_by(DispatchLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def for_dispatches(
        self, dispatch_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[DispatchLine]]:
        """Lines for many consignments in one statement.

        The answer to a loop of :meth:`for_dispatch` - a detail list that fetched
        its lines per row would be the N+1 the rules forbid (section 32).
        """
        if not dispatch_ids:
            return {}
        stmt = (
            self._scoped()
            .where(DispatchLine.dispatch_id.in_(set(dispatch_ids)))
            .order_by(DispatchLine.dispatch_id, DispatchLine.line_number)
        )
        grouped: dict[uuid.UUID, list[DispatchLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().unique().all():
            grouped.setdefault(line.dispatch_id, []).append(line)
        return grouped

    async def delete_for_dispatch(self, dispatch_id: uuid.UUID) -> int:
        """Clear a consignment's lines before rewriting them.

        Only ever called for a consignment that has not left: past that the lines
        carry the ledger rows they posted and deleting one would orphan a
        movement (see ``service.update_dispatch``).
        """
        stmt = delete(DispatchLine).where(
            DispatchLine.company_id == self.tenant.company_id,
            DispatchLine.dispatch_id == dispatch_id,
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class DispatchTrackingEventRepository(BaseRepository[DispatchTrackingEvent]):
    """Append-only. There is deliberately no update or delete method here."""

    model = DispatchTrackingEvent

    async def for_dispatch(
        self, dispatch_id: uuid.UUID, *, params: CursorParams
    ) -> tuple[list[DispatchTrackingEvent], CursorPagination]:
        """One consignment's history, newest first, keyset-paged.

        Keyset rather than offset because a tracking feed is append-heavy and a
        chatty courier integration makes it long (section 31).
        """
        stmt = self._scoped().where(DispatchTrackingEvent.dispatch_id == dispatch_id)
        return await self.list_keyset(params, stmt=stmt)

    async def latest_for_dispatch(
        self, dispatch_id: uuid.UUID, *, limit: int
    ) -> list[DispatchTrackingEvent]:
        stmt = (
            self._scoped()
            .where(DispatchTrackingEvent.dispatch_id == dispatch_id)
            .order_by(DispatchTrackingEvent.event_time.desc(), DispatchTrackingEvent.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())


class DispatchRepository(BaseRepository[Dispatch]):
    model = Dispatch
    searchable_fields = ("code", "tracking_id", "lr_number", "vehicle_number", "courier_name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Dispatch.code,
        "status": Dispatch.status,
        "dispatch_date": Dispatch.dispatch_date,
        "expected_delivery_date": Dispatch.expected_delivery_date,
        "actual_delivery_date": Dispatch.actual_delivery_date,
        "created_at": Dispatch.created_at,
    }

    async def get_by_code(self, code: str) -> Dispatch | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, dispatch_id: uuid.UUID) -> Dispatch | None:
        """Take the header's row lock before changing its state or its lines.

        Dispatching, confirming delivery and recording a tracking event are all
        read-modify-write sequences over the consignment and its children. The
        header is the one row all three pass through, so it is the serialisation
        point for the whole document.
        """
        stmt = self._scoped().where(Dispatch.id == dispatch_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def claimed_by_packing_order(self, packing_order_id: uuid.UUID) -> uuid.UUID | None:
        """Which consignment already carries these boxes, if any."""
        stmt = select(Dispatch.id).where(
            Dispatch.company_id == self.tenant.company_id,
            Dispatch.packing_order_id == packing_order_id,
            Dispatch.status != DispatchStatus.CANCELLED,
        )
        row = (await self.session.execute(stmt.limit(1))).first()
        return row.id if row is not None else None

    # --- filtered list ------------------------------------------------------

    def _filtered(self, filters: DispatchFilters, *, as_of: date) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(Dispatch.status == filters.status)
        if filters.dispatch_type is not None:
            stmt = stmt.where(Dispatch.dispatch_type == filters.dispatch_type)
        if filters.customer_id is not None:
            stmt = stmt.where(Dispatch.customer_id == filters.customer_id)
        if filters.vendor_id is not None:
            stmt = stmt.where(Dispatch.vendor_id == filters.vendor_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(Dispatch.warehouse_id == filters.warehouse_id)
        if filters.sales_order_id is not None:
            stmt = stmt.where(Dispatch.sales_order_id == filters.sales_order_id)
        if filters.transporter_id is not None:
            stmt = stmt.where(Dispatch.transporter_id == filters.transporter_id)
        if filters.tracking_id:
            stmt = stmt.where(Dispatch.tracking_id == filters.tracking_id.strip())
        if filters.in_flight_only:
            stmt = stmt.where(Dispatch.status.in_(IN_FLIGHT_STATUSES))
        if filters.delayed_only:
            stmt = stmt.where(_delayed_predicate(as_of))
        if filters.dispatch_from is not None:
            stmt = stmt.where(Dispatch.dispatch_date >= filters.dispatch_from)
        if filters.dispatch_to is not None:
            stmt = stmt.where(Dispatch.dispatch_date <= filters.dispatch_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: DispatchFilters, params: PageParams, sort: SortParams, *, as_of: date
    ) -> tuple[list[Dispatch], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, Dispatch.created_at.desc())
        return await self.list_page(
            params, stmt=self._filtered(filters, as_of=as_of), order_by=order_by
        )

    # --- the tracking board (master rules section 23) -----------------------

    def tracking_board_stmt(self, filters: DispatchFilters, *, as_of: date) -> Select[Any]:
        """Every column the board prints, in one statement.

        The joins are on ``(id, company_id)`` so the tenant predicate applies to
        both sides and PostgreSQL can use the tenant-leading index on each table.
        They are outer joins and they deliberately do **not** filter
        ``deleted_at``: a customer retired last year must still be named on the
        consignment we sent them, and dropping the row instead would make a
        historic dispatch vanish from the board entirely.
        """
        delayed = _delayed_predicate(as_of)
        stmt = (
            select(
                Dispatch.id.label("dispatch_id"),
                Dispatch.code.label("code"),
                Dispatch.dispatch_type.label("dispatch_type"),
                Dispatch.status.label("status"),
                Dispatch.customer_id.label("customer_id"),
                Dispatch.vendor_id.label("vendor_id"),
                Customer.name.label("customer_name"),
                Vendor.name.label("vendor_name"),
                SalesOrder.code.label("sales_order_code"),
                Dispatch.courier_name.label("courier_name"),
                Dispatch.transport_name.label("transport_name"),
                Transporter.name.label("transporter_name"),
                Dispatch.tracking_id.label("tracking_id"),
                Dispatch.dispatch_date.label("dispatch_date"),
                Dispatch.expected_delivery_date.label("expected_delivery_date"),
                Dispatch.actual_delivery_date.label("actual_delivery_date"),
                Dispatch.delay_reason.label("delay_reason"),
                delayed.label("is_delayed"),
                (Dispatch.status == DispatchStatus.QC_PENDING).label("qc_pending"),
                Dispatch.status.in_(GRN_OUTSTANDING_STATUSES).label("grn_pending"),
            )
            .select_from(Dispatch)
            .outerjoin(
                Customer,
                and_(
                    Customer.id == Dispatch.customer_id,
                    Customer.company_id == Dispatch.company_id,
                ),
            )
            .outerjoin(
                Vendor,
                and_(Vendor.id == Dispatch.vendor_id, Vendor.company_id == Dispatch.company_id),
            )
            .outerjoin(
                Transporter,
                and_(
                    Transporter.id == Dispatch.transporter_id,
                    Transporter.company_id == Dispatch.company_id,
                ),
            )
            .outerjoin(
                SalesOrder,
                and_(
                    SalesOrder.id == Dispatch.sales_order_id,
                    SalesOrder.company_id == Dispatch.company_id,
                ),
            )
            .where(Dispatch.company_id == self.tenant.company_id)
        )

        if filters.status is not None:
            stmt = stmt.where(Dispatch.status == filters.status)
        if filters.dispatch_type is not None:
            stmt = stmt.where(Dispatch.dispatch_type == filters.dispatch_type)
        if filters.customer_id is not None:
            stmt = stmt.where(Dispatch.customer_id == filters.customer_id)
        if filters.vendor_id is not None:
            stmt = stmt.where(Dispatch.vendor_id == filters.vendor_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(Dispatch.warehouse_id == filters.warehouse_id)
        if filters.transporter_id is not None:
            stmt = stmt.where(Dispatch.transporter_id == filters.transporter_id)
        if filters.tracking_id:
            stmt = stmt.where(Dispatch.tracking_id == filters.tracking_id.strip())
        if filters.in_flight_only:
            stmt = stmt.where(Dispatch.status.in_(IN_FLIGHT_STATUSES))
        if filters.delayed_only:
            stmt = stmt.where(delayed)
        if filters.dispatch_from is not None:
            stmt = stmt.where(Dispatch.dispatch_date >= filters.dispatch_from)
        if filters.dispatch_to is not None:
            stmt = stmt.where(Dispatch.dispatch_date <= filters.dispatch_to)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(
                or_(
                    Dispatch.code.ilike(pattern),
                    Dispatch.tracking_id.ilike(pattern),
                    Dispatch.lr_number.ilike(pattern),
                    Customer.name.ilike(pattern),
                    Vendor.name.ilike(pattern),
                )
            )

        # Late consignments first, then the ones due soonest: the board exists to
        # be worked top-down. ``id`` breaks ties so a page boundary cannot repeat
        # or drop a row when two consignments share a promised date.
        return stmt.order_by(
            delayed.desc(),
            Dispatch.expected_delivery_date.asc().nullslast(),
            Dispatch.id.asc(),
        )

    async def tracking_board_page(
        self, filters: DispatchFilters, params: PageParams, *, as_of: date
    ) -> tuple[list[TrackingBoardRow], OffsetPagination]:
        rows, pagination = await paginate_projection(
            self.session, self.tracking_board_stmt(filters, as_of=as_of), params
        )
        return [_board_row(row, as_of=as_of) for row in rows], pagination

    # --- delayed consignments ------------------------------------------------

    def delayed_stmt(self, *, as_of: date, warehouse_id: uuid.UUID | None = None) -> Select[Any]:
        """Consignments past their promised date with no delivery recorded.

        The predicate is exactly the one ``ix_dispatches_in_flight_expected`` is
        built for, plus the date comparison the index's leading column serves.
        """
        stmt = (
            select(
                Dispatch.id.label("dispatch_id"),
                Dispatch.company_id.label("company_id"),
                Dispatch.code.label("code"),
                Dispatch.status.label("status"),
                Dispatch.customer_id.label("customer_id"),
                Dispatch.vendor_id.label("vendor_id"),
                Customer.name.label("customer_name"),
                Vendor.name.label("vendor_name"),
                Dispatch.tracking_id.label("tracking_id"),
                Dispatch.dispatch_date.label("dispatch_date"),
                Dispatch.expected_delivery_date.label("expected_delivery_date"),
                Dispatch.delay_reason.label("delay_reason"),
                Dispatch.delay_notified_at.label("delay_notified_at"),
            )
            .select_from(Dispatch)
            .outerjoin(
                Customer,
                and_(
                    Customer.id == Dispatch.customer_id,
                    Customer.company_id == Dispatch.company_id,
                ),
            )
            .outerjoin(
                Vendor,
                and_(Vendor.id == Dispatch.vendor_id, Vendor.company_id == Dispatch.company_id),
            )
            .where(Dispatch.company_id == self.tenant.company_id, _delayed_predicate(as_of))
        )
        if warehouse_id is not None:
            stmt = stmt.where(Dispatch.warehouse_id == warehouse_id)
        # Longest overdue first: the oldest promise is the one somebody is
        # already chasing.
        return stmt.order_by(Dispatch.expected_delivery_date.asc(), Dispatch.id.asc())

    async def delayed_page(
        self, params: PageParams, *, as_of: date, warehouse_id: uuid.UUID | None = None
    ) -> tuple[list[DelayedDispatchRow], OffsetPagination]:
        rows, pagination = await paginate_projection(
            self.session, self.delayed_stmt(as_of=as_of, warehouse_id=warehouse_id), params
        )
        return [_delayed_row(row, as_of=as_of) for row in rows], pagination


# -----------------------------------------------------------------------------
# Cross-tenant delay sweep - the one query with no tenant scope
# -----------------------------------------------------------------------------


def _delayed_predicate(as_of: date) -> Any:
    """A consignment is late when it is on the road past its promised date.

    One definition, used by the list filter, the board, the delayed feed and the
    Celery sweep. Written as a SQL expression rather than a Python predicate
    because every one of those has to filter on it in the database - deriving it
    after the fact would mean reading rows in order to discard them.
    """
    return and_(
        Dispatch.status.in_(IN_FLIGHT_STATUSES),
        Dispatch.expected_delivery_date.isnot(None),
        Dispatch.expected_delivery_date < as_of,
    )


async def scan_delayed_across_tenants(
    session: AsyncSession, *, as_of: date, limit: int
) -> list[DelayedDispatchRow]:
    """Every company's overdue consignments, for the scheduled sweep.

    Deliberately not tenant-scoped, and deliberately not reachable from a route:
    Celery beat has no principal and no company, so a sweep that needed a
    ``TenantContext`` would have to invent one. The safeguards are that it is
    bounded by ``limit``, ordered deterministically so successive runs work
    through a backlog rather than re-reading its head, and returns
    ``company_id`` on every row so the caller can only act one tenant at a time.
    """
    stmt = (
        select(
            Dispatch.id.label("dispatch_id"),
            Dispatch.company_id.label("company_id"),
            Dispatch.code.label("code"),
            Dispatch.status.label("status"),
            Dispatch.customer_id.label("customer_id"),
            Dispatch.vendor_id.label("vendor_id"),
            Customer.name.label("customer_name"),
            Vendor.name.label("vendor_name"),
            Dispatch.tracking_id.label("tracking_id"),
            Dispatch.dispatch_date.label("dispatch_date"),
            Dispatch.expected_delivery_date.label("expected_delivery_date"),
            Dispatch.delay_reason.label("delay_reason"),
            Dispatch.delay_notified_at.label("delay_notified_at"),
        )
        .select_from(Dispatch)
        .outerjoin(
            Customer,
            and_(Customer.id == Dispatch.customer_id, Customer.company_id == Dispatch.company_id),
        )
        .outerjoin(
            Vendor,
            and_(Vendor.id == Dispatch.vendor_id, Vendor.company_id == Dispatch.company_id),
        )
        .where(_delayed_predicate(as_of))
        .order_by(Dispatch.company_id, Dispatch.expected_delivery_date.asc(), Dispatch.id.asc())
        .limit(limit)
    )
    return [_delayed_row(row, as_of=as_of) for row in (await session.execute(stmt)).all()]


async def mark_delay_notified(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    dispatch_ids: Sequence[uuid.UUID],
    at: Any = None,
) -> int:
    """Stamp ``delay_notified_at`` on the consignments an alert covered.

    One UPDATE for the whole set rather than a save per row, and explicitly
    scoped by ``company_id`` even though the caller has already partitioned by
    it: the sweep is the one code path here without a ``TenantContext``, so the
    tenant predicate is written out rather than assumed.
    """
    if not dispatch_ids:
        return 0
    stmt = (
        update(Dispatch)
        .where(Dispatch.company_id == company_id, Dispatch.id.in_(list(dispatch_ids)))
        .values(delay_notified_at=at or utcnow())
        .execution_options(synchronize_session=False)
    )
    return int((await session.execute(stmt)).rowcount or 0)


# -----------------------------------------------------------------------------
# Row mapping and projection pagination
# -----------------------------------------------------------------------------


def _delay_days(expected: date | None, as_of: date) -> int:
    """How many days a promise is overdue. Never negative.

    Computed in Python from a column the query already returns rather than as a
    SQL expression: it is presentation arithmetic, nothing filters or sorts on
    it, and date subtraction is one of the few things that renders differently on
    every database.
    """
    if expected is None or expected >= as_of:
        return 0
    return (as_of - expected).days


def _board_row(row: Any, *, as_of: date) -> TrackingBoardRow:
    if row.customer_id is not None:
        party_name, party_type = row.customer_name, "CUSTOMER"
    elif row.vendor_id is not None:
        party_name, party_type = row.vendor_name, "VENDOR"
    else:
        party_name, party_type = None, "INTERNAL"

    return TrackingBoardRow(
        dispatch_id=row.dispatch_id,
        code=row.code,
        dispatch_type=row.dispatch_type,
        status=row.status,
        party_name=party_name,
        party_type=party_type,
        reference_code=row.sales_order_code,
        # The carrier as the board should print it: the master record's name when
        # there is one, otherwise whatever was typed on the paperwork.
        courier_name=row.courier_name or row.transport_name,
        transporter_name=row.transporter_name,
        tracking_id=row.tracking_id,
        dispatch_date=row.dispatch_date,
        expected_delivery_date=row.expected_delivery_date,
        actual_delivery_date=row.actual_delivery_date,
        is_delayed=bool(row.is_delayed),
        delay_days=_delay_days(row.expected_delivery_date, as_of),
        delay_reason=row.delay_reason,
        qc_pending=bool(row.qc_pending),
        grn_pending=bool(row.grn_pending),
    )


def _delayed_row(row: Any, *, as_of: date) -> DelayedDispatchRow:
    return DelayedDispatchRow(
        dispatch_id=row.dispatch_id,
        company_id=row.company_id,
        code=row.code,
        status=row.status,
        customer_id=row.customer_id,
        vendor_id=row.vendor_id,
        party_name=row.customer_name or row.vendor_name,
        tracking_id=row.tracking_id,
        dispatch_date=row.dispatch_date,
        expected_delivery_date=row.expected_delivery_date,
        delay_days=_delay_days(row.expected_delivery_date, as_of),
        delay_reason=row.delay_reason,
        already_notified=row.delay_notified_at is not None,
    )


async def paginate_projection(
    session: AsyncSession, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], OffsetPagination]:
    """Offset-paginate a statement that selects columns rather than entities.

    ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``, which
    would keep only the first column of a projected row. The semantics are
    reproduced here - one extra row to decide ``has_next``, and a bounded count
    only when the caller asked for a total.
    """
    rows = (await session.execute(stmt.limit(params.limit + 1).offset(params.offset))).all()

    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    total: int | None = None
    total_pages: int | None = None
    if params.with_total:
        count_stmt = select(func.count()).select_from(
            stmt.order_by(None).limit(COUNT_CEILING).subquery()
        )
        total = int((await session.execute(count_stmt)).scalar_one())
        total_pages = max(1, -(-total // params.limit))

    return list(rows), OffsetPagination(
        page=params.page,
        limit=params.limit,
        total=total,
        total_pages=total_pages,
        has_next=has_next,
        has_previous=params.page > 1,
    )


__all__ = [
    "GRN_OUTSTANDING_STATUSES",
    "BatchFacts",
    "BatchLookupRepository",
    "CartonLookupRepository",
    "CustomerLookupRepository",
    "DispatchItem",
    "DispatchLineRepository",
    "DispatchRepository",
    "DispatchTrackingEventRepository",
    "DocumentLookupRepository",
    "ItemLookupRepository",
    "LedgerLookupRepository",
    "PackingOrderLookupRepository",
    "PartyFacts",
    "SalesLineFacts",
    "SalesOrderLineRepository",
    "SalesOrderRepository",
    "SerialFacts",
    "SerialLookupRepository",
    "TransporterLookupRepository",
    "VendorLookupRepository",
    "WarehouseLookupRepository",
    "mark_delay_notified",
    "paginate_projection",
    "scan_delayed_across_tenants",
]
