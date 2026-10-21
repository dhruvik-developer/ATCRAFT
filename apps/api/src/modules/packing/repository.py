"""Queries for packing orders, cartons and carton contents.

Four shapes here exist for performance or correctness reasons rather than
convenience:

* **Every lookup a write path needs is batched.** ``ItemLookupRepository`` and
  ``SerialLookupRepository`` take a collection of ids and answer in one
  statement. A twenty-line carton validated with a lookup per content row would
  be forty round trips on the write path - the N+1 section 32 forbids.
* **The dispatch feed is one statement**, not a list plus a carton count per
  row. It aggregates ``cartons`` in the database; asking for the carton count
  per packed order is the same N+1 wearing a service call's clothes.
* **Narrow projections.** ``items`` is a very wide table and sealing reads it for
  every content row; pulling whole rows to use six columns is the ``SELECT *``
  the rules forbid.
* **The header is the serialisation point.** ``lock_for_update`` is what makes
  the next carton number, the packed-quantity roll-up and the completion totals
  safe: they are all read-modify-write sequences over the order's children, and
  two packers on one order would otherwise each act on what they read first.

Reads that cross into ``items``, ``customers``, ``warehouses``, ``stock_batches``
and ``stock_serials`` go through ``BaseRepository`` so the tenant filter - and
the soft-delete filter where the table has one - stays automatic rather than
something each statement has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, delete, func, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.customers.models import Customer
from src.modules.inventory.models import InventoryBalance, SerialStatus, StockBatch, StockSerial
from src.modules.packing.models import (
    OPEN_STATUSES,
    Carton,
    CartonContent,
    PackingOrder,
    PackingOrderLine,
    PackingOrderStatus,
)
from src.modules.packing.schemas import (
    CartonFilters,
    PackingMaterialRow,
    PackingOrderFilters,
    ReadyForDispatchRow,
)
from src.modules.products.models import Item, ItemGroup
from src.modules.sales.models import SalesOrder, SalesOrderLine
from src.modules.warehouses.models import Warehouse


@dataclass(frozen=True, slots=True)
class PackingItem:
    """The item-master facts a packing order validates against.

    A projection, not the ``Item`` entity - see the module docstring. ``code``
    and ``name`` are printed on the label and the packing list; the two tracking
    flags decide what a content row must carry and are frozen onto it;
    ``base_uom_id`` is the unit the packing-material movement posts in;
    ``item_group`` drives the packing-material pick-list only and is never read
    to decide whether an operation is permitted (database.md section 3).
    """

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    item_group: ItemGroup
    is_batch_tracked: bool
    is_serial_tracked: bool
    is_active: bool
    #: Starting the order raises a FINAL inspection per line of this item (§E5-8).
    #: Default False so a projection built before the flag existed keeps meaning
    #: "no final QC owed".
    requires_final_qc: bool = False


@dataclass(frozen=True, slots=True)
class CustomerFacts:
    """Who the boxes are addressed to, and whether we may still ship to them."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    is_blocked: bool


@dataclass(frozen=True, slots=True)
class SerialFacts:
    """Where one serialised unit is and whether it is still in stock."""

    id: uuid.UUID
    item_id: uuid.UUID
    serial_number: str
    warehouse_id: uuid.UUID | None
    status: SerialStatus


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """A batch and the item it belongs to, for validating a content row."""

    id: uuid.UUID
    item_id: uuid.UUID
    batch_number: str


@dataclass(frozen=True, slots=True)
class CartonTotals:
    """The aggregate completion freezes onto the order header."""

    carton_count: int
    sealed_count: int
    gross_weight: Decimal
    net_weight: Decimal

    @property
    def open_count(self) -> int:
        return self.carton_count - self.sealed_count


# -----------------------------------------------------------------------------
# Master-data lookups
# -----------------------------------------------------------------------------


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for order entry, packing and labels."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PackingItem]:
        """Batched id -> item facts. One statement for a whole carton."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.item_group,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.is_active,
            Item.requires_final_qc,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        return {
            row.id: PackingItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                item_group=ItemGroup(row.item_group),
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                is_active=row.is_active,
                requires_final_qc=row.requires_final_qc,
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def packing_materials(
        self, *, warehouse_id: uuid.UUID, params: PageParams, search: str | None = None
    ) -> tuple[list[PackingMaterialRow], OffsetPagination]:
        """The packing-material pick-list, with live availability beside it.

        ``item_group`` is used here and only here: as the classification that
        decides what the carton form *offers*, which is exactly the reporting and
        default-behaviour role database.md section 3 permits. Nothing downstream
        reads it to decide whether a seal may proceed.

        The availability is aggregated in the same statement rather than read per
        row, and it is never cached (section 34) - a packer choosing a box needs
        to know there are boxes.
        """
        available = func.coalesce(func.sum(InventoryBalance.available_quantity), 0)
        stmt = (
            select(
                Item.id.label("item_id"),
                Item.code.label("code"),
                Item.name.label("name"),
                Item.base_uom_id.label("base_uom_id"),
                available.label("available_quantity"),
            )
            .select_from(Item)
            .outerjoin(
                InventoryBalance,
                and_(
                    InventoryBalance.company_id == Item.company_id,
                    InventoryBalance.item_id == Item.id,
                    InventoryBalance.warehouse_id == warehouse_id,
                ),
            )
            .where(
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                Item.is_active.is_(True),
                Item.item_group == ItemGroup.PACKING_MATERIAL,
            )
            .group_by(Item.id)
            .order_by(Item.code.asc())
        )
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(Item.code.ilike(pattern) | Item.name.ilike(pattern))

        rows, pagination = await _paginate_aggregate(self.session, stmt, params)
        return [
            PackingMaterialRow(
                item_id=row.item_id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                available_quantity=Decimal(str(row.available_quantity)),
            )
            for row in rows
        ], pagination


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence and activity checks for the warehouse on the header."""

    model = Warehouse

    async def load(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, bool]]:
        if not warehouse_ids:
            return {}
        stmt = select(Warehouse.id, Warehouse.code, Warehouse.is_active).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.deleted_at.is_(None),
            Warehouse.id.in_(list(dict.fromkeys(warehouse_ids))),
        )
        return {
            row.id: (row.code, row.is_active) for row in (await self.session.execute(stmt)).all()
        }


class CustomerLookupRepository(BaseRepository[Customer]):
    """Who the shipment is for - checked at creation and printed on the label."""

    model = Customer

    async def get_facts(self, customer_id: uuid.UUID) -> CustomerFacts | None:
        stmt = select(
            Customer.id,
            Customer.code,
            Customer.name,
            Customer.is_active,
            Customer.is_blocked,
        ).where(
            Customer.company_id == self.tenant.company_id,
            Customer.deleted_at.is_(None),
            Customer.id == customer_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return CustomerFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            is_blocked=row.is_blocked,
        )


class SalesOrderLookupRepository(BaseRepository[SalesOrder]):
    """The demand a packing order is filling."""

    model = SalesOrder

    async def get_header(self, sales_order_id: uuid.UUID) -> tuple[str, uuid.UUID] | None:
        """``(code, customer_id)`` - enough to prove the order is shippable to
        the customer named on the packing order."""
        stmt = select(SalesOrder.code, SalesOrder.customer_id).where(
            SalesOrder.company_id == self.tenant.company_id,
            SalesOrder.id == sales_order_id,
        )
        row = (await self.session.execute(stmt)).first()
        return (row.code, row.customer_id) if row is not None else None

    async def load_lines(
        self, line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]:
        """Batched line id -> ``(sales_order_id, item_id)``.

        Both halves are checked: a packing line may not point at a sales line
        belonging to a different order, nor at one for a different item, because
        either would make the fulfilment arithmetic on the sales order wrong in a
        way nothing downstream could detect.
        """
        if not line_ids:
            return {}
        stmt = select(
            SalesOrderLine.id, SalesOrderLine.sales_order_id, SalesOrderLine.item_id
        ).where(
            SalesOrderLine.company_id == self.tenant.company_id,
            SalesOrderLine.id.in_(list(dict.fromkeys(line_ids))),
        )
        return {
            row.id: (row.sales_order_id, row.item_id)
            for row in (await self.session.execute(stmt)).all()
        }


class BatchLookupRepository(BaseRepository[StockBatch]):
    """Batch identity for a content row of a batch-tracked item."""

    model = StockBatch

    async def load(self, batch_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, BatchFacts]:
        if not batch_ids:
            return {}
        stmt = select(StockBatch.id, StockBatch.item_id, StockBatch.batch_number).where(
            StockBatch.company_id == self.tenant.company_id,
            StockBatch.id.in_(list(dict.fromkeys(batch_ids))),
        )
        return {
            row.id: BatchFacts(id=row.id, item_id=row.item_id, batch_number=row.batch_number)
            for row in (await self.session.execute(stmt)).all()
        }


class SerialLookupRepository(BaseRepository[StockSerial]):
    """Traceability lookups for serialised contents."""

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
            StockSerial.id.in_(list(dict.fromkeys(serial_ids))),
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


# -----------------------------------------------------------------------------
# The documents themselves
# -----------------------------------------------------------------------------


class PackingOrderLineRepository(BaseRepository[PackingOrderLine]):
    model = PackingOrderLine
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "line_number": PackingOrderLine.line_number,
    }

    async def list_for_order(self, order_id: uuid.UUID) -> list[PackingOrderLine]:
        """Every line of one order.

        Not paginated, by deliberate exception to section 31: the set is bounded
        by ``MAX_LINES_PER_ORDER`` at write time and completion is only correct
        if it sees all of it.
        """
        stmt = (
            self._scoped()
            .where(PackingOrderLine.packing_order_id == order_id)
            .order_by(PackingOrderLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_order(self, order_id: uuid.UUID) -> list[PackingOrderLine]:
        """The line set, locked, for a packed-quantity roll-up.

        Ordered by primary key rather than by line number: two content edits on
        one order that lock the same rows in a different order would deadlock,
        and the primary key is the one ordering every caller can agree on
        (database.md section 4.4).
        """
        stmt = (
            self._scoped()
            .where(PackingOrderLine.packing_order_id == order_id)
            .order_by(PackingOrderLine.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_order(self, order_id: uuid.UUID) -> int:
        """Drop a draft's line set before replacing it.

        A Core DELETE rather than a loop: replacing a 200-line set must be one
        statement. Safe only for a DRAFT, where no carton references a line.
        """
        stmt = (
            delete(PackingOrderLine)
            .where(
                PackingOrderLine.company_id == self.tenant.company_id,
                PackingOrderLine.packing_order_id == order_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


class CartonContentRepository(BaseRepository[CartonContent]):
    model = CartonContent

    async def list_for_carton(self, carton_id: uuid.UUID) -> list[CartonContent]:
        stmt = (
            self._scoped()
            .where(CartonContent.carton_id == carton_id)
            .order_by(CartonContent.created_at, CartonContent.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_cartons(
        self, carton_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[CartonContent]]:
        """Contents for several cartons in one statement - the packing list read."""
        if not carton_ids:
            return {}
        stmt = (
            self._scoped()
            .where(CartonContent.carton_id.in_(list(dict.fromkeys(carton_ids))))
            .order_by(CartonContent.carton_id, CartonContent.created_at, CartonContent.id)
        )
        grouped: dict[uuid.UUID, list[CartonContent]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.carton_id, []).append(row)
        return grouped

    async def delete_for_carton(self, carton_id: uuid.UUID) -> int:
        stmt = (
            delete(CartonContent)
            .where(
                CartonContent.company_id == self.tenant.company_id,
                CartonContent.carton_id == carton_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def delete_for_cartons(self, carton_ids: Sequence[uuid.UUID]) -> int:
        """Contents of several cartons in one statement - the cancel path.

        An empty set deletes nothing: without the early return the ``IN ()``
        would be dropped and the statement would clear every carton the tenant
        owns.
        """
        if not carton_ids:
            return 0
        stmt = (
            delete(CartonContent)
            .where(
                CartonContent.company_id == self.tenant.company_id,
                CartonContent.carton_id.in_(list(dict.fromkeys(carton_ids))),
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def already_packed(
        self, serial_ids: Sequence[uuid.UUID], *, exclude_carton_id: uuid.UUID | None = None
    ) -> dict[uuid.UUID, str]:
        """Serials some *other* carton already holds, mapped to its barcode.

        The database cannot express "a serial appears in at most one carton" as a
        constraint, so this containment query is the guard. It uses the GIN index
        on ``serial_ids``; without it this would be a sequential scan of every
        carton ever packed.
        """
        if not serial_ids:
            return {}
        unique = list(dict.fromkeys(serial_ids))
        stmt = (
            select(Carton.barcode, CartonContent.serial_ids)
            .join(
                Carton,
                and_(
                    Carton.id == CartonContent.carton_id,
                    Carton.company_id == CartonContent.company_id,
                ),
            )
            .where(
                CartonContent.company_id == self.tenant.company_id,
                CartonContent.serial_ids.overlap(unique),
            )
        )
        if exclude_carton_id is not None:
            stmt = stmt.where(CartonContent.carton_id != exclude_carton_id)

        wanted = set(unique)
        found: dict[uuid.UUID, str] = {}
        for row in (await self.session.execute(stmt)).all():
            for serial_id in row.serial_ids:
                if serial_id in wanted:
                    found[serial_id] = row.barcode
        return found

    async def find_by_serial(self, serial_id: uuid.UUID) -> list[CartonContent]:
        """The traceability answer: which carton contents hold this serial."""
        stmt = self._scoped().where(CartonContent.serial_ids.overlap([serial_id]))
        return list((await self.session.execute(stmt)).scalars().all())


class CartonRepository(BaseRepository[Carton]):
    model = Carton
    searchable_fields = ("barcode",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "carton_number": Carton.carton_number,
        "barcode": Carton.barcode,
        "gross_weight_kg": Carton.gross_weight_kg,
        "sealed_at": Carton.sealed_at,
        "created_at": Carton.created_at,
    }

    async def get_by_barcode(self, barcode: str) -> Carton | None:
        stmt = self._scoped().where(Carton.barcode == barcode.strip().upper())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_in_order(self, order_id: uuid.UUID, carton_id: uuid.UUID) -> Carton | None:
        """A carton, proved to belong to the order in the URL.

        Fetching it by id alone would let a carton from another order be sealed
        through a path whose permissions were checked against this one.
        """
        stmt = self._scoped().where(Carton.id == carton_id, Carton.packing_order_id == order_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_order(self, order_id: uuid.UUID) -> list[Carton]:
        stmt = (
            self._scoped().where(Carton.packing_order_id == order_id).order_by(Carton.carton_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    def _filtered(self, filters: CartonFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.packing_order_id is not None:
            stmt = stmt.where(Carton.packing_order_id == filters.packing_order_id)
        if filters.barcode is not None:
            stmt = stmt.where(Carton.barcode == filters.barcode.strip().upper())
        if filters.is_sealed is not None:
            stmt = stmt.where(Carton.is_sealed.is_(filters.is_sealed))
        return stmt

    async def list_page_filtered(
        self, filters: CartonFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[Carton], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, Carton.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    async def next_carton_number(self, order_id: uuid.UUID) -> int:
        """The next sequential number within one order.

        ``MAX + 1`` is forbidden for document numbers (``common.codes``) because
        two requests race on it. It is correct here for one specific reason: the
        caller holds a ``FOR UPDATE`` lock on the packing order header, so the
        two requests are already serialised behind that row. The number is also
        not a legally sequential series - a gap from a removed carton is
        expected, and renumbering the survivors would invalidate labels that have
        already been printed.
        """
        stmt = select(func.coalesce(func.max(Carton.carton_number), 0)).where(
            Carton.company_id == self.tenant.company_id,
            Carton.packing_order_id == order_id,
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def totals_for_order(self, order_id: uuid.UUID) -> CartonTotals:
        """One aggregate row: the carton counts and weights completion freezes."""
        sealed = func.count().filter(Carton.is_sealed.is_(True))
        stmt = select(
            func.count().label("carton_count"),
            sealed.label("sealed_count"),
            func.coalesce(func.sum(Carton.gross_weight_kg), 0).label("gross_weight"),
            func.coalesce(func.sum(Carton.net_weight_kg), 0).label("net_weight"),
        ).where(
            Carton.company_id == self.tenant.company_id,
            Carton.packing_order_id == order_id,
        )
        row = (await self.session.execute(stmt)).one()
        return CartonTotals(
            carton_count=int(row.carton_count),
            sealed_count=int(row.sealed_count),
            gross_weight=Decimal(str(row.gross_weight)),
            net_weight=Decimal(str(row.net_weight)),
        )

    async def counts_for_orders(self, order_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Carton counts for several orders in one statement."""
        if not order_ids:
            return {}
        stmt = (
            select(Carton.packing_order_id, func.count().label("carton_count"))
            .where(
                Carton.company_id == self.tenant.company_id,
                Carton.packing_order_id.in_(list(dict.fromkeys(order_ids))),
            )
            .group_by(Carton.packing_order_id)
        )
        return {
            row.packing_order_id: int(row.carton_count)
            for row in (await self.session.execute(stmt)).all()
        }


class PackingOrderRepository(BaseRepository[PackingOrder]):
    model = PackingOrder
    searchable_fields = ("code", "notes")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": PackingOrder.code,
        "status": PackingOrder.status,
        "planned_date": PackingOrder.planned_date,
        "packed_date": PackingOrder.packed_date,
        "total_cartons": PackingOrder.total_cartons,
        "created_at": PackingOrder.created_at,
    }

    async def get_by_code(self, code: str) -> PackingOrder | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, order_id: uuid.UUID) -> PackingOrder | None:
        """Take a row lock on the header before changing its state or its boxes.

        Adding a carton, sealing one and completing the order all read the
        children, decide something from them and write back. Two of them running
        at once would each act on the quantities they read first, so the header is
        the serialisation point for the whole document - and it is what makes
        ``next_carton_number`` safe.
        """
        stmt = self._scoped().where(PackingOrder.id == order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: PackingOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(PackingOrder.status == filters.status)
        if filters.customer_id is not None:
            stmt = stmt.where(PackingOrder.customer_id == filters.customer_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(PackingOrder.warehouse_id == filters.warehouse_id)
        if filters.sales_order_id is not None:
            stmt = stmt.where(PackingOrder.sales_order_id == filters.sales_order_id)
        if filters.open_only:
            stmt = stmt.where(PackingOrder.status.in_(OPEN_STATUSES))
        if filters.awaiting_dispatch:
            stmt = stmt.where(
                PackingOrder.status == PackingOrderStatus.PACKED,
                PackingOrder.dispatch_id.is_(None),
            )
        if filters.planned_from is not None:
            stmt = stmt.where(PackingOrder.planned_date >= filters.planned_from)
        if filters.planned_to is not None:
            stmt = stmt.where(PackingOrder.planned_date <= filters.planned_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: PackingOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[PackingOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, PackingOrder.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    # --- the dispatch handover feed ----------------------------------------

    def ready_for_dispatch_stmt(self, warehouse_id: uuid.UUID | None) -> Select[Any]:
        """Packed orders no dispatch has claimed.

        Two conditions and one aggregate:

        1. the order is PACKED - an order still IN_PROGRESS has open boxes, and
           one already HANDED_OVER belongs to a dispatch;
        2. ``dispatch_id IS NULL`` - nobody has taken it, so two dispatchers
           cannot both plan around the same boxes;
        3. the carton count comes from the carton rows rather than from
           ``total_cartons``, so the feed cannot advertise a number the boxes
           disagree with.

        The predicate matches ``ix_packing_orders_ready_for_dispatch`` exactly;
        the two have to change together. The join is on ``(order, company)`` so
        the tenant predicate applies to both sides and PostgreSQL can use the
        tenant-leading index on each table.
        """
        stmt = (
            select(
                PackingOrder.id.label("packing_order_id"),
                PackingOrder.code.label("code"),
                PackingOrder.customer_id.label("customer_id"),
                PackingOrder.warehouse_id.label("warehouse_id"),
                PackingOrder.sales_order_id.label("sales_order_id"),
                PackingOrder.packed_date.label("packed_date"),
                PackingOrder.total_gross_weight.label("total_gross_weight"),
                PackingOrder.total_net_weight.label("total_net_weight"),
                func.count(Carton.id).label("carton_count"),
            )
            .select_from(PackingOrder)
            .outerjoin(
                Carton,
                and_(
                    Carton.company_id == PackingOrder.company_id,
                    Carton.packing_order_id == PackingOrder.id,
                ),
            )
            .where(
                PackingOrder.company_id == self.tenant.company_id,
                PackingOrder.status == PackingOrderStatus.PACKED,
                PackingOrder.dispatch_id.is_(None),
            )
            .group_by(PackingOrder.id)
        )
        if warehouse_id is not None:
            stmt = stmt.where(PackingOrder.warehouse_id == warehouse_id)

        # Oldest packed first: boxes that have been sitting on the dock longest
        # should go on the first truck. ``id`` breaks ties so a page boundary
        # cannot repeat or drop a row when two orders were packed in one instant.
        return stmt.order_by(PackingOrder.packed_date.asc(), PackingOrder.id.asc())

    async def ready_for_dispatch(
        self, *, warehouse_id: uuid.UUID | None, limit: int
    ) -> list[ReadyForDispatchRow]:
        """Bounded handover list for the dispatch module's own service call."""
        stmt = self.ready_for_dispatch_stmt(warehouse_id).limit(limit)
        return [_dispatch_row(row) for row in (await self.session.execute(stmt)).all()]

    async def ready_for_dispatch_page(
        self, *, warehouse_id: uuid.UUID | None, params: PageParams
    ) -> tuple[list[ReadyForDispatchRow], OffsetPagination]:
        rows, pagination = await _paginate_aggregate(
            self.session, self.ready_for_dispatch_stmt(warehouse_id), params
        )
        return [_dispatch_row(row) for row in rows], pagination


def _dispatch_row(row: Any) -> ReadyForDispatchRow:
    """Map one aggregate row, keeping the numerics as ``Decimal`` (section 84)."""
    packed_date: datetime | None = row.packed_date
    return ReadyForDispatchRow(
        packing_order_id=row.packing_order_id,
        code=row.code,
        customer_id=row.customer_id,
        warehouse_id=row.warehouse_id,
        sales_order_id=row.sales_order_id,
        packed_date=packed_date,
        carton_count=int(row.carton_count),
        total_gross_weight=Decimal(str(row.total_gross_weight)),
        total_net_weight=Decimal(str(row.total_net_weight)),
    )


async def _paginate_aggregate(
    session: Any, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], OffsetPagination]:
    """Offset-paginate a statement that selects columns rather than entities.

    ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``, which
    would keep only the first column of an aggregate row. The semantics are
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
    "BatchFacts",
    "BatchLookupRepository",
    "CartonContentRepository",
    "CartonRepository",
    "CartonTotals",
    "CustomerFacts",
    "CustomerLookupRepository",
    "ItemLookupRepository",
    "PackingItem",
    "PackingOrderLineRepository",
    "PackingOrderRepository",
    "SalesOrderLookupRepository",
    "SerialFacts",
    "SerialLookupRepository",
    "WarehouseLookupRepository",
]
