"""Queries for assembly orders.

Three shapes here exist for performance reasons rather than convenience:

* **Everything the component set needs is batched.** ``ItemLookupRepository``
  and ``SerialLookupRepository`` both take a collection of ids and answer in one
  statement. A twenty-component kitting list validated with a lookup per line
  would be forty round trips on the write path - the N+1 section 32 forbids.
* **The packing feed is one statement, not a list plus a balance read per row.**
  ``ready_for_packing_stmt`` joins ``inventory_balances`` and aggregates in the
  database. Asking ``inventory.get_available`` per completed order is the same
  N+1 wearing a service call's clothes, and the availability it reads is never
  cached (database.md section 4.5, section 34).
* **Narrow projections.** ``items`` is a wide table and completion reads it for
  every component; pulling whole rows to use six columns is the ``SELECT *`` the
  rules forbid.

Reads that cross into ``items``, ``stock_serials`` and ``inventory_balances`` go
through ``BaseRepository`` so the tenant filter - and the soft-delete filter
where the table has one - stays automatic rather than something each statement
has to remember.
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
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.assembly.models import (
    OPEN_SUPPLY_STATUSES,
    AssemblyOrder,
    AssemblyOrderComponent,
    AssemblyOrderStatus,
)
from src.modules.assembly.schemas import AssemblyOrderFilters, ReadyForPackingRow
from src.modules.inventory.models import InventoryBalance, SerialStatus, StockSerial
from src.modules.products.models import Item
from src.modules.warehouses.models import Warehouse


@dataclass(frozen=True, slots=True)
class AssemblyItem:
    """The item-master facts an assembly order validates against.

    A projection, not the ``Item`` entity - see the module docstring. Six
    columns of a very wide table, and every one of them is read by a rule:
    ``code`` names the item in an error, the two tracking flags decide whether
    this module can post movements for it at all, ``can_be_assembly_component``
    is the capability check section 3 requires instead of reading ``item_group``,
    and ``is_active`` stops a retired part being kitted.
    """

    id: uuid.UUID
    code: str
    is_batch_tracked: bool
    is_serial_tracked: bool
    can_be_assembly_component: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class SerialFacts:
    """Where one serialised unit is and whether it is still in stock."""

    id: uuid.UUID
    item_id: uuid.UUID
    serial_number: str
    warehouse_id: uuid.UUID | None
    status: SerialStatus


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for order entry and kitting."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AssemblyItem]:
        """Batched id -> item facts. One statement for a whole component set."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.can_be_assembly_component,
            Item.is_active,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: AssemblyItem(
                id=row.id,
                code=row.code,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                can_be_assembly_component=row.can_be_assembly_component,
                is_active=row.is_active,
            )
            for row in rows
        }


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence and activity checks for the two warehouses on the header."""

    model = Warehouse

    async def load(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, bool]]:
        """Batched id -> (code, is_active). Both warehouses in one statement."""
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


class SerialLookupRepository(BaseRepository[StockSerial]):
    """Traceability lookups for serialised components."""

    model = StockSerial

    async def load(self, serial_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SerialFacts]:
        """Batched id -> serial facts, for validating a consumption set."""
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

    async def already_consumed(
        self, serial_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Serials some *other* assembly already swallowed, mapped to its code.

        The database cannot express "a serial appears in at most one component's
        array" as a constraint, so this containment query is the guard. It uses
        the GIN index on ``consumed_serial_ids``; without it this would be a
        sequential scan of every assembly ever completed.
        """
        if not serial_ids:
            return {}
        unique = list(dict.fromkeys(serial_ids))
        stmt = (
            select(AssemblyOrder.code, AssemblyOrderComponent.consumed_serial_ids)
            .join(
                AssemblyOrder,
                and_(
                    AssemblyOrder.id == AssemblyOrderComponent.assembly_order_id,
                    AssemblyOrder.company_id == AssemblyOrderComponent.company_id,
                ),
            )
            .where(
                AssemblyOrderComponent.company_id == self.tenant.company_id,
                AssemblyOrderComponent.consumed_serial_ids.overlap(unique),
            )
        )
        wanted = set(unique)
        found: dict[uuid.UUID, str] = {}
        for row in (await self.session.execute(stmt)).all():
            for serial_id in row.consumed_serial_ids:
                if serial_id in wanted:
                    found[serial_id] = row.code
        return found


class AssemblyComponentRepository(BaseRepository[AssemblyOrderComponent]):
    model = AssemblyOrderComponent
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "line_number": AssemblyOrderComponent.line_number,
    }

    async def list_for_order(self, order_id: uuid.UUID) -> list[AssemblyOrderComponent]:
        """Every component of one order, for the kitting list and for completion.

        Not paginated, by deliberate exception to section 31: the set is bounded
        by ``MAX_COMPONENTS_PER_ORDER`` at write time and completion is only
        correct if it sees all of it.
        """
        stmt = (
            self._scoped()
            .where(AssemblyOrderComponent.assembly_order_id == order_id)
            .order_by(AssemblyOrderComponent.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_orders(
        self, order_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[AssemblyOrderComponent]]:
        """Components for several orders in one statement - used by batch readers."""
        if not order_ids:
            return {}
        stmt = (
            self._scoped()
            .where(AssemblyOrderComponent.assembly_order_id.in_(list(dict.fromkeys(order_ids))))
            .order_by(
                AssemblyOrderComponent.assembly_order_id, AssemblyOrderComponent.line_number
            )
        )
        grouped: dict[uuid.UUID, list[AssemblyOrderComponent]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.assembly_order_id, []).append(row)
        return grouped

    async def delete_for_order(self, order_id: uuid.UUID) -> int:
        """Drop a draft's component set before re-exploding it.

        A Core DELETE rather than a loop: replacing a 200-line kitting list must
        be one statement. Safe only for a DRAFT, where nothing has been issued.
        """
        stmt = (
            delete(AssemblyOrderComponent)
            .where(
                AssemblyOrderComponent.company_id == self.tenant.company_id,
                AssemblyOrderComponent.assembly_order_id == order_id,
            )
            .execution_options(synchronize_session=False)
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class AssemblyOrderRepository(BaseRepository[AssemblyOrder]):
    model = AssemblyOrder
    searchable_fields = ("code", "assembly_line")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": AssemblyOrder.code,
        "status": AssemblyOrder.status,
        "planned_start": AssemblyOrder.planned_start,
        "planned_end": AssemblyOrder.planned_end,
        "actual_end": AssemblyOrder.actual_end,
        "planned_quantity": AssemblyOrder.planned_quantity,
        "created_at": AssemblyOrder.created_at,
    }

    # --- reads --------------------------------------------------------------

    async def get_by_code(self, code: str) -> AssemblyOrder | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, order_id: uuid.UUID) -> AssemblyOrder | None:
        """Take a row lock on the header before changing its state or stock.

        Issue, completion and cancellation all read the components, decide what
        to reserve, consume or release, and write back. Two of them running at
        once on the same order would each act on the quantities they read first,
        so the header is the serialisation point for the whole document.
        """
        stmt = self._scoped().where(AssemblyOrder.id == order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: AssemblyOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.item_id is not None:
            stmt = stmt.where(AssemblyOrder.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(AssemblyOrder.warehouse_id == filters.warehouse_id)
        if filters.output_warehouse_id is not None:
            stmt = stmt.where(AssemblyOrder.output_warehouse_id == filters.output_warehouse_id)
        if filters.status is not None:
            stmt = stmt.where(AssemblyOrder.status == filters.status)
        if filters.assembly_line is not None:
            stmt = stmt.where(AssemblyOrder.assembly_line == filters.assembly_line)
        if filters.open_only:
            stmt = stmt.where(AssemblyOrder.status.in_(OPEN_SUPPLY_STATUSES))
        if filters.requires_qc is not None:
            stmt = stmt.where(AssemblyOrder.requires_qc.is_(filters.requires_qc))
        if filters.planned_from is not None:
            stmt = stmt.where(AssemblyOrder.planned_start >= filters.planned_from)
        if filters.planned_to is not None:
            stmt = stmt.where(AssemblyOrder.planned_start <= filters.planned_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: AssemblyOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[AssemblyOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, AssemblyOrder.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    # --- the packing handover feed -----------------------------------------

    def ready_for_packing_stmt(self, warehouse_id: uuid.UUID | None) -> Select[Any]:
        """Completed assemblies whose output is free stock and unclaimed.

        Three conditions, all in one statement:

        1. the order is COMPLETED - an output still on QC hold is in QC_PENDING
           and is deliberately not offered;
        2. no packing order has claimed it (``packing_order_id IS NULL``);
        3. the output position actually has promisable stock. ``available_quantity``
           is the generated column ``on_hand - reserved - qc_hold - damaged``, so
           an assembly whose output was reserved by a sales order, or held after
           the fact, drops out here rather than being offered and then failing
           to pick.

        The outer join is on ``(item, warehouse, company)`` so the tenant
        predicate applies to both sides and PostgreSQL can use the tenant-leading
        indexes on each table. ``GROUP BY`` on the header's primary key is what
        lets the balance rows - one per location and batch - be summed without
        listing every selected column in the grouping.
        """
        available = func.coalesce(func.sum(InventoryBalance.available_quantity), 0)

        stmt = (
            select(
                AssemblyOrder.id.label("assembly_order_id"),
                AssemblyOrder.code.label("code"),
                AssemblyOrder.item_id.label("item_id"),
                AssemblyOrder.output_warehouse_id.label("output_warehouse_id"),
                AssemblyOrder.assembled_quantity.label("assembled_quantity"),
                AssemblyOrder.actual_end.label("completed_at"),
                AssemblyOrder.requires_qc.label("requires_qc"),
                available.label("available_quantity"),
            )
            .select_from(AssemblyOrder)
            .outerjoin(
                InventoryBalance,
                and_(
                    InventoryBalance.company_id == AssemblyOrder.company_id,
                    InventoryBalance.item_id == AssemblyOrder.item_id,
                    InventoryBalance.warehouse_id == AssemblyOrder.output_warehouse_id,
                ),
            )
            .where(
                AssemblyOrder.company_id == self.tenant.company_id,
                AssemblyOrder.status == AssemblyOrderStatus.COMPLETED,
                AssemblyOrder.packing_order_id.is_(None),
            )
            .group_by(AssemblyOrder.id)
            .having(available > 0)
        )

        if warehouse_id is not None:
            stmt = stmt.where(AssemblyOrder.output_warehouse_id == warehouse_id)

        # Oldest completion first: assembled stock that has been sitting longest
        # should be packed first. ``id`` breaks ties so a page boundary cannot
        # repeat or drop a row when two orders finished in the same instant.
        return stmt.order_by(AssemblyOrder.actual_end.asc(), AssemblyOrder.id.asc())

    async def ready_for_packing(
        self, *, warehouse_id: uuid.UUID | None, limit: int
    ) -> list[ReadyForPackingRow]:
        """Bounded handover list for the packing module's own service call."""
        stmt = self.ready_for_packing_stmt(warehouse_id).limit(limit)
        return [_packing_row(row) for row in (await self.session.execute(stmt)).all()]

    async def ready_for_packing_page(
        self, *, warehouse_id: uuid.UUID | None, params: PageParams
    ) -> tuple[list[ReadyForPackingRow], OffsetPagination]:
        """Paginate the aggregate.

        ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``,
        which would keep only the first column of an aggregate row. The
        semantics are reproduced here - one extra row to decide ``has_next``,
        and a bounded count only when the caller asked for a total.
        """
        stmt = self.ready_for_packing_stmt(warehouse_id)
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

        return [_packing_row(row) for row in rows], OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )


def _packing_row(row: Any) -> ReadyForPackingRow:
    """Map one aggregate row, keeping the numerics as ``Decimal`` (section 84)."""
    completed_at: datetime | None = row.completed_at
    return ReadyForPackingRow(
        assembly_order_id=row.assembly_order_id,
        code=row.code,
        item_id=row.item_id,
        output_warehouse_id=row.output_warehouse_id,
        assembled_quantity=Decimal(str(row.assembled_quantity)),
        available_quantity=Decimal(str(row.available_quantity)),
        completed_at=completed_at,
        requires_qc=bool(row.requires_qc),
    )


__all__ = [
    "AssemblyComponentRepository",
    "AssemblyItem",
    "AssemblyOrderRepository",
    "ItemLookupRepository",
    "SerialFacts",
    "SerialLookupRepository",
    "WarehouseLookupRepository",
]
