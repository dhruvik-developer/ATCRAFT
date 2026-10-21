"""Queries and persistence for the inventory engine.

The interesting method in this file is :meth:`InventoryRepository.lock_balances`.
It implements steps 2 and 3 of the mandatory concurrency pattern (database
contract section 4.4) and everything that changes stock goes through it:

1. one multi-row ``INSERT ... ON CONFLICT (grain) DO UPDATE ... RETURNING`` that
   creates the balance rows that do not exist yet and returns the ids of all of
   them, whether created or found;
2. one ``SELECT ... FOR UPDATE`` over those ids, **ordered by primary key**, with
   ``populate_existing`` so the quantities are genuinely re-read from the
   database after the lock rather than served from the session's identity map.

Both statements are a fixed cost regardless of how many lines the document has -
there is no per-line round trip anywhere in the write path (section 32).

Everything else here is ordinary tenant-scoped query building. The list
statements join ``items`` where a filter or a label needs it, so a page of
balances costs one query plus a small number of batched label lookups rather
than one query per row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from src.common.repository import BaseRepository
from src.common.warehouse_scope import warehouse_scope
from src.core.clock import utcnow
from src.core.ids import new_id, persisted_actor
from src.core.pagination import PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.inventory.models import (
    BALANCE_GRAIN_SQL,
    SERIAL_IN_WAREHOUSE_STATUSES,
    BatchStatus,
    BinMove,
    BinMoveLine,
    CostLayerPool,
    InternalIssue,
    InternalIssueLine,
    InventoryBalance,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
    InventoryTransaction,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalCountStatus,
    ScrapDisposal,
    ScrapDisposalLine,
    SerialStatus,
    StockAdjustment,
    StockAdjustmentLine,
    StockBatch,
    StockSerial,
    StockTransfer,
    StockTransferLine,
    fifo_sort_key,
)
from src.modules.inventory.schemas import (
    AdjustmentFilters,
    BalanceFilters,
    BinMoveFilters,
    InternalIssueFilters,
    LedgerFilters,
    PhysicalCountFilters,
    ScrapDisposalFilters,
    TransferFilters,
)
from src.modules.products.models import Item
from src.modules.warehouses.models import StorageLocation, Warehouse


@dataclass(frozen=True, slots=True, order=True)
class BalanceGrain:
    """The identity of one stock position.

    ``order=True`` is load-bearing: the upsert that creates missing rows also
    takes their locks, and it takes them in the order the VALUES list is written.
    Sorting the grains gives every transaction the same acquisition order, so two
    concurrent multi-line documents touching the same items cannot deadlock each
    other before they reach the primary-key-ordered ``SELECT ... FOR UPDATE``.

    ``uuid.UUID`` sorts by its integer value, and ``None`` cannot be compared to
    a UUID, so the sort key substitutes the nil UUID for a missing location or
    batch - the same substitution the unique index makes.
    """

    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None

    @property
    def sort_key(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        nil = uuid.UUID(int=0)
        return (
            self.item_id,
            self.warehouse_id,
            self.location_id or nil,
            self.batch_id or nil,
        )


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """The item-master columns the engine actually needs.

    A narrow projection on purpose: ``items`` is a wide table and the write path
    reads it on every posting, so pulling whole rows would move a lot of bytes
    nobody looks at (section 33).
    """

    id: uuid.UUID
    code: str
    name: str
    is_batch_tracked: bool
    is_serial_tracked: bool
    is_active: bool
    standard_cost: Decimal
    reorder_level: Decimal
    can_scrap: bool = True


@dataclass(frozen=True, slots=True)
class WarehouseFacts:
    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    allow_negative_stock: bool


@dataclass(frozen=True, slots=True)
class LocationFacts:
    """The bin columns the engine validates a movement's ``location_id`` against.

    ``location_id`` is a foreign key to ``storage_locations`` and nothing more:
    the constraint is company-blind and says nothing about which warehouse the
    bin is in. Without these three columns a movement could key an
    ``inventory_balances`` row - and a ledger row - on a bin belonging to another
    tenant, or to a different warehouse than the movement itself, which is a
    stock position no warehouse view will ever show.
    """

    id: uuid.UUID
    code: str
    warehouse_id: uuid.UUID
    is_active: bool


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """The lot facts the movement engine validates against.

    Separate from the ``(item_id, batch_number)`` projection used by the
    posting callers because the engine needs the lifecycle state and expiry,
    and the callers must keep working unchanged.
    """

    item_id: uuid.UUID
    batch_number: str
    status: BatchStatus
    expires_on: date | None
    received_on: date | None


@dataclass(frozen=True, slots=True)
class BatchQuantityTotals:
    on_hand: Decimal
    available: Decimal
    in_transit: Decimal
    job_work: Decimal
    scrap: Decimal

    @property
    def owned(self) -> Decimal:
        """Every unit the company still owns, wherever it is."""
        return self.on_hand + self.in_transit + self.job_work + self.scrap


@dataclass(frozen=True, slots=True)
class FefoBatch:
    batch_id: uuid.UUID
    expires_on: date | None
    received_on: date | None
    available_quantity: Decimal


# -----------------------------------------------------------------------------
# Balances
# -----------------------------------------------------------------------------


class InventoryRepository(BaseRepository[InventoryBalance]):
    """Balance reads, and the locking primitive every write depends on."""

    model = InventoryBalance
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "on_hand_quantity": InventoryBalance.on_hand_quantity,
        "available_quantity": InventoryBalance.available_quantity,
        "stock_value": InventoryBalance.stock_value,
        "last_movement_at": InventoryBalance.last_movement_at,
        "updated_at": InventoryBalance.updated_at,
    }

    # --- the concurrency primitive -----------------------------------------

    async def lock_balances(
        self, grains: Iterable[BalanceGrain]
    ) -> dict[BalanceGrain, InventoryBalance]:
        """Create missing balance rows, then lock every one of them for update.

        Returns the locked rows keyed by grain, with quantities re-read *after*
        the lock was acquired. A value read before the lock is worthless, which
        is why ``populate_existing`` is set: without it SQLAlchemy would hand
        back whatever version of the row is already in the identity map and the
        re-read would silently be a no-op.
        """
        ordered = sorted(set(grains), key=lambda g: g.sort_key)
        if not ordered:
            return {}

        ids = await self._upsert_grains(ordered)

        stmt = (
            select(InventoryBalance)
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.id.in_(ids),
            )
            # Primary-key order: two transactions that touch an overlapping set
            # of positions queue behind each other in the same sequence instead
            # of deadlocking head to head.
            .order_by(InventoryBalance.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())

        return {
            BalanceGrain(
                item_id=row.item_id,
                warehouse_id=row.warehouse_id,
                location_id=row.location_id,
                batch_id=row.batch_id,
            ): row
            for row in rows
        }

    async def _upsert_grains(self, grains: Sequence[BalanceGrain]) -> list[uuid.UUID]:
        """Create-if-missing for every grain, in one statement.

        ``ON CONFLICT ... DO UPDATE`` rather than ``DO NOTHING`` because the
        statement has to return the id of rows that already existed as well as
        the ones it inserted; ``DO NOTHING`` returns nothing for a conflict and
        would need a second query to find them.

        The grains are pre-sorted and de-duplicated by the caller. Both matter:
        PostgreSQL rejects an ``ON CONFLICT DO UPDATE`` that would touch the same
        row twice in one statement, and the order is the lock acquisition order.
        """
        params: dict[str, Any] = {
            "company_id": self.tenant.company_id,
            # A scheduled job's placeholder actor is stored as NULL, never as a
            # foreign key to a user that does not exist.
            "user_id": persisted_actor(self.tenant.user_id),
            "now": utcnow(),
        }
        rows_sql: list[str] = []
        for position, grain in enumerate(grains):
            params[f"id_{position}"] = new_id()
            params[f"item_{position}"] = grain.item_id
            params[f"warehouse_{position}"] = grain.warehouse_id
            params[f"location_{position}"] = grain.location_id
            params[f"batch_{position}"] = grain.batch_id
            # Explicit casts: a bare NULL parameter gives asyncpg nothing to
            # infer a type from and it refuses rather than guessing. Spelled
            # CAST(... AS uuid) rather than `::uuid`, because SQLAlchemy's
            # text() parser skips a parameter that is immediately followed by a
            # colon - it reads `:id::uuid` as a cast, not as a bind parameter.
            rows_sql.append(
                f"(CAST(:id_{position} AS uuid), CAST(:company_id AS uuid), "
                f"CAST(:item_{position} AS uuid), CAST(:warehouse_{position} AS uuid), "
                f"CAST(:location_{position} AS uuid), CAST(:batch_{position} AS uuid), "
                f":now, :now, CAST(:user_id AS uuid))"
            )

        # S608 is a false positive here: every value is a bind parameter, the
        # VALUES skeleton is built from an integer index, and BALANCE_GRAIN_SQL
        # is a module constant. No caller-supplied string reaches the SQL text.
        statement = text(
            "INSERT INTO inventory_balances "  # noqa: S608
            "(id, company_id, item_id, warehouse_id, location_id, batch_id, "
            " created_at, updated_at, created_by_id) "
            f"VALUES {', '.join(rows_sql)} "
            f"ON CONFLICT ({BALANCE_GRAIN_SQL}) "
            "DO UPDATE SET updated_at = EXCLUDED.updated_at "
            "RETURNING id"
        )
        result = await self.session.execute(statement, params)
        return [row[0] for row in result.fetchall()]

    # --- reads --------------------------------------------------------------

    async def get_position(
        self,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        *,
        location_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> InventoryBalance | None:
        """One stock position. ``IS NOT DISTINCT FROM`` matches the grain index."""
        stmt = select(InventoryBalance).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.item_id == item_id,
            InventoryBalance.warehouse_id == warehouse_id,
            InventoryBalance.location_id.is_not_distinct_from(location_id),
            InventoryBalance.batch_id.is_not_distinct_from(batch_id),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def positions_for(
        self, grains: Sequence[BalanceGrain]
    ) -> dict[BalanceGrain, InventoryBalance]:
        """Read (without locking) the balance rows for a set of positions.

        For estimating a document's value before it is posted. One query for the
        whole document: the filter is by item and warehouse, and the exact grain
        match is done in Python, because a per-grain OR of 500 clauses is a worse
        query plan than a narrow index scan plus a dictionary lookup.
        """
        if not grains:
            return {}
        item_ids = {g.item_id for g in grains}
        warehouse_ids = {g.warehouse_id for g in grains}
        stmt = select(InventoryBalance).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.item_id.in_(item_ids),
            InventoryBalance.warehouse_id.in_(warehouse_ids),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        found = {
            BalanceGrain(
                item_id=row.item_id,
                warehouse_id=row.warehouse_id,
                location_id=row.location_id,
                batch_id=row.batch_id,
            ): row
            for row in rows
        }
        return {grain: found[grain] for grain in grains if grain in found}

    async def item_totals(self, item_id: uuid.UUID) -> dict[str, Decimal]:
        """Company-wide totals for one item, across every warehouse.

        Separate from the paginated per-warehouse breakdown on purpose: a total
        that only covered the current page would change as the user pages, which
        is worse than no total at all.
        """
        columns = (
            InventoryBalance.on_hand_quantity,
            InventoryBalance.reserved_quantity,
            InventoryBalance.available_quantity,
            InventoryBalance.in_transit_quantity,
            InventoryBalance.job_work_quantity,
            InventoryBalance.scrap_quantity,
            InventoryBalance.stock_value,
        )
        stmt = select(*[func.coalesce(func.sum(column), 0) for column in columns]).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.item_id == item_id,
            # Scoped for the same reason the per-warehouse breakdown below is:
            # a total that includes warehouses the reader cannot see would not
            # add up to the rows underneath it.
            *warehouse_scope(self.tenant, InventoryBalance.warehouse_id),
        )
        row = (await self.session.execute(stmt)).one()
        return {column.key: Decimal(str(value)) for column, value in zip(columns, row, strict=True)}

    async def sum_available(
        self, item_id: uuid.UUID, warehouse_id: uuid.UUID | None = None
    ) -> Decimal:
        """Total promisable quantity. Read from the table every time (section 34)."""
        stmt = select(func.coalesce(func.sum(InventoryBalance.available_quantity), 0)).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.item_id == item_id,
        )
        if warehouse_id is not None:
            stmt = stmt.where(InventoryBalance.warehouse_id == warehouse_id)
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))

    def build_list_stmt(
        self, filters: BalanceFilters, sort: SortParams | None = None
    ) -> Select[tuple[InventoryBalance]]:
        stmt = select(InventoryBalance).where(
            InventoryBalance.company_id == self.tenant.company_id,
            *warehouse_scope(self.tenant, InventoryBalance.warehouse_id),
        )

        if filters.item_id is not None:
            stmt = stmt.where(InventoryBalance.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(InventoryBalance.warehouse_id == filters.warehouse_id)
        if filters.location_id is not None:
            stmt = stmt.where(InventoryBalance.location_id == filters.location_id)
        if filters.batch_id is not None:
            stmt = stmt.where(InventoryBalance.batch_id == filters.batch_id)
        if filters.only_in_stock:
            stmt = stmt.where(InventoryBalance.on_hand_quantity > 0)

        # The reorder and search filters are the only reasons to touch items, so
        # the join is conditional: an unfiltered stock list must not pay for it.
        if filters.below_reorder or filters.search:
            stmt = stmt.join(Item, Item.id == InventoryBalance.item_id)
            if filters.below_reorder:
                # An item with no reorder level configured is never "below" it -
                # otherwise every zero position in the company reports as short.
                stmt = stmt.where(
                    Item.reorder_level > 0,
                    InventoryBalance.available_quantity <= Item.reorder_level,
                )
            if filters.search:
                pattern = f"%{filters.search.strip()}%"
                stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))

        default_order = (InventoryBalance.updated_at.desc(), InventoryBalance.id.desc())
        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, InventoryBalance.id.desc()))
        return stmt.order_by(*default_order)

    async def warehouse_summary(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[list[Any], OffsetPagination]:
        """Stock of one item per warehouse, paginated.

        Grouped in SQL rather than by loading every position and folding them in
        Python: a heavily binned item can have hundreds of balance rows per
        warehouse, and none of them are interesting on their own here.
        """
        grouped = (
            select(
                InventoryBalance.warehouse_id.label("warehouse_id"),
                Warehouse.code.label("warehouse_code"),
                Warehouse.name.label("warehouse_name"),
                func.sum(InventoryBalance.on_hand_quantity).label("on_hand_quantity"),
                func.sum(InventoryBalance.reserved_quantity).label("reserved_quantity"),
                func.sum(InventoryBalance.available_quantity).label("available_quantity"),
                func.sum(InventoryBalance.in_transit_quantity).label("in_transit_quantity"),
                func.sum(InventoryBalance.qc_hold_quantity).label("qc_hold_quantity"),
                func.sum(InventoryBalance.damaged_quantity).label("damaged_quantity"),
                func.sum(InventoryBalance.job_work_quantity).label("job_work_quantity"),
                func.sum(InventoryBalance.scrap_quantity).label("scrap_quantity"),
                func.sum(InventoryBalance.stock_value).label("stock_value"),
            )
            .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.item_id == item_id,
                *warehouse_scope(self.tenant, InventoryBalance.warehouse_id),
            )
            .group_by(InventoryBalance.warehouse_id, Warehouse.code, Warehouse.name)
            .order_by(Warehouse.code)
        )

        # paginate() in core is built for entity selects; a grouped projection
        # needs its own window, but the endpoint stays bounded either way.
        rows = list(
            (
                await self.session.execute(grouped.limit(params.limit + 1).offset(params.offset))
            ).all()
        )
        has_next = len(rows) > params.limit
        rows = rows[: params.limit]

        total: int | None = None
        total_pages: int | None = None
        if params.with_total:
            count_stmt = select(func.count()).select_from(grouped.order_by(None).subquery())
            total = int((await self.session.execute(count_stmt)).scalar_one())
            total_pages = max(1, -(-total // params.limit))

        return rows, OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )

    async def positions_for_count(
        self,
        *,
        warehouse_id: uuid.UUID,
        location_id: uuid.UUID | None,
        item_ids: Sequence[uuid.UUID],
        include_zero: bool,
        limit: int,
    ) -> list[InventoryBalance]:
        """Positions to snapshot onto a count sheet.

        Bounded by ``limit`` so an unscoped count of a large warehouse fails with
        a clear message instead of building a sheet nobody can work through.
        """
        stmt = select(InventoryBalance).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.warehouse_id == warehouse_id,
            *warehouse_scope(self.tenant, InventoryBalance.warehouse_id),
        )
        if location_id is not None:
            stmt = stmt.where(InventoryBalance.location_id == location_id)
        if item_ids:
            stmt = stmt.where(InventoryBalance.item_id.in_(list(item_ids)))
        if not include_zero:
            stmt = stmt.where(InventoryBalance.on_hand_quantity > 0)

        stmt = stmt.order_by(
            InventoryBalance.item_id,
            InventoryBalance.location_id,
            InventoryBalance.batch_id,
        ).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    # --- master-data lookups used by the write path ------------------------

    async def item_facts(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ItemFacts]:
        """Batch-load the item columns the engine validates against."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.is_active,
            Item.standard_cost,
            Item.reorder_level,
            Item.can_scrap,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        return {
            row.id: ItemFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                is_active=row.is_active,
                standard_cost=row.standard_cost,
                reorder_level=row.reorder_level,
                can_scrap=row.can_scrap,
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def batch_facts(self, batch_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, BatchFacts]:
        """The lifecycle facts behind a set of lots, in one query."""
        wanted = list(dict.fromkeys(batch_ids))
        if not wanted:
            return {}
        rows = await self.session.execute(
            select(
                StockBatch.id,
                StockBatch.item_id,
                StockBatch.batch_number,
                StockBatch.status,
                StockBatch.expires_on,
                StockBatch.received_on,
            ).where(
                StockBatch.company_id == self.tenant.company_id,
                StockBatch.id.in_(wanted),
            )
        )
        return {
            row.id: BatchFacts(
                item_id=row.item_id,
                batch_number=row.batch_number,
                status=BatchStatus(row.status),
                expires_on=row.expires_on,
                received_on=row.received_on,
            )
            for row in rows
        }

    async def batch_totals(
        self, batch_ids: Sequence[uuid.UUID], *, scoped: bool = True
    ) -> dict[uuid.UUID, BatchQuantityTotals]:
        """Current quantities per lot, including stock that has left the shelf.

        ``scoped=False`` is for the lifecycle decision, which is about the
        company's whole ownership of the lot. A warehouse-limited user issuing
        the last unit they can see must not mark a lot consumed while another
        warehouse still holds it.
        """
        wanted = list(dict.fromkeys(batch_ids))
        if not wanted:
            return {}
        rows = await self.session.execute(
            select(
                InventoryBalance.batch_id,
                func.coalesce(func.sum(InventoryBalance.on_hand_quantity), 0),
                func.coalesce(func.sum(InventoryBalance.available_quantity), 0),
                func.coalesce(func.sum(InventoryBalance.in_transit_quantity), 0),
                func.coalesce(func.sum(InventoryBalance.job_work_quantity), 0),
                func.coalesce(func.sum(InventoryBalance.scrap_quantity), 0),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.batch_id.in_(wanted),
                *(warehouse_scope(self.tenant, InventoryBalance.warehouse_id) if scoped else ()),
            )
            .group_by(InventoryBalance.batch_id)
        )
        return {
            row[0]: BatchQuantityTotals(
                on_hand=row[1], available=row[2], in_transit=row[3], job_work=row[4], scrap=row[5]
            )
            for row in rows
            if row[0] is not None
        }

    async def batch_available_positions(self, batch_id: uuid.UUID) -> list[InventoryBalance]:
        """Every position holding available quantity of one lot.

        Used by quarantine: all of it moves to QC hold, not just the position
        the operator happened to open. Scoped like a read, because the action
        may only move stock in the warehouses the principal can see.
        """
        stmt = select(InventoryBalance).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.batch_id == batch_id,
            InventoryBalance.available_quantity > 0,
            *warehouse_scope(self.tenant, InventoryBalance.warehouse_id),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def fefo_candidates(
        self,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        location_id: uuid.UUID | None,
        minimum_quantity: Decimal,
        as_of: date,
    ) -> list[FefoBatch]:
        """Lots that can cover a pick, oldest expiry first.

        A single lot must cover the whole movement: splitting one movement
        across several balances would change the cost layers it draws from
        under a caller that asked for one line. When no single lot is enough
        the caller gets nothing, and the ordinary shortfall error explains why.
        """
        stmt = (
            select(
                InventoryBalance.batch_id,
                StockBatch.expires_on,
                StockBatch.received_on,
                InventoryBalance.available_quantity,
            )
            .join(StockBatch, StockBatch.id == InventoryBalance.batch_id)
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.item_id == item_id,
                InventoryBalance.warehouse_id == warehouse_id,
                InventoryBalance.batch_id.is_not(None),
                InventoryBalance.available_quantity >= minimum_quantity,
                StockBatch.status == BatchStatus.ACTIVE,
                (StockBatch.expires_on.is_(None)) | (StockBatch.expires_on >= as_of),
            )
            .order_by(
                StockBatch.expires_on.asc().nulls_last(),
                StockBatch.received_on.asc(),
                StockBatch.id.asc(),
            )
        )
        if location_id is None:
            stmt = stmt.where(InventoryBalance.location_id.is_(None))
        else:
            stmt = stmt.where(InventoryBalance.location_id == location_id)
        rows = await self.session.execute(stmt)
        return [
            FefoBatch(
                batch_id=row[0],
                expires_on=row[1],
                received_on=row[2],
                available_quantity=row[3],
            )
            for row in rows
        ]

    async def warehouse_facts(
        self, warehouse_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, WarehouseFacts]:
        if not warehouse_ids:
            return {}
        stmt = select(
            Warehouse.id,
            Warehouse.code,
            Warehouse.name,
            Warehouse.is_active,
            Warehouse.allow_negative_stock,
        ).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.id.in_(list(dict.fromkeys(warehouse_ids))),
            Warehouse.deleted_at.is_(None),
        )
        return {
            row.id: WarehouseFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                is_active=row.is_active,
                allow_negative_stock=row.allow_negative_stock,
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def document_position_labels(
        self, location_ids: set[uuid.UUID], batch_ids: set[uuid.UUID]
    ) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
        # Historical document labels remain readable after a bin or batch is
        # retired. Scope both projections; never issue one query per line.
        locations: dict[uuid.UUID, str] = {}
        batches: dict[uuid.UUID, str] = {}
        if location_ids:
            rows = await self.session.execute(
                select(StorageLocation.id, StorageLocation.code).where(
                    StorageLocation.company_id == self.tenant.company_id,
                    StorageLocation.id.in_(location_ids),
                    *warehouse_scope(self.tenant, StorageLocation.warehouse_id),
                )
            )
            locations = {row.id: row.code for row in rows}
        if batch_ids:
            rows = await self.session.execute(
                select(StockBatch.id, StockBatch.batch_number).where(
                    StockBatch.company_id == self.tenant.company_id, StockBatch.id.in_(batch_ids)
                )
            )
            batches = {row.id: row.batch_number for row in rows}
        return locations, batches

    async def location_facts(
        self, location_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LocationFacts]:
        """Batch-load the bins a document names. One query, whatever the line count.

        Soft-deleted bins are excluded and the filter is tenant-scoped, so a
        location that belongs to another company - or to no live row at all -
        simply does not come back, and the caller reports it as not found. That
        is the same shape as :meth:`batch_owners`: absence is the answer, never a
        second round trip per line.
        """
        wanted = list(dict.fromkeys(location_ids))
        if not wanted:
            return {}
        stmt = select(
            StorageLocation.id,
            StorageLocation.code,
            StorageLocation.warehouse_id,
            StorageLocation.is_active,
        ).where(
            StorageLocation.company_id == self.tenant.company_id,
            StorageLocation.id.in_(wanted),
            StorageLocation.deleted_at.is_(None),
        )
        return {
            row.id: LocationFacts(
                id=row.id,
                code=row.code,
                warehouse_id=row.warehouse_id,
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def batch_owners(
        self, batch_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
        """``batch_id -> (item_id, batch_number)``, for cross-item validation."""
        if not batch_ids:
            return {}
        stmt = select(StockBatch.id, StockBatch.item_id, StockBatch.batch_number).where(
            StockBatch.company_id == self.tenant.company_id,
            StockBatch.id.in_(list(dict.fromkeys(batch_ids))),
        )
        return {
            row.id: (row.item_id, row.batch_number)
            for row in (await self.session.execute(stmt)).all()
        }

    async def serial_owners(
        self, serial_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
        """``serial_id -> (item_id, serial_number)``, for cross-item validation.

        A projection, and deliberately *not* locked. Both columns are immutable,
        so the engine can check every pure master-data rule - does the unit
        exist, does it belong to this tenant, is it a unit of the item being
        moved - before it takes its first lock. The unit's *status* is the
        opposite kind of value and is read under
        :meth:`StockSerialRepository.lock_serials`.

        Absence is the answer for a serial belonging to another company, exactly
        as in :meth:`batch_owners`: the filter is tenant-scoped, so a row from
        another tenant does not come back and the caller reports it as not
        found.
        """
        if not serial_ids:
            return {}
        stmt = select(StockSerial.id, StockSerial.item_id, StockSerial.serial_number).where(
            StockSerial.company_id == self.tenant.company_id,
            StockSerial.id.in_(list(dict.fromkeys(serial_ids))),
        )
        return {
            row.id: (row.item_id, row.serial_number)
            for row in (await self.session.execute(stmt)).all()
        }

    async def batch_numbers(self, batch_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        owners = await self.batch_owners(batch_ids)
        return {batch_id: number for batch_id, (_, number) in owners.items()}


# -----------------------------------------------------------------------------
# Ledger
# -----------------------------------------------------------------------------


class LedgerRepository(BaseRepository[InventoryTransaction]):
    """Append-only movement history. Read paths only - writes go through the engine."""

    model = InventoryTransaction

    async def for_serial(
        self, serial_id: uuid.UUID, *, limit: int = 500
    ) -> list[InventoryTransaction]:
        """A unit's whole journey, newest first."""
        stmt = (
            select(InventoryTransaction)
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.serial_id == serial_id,
                *warehouse_scope(self.tenant, InventoryTransaction.warehouse_id),
            )
            .order_by(
                InventoryTransaction.occurred_at.desc(),
                InventoryTransaction.created_at.desc(),
                InventoryTransaction.id.desc(),
            )
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def by_ids(
        self, transaction_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, InventoryTransaction]:
        """Load outbound rows by id so a multi-serial receipt can price each unit.

        The rows are returned in the same order as the ids the caller supplied,
        because a transfer line's ``dispatch_transaction_ids`` array is aligned
        with its ``serial_ids``.
        """
        wanted = list(dict.fromkeys(transaction_ids))
        if not wanted:
            return {}
        stmt = select(InventoryTransaction).where(
            InventoryTransaction.company_id == self.tenant.company_id,
            InventoryTransaction.id.in_(wanted),
        )
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    def build_list_stmt(self, filters: LedgerFilters) -> Select[tuple[InventoryTransaction]]:
        stmt = select(InventoryTransaction).where(
            InventoryTransaction.company_id == self.tenant.company_id,
            *warehouse_scope(self.tenant, InventoryTransaction.warehouse_id),
        )
        if filters.item_id is not None:
            stmt = stmt.where(InventoryTransaction.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(InventoryTransaction.warehouse_id == filters.warehouse_id)
        if filters.batch_id is not None:
            stmt = stmt.where(InventoryTransaction.batch_id == filters.batch_id)
        if filters.serial_id is not None:
            stmt = stmt.where(InventoryTransaction.serial_id == filters.serial_id)
        if filters.transaction_type is not None:
            stmt = stmt.where(InventoryTransaction.transaction_type == filters.transaction_type)
        if filters.stock_state is not None:
            stmt = stmt.where(InventoryTransaction.stock_state == filters.stock_state)
        if filters.reference_type is not None:
            stmt = stmt.where(InventoryTransaction.reference_type == filters.reference_type)
        if filters.reference_id is not None:
            stmt = stmt.where(InventoryTransaction.reference_id == filters.reference_id)
        if filters.occurred_from is not None:
            stmt = stmt.where(InventoryTransaction.occurred_at >= filters.occurred_from)
        if filters.occurred_to is not None:
            stmt = stmt.where(InventoryTransaction.occurred_at <= filters.occurred_to)
        return stmt

    async def received_serial_ids(self, serial_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these units the ledger already holds a stock receipt for.

        The one fact that separates a serial row registered from a packing list,
        which has never been received and whose first receipt is legal, from one
        that is ``IN_STOCK`` because a receipt has already been posted against
        it. The unit's own status cannot tell them apart - both are ``IN_STOCK``
        - and neither can its recorded position, because a registration may
        state where the unit is expected to land.

        A "stock receipt" is any movement type the serial lifecycle maps onto
        ``IN_STOCK``: exactly the movements that book a unit onto a shelf. One
        query for the whole batch, on ``ix_inventory_transactions_serial``, and
        ``DISTINCT`` so a unit with a long history costs one row rather than
        one per movement.
        """
        # Deferred: the service imports this module, so the lifecycle table
        # cannot be imported at module scope without a cycle.
        from src.modules.inventory.service import SERIAL_MOVEMENT_TARGETS

        wanted = list(dict.fromkeys(serial_ids))
        if not wanted:
            return set()
        receipt_types = [
            str(transaction_type)
            for transaction_type, target in SERIAL_MOVEMENT_TARGETS.items()
            if target is SerialStatus.IN_STOCK
        ]
        stmt = (
            select(InventoryTransaction.serial_id)
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.serial_id.in_(wanted),
                InventoryTransaction.transaction_type.in_(receipt_types),
            )
            .distinct()
        )
        return {row[0] for row in (await self.session.execute(stmt)).all()}

    async def posted_quantity(
        self,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        location_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> Decimal:
        """Net on-hand change the ledger accounts for at one position.

        The reconciliation identity: this must equal ``on_hand_quantity`` on the
        matching balance row.

        A movement is included only when it actually changed on-hand, and that
        is decided by re-running the engine's own ``movement_effect`` over the
        three columns the row stores - type, target pool, source pool - not by
        the transaction type alone. Filtering on the type is wrong: a material
        issue with ``stock_state=JOB_WORK`` draws the job-work pool down and
        leaves on-hand untouched, and scrap raised against stock already at a
        job-work vendor does the same, yet both carry a signed quantity and a
        type outside ``ON_HAND_NEUTRAL_TYPES``. Counting them made the identity
        drift by the whole job-worked quantity - permanently, and in a direction
        that invites someone to "correct" a balance that was right all along.

        Grouped in SQL, so the number of rows brought back is the number of
        distinct movement shapes at the position (a handful), not the number of
        movements. This is a *reconciliation* query, run by a maintenance job or
        a test; current stock is never computed this way on a request path.
        """
        # Deferred: the service imports this module, so the effect table cannot
        # be imported at module scope without a cycle.
        from src.modules.inventory.service import movement_effect

        stmt = (
            select(
                InventoryTransaction.transaction_type,
                InventoryTransaction.stock_state,
                InventoryTransaction.source_state,
                func.coalesce(func.sum(InventoryTransaction.quantity), 0).label("net"),
            )
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.item_id == item_id,
                InventoryTransaction.warehouse_id == warehouse_id,
                InventoryTransaction.location_id.is_not_distinct_from(location_id),
                InventoryTransaction.batch_id.is_not_distinct_from(batch_id),
            )
            .group_by(
                InventoryTransaction.transaction_type,
                InventoryTransaction.stock_state,
                InventoryTransaction.source_state,
            )
        )

        total = Decimal("0")
        for transaction_type, stock_state, source_state, net in (
            await self.session.execute(stmt)
        ).all():
            effect = movement_effect(transaction_type, stock_state, source_state)
            if effect.on_hand_quantity:
                total += Decimal(str(net))
        return total


# -----------------------------------------------------------------------------
# FIFO cost layers
# -----------------------------------------------------------------------------


class CostLayerRepository(BaseRepository[InventoryCostLayer]):
    """The FIFO layer store, and the second lock the write path takes.

    Consuming a layer is a read-modify-write on ``remaining_quantity``, which is
    exactly the shape of bug the balance row lock exists to stop: two issues
    both read 20 remaining, both decide they may take it, and 40 units of cost
    leave a layer that only ever held 20. The balance row lock already
    serialises two postings at the same position, so this is the belt to that
    pair of braces - but it is not redundant. It also forces the *re-read*
    (``populate_existing``), without which SQLAlchemy would hand back whatever
    version of the layer is already in the session's identity map and the whole
    acquisition would be a no-op.

    Lock order is fixed and must stay fixed: balances first, then layers, then
    serials, each set ordered by primary key. Every path through
    :class:`~src.modules.inventory.service.MovementEngine` takes them in that
    order, so two postings touching an overlapping set queue behind each other
    instead of holding one set each and waiting for the other.
    """

    model = InventoryCostLayer

    async def lock_open_layers(
        self, balance_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[InventoryCostLayer]]:
        """Lock and re-read every open layer at these positions, in FIFO order.

        One query for the whole batch, keyed on ``balance_id`` rather than on a
        rebuilt grain tuple: the balance row *is* the grain, so this cannot
        return a neighbouring position's layers the way an item/warehouse filter
        with the exact match done in Python could.

        Both of a position's queues come back - the on-hand queue and the scrap
        queue (``pool``, see :class:`~src.modules.inventory.models.CostLayerPool`)
        - in one acquisition, so the lock order stays a single primary-key
        sequence. The engine splits them before anything is costed.

        ``ORDER BY id`` is the *lock acquisition* order and is deliberately not
        the FIFO order - primary key, like the balance lock, so the acquisition
        sequence is the same for every transaction. The rows come back sorted by
        ``(received_at, id)``, which is the consumption order: business time
        first, and the UUIDv7 primary key as the tiebreaker, because layers
        opened by one document all carry the same timestamp and ordering on it
        alone would consume them arbitrarily.
        """
        wanted = list(dict.fromkeys(balance_ids))
        if not wanted:
            return {}
        stmt = (
            select(InventoryCostLayer)
            .where(
                InventoryCostLayer.company_id == self.tenant.company_id,
                InventoryCostLayer.balance_id.in_(wanted),
                InventoryCostLayer.remaining_quantity > 0,
            )
            .order_by(InventoryCostLayer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())

        layers: dict[uuid.UUID, list[InventoryCostLayer]] = {
            balance_id: [] for balance_id in wanted
        }
        for row in rows:
            layers[row.balance_id].append(row)
        for open_layers in layers.values():
            open_layers.sort(key=fifo_sort_key)
        return layers

    async def open_layers_for_balances(
        self,
        balance_ids: Sequence[uuid.UUID],
        *,
        pool: CostLayerPool = CostLayerPool.ON_HAND,
    ) -> dict[uuid.UUID, list[InventoryCostLayer]]:
        """The open layers at a set of positions, in FIFO order, without a lock.

        One query for a whole document, which is what makes a FIFO cost preview
        affordable: a stock adjustment or a count sheet prices every line off
        the layers it will really consume, and a query per line would be exactly
        the N+1 section 32 forbids.

        Lock-free on purpose, and only ever for an *estimate*. A layer read
        without the lock is a quantity another transaction may already be
        spending, so nothing that decides what to consume may use this - the
        posting path locks through :meth:`lock_open_layers` and re-checks the
        number it actually posted.
        """
        wanted = list(dict.fromkeys(balance_ids))
        if not wanted:
            return {}
        stmt = select(InventoryCostLayer).where(
            InventoryCostLayer.company_id == self.tenant.company_id,
            InventoryCostLayer.balance_id.in_(wanted),
            InventoryCostLayer.remaining_quantity > 0,
            InventoryCostLayer.pool == pool,
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        layers: dict[uuid.UUID, list[InventoryCostLayer]] = {
            balance_id: [] for balance_id in wanted
        }
        for row in rows:
            layers[row.balance_id].append(row)
        for open_layers in layers.values():
            open_layers.sort(key=fifo_sort_key)
        return layers

    async def open_layers_for_balance(
        self, balance_id: uuid.UUID, *, pool: CostLayerPool = CostLayerPool.ON_HAND
    ) -> list[InventoryCostLayer]:
        """The open layers of one of a position's queues, in FIFO order, without a lock.

        For reads - a stock-ageing view, a valuation drill-down, a test checking
        what the engine left behind. Never use this to decide what to consume:
        a layer read without the lock is a value another transaction may already
        be spending.
        """
        stmt = select(InventoryCostLayer).where(
            InventoryCostLayer.company_id == self.tenant.company_id,
            InventoryCostLayer.balance_id == balance_id,
            InventoryCostLayer.remaining_quantity > 0,
            InventoryCostLayer.pool == pool,
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        rows.sort(key=fifo_sort_key)
        return rows


class CostConsumptionRepository(BaseRepository[InventoryCostLayerConsumption]):
    """Which layers each outbound movement drew on. Append-only."""

    model = InventoryCostLayerConsumption

    async def by_transaction(
        self, transaction_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[InventoryCostLayerConsumption]]:
        """The per-layer cost breakdown of a set of movements, in FIFO order.

        One query for the whole set - a stock transfer receives every line at
        once, and a query per line would be exactly the N+1 section 32 forbids.
        Ordered by ``(created_at, id)``: the rows of one movement are written in
        one transaction and therefore share a ``created_at``, so the UUIDv7
        primary key is what recovers the order the layers were actually drawn
        in. Getting that wrong would re-open a transfer's destination layers in
        the wrong sequence, and the *next* issue at the destination would be
        costed from the wrong end of the queue.
        """
        wanted = list(dict.fromkeys(transaction_ids))
        if not wanted:
            return {}
        stmt = (
            select(InventoryCostLayerConsumption)
            .where(
                InventoryCostLayerConsumption.company_id == self.tenant.company_id,
                InventoryCostLayerConsumption.transaction_id.in_(wanted),
            )
            .order_by(
                InventoryCostLayerConsumption.created_at,
                InventoryCostLayerConsumption.id,
            )
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        breakdown: dict[uuid.UUID, list[InventoryCostLayerConsumption]] = {}
        for row in rows:
            breakdown.setdefault(row.transaction_id, []).append(row)
        return breakdown


# -----------------------------------------------------------------------------
# Documents
# -----------------------------------------------------------------------------


class StockTransferRepository(BaseRepository[StockTransfer]):
    model = StockTransfer

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[StockTransfer]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(
                *warehouse_scope(
                    self.tenant, StockTransfer.from_warehouse_id, StockTransfer.to_warehouse_id
                )
            )
        )

    searchable_fields = ("code", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": StockTransfer.code,
        "transfer_date": StockTransfer.transfer_date,
        "status": StockTransfer.status,
        "created_at": StockTransfer.created_at,
    }

    def build_list_stmt(
        self, filters: TransferFilters, sort: SortParams | None = None
    ) -> Select[tuple[StockTransfer]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(StockTransfer.status == filters.status)
        if filters.from_warehouse_id is not None:
            stmt = stmt.where(StockTransfer.from_warehouse_id == filters.from_warehouse_id)
        if filters.to_warehouse_id is not None:
            stmt = stmt.where(StockTransfer.to_warehouse_id == filters.to_warehouse_id)
        if filters.date_from is not None:
            stmt = stmt.where(StockTransfer.transfer_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(StockTransfer.transfer_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, StockTransfer.id.desc()))
        return stmt.order_by(StockTransfer.transfer_date.desc(), StockTransfer.id.desc())

    async def lock_for_update(self, transfer_id: uuid.UUID) -> StockTransfer | None:
        """Take the header's row lock before reading its status or its lines.

        Approving, dispatching, receiving and cancelling are all read-modify-write
        sequences: they read the status, decide a transition from it, and post
        stock. Without this lock two simultaneous dispatch requests both read
        APPROVED, both pass ``assert_transition`` and both post a TRANSFER_OUT -
        the balance row locks serialise the two postings but do nothing to stop
        the second one happening. The header is the serialisation point for the
        whole document, and it is always taken *before* any balance lock so the
        acquisition order is the same in every code path.

        ``populate_existing`` for the same reason as ``lock_balances``: the
        status has to be re-read from the database after the lock, not served
        from the session's identity map.
        """
        stmt = (
            self._scoped()
            .where(StockTransfer.id == transfer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_lines(self, transfer_id: uuid.UUID) -> StockTransfer | None:
        """Header plus lines in two queries, never one per line."""
        stmt = (
            self._scoped()
            .where(StockTransfer.id == transfer_id)
            .options(selectinload(StockTransfer.lines))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()


class StockAdjustmentRepository(BaseRepository[StockAdjustment]):
    model = StockAdjustment

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[StockAdjustment]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*warehouse_scope(self.tenant, StockAdjustment.warehouse_id))
        )

    searchable_fields = ("code", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": StockAdjustment.code,
        "adjustment_date": StockAdjustment.adjustment_date,
        "status": StockAdjustment.status,
        "total_value": StockAdjustment.total_value,
        "created_at": StockAdjustment.created_at,
    }

    def build_list_stmt(
        self, filters: AdjustmentFilters, sort: SortParams | None = None
    ) -> Select[tuple[StockAdjustment]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(StockAdjustment.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(StockAdjustment.warehouse_id == filters.warehouse_id)
        if filters.reason_code is not None:
            stmt = stmt.where(StockAdjustment.reason_code == filters.reason_code)
        if filters.date_from is not None:
            stmt = stmt.where(StockAdjustment.adjustment_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(StockAdjustment.adjustment_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, StockAdjustment.id.desc()))
        return stmt.order_by(StockAdjustment.adjustment_date.desc(), StockAdjustment.id.desc())

    async def lock_for_update(self, adjustment_id: uuid.UUID) -> StockAdjustment | None:
        """Row lock on the header, taken before the status is read.

        Posting is the reason this exists: two simultaneous post requests both
        read APPROVED, both pass ``assert_transition`` and both write the whole
        document to the ledger, doubling the write-off. See
        :meth:`StockTransferRepository.lock_for_update`.
        """
        stmt = (
            self._scoped()
            .where(StockAdjustment.id == adjustment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_lines(self, adjustment_id: uuid.UUID) -> StockAdjustment | None:
        stmt = (
            self._scoped()
            .where(StockAdjustment.id == adjustment_id)
            .options(selectinload(StockAdjustment.lines))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()


class PhysicalCountRepository(BaseRepository[PhysicalCount]):
    model = PhysicalCount

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[PhysicalCount]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*warehouse_scope(self.tenant, PhysicalCount.warehouse_id))
        )

    searchable_fields = ("code", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": PhysicalCount.code,
        "count_date": PhysicalCount.count_date,
        "status": PhysicalCount.status,
        "created_at": PhysicalCount.created_at,
    }

    def build_list_stmt(
        self, filters: PhysicalCountFilters, sort: SortParams | None = None
    ) -> Select[tuple[PhysicalCount]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(PhysicalCount.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(PhysicalCount.warehouse_id == filters.warehouse_id)
        if filters.date_from is not None:
            stmt = stmt.where(PhysicalCount.count_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(PhysicalCount.count_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, PhysicalCount.id.desc()))
        return stmt.order_by(PhysicalCount.count_date.desc(), PhysicalCount.id.desc())

    async def lock_for_update(self, count_id: uuid.UUID) -> PhysicalCount | None:
        """Row lock on the sheet header before its status or its lines are read.

        Two count-entry requests running at once would each compute "how many
        lines are still uncounted" from a snapshot that does not include the
        other's writes, so a sheet whose last two lines are entered
        simultaneously never reaches COUNTED and can no longer be confirmed.
        Confirming twice would post the variance twice. See
        :meth:`StockTransferRepository.lock_for_update`.
        """
        stmt = (
            self._scoped()
            .where(PhysicalCount.id == count_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def open_count_exists(
        self, warehouse_id: uuid.UUID, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        """Is a sheet already open for this warehouse?

        Two concurrent open counts on the same warehouse would each snapshot the
        other's variance postings and fight over the same positions.
        """
        stmt = (
            select(PhysicalCount.id)
            .where(
                PhysicalCount.company_id == self.tenant.company_id,
                PhysicalCount.warehouse_id == warehouse_id,
                PhysicalCount.status.in_(
                    [
                        str(PhysicalCountStatus.DRAFT),
                        str(PhysicalCountStatus.IN_PROGRESS),
                        str(PhysicalCountStatus.COUNTED),
                    ]
                ),
            )
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(PhysicalCount.id != exclude_id)
        return (await self.session.execute(stmt)).first() is not None


class StockBatchRepository(BaseRepository[StockBatch]):
    model = StockBatch
    searchable_fields = ("batch_number", "supplier_batch_number")

    async def load(self, batch_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, StockBatch]:
        if not batch_ids:
            return {}
        stmt = self._scoped().where(StockBatch.id.in_(list(dict.fromkeys(batch_ids))))
        return {batch.id: batch for batch in (await self.session.execute(stmt)).scalars().all()}

    async def by_number(self, item_id: uuid.UUID, batch_number: str) -> StockBatch | None:
        stmt = self._scoped().where(
            StockBatch.item_id == item_id, StockBatch.batch_number == batch_number
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_update(self, batch_id: uuid.UUID) -> StockBatch | None:
        stmt = (
            self._scoped()
            .where(StockBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def build_list_stmt(
        self,
        *,
        item_id: uuid.UUID | None,
        search: str | None,
        batch_id: uuid.UUID | None = None,
        status: BatchStatus | None = None,
        expiry_from: date | None = None,
        expiry_to: date | None = None,
    ) -> Select[tuple[StockBatch]]:
        stmt = self._scoped()
        if item_id is not None:
            stmt = stmt.where(StockBatch.item_id == item_id)
        if batch_id is not None:
            stmt = stmt.where(StockBatch.id == batch_id)
        if status is not None:
            stmt = stmt.where(StockBatch.status == status)
        if expiry_from is not None:
            stmt = stmt.where(StockBatch.expires_on >= expiry_from)
        if expiry_to is not None:
            stmt = stmt.where(StockBatch.expires_on <= expiry_to)
        stmt = self._apply_search(stmt, search)
        # FEFO order, with undated lots last: the tracker's default is the
        # order a picker should work in, not the order rows were created.
        return stmt.order_by(
            StockBatch.expires_on.asc().nulls_last(),
            StockBatch.received_on.asc(),
            StockBatch.id.asc(),
        )

    async def companies_with_batches(self) -> list[uuid.UUID]:
        """Companies the expiry sweep has work for, without a tenant scope."""
        rows = await self.session.execute(
            select(StockBatch.company_id)
            .where(
                StockBatch.status.in_([BatchStatus.ACTIVE, BatchStatus.QUARANTINED]),
                StockBatch.expires_on.is_not(None),
            )
            .distinct()
        )
        return [row[0] for row in rows]

    async def expiring(
        self, *, as_of: date, warn_before: date, limit: int = 500
    ) -> list[StockBatch]:
        """Lots already expired or about to, oldest expiry first."""
        stmt = (
            self._scoped()
            .where(
                StockBatch.status.in_([BatchStatus.ACTIVE, BatchStatus.QUARANTINED]),
                StockBatch.expires_on.is_not(None),
                StockBatch.expires_on <= warn_before,
            )
            .order_by(StockBatch.expires_on.asc(), StockBatch.id.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class StockSerialRepository(BaseRepository[StockSerial]):
    model = StockSerial
    searchable_fields = ("serial_number",)

    async def lock_serials(self, serial_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, StockSerial]:
        """Take the row locks for a set of serials and re-read them.

        The same primitive as :meth:`InventoryRepository.lock_balances`, for the
        same reason: a serial's status is read, decided on, and written, and a
        value read before the lock is worthless. Without this, two concurrent
        issues of one serial both read ``IN_STOCK``, both decide the issue is
        legal, and the unit leaves the building twice.

        ``ORDER BY id`` is the acquisition order, so two documents naming an
        overlapping set of serials queue instead of deadlocking, and
        ``populate_existing`` forces the re-read - otherwise SQLAlchemy serves
        whatever version is already in the identity map and the lock protects a
        stale value.

        Called *after* the balance rows are locked. Every posting takes the two
        sets in that order, so the pair cannot deadlock either.
        """
        wanted = list(dict.fromkeys(serial_ids))
        if not wanted:
            return {}
        stmt = (
            self._scoped()
            .where(StockSerial.id.in_(wanted))
            .order_by(StockSerial.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def by_number(self, item_id: uuid.UUID, serial_number: str) -> StockSerial | None:
        stmt = self._scoped().where(
            StockSerial.item_id == item_id, StockSerial.serial_number == serial_number
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def by_ids(self, serial_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, StockSerial]:
        wanted = list(dict.fromkeys(serial_ids))
        if not wanted:
            return {}
        stmt = self._scoped().where(StockSerial.id.in_(wanted))
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def by_numbers(
        self, item_id: uuid.UUID, serial_numbers: Sequence[str]
    ) -> dict[str, StockSerial]:
        wanted = list(dict.fromkeys(serial_numbers))
        if not wanted:
            return {}
        stmt = self._scoped().where(
            StockSerial.item_id == item_id, StockSerial.serial_number.in_(wanted)
        )
        return {
            row.serial_number: row for row in (await self.session.execute(stmt)).scalars().all()
        }

    async def in_stock_for_batch(self, batch_id: uuid.UUID) -> list[StockSerial]:
        """Every in-stock unit of one lot, in one query, ordered for grouping.

        Quarantine needs the individual units because the engine refuses any
        movement of more than one against a serialised item. The serial row has
        no pool column, so this can only ask "which units are still in stock",
        not "which are in the AVAILABLE pool": the caller compares each
        position's count with its balance and refuses rather than guessing when
        the two disagree.
        """
        stmt = (
            self._scoped()
            .where(
                StockSerial.batch_id == batch_id,
                StockSerial.status == SerialStatus.IN_STOCK,
            )
            .order_by(StockSerial.serial_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    def build_list_stmt(
        self,
        *,
        item_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        search: str | None,
        status: SerialStatus | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> Select[tuple[StockSerial]]:
        stmt = self._scoped()
        if item_id is not None:
            stmt = stmt.where(StockSerial.item_id == item_id)
        if status is not None:
            stmt = stmt.where(StockSerial.status == status)
        if batch_id is not None:
            stmt = stmt.where(StockSerial.batch_id == batch_id)
        if warehouse_id is not None:
            # ASSUMPTION: "the units at this warehouse" means the ones that are
            # still there. ``warehouse_id`` on a serial records the last position
            # the unit was put in and is kept after it leaves - that is what lets
            # a warranty claim ask which warehouse shipped a unit - so the status
            # has to carry the other half of the question. Without this clause a
            # warehouse's serial list would slowly fill with every unit it ever
            # dispatched.
            stmt = stmt.where(
                StockSerial.warehouse_id == warehouse_id,
                StockSerial.status.in_(
                    sorted(str(status) for status in SERIAL_IN_WAREHOUSE_STATUSES)
                ),
            )
        stmt = self._apply_search(stmt, search)
        return stmt.order_by(StockSerial.serial_number)


# Line repositories exist so a line is added through a repository typed for it.
# Reusing the header's repository would type-check as the wrong aggregate, and
# `add()` stamps the tenant and actor columns - a detail worth keeping in one
# place rather than repeating per line in the service.


class StockTransferLineRepository(BaseRepository[StockTransferLine]):
    model = StockTransferLine


class StockAdjustmentLineRepository(BaseRepository[StockAdjustmentLine]):
    model = StockAdjustmentLine


class PhysicalCountLineRepository(BaseRepository[PhysicalCountLine]):
    """Count lines, queried on their own: a sheet can run to thousands of rows."""

    model = PhysicalCountLine

    def build_list_stmt(self, count_id: uuid.UUID) -> Select[tuple[PhysicalCountLine]]:
        return (
            self._scoped()
            .where(PhysicalCountLine.count_id == count_id)
            .order_by(PhysicalCountLine.line_number)
        )

    async def by_ids(
        self, count_id: uuid.UUID, line_ids: Sequence[uuid.UUID]
    ) -> list[PhysicalCountLine]:
        """Fetch exactly the lines an entry request names, in one query."""
        if not line_ids:
            return []
        stmt = self._scoped().where(
            PhysicalCountLine.count_id == count_id,
            PhysicalCountLine.id.in_(list(line_ids)),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def all_for_count(self, count_id: uuid.UUID) -> list[PhysicalCountLine]:
        stmt = (
            self._scoped()
            .where(PhysicalCountLine.count_id == count_id)
            .order_by(PhysicalCountLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def uncounted_count(self, count_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(PhysicalCountLine)
            .where(
                PhysicalCountLine.company_id == self.tenant.company_id,
                PhysicalCountLine.count_id == count_id,
                PhysicalCountLine.counted_quantity.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


# -----------------------------------------------------------------------------
# Bin move, scrap disposal and internal issue
#
# Scoped to the principal's warehouses on reads as well as writes: a store
# keeper assigned to one store reads that store's documents and gets "not
# found" for the rest - the same answer ``warehouse_scope`` gives the ledger.
# -----------------------------------------------------------------------------


class BinMoveRepository(BaseRepository[BinMove]):
    model = BinMove
    searchable_fields = ("code", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": BinMove.code,
        "move_date": BinMove.move_date,
        "status": BinMove.status,
        "total_quantity": BinMove.total_quantity,
        "created_at": BinMove.created_at,
    }

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[BinMove]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*warehouse_scope(self.tenant, BinMove.warehouse_id))
        )

    def build_list_stmt(
        self, filters: BinMoveFilters, sort: SortParams | None = None
    ) -> Select[tuple[BinMove]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(BinMove.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(BinMove.warehouse_id == filters.warehouse_id)
        if filters.date_from is not None:
            stmt = stmt.where(BinMove.move_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(BinMove.move_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, BinMove.id.desc()))
        return stmt.order_by(BinMove.move_date.desc(), BinMove.id.desc())

    async def get_with_lines(self, bin_move_id: uuid.UUID) -> BinMove | None:
        """Header plus lines in two queries, never one per line."""
        stmt = self._scoped().where(BinMove.id == bin_move_id).options(selectinload(BinMove.lines))
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()


class ScrapDisposalRepository(BaseRepository[ScrapDisposal]):
    model = ScrapDisposal
    searchable_fields = ("code", "reason", "buyer_reference")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": ScrapDisposal.code,
        "disposal_date": ScrapDisposal.disposal_date,
        "disposal_kind": ScrapDisposal.disposal_kind,
        "status": ScrapDisposal.status,
        "realised_amount": ScrapDisposal.realised_amount,
        "created_at": ScrapDisposal.created_at,
    }

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[ScrapDisposal]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*warehouse_scope(self.tenant, ScrapDisposal.warehouse_id))
        )

    def build_list_stmt(
        self, filters: ScrapDisposalFilters, sort: SortParams | None = None
    ) -> Select[tuple[ScrapDisposal]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(ScrapDisposal.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(ScrapDisposal.warehouse_id == filters.warehouse_id)
        if filters.disposal_kind is not None:
            stmt = stmt.where(ScrapDisposal.disposal_kind == filters.disposal_kind)
        if filters.date_from is not None:
            stmt = stmt.where(ScrapDisposal.disposal_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(ScrapDisposal.disposal_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, ScrapDisposal.id.desc()))
        return stmt.order_by(ScrapDisposal.disposal_date.desc(), ScrapDisposal.id.desc())

    async def get_with_lines(self, disposal_id: uuid.UUID) -> ScrapDisposal | None:
        stmt = (
            self._scoped()
            .where(ScrapDisposal.id == disposal_id)
            .options(selectinload(ScrapDisposal.lines))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()


class InternalIssueRepository(BaseRepository[InternalIssue]):
    model = InternalIssue
    searchable_fields = ("code", "reason")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": InternalIssue.code,
        "issue_date": InternalIssue.issue_date,
        "status": InternalIssue.status,
        "total_value": InternalIssue.total_value,
        "created_at": InternalIssue.created_at,
    }

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[InternalIssue]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*warehouse_scope(self.tenant, InternalIssue.warehouse_id))
        )

    def build_list_stmt(
        self, filters: InternalIssueFilters, sort: SortParams | None = None
    ) -> Select[tuple[InternalIssue]]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(InternalIssue.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(InternalIssue.warehouse_id == filters.warehouse_id)
        if filters.department_id is not None:
            stmt = stmt.where(InternalIssue.department_id == filters.department_id)
        if filters.cost_centre_id is not None:
            stmt = stmt.where(InternalIssue.cost_centre_id == filters.cost_centre_id)
        if filters.date_from is not None:
            stmt = stmt.where(InternalIssue.issue_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(InternalIssue.issue_date <= filters.date_to)
        stmt = self._apply_search(stmt, filters.search)

        if sort is not None and sort.sort_by:
            return stmt.order_by(sort.resolve(self.sortable_fields, InternalIssue.id.desc()))
        return stmt.order_by(InternalIssue.issue_date.desc(), InternalIssue.id.desc())

    async def lock_for_update(self, issue_id: uuid.UUID) -> InternalIssue | None:
        """Row lock on the header before its status is read.

        Posting and approving are read-modify-writes over the status: without
        the lock two simultaneous approvals both read PENDING_APPROVAL and both
        post the issue to the ledger. See
        :meth:`StockTransferRepository.lock_for_update`.
        """
        stmt = (
            self._scoped()
            .where(InternalIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_lines(self, issue_id: uuid.UUID) -> InternalIssue | None:
        stmt = (
            self._scoped()
            .where(InternalIssue.id == issue_id)
            .options(selectinload(InternalIssue.lines))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()


class BinMoveLineRepository(BaseRepository[BinMoveLine]):
    model = BinMoveLine


class ScrapDisposalLineRepository(BaseRepository[ScrapDisposalLine]):
    model = ScrapDisposalLine


class InternalIssueLineRepository(BaseRepository[InternalIssueLine]):
    model = InternalIssueLine


__all__ = [
    "BalanceGrain",
    "BinMoveLineRepository",
    "BinMoveRepository",
    "InternalIssueLineRepository",
    "InternalIssueRepository",
    "InventoryRepository",
    "ItemFacts",
    "LedgerRepository",
    "PhysicalCountLineRepository",
    "PhysicalCountRepository",
    "ScrapDisposalLineRepository",
    "ScrapDisposalRepository",
    "StockAdjustmentLineRepository",
    "StockAdjustmentRepository",
    "StockBatchRepository",
    "StockSerialRepository",
    "StockTransferLineRepository",
    "StockTransferRepository",
    "WarehouseFacts",
    "warehouse_scope",
]
