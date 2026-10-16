"""Queries for the production lifecycle.

Five shapes in this file exist for performance or correctness reasons rather
than convenience:

* **Everything a write path needs is batched.** Releasing a job resolves items,
  warehouses and availability for the whole exploded structure in one statement
  each. A twenty-component release validated with a lookup per component would
  be sixty round trips on the write path, which is the N+1 the rules forbid
  (master rules section 32).
* **Projections, not entities, wherever a wide table is read for a few
  columns.** ``items`` carries nine capability flags and a dozen planning
  parameters; production reads seven columns of it.
* **The production order header is the serialisation point.** ``lock_for_update``
  takes the row lock before any issue, booking or transition reads the running
  quantities, so two store keepers issuing against one job cannot both validate
  against the totals they read first.
* **Components are locked in primary-key order.** A multi-line issue updates
  several component rows; taking them in a deterministic order is what stops two
  documents that share components deadlocking each other - the same rule
  database.md section 4.4 imposes on balance rows.
* **Every locking read sets ``populate_existing``.** ``SELECT ... FOR UPDATE``
  takes the lock either way, but without this SQLAlchemy hands back whatever
  copy of the row is already in the identity map, so the "re-read after the
  lock" of database.md section 4.4 step 3 silently becomes a no-op and the
  quantities validated are the ones read before the lock. The inventory engine's
  ``lock_balances`` documents the same requirement for the same reason.
* **The WIP boards aggregate in SQL.** "What is running, and how loaded is each
  cell?" is a GROUP BY with the counts computed by the database, never a page of
  orders followed by a query per row.

Two cross-module tables are read here directly, both read-only:
``inventory_balances`` for component availability and ``stock_reservations`` for
what a job is already holding. Both are needed *per page of components* as an
aggregate; going through the owning module's per-item service call would be one
round trip per component.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.common.warehouse_scope import warehouse_scope
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.core.types import ZERO, to_quantity
from src.modules.allocation.models import (
    OPEN_RESERVATION_STATUSES,
    DemandType,
    StockReservation,
)
from src.modules.inventory.models import InventoryBalance
from src.modules.organization.models import Plant
from src.modules.production.models import (
    OPEN_PRODUCTION_STATUSES,
    MaterialIssue,
    MaterialIssueLine,
    MaterialIssueStatus,
    MaterialIssueType,
    ProductionEntry,
    ProductionOrder,
    ProductionOrderComponent,
    ProductionOrderStatus,
    WorkOrder,
    WorkOrderStatus,
)
from src.modules.production.schemas import (
    MaterialIssueFilters,
    ProductionEntryFilters,
    ProductionOrderFilters,
    WipFilters,
    WorkOrderFilters,
)
from src.modules.products.models import Item
from src.modules.routing.models import WorkCenter
from src.modules.warehouses.models import StorageLocation, Warehouse

#: Work-order statuses still occupying capacity on a work centre.
OPEN_WORK_ORDER_STATUSES: tuple[WorkOrderStatus, ...] = (
    WorkOrderStatus.PENDING,
    WorkOrderStatus.IN_PROGRESS,
)

#: A work-centre load board covering more cells than this is not a board, it is
#: an export. Bounded so the aggregate can never become an unbounded response.
MAX_WORK_CENTRE_ROWS = 200

#: Production-order statuses whose unmade balance is *committed output* MRP may
#: net a shortage against - identical to :data:`OPEN_PRODUCTION_STATUSES`, and
#: derived from it so the two cannot drift.
#:
#: ``DRAFT`` and ``APPROVED`` are deliberately outside it. Release is the
#: commitment on this document: it is the transition ``PRODUCTION_ORDER_FLOW``
#: marks ``is_irreversible`` ("Release explodes the structure, freezes it and
#: holds stock against it. Reversing that is a cancellation, not an edit"),
#: where a purchase order's equivalent commitment is ``APPROVED``. An approved
#: but unreleased job holds no material and can still be cancelled with one
#: click, so counting its output as supply would net a real requirement against
#: work nobody has started.
#:
#: ``COMPLETED`` and ``CLOSED`` are outside it because the output has already
#: been received into stock by a ``PRODUCTION_RECEIPT`` movement and is in
#: ``inventory_balances``; a completed job that made less than it planned will
#: never make the difference, so its balance is not supply either. ``CANCELLED``
#: never produces anything.
NETTABLE_PRODUCTION_STATUSES: frozenset[ProductionOrderStatus] = frozenset(OPEN_PRODUCTION_STATUSES)


# -----------------------------------------------------------------------------
# Master-data projections
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductionItem:
    """The item-master facts production needs, and nothing else."""

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    can_manufacture: bool
    is_active: bool
    is_batch_tracked: bool
    is_serial_tracked: bool
    standard_cost: Decimal
    default_warehouse_id: uuid.UUID | None
    #: Whether the item's output is held for production QC. Default False so a
    #: projection built before the flag existed keeps meaning "no QC owed".
    requires_production_qc: bool = False


@dataclass(frozen=True, slots=True)
class WipOrderAggregate:
    """One live job with its operation counts and value already resolved."""

    production_order_id: uuid.UUID
    code: str
    status: ProductionOrderStatus
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    plant_id: uuid.UUID | None
    priority: int
    planned_quantity: Decimal
    produced_quantity: Decimal
    rejected_quantity: Decimal
    planned_start_date: date
    planned_end_date: date
    actual_start_date: date | None
    open_work_orders: int
    total_work_orders: int
    wip_value: Decimal


@dataclass(frozen=True, slots=True)
class WorkCentreLoad:
    """Outstanding load on one work centre."""

    work_center_id: uuid.UUID
    pending_work_orders: int
    in_progress_work_orders: int
    planned_quantity: Decimal
    completed_quantity: Decimal
    remaining_minutes: Decimal


@dataclass(frozen=True, slots=True)
class MachineConflict:
    """One open operation already planned on a machine inside a proposed window.

    The order code is carried so the refusal names the job that holds the
    machine, not just the operation: a planner reading "WO-1042" has to go and
    find whose it is, while "WO-1042 on ORD-0091" says where to move it.
    """

    work_order_id: uuid.UUID
    work_order_code: str
    production_order_id: uuid.UUID
    production_order_code: str
    machine_id: uuid.UUID
    planned_start_at: datetime
    planned_end_at: datetime


@dataclass(frozen=True, slots=True)
class WorkCentreCapacityFacts:
    """What the capacity board needs of a work centre: its name and day length."""

    id: uuid.UUID
    code: str
    name: str
    working_hours_per_day: Decimal


@dataclass(frozen=True, slots=True)
class CapacityWindow:
    """One open operation's planned window and length, for capacity bucketing.

    ``minutes`` is setup plus run - the load the operation books against the
    day, the same figure the work-centre board reports as remaining.
    """

    work_center_id: uuid.UUID
    planned_start_at: datetime
    planned_end_at: datetime
    minutes: Decimal


@dataclass(frozen=True, slots=True)
class IssuedComponentTotals:
    """Net issued quantity and net value for one component, from posted documents.

    A return carries a negative sign so a component issued 100 and returned 30
    reports 70 - the quantity the job actually consumed, valued at what those
    movements were actually worth.
    """

    component_id: uuid.UUID
    issued_quantity: Decimal
    returned_quantity: Decimal
    net_quantity: Decimal
    net_cost: Decimal


class ProductionItemRepository(BaseRepository[Item]):
    """Read-only access to the item master for production."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ProductionItem]:
        """Batched id -> item facts. One statement for a whole structure."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.can_manufacture,
            Item.is_active,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.requires_production_qc,
            Item.standard_cost,
            Item.default_warehouse_id,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: ProductionItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                can_manufacture=row.can_manufacture,
                is_active=row.is_active,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                requires_production_qc=row.requires_production_qc,
                standard_cost=Decimal(row.standard_cost),
                default_warehouse_id=row.default_warehouse_id,
            )
            for row in rows
        }


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence and code lookups. Never loads the whole warehouse row."""

    model = Warehouse

    async def codes(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not warehouse_ids:
            return {}
        stmt = select(Warehouse.id, Warehouse.code).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.deleted_at.is_(None),
            *warehouse_scope(self.tenant, Warehouse.id),
            Warehouse.id.in_(list(dict.fromkeys(warehouse_ids))),
        )
        return {row.id: row.code for row in (await self.session.execute(stmt)).all()}

    async def exists_in_tenant(self, warehouse_id: uuid.UUID) -> bool:
        stmt = select(Warehouse.id).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.id == warehouse_id,
            Warehouse.deleted_at.is_(None),
            *warehouse_scope(self.tenant, Warehouse.id),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def existing_ids(self, warehouse_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these warehouses are this tenant's. One statement for a kit."""
        wanted = list(dict.fromkeys(warehouse_ids))
        if not wanted:
            return set()
        stmt = select(Warehouse.id).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.id.in_(wanted),
            Warehouse.deleted_at.is_(None),
            *warehouse_scope(self.tenant, Warehouse.id),
        )
        return {row.id for row in (await self.session.execute(stmt)).all()}


class StorageLocationLookupRepository(BaseRepository[StorageLocation]):
    """Which warehouse each named bin belongs to, inside this tenant.

    ``material_issue_lines.location_id`` is a foreign key to ``storage_locations``
    and nothing more: the constraint is company-blind, so without this check a
    line could name another tenant's bin, and the movement would key an
    ``inventory_balances`` row and a ledger row on a position this company cannot
    see. The engine validates the item, the warehouse and the batch; the location
    is the one reference nothing else checks.
    """

    model = StorageLocation

    async def warehouses_of(self, location_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        """``location_id -> warehouse_id`` for the locations this tenant owns."""
        wanted = list(dict.fromkeys(location_ids))
        if not wanted:
            return {}
        stmt = select(StorageLocation.id, StorageLocation.warehouse_id).where(
            StorageLocation.company_id == self.tenant.company_id,
            StorageLocation.id.in_(wanted),
            StorageLocation.deleted_at.is_(None),
        )
        return {row.id: row.warehouse_id for row in (await self.session.execute(stmt)).all()}


class PlantLookupRepository(BaseRepository[Plant]):
    """Existence check for the plant a job is scheduled at.

    A foreign key alone would not stop another tenant's plant id being written:
    ``plants`` is company-scoped in the application, not in the constraint.
    """

    model = Plant

    async def exists_in_tenant(self, plant_id: uuid.UUID) -> bool:
        stmt = select(Plant.id).where(
            Plant.company_id == self.tenant.company_id,
            Plant.id == plant_id,
            Plant.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None


class WorkCentreRateRepository(BaseRepository[WorkCenter]):
    """Work-centre rates for closing costing.

    The rate is read live rather than snapshotted on the work order: the routing
    module documents that a rate revision is meant to flow through to approved
    processes. What must not move is a *closed* job's cost, and that is fixed by
    storing the computed total on the order at closure, not by freezing the rate.
    """

    model = WorkCenter

    async def rates(
        self, work_center_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
        """id -> (cost_per_hour, setup_cost)."""
        if not work_center_ids:
            return {}
        stmt = select(WorkCenter.id, WorkCenter.cost_per_hour, WorkCenter.setup_cost).where(
            WorkCenter.company_id == self.tenant.company_id,
            WorkCenter.deleted_at.is_(None),
            WorkCenter.id.in_(list(dict.fromkeys(work_center_ids))),
        )
        return {
            row.id: (Decimal(row.cost_per_hour), Decimal(row.setup_cost))
            for row in (await self.session.execute(stmt)).all()
        }

    async def capacity_facts(
        self, work_center_ids: Sequence[uuid.UUID] = ()
    ) -> dict[uuid.UUID, WorkCentreCapacityFacts]:
        """The identity and standard day a capacity board prints per centre.

        Retired masters are excluded: capacity is a statement about the plant
        as it runs now. A centre deactivated mid-week keeps its open operations
        on the queue, but the board no longer plans against it.
        """
        stmt = select(
            WorkCenter.id,
            WorkCenter.code,
            WorkCenter.name,
            WorkCenter.working_hours_per_day,
        ).where(
            WorkCenter.company_id == self.tenant.company_id,
            WorkCenter.deleted_at.is_(None),
            WorkCenter.is_active.is_(True),
        )
        if work_center_ids:
            stmt = stmt.where(WorkCenter.id.in_(list(dict.fromkeys(work_center_ids))))
        return {
            row.id: WorkCentreCapacityFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                working_hours_per_day=Decimal(str(row.working_hours_per_day)),
            )
            for row in (await self.session.execute(stmt)).all()
        }


# -----------------------------------------------------------------------------
# Cross-module read-only aggregates
# -----------------------------------------------------------------------------


class ComponentAvailabilityRepository(BaseRepository[InventoryBalance]):
    """Free stock for a set of (item, warehouse) positions, in one statement.

    ``available_quantity`` is the generated column on ``inventory_balances``
    (database.md section 4.3), so this reads the same number the reservation
    engine arbitrates - it is never computed here and never cached (section 34).
    """

    model = InventoryBalance

    async def available_for(
        self, positions: Sequence[tuple[uuid.UUID, uuid.UUID]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
        if not positions:
            return {}
        unique = list(dict.fromkeys(positions))
        stmt = (
            select(
                InventoryBalance.item_id,
                InventoryBalance.warehouse_id,
                func.coalesce(func.sum(InventoryBalance.available_quantity), 0).label("qty"),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                tuple_(InventoryBalance.item_id, InventoryBalance.warehouse_id).in_(unique),
            )
            .group_by(InventoryBalance.item_id, InventoryBalance.warehouse_id)
        )
        rows = (await self.session.execute(stmt)).all()
        found = {(row.item_id, row.warehouse_id): Decimal(str(row.qty)) for row in rows}
        # A position that has never held stock has no balance row at all, and
        # "no row" means zero available - not "unknown".
        return {position: found.get(position, ZERO) for position in unique}


class ReservationCoverageRepository(BaseRepository[StockReservation]):
    """How much each component of an order is still holding.

    Keyed on ``demand_line_id``, which the release path sets to the component
    row id, so a component's coverage is one aggregate rather than a scan of the
    order's reservations in Python.
    """

    model = StockReservation

    async def remaining_by_component(
        self, order_id: uuid.UUID, component_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        if not component_ids:
            return {}
        stmt = (
            select(
                StockReservation.demand_line_id,
                func.coalesce(func.sum(StockReservation.remaining_quantity), 0).label("qty"),
            )
            .where(
                StockReservation.company_id == self.tenant.company_id,
                StockReservation.demand_type == DemandType.PRODUCTION_ORDER,
                StockReservation.demand_id == order_id,
                StockReservation.demand_line_id.in_(list(dict.fromkeys(component_ids))),
                StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
            )
            .group_by(StockReservation.demand_line_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row.demand_line_id: Decimal(str(row.qty)) for row in rows}

    async def open_for_order(self, order_id: uuid.UUID) -> list[StockReservation]:
        """Every reservation this order is still holding.

        Bounded by the component count, which the release path caps.
        """
        stmt = self._scoped().where(
            StockReservation.demand_type == DemandType.PRODUCTION_ORDER,
            StockReservation.demand_id == order_id,
            StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
        )
        return list((await self.session.execute(stmt)).scalars().all())


# -----------------------------------------------------------------------------
# Production orders
# -----------------------------------------------------------------------------


class ProductionOrderRepository(BaseRepository[ProductionOrder]):
    model = ProductionOrder
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": ProductionOrder.code,
        "status": ProductionOrder.status,
        "priority": ProductionOrder.priority,
        "planned_start_date": ProductionOrder.planned_start_date,
        "planned_end_date": ProductionOrder.planned_end_date,
        "planned_quantity": ProductionOrder.planned_quantity,
        "created_at": ProductionOrder.created_at,
    }
    searchable_fields = ("code",)

    async def lock_for_update(self, order_id: uuid.UUID) -> ProductionOrder | None:
        """Serialise everything that changes a job's running totals."""
        stmt = (
            self._scoped()
            .where(ProductionOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: ProductionOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.status is not None:
            stmt = stmt.where(ProductionOrder.status == filters.status)
        if filters.open_only:
            stmt = stmt.where(
                ProductionOrder.status.in_(sorted(str(s) for s in OPEN_PRODUCTION_STATUSES))
            )
        if filters.item_id is not None:
            stmt = stmt.where(ProductionOrder.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(ProductionOrder.warehouse_id == filters.warehouse_id)
        if filters.plant_id is not None:
            stmt = stmt.where(ProductionOrder.plant_id == filters.plant_id)
        if filters.source_type is not None:
            stmt = stmt.where(ProductionOrder.source_type == filters.source_type)
        if filters.source_id is not None:
            stmt = stmt.where(ProductionOrder.source_id == filters.source_id)
        if filters.planned_from is not None:
            stmt = stmt.where(ProductionOrder.planned_end_date >= filters.planned_from)
        if filters.planned_to is not None:
            stmt = stmt.where(ProductionOrder.planned_start_date <= filters.planned_to)
        if filters.max_priority is not None:
            stmt = stmt.where(ProductionOrder.priority <= filters.max_priority)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: ProductionOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ProductionOrder], OffsetPagination]:
        order_by = sort.resolve(
            self.sortable_fields,
            # Most urgent first, then the earliest promise: the order a planner
            # actually works the list in.
            (ProductionOrder.priority.asc()),
        )
        stmt = self._filtered(filters).order_by(order_by, ProductionOrder.id.desc())
        return await self.list_page(params, stmt=stmt)

    def _wip_stmt(self, filters: WipFilters) -> Select[Any]:
        """Live jobs with their work-order counts and value, computed in batches.

        Counts remain scalar sub-selects so joins cannot multiply the header
        rows. Material and operation values are separate grouped aggregates,
        joined once for the whole page rather than queried once per order.
        """
        open_count = (
            select(func.count())
            .select_from(WorkOrder)
            .where(
                WorkOrder.company_id == self.tenant.company_id,
                WorkOrder.production_order_id == ProductionOrder.id,
                WorkOrder.status.in_(sorted(str(s) for s in OPEN_WORK_ORDER_STATUSES)),
            )
            .scalar_subquery()
        )
        total_count = (
            select(func.count())
            .select_from(WorkOrder)
            .where(
                WorkOrder.company_id == self.tenant.company_id,
                WorkOrder.production_order_id == ProductionOrder.id,
            )
            .scalar_subquery()
        )

        material_costs = (
            select(
                MaterialIssue.production_order_id.label("production_order_id"),
                func.sum(
                    case(
                        (
                            MaterialIssue.issue_type == MaterialIssueType.ISSUE,
                            MaterialIssueLine.total_cost,
                        ),
                        else_=-MaterialIssueLine.total_cost,
                    )
                ).label("material_cost"),
            )
            .join(MaterialIssueLine, MaterialIssueLine.material_issue_id == MaterialIssue.id)
            .where(
                MaterialIssue.company_id == self.tenant.company_id,
                MaterialIssueLine.company_id == self.tenant.company_id,
                MaterialIssue.status == MaterialIssueStatus.POSTED,
            )
            .group_by(MaterialIssue.production_order_id)
            .subquery()
        )

        # WIP valuation includes elapsed time for a running operation. Its
        # close-time cost is still calculated from completed clockings; this is
        # a live estimate, not a change to the frozen final-cost calculation.
        # ASSUMPTION: docs/assumptions.md §E7-3.
        elapsed_minutes = case(
            (
                and_(
                    WorkOrder.actual_start_at.is_not(None),
                    WorkOrder.actual_end_at.is_not(None),
                ),
                func.greatest(
                    func.extract("epoch", WorkOrder.actual_end_at - WorkOrder.actual_start_at) / 60,
                    0,
                ),
            ),
            (
                WorkOrder.status == WorkOrderStatus.COMPLETED,
                WorkOrder.planned_setup_minutes + WorkOrder.planned_run_minutes,
            ),
            (
                and_(
                    WorkOrder.status == WorkOrderStatus.IN_PROGRESS,
                    WorkOrder.actual_start_at.is_not(None),
                    WorkOrder.actual_end_at.is_(None),
                ),
                func.greatest(
                    func.extract("epoch", func.now() - WorkOrder.actual_start_at) / 60,
                    0,
                ),
            ),
            else_=0,
        )
        operation_ran = or_(
            elapsed_minutes > 0,
            WorkOrder.status == WorkOrderStatus.COMPLETED,
        )
        operation_cost = case(
            (WorkOrder.is_outsourced.is_(True), 0),
            else_=(
                elapsed_minutes / 60 * WorkOrder.work_center_cost_per_hour
                + case(
                    (operation_ran, WorkOrder.work_center_setup_cost + WorkOrder.process_cost),
                    else_=0,
                )
            ),
        )
        operation_costs = (
            select(
                WorkOrder.production_order_id.label("production_order_id"),
                func.sum(operation_cost).label("operation_cost"),
            )
            .where(WorkOrder.company_id == self.tenant.company_id)
            .group_by(WorkOrder.production_order_id)
            .subquery()
        )

        stmt = (
            select(
                ProductionOrder.id.label("production_order_id"),
                ProductionOrder.code,
                ProductionOrder.status,
                ProductionOrder.item_id,
                ProductionOrder.warehouse_id,
                ProductionOrder.plant_id,
                ProductionOrder.priority,
                ProductionOrder.planned_quantity,
                ProductionOrder.produced_quantity,
                ProductionOrder.rejected_quantity,
                ProductionOrder.planned_start_date,
                ProductionOrder.planned_end_date,
                ProductionOrder.actual_start_date,
                open_count.label("open_work_orders"),
                total_count.label("total_work_orders"),
                (
                    func.coalesce(material_costs.c.material_cost, 0)
                    + func.coalesce(operation_costs.c.operation_cost, 0)
                ).label("wip_value"),
            )
            .where(
                ProductionOrder.company_id == self.tenant.company_id,
                ProductionOrder.status.in_(sorted(str(s) for s in OPEN_PRODUCTION_STATUSES)),
            )
            .outerjoin(
                material_costs,
                material_costs.c.production_order_id == ProductionOrder.id,
            )
            .outerjoin(
                operation_costs,
                operation_costs.c.production_order_id == ProductionOrder.id,
            )
        )

        if filters.warehouse_id is not None:
            stmt = stmt.where(ProductionOrder.warehouse_id == filters.warehouse_id)
        if filters.plant_id is not None:
            stmt = stmt.where(ProductionOrder.plant_id == filters.plant_id)
        if filters.item_id is not None:
            stmt = stmt.where(ProductionOrder.item_id == filters.item_id)
        if filters.work_center_id is not None:
            stmt = stmt.where(
                select(WorkOrder.id)
                .where(
                    WorkOrder.company_id == self.tenant.company_id,
                    WorkOrder.production_order_id == ProductionOrder.id,
                    WorkOrder.work_center_id == filters.work_center_id,
                )
                .exists()
            )
        return stmt.order_by(
            ProductionOrder.priority.asc(),
            ProductionOrder.planned_end_date.asc(),
            ProductionOrder.id.desc(),
        )

    async def wip_page(
        self, filters: WipFilters, params: PageParams, *, as_of: date
    ) -> tuple[list[WipOrderAggregate], OffsetPagination]:
        """Paginate the WIP board.

        ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``,
        which would keep only the first column of each row. The semantics are
        reproduced - one extra row to decide ``has_next``, and a bounded count
        only when the caller asked for a total.
        """
        stmt = self._wip_stmt(filters)
        if filters.late_only:
            stmt = stmt.where(ProductionOrder.planned_end_date < as_of)

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
            WipOrderAggregate(
                production_order_id=row.production_order_id,
                code=row.code,
                status=ProductionOrderStatus(str(row.status)),
                item_id=row.item_id,
                warehouse_id=row.warehouse_id,
                plant_id=row.plant_id,
                priority=int(row.priority),
                planned_quantity=Decimal(row.planned_quantity),
                produced_quantity=Decimal(row.produced_quantity),
                rejected_quantity=Decimal(row.rejected_quantity),
                planned_start_date=row.planned_start_date,
                planned_end_date=row.planned_end_date,
                actual_start_date=row.actual_start_date,
                open_work_orders=int(row.open_work_orders),
                total_work_orders=int(row.total_work_orders),
                wip_value=Decimal(str(row.wip_value)),
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

    async def in_progress_output_by_item(
        self,
        item_ids: Sequence[uuid.UUID],
        *,
        warehouse_ids: Sequence[uuid.UUID] | None = None,
        upto: date,
    ) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
        """Good output still to come off live jobs, *dated*, due on or before ``upto``.

        One grouped statement for the whole item set, for the same reason the
        WIP boards aggregate in SQL: MRP asks this once per round for every item
        it is planning, and a statement per item is the N+1 the rules forbid.

        **Grouped by ``planned_end_date``, not summed into one figure per item.**
        A job finishing in six weeks is not supply for a kit shipping on
        Thursday, and one lump per item cannot say so - the planner netted the
        two against each other, reported no shortage and raised nothing. The
        grouping key gains a column and the caller gains the ability to net each
        requirement against the jobs that land in time
        (:func:`~src.modules.mrp.engine.split_supply`).

        **Remaining output is ``planned_quantity - produced_quantity``**, not
        ``planned - produced - rejected - scrap``. That follows from the
        module's own completion rule rather than from a guess:
        ``assert_within_over_production_tolerance`` bounds
        ``already_produced + incoming`` against
        ``planned_quantity * OVER_PRODUCTION_MULTIPLIER`` - ``rejected_quantity``
        and ``scrap_quantity`` do not appear in it, so refusing a piece does not
        consume any of the plan and the job is still expected to make the whole
        planned quantity of *good* output. The WIP board states the same rule
        as ``balance_quantity = max(0, planned - produced)``. Only
        ``produced_quantity`` is received into stock; rejected output "was never
        good" and scrap is "planned output written off", so neither will ever
        arrive in ``inventory_balances`` and neither reduces what is still owed.

        Floored per order inside the SUM: a job produced past its plan under the
        over-production tolerance would otherwise contribute a negative that
        silently cancels another job's genuine balance for the same item.
        """
        if not item_ids:
            return {}

        remaining = func.greatest(
            ProductionOrder.planned_quantity - ProductionOrder.produced_quantity, 0
        )
        stmt = (
            select(
                ProductionOrder.item_id.label("item_id"),
                ProductionOrder.planned_end_date.label("due_date"),
                func.coalesce(func.sum(remaining), 0).label("remaining"),
            )
            .where(
                ProductionOrder.company_id == self.tenant.company_id,
                ProductionOrder.item_id.in_(list(dict.fromkeys(item_ids))),
                ProductionOrder.status.in_(
                    sorted(str(status) for status in NETTABLE_PRODUCTION_STATUSES)
                ),
                # ``planned_end_date`` is NOT NULL, so there is no undated case
                # to resolve here. A job whose promise is already in the past is
                # still counted: it is late, not gone, and the planner nets
                # against what the floor is holding.
                ProductionOrder.planned_end_date <= upto,
            )
            .group_by(ProductionOrder.item_id, ProductionOrder.planned_end_date)
            # Ordered in SQL so the caller never has to sort the tranches, and so
            # two runs of the same plan read the same way.
            .order_by(ProductionOrder.item_id, ProductionOrder.planned_end_date)
        )
        if warehouse_ids:
            stmt = stmt.where(ProductionOrder.warehouse_id.in_(list(dict.fromkeys(warehouse_ids))))

        rows = (await self.session.execute(stmt)).all()
        # Absent means nothing in progress; the planner reads this with
        # ``in_production.get(item_id, ())``, so a zero row would be noise.
        schedule: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
        for row in rows:
            quantity = to_quantity(Decimal(str(row.remaining)))
            if quantity <= 0:
                continue
            schedule.setdefault(row.item_id, []).append((row.due_date, quantity))
        return schedule


class ProductionOrderComponentRepository(BaseRepository[ProductionOrderComponent]):
    model = ProductionOrderComponent
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "line_number": ProductionOrderComponent.line_number,
        "item_code": ProductionOrderComponent.item_code,
        "required_quantity": ProductionOrderComponent.required_quantity,
    }
    searchable_fields = ("item_code",)

    async def list_for_order(self, order_id: uuid.UUID) -> list[ProductionOrderComponent]:
        """The whole snapshot, in kit-list order.

        Not paginated, by deliberate exception to section 31: the set is bounded
        at release time and the posting and costing paths need it whole.
        """
        stmt = (
            self._scoped()
            .where(ProductionOrderComponent.production_order_id == order_id)
            .order_by(ProductionOrderComponent.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_many(
        self, order_id: uuid.UUID, component_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ProductionOrderComponent]:
        """Lock the component rows an issue is about to change.

        Ordered by primary key, which is what stops two issues that share
        components deadlocking each other (database.md section 4.4).
        """
        if not component_ids:
            return {}
        stmt = (
            self._scoped()
            .where(
                ProductionOrderComponent.production_order_id == order_id,
                ProductionOrderComponent.id.in_(list(dict.fromkeys(component_ids))),
            )
            .order_by(ProductionOrderComponent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def lock_for_order(self, order_id: uuid.UUID) -> list[ProductionOrderComponent]:
        """Lock the whole snapshot - completion touches every component."""
        stmt = (
            self._scoped()
            .where(ProductionOrderComponent.production_order_id == order_id)
            .order_by(ProductionOrderComponent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for_order(self, order_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ProductionOrderComponent)
            .where(
                ProductionOrderComponent.company_id == self.tenant.company_id,
                ProductionOrderComponent.production_order_id == order_id,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def page_for_order(
        self, order_id: uuid.UUID, params: PageParams, sort: SortParams, *, search: str | None
    ) -> tuple[list[ProductionOrderComponent], OffsetPagination]:
        stmt = self._scoped().where(ProductionOrderComponent.production_order_id == order_id)
        stmt = self._apply_search(stmt, search)
        order_by = sort.resolve(self.sortable_fields, ProductionOrderComponent.line_number.asc())
        return await self.list_page(params, stmt=stmt.order_by(order_by))


# -----------------------------------------------------------------------------
# Work orders
# -----------------------------------------------------------------------------


class WorkOrderRepository(BaseRepository[WorkOrder]):
    model = WorkOrder
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "sequence": WorkOrder.sequence,
        "status": WorkOrder.status,
        "planned_start_at": WorkOrder.planned_start_at,
        "created_at": WorkOrder.created_at,
    }
    searchable_fields = ("code", "operation_code", "operation_name")

    async def lock_for_update(self, work_order_id: uuid.UUID) -> WorkOrder | None:
        stmt = (
            self._scoped()
            .where(WorkOrder.id == work_order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def order_id_of(self, work_order_id: uuid.UUID) -> uuid.UUID | None:
        """Which job owns this operation, without loading the operation.

        Used to take the production order's lock *before* the work order's. A
        column projection rather than ``get``: loading the entity first would put
        a pre-lock copy in the identity map, and the locking read that follows
        would hand that copy back rather than the freshly locked row.
        """
        stmt = select(WorkOrder.production_order_id).where(
            WorkOrder.company_id == self.tenant.company_id,
            WorkOrder.id == work_order_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def work_center_of(self, work_order_id: uuid.UUID) -> uuid.UUID | None:
        """The cell an operation runs at - for the roster check on a booking."""
        stmt = select(WorkOrder.work_center_id).where(
            WorkOrder.company_id == self.tenant.company_id,
            WorkOrder.id == work_order_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_order(self, order_id: uuid.UUID) -> list[WorkOrder]:
        """Every operation of one job, in execution order.

        Not paginated: a routing is bounded at write time by the routing module
        and a traveller is only meaningful whole.
        """
        stmt = (
            self._scoped()
            .where(WorkOrder.production_order_id == order_id)
            .order_by(WorkOrder.sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_order(self, order_id: uuid.UUID) -> list[WorkOrder]:
        stmt = (
            self._scoped()
            .where(WorkOrder.production_order_id == order_id)
            .order_by(WorkOrder.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def last_sequence_for_order(self, order_id: uuid.UUID) -> int:
        """The final operation of a job - the one whose output is finished goods."""
        stmt = select(func.coalesce(func.max(WorkOrder.sequence), 0)).where(
            WorkOrder.company_id == self.tenant.company_id,
            WorkOrder.production_order_id == order_id,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    def _filtered(self, filters: WorkOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.production_order_id is not None:
            stmt = stmt.where(WorkOrder.production_order_id == filters.production_order_id)
        if filters.status is not None:
            stmt = stmt.where(WorkOrder.status == filters.status)
        if filters.open_only:
            stmt = stmt.where(
                WorkOrder.status.in_(sorted(str(s) for s in OPEN_WORK_ORDER_STATUSES))
            )
        if filters.work_center_id is not None:
            stmt = stmt.where(WorkOrder.work_center_id == filters.work_center_id)
        if filters.machine_id is not None:
            stmt = stmt.where(WorkOrder.machine_id == filters.machine_id)
        if filters.shift_id is not None:
            stmt = stmt.where(WorkOrder.shift_id == filters.shift_id)
        if filters.shift is not None:
            stmt = stmt.where(WorkOrder.shift == filters.shift)
        if filters.operator_user_id is not None:
            stmt = stmt.where(WorkOrder.operator_user_id == filters.operator_user_id)
        return stmt

    async def list_page_filtered(
        self, filters: WorkOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[WorkOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, WorkOrder.sequence.asc())
        stmt = self._filtered(filters).order_by(order_by, WorkOrder.id.desc())
        return await self.list_page(params, stmt=stmt)

    async def load_by_work_centre(
        self, filters: WipFilters, params: PageParams, *, as_of: date
    ) -> tuple[list[WorkCentreLoad], OffsetPagination]:
        """Outstanding load per work centre, aggregated by the database.

        ``remaining_minutes`` is the chargeable time still to run: a pending
        operation still owes its setup, an operation already under way does not,
        and run time is pro-rated by what is left to make. ``planned_quantity``
        is positive by CHECK constraint, so the division is safe.
        """
        remaining_ratio = func.greatest(
            (WorkOrder.planned_quantity - WorkOrder.completed_quantity)
            / WorkOrder.planned_quantity,
            0,
        )
        remaining_minutes = func.sum(
            case(
                (WorkOrder.status == WorkOrderStatus.PENDING, WorkOrder.planned_setup_minutes),
                else_=0,
            )
            + WorkOrder.planned_run_minutes * remaining_ratio
        ).label("remaining_minutes")

        stmt = (
            select(
                WorkOrder.work_center_id,
                func.count()
                .filter(WorkOrder.status == WorkOrderStatus.PENDING)
                .label("pending_work_orders"),
                func.count()
                .filter(WorkOrder.status == WorkOrderStatus.IN_PROGRESS)
                .label("in_progress_work_orders"),
                func.coalesce(func.sum(WorkOrder.planned_quantity), 0).label("planned_quantity"),
                func.coalesce(func.sum(WorkOrder.completed_quantity), 0).label(
                    "completed_quantity"
                ),
                func.coalesce(remaining_minutes, 0).label("remaining_minutes"),
            )
            .join(ProductionOrder, ProductionOrder.id == WorkOrder.production_order_id)
            .where(
                WorkOrder.company_id == self.tenant.company_id,
                ProductionOrder.company_id == self.tenant.company_id,
                WorkOrder.status.in_(sorted(str(s) for s in OPEN_WORK_ORDER_STATUSES)),
                ProductionOrder.status.in_(sorted(str(s) for s in OPEN_PRODUCTION_STATUSES)),
            )
            .group_by(WorkOrder.work_center_id)
        )

        if filters.work_center_id is not None:
            stmt = stmt.where(WorkOrder.work_center_id == filters.work_center_id)
        if filters.plant_id is not None:
            stmt = stmt.where(ProductionOrder.plant_id == filters.plant_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(ProductionOrder.warehouse_id == filters.warehouse_id)
        if filters.item_id is not None:
            stmt = stmt.where(ProductionOrder.item_id == filters.item_id)
        if filters.late_only:
            stmt = stmt.where(ProductionOrder.planned_end_date < as_of)

        # Busiest cell first: the board is read to find the bottleneck.
        stmt = stmt.order_by(func.count().desc(), WorkOrder.work_center_id)

        limit = min(params.limit, MAX_WORK_CENTRE_ROWS)
        rows = (await self.session.execute(stmt.limit(limit + 1).offset(params.offset))).all()
        has_next = len(rows) > limit
        rows = rows[:limit]

        items = [
            WorkCentreLoad(
                work_center_id=row.work_center_id,
                pending_work_orders=int(row.pending_work_orders),
                in_progress_work_orders=int(row.in_progress_work_orders),
                planned_quantity=Decimal(str(row.planned_quantity)),
                completed_quantity=Decimal(str(row.completed_quantity)),
                remaining_minutes=Decimal(str(row.remaining_minutes)),
            )
            for row in rows
        ]
        return items, OffsetPagination(
            page=params.page,
            limit=limit,
            # Counting distinct work centres would be a second aggregate over
            # the same scan to produce a number bounded by MAX_WORK_CENTRE_ROWS.
            total=None,
            total_pages=None,
            has_next=has_next,
            has_previous=params.page > 1,
        )

    async def open_windows(
        self,
        start_at: datetime,
        end_at: datetime,
        work_center_id: uuid.UUID | None = None,
    ) -> list[CapacityWindow]:
        """Planned windows of open operations overlapping ``[start_at, end_at)``.

        One statement for the whole board: only PENDING and IN_PROGRESS
        operations of open jobs count, because finished work no longer occupies
        capacity and a closed job's operations cannot move anyway. Rows without
        a planned window are skipped - unscheduled work is invisible to capacity
        by definition, and the queue is where it waits to be planned.
        """
        if end_at <= start_at:
            return []
        stmt = (
            select(
                WorkOrder.work_center_id.label("work_center_id"),
                WorkOrder.planned_start_at.label("planned_start_at"),
                WorkOrder.planned_end_at.label("planned_end_at"),
                (WorkOrder.planned_setup_minutes + WorkOrder.planned_run_minutes).label("minutes"),
            )
            .join(ProductionOrder, ProductionOrder.id == WorkOrder.production_order_id)
            .where(
                WorkOrder.company_id == self.tenant.company_id,
                ProductionOrder.company_id == self.tenant.company_id,
                WorkOrder.status.in_(sorted(str(s) for s in OPEN_WORK_ORDER_STATUSES)),
                ProductionOrder.status.in_(sorted(str(s) for s in OPEN_PRODUCTION_STATUSES)),
                WorkOrder.planned_start_at.is_not(None),
                WorkOrder.planned_end_at.is_not(None),
                WorkOrder.planned_start_at < end_at,
                WorkOrder.planned_end_at > start_at,
            )
            .order_by(WorkOrder.work_center_id, WorkOrder.planned_start_at, WorkOrder.id)
        )
        if work_center_id is not None:
            stmt = stmt.where(WorkOrder.work_center_id == work_center_id)
        return [
            CapacityWindow(
                work_center_id=row.work_center_id,
                planned_start_at=row.planned_start_at,
                planned_end_at=row.planned_end_at,
                minutes=Decimal(str(row.minutes)),
            )
            for row in (await self.session.execute(stmt)).all()
        ]

    async def machine_conflicts(
        self,
        machine_ids: Sequence[uuid.UUID],
        *,
        start_at: datetime,
        end_at: datetime,
        exclude_work_order_ids: Sequence[uuid.UUID] = (),
        exclude_order_id: uuid.UUID | None = None,
    ) -> list[MachineConflict]:
        """Open operations already planned on these machines inside ``[start_at, end_at)``.

        One statement for every machine and window the caller is placing, read
        off ``ix_work_orders_machine``: the planner placing five operations does
        not pay five round trips. Only PENDING and IN_PROGRESS operations count -
        a finished run's planned window is a stale plan, not a hold on the
        machine, and a cancelled one never happens. ``exclude_work_order_ids``
        keeps an operation being rescheduled from conflicting with itself, and
        ``exclude_order_id`` keeps a release that is already flushed from
        tripping over its own traveller.
        """
        unique = list(dict.fromkeys(machine_ids))
        if not unique or end_at <= start_at:
            return []
        stmt = (
            select(
                WorkOrder.id.label("work_order_id"),
                WorkOrder.code.label("work_order_code"),
                WorkOrder.production_order_id.label("production_order_id"),
                ProductionOrder.code.label("production_order_code"),
                WorkOrder.machine_id.label("machine_id"),
                WorkOrder.planned_start_at.label("planned_start_at"),
                WorkOrder.planned_end_at.label("planned_end_at"),
            )
            .join(ProductionOrder, ProductionOrder.id == WorkOrder.production_order_id)
            .where(
                WorkOrder.company_id == self.tenant.company_id,
                ProductionOrder.company_id == self.tenant.company_id,
                WorkOrder.machine_id.in_(unique),
                WorkOrder.status.in_(sorted(str(s) for s in OPEN_WORK_ORDER_STATUSES)),
                WorkOrder.planned_start_at.is_not(None),
                WorkOrder.planned_end_at.is_not(None),
                WorkOrder.planned_start_at < end_at,
                WorkOrder.planned_end_at > start_at,
            )
            .order_by(WorkOrder.machine_id, WorkOrder.planned_start_at, WorkOrder.id)
        )
        if exclude_work_order_ids:
            stmt = stmt.where(WorkOrder.id.not_in(list(exclude_work_order_ids)))
        if exclude_order_id is not None:
            stmt = stmt.where(WorkOrder.production_order_id != exclude_order_id)
        return [
            MachineConflict(
                work_order_id=row.work_order_id,
                work_order_code=row.work_order_code,
                production_order_id=row.production_order_id,
                production_order_code=row.production_order_code,
                machine_id=row.machine_id,
                planned_start_at=row.planned_start_at,
                planned_end_at=row.planned_end_at,
            )
            for row in (await self.session.execute(stmt)).all()
        ]


# -----------------------------------------------------------------------------
# Material issues
# -----------------------------------------------------------------------------


class MaterialIssueRepository(BaseRepository[MaterialIssue]):
    model = MaterialIssue
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": MaterialIssue.code,
        "issue_date": MaterialIssue.issue_date,
        "status": MaterialIssue.status,
        "created_at": MaterialIssue.created_at,
    }
    searchable_fields = ("code",)

    async def lock_for_update(self, issue_id: uuid.UUID) -> MaterialIssue | None:
        stmt = (
            self._scoped()
            .where(MaterialIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: MaterialIssueFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.production_order_id is not None:
            stmt = stmt.where(MaterialIssue.production_order_id == filters.production_order_id)
        if filters.issue_type is not None:
            stmt = stmt.where(MaterialIssue.issue_type == filters.issue_type)
        if filters.status is not None:
            stmt = stmt.where(MaterialIssue.status == filters.status)
        if filters.warehouse_id is not None:
            stmt = stmt.where(MaterialIssue.warehouse_id == filters.warehouse_id)
        if filters.issued_from is not None:
            stmt = stmt.where(MaterialIssue.issue_date >= filters.issued_from)
        if filters.issued_to is not None:
            stmt = stmt.where(MaterialIssue.issue_date <= filters.issued_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: MaterialIssueFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[MaterialIssue], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, MaterialIssue.issue_date.desc())
        stmt = self._filtered(filters).order_by(order_by, MaterialIssue.id.desc())
        return await self.list_page(params, stmt=stmt)


class MaterialIssueLineRepository(BaseRepository[MaterialIssueLine]):
    model = MaterialIssueLine

    async def list_for_issue(self, issue_id: uuid.UUID) -> list[MaterialIssueLine]:
        stmt = (
            self._scoped()
            .where(MaterialIssueLine.material_issue_id == issue_id)
            .order_by(MaterialIssueLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def posted_totals_for_order(
        self, order_id: uuid.UUID
    ) -> dict[uuid.UUID, IssuedComponentTotals]:
        """Net issued quantity and value per component, from POSTED documents.

        One aggregate for the whole job: costing a twenty-component order must
        not be twenty queries. A ``RETURN`` document contributes negatively, so
        the net is what the job actually holds and what it actually cost.
        """
        sign = case((MaterialIssue.issue_type == MaterialIssueType.RETURN, -1), else_=1)
        issued = case((MaterialIssue.issue_type == MaterialIssueType.ISSUE, 1), else_=0)
        returned = case((MaterialIssue.issue_type == MaterialIssueType.RETURN, 1), else_=0)

        stmt = (
            select(
                MaterialIssueLine.component_id,
                func.coalesce(func.sum(MaterialIssueLine.quantity * issued), 0).label(
                    "issued_quantity"
                ),
                func.coalesce(func.sum(MaterialIssueLine.quantity * returned), 0).label(
                    "returned_quantity"
                ),
                func.coalesce(func.sum(MaterialIssueLine.quantity * sign), 0).label("net_quantity"),
                func.coalesce(func.sum(MaterialIssueLine.total_cost * sign), 0).label("net_cost"),
            )
            .join(MaterialIssue, MaterialIssue.id == MaterialIssueLine.material_issue_id)
            .where(
                MaterialIssueLine.company_id == self.tenant.company_id,
                MaterialIssue.company_id == self.tenant.company_id,
                MaterialIssue.production_order_id == order_id,
                MaterialIssue.status == MaterialIssueStatus.POSTED,
            )
            .group_by(MaterialIssueLine.component_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.component_id: IssuedComponentTotals(
                component_id=row.component_id,
                issued_quantity=Decimal(str(row.issued_quantity)),
                returned_quantity=Decimal(str(row.returned_quantity)),
                net_quantity=Decimal(str(row.net_quantity)),
                net_cost=Decimal(str(row.net_cost)),
            )
            for row in rows
        }


# -----------------------------------------------------------------------------
# Production entries
# -----------------------------------------------------------------------------


class ProductionEntryRepository(BaseRepository[ProductionEntry]):
    model = ProductionEntry
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "entry_date": ProductionEntry.entry_date,
        "created_at": ProductionEntry.created_at,
        "produced_quantity": ProductionEntry.produced_quantity,
    }

    def _filtered(self, filters: ProductionEntryFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.production_order_id is not None:
            stmt = stmt.where(ProductionEntry.production_order_id == filters.production_order_id)
        if filters.work_order_id is not None:
            stmt = stmt.where(ProductionEntry.work_order_id == filters.work_order_id)
        if filters.operator_user_id is not None:
            stmt = stmt.where(ProductionEntry.operator_user_id == filters.operator_user_id)
        if filters.machine_id is not None:
            stmt = stmt.where(ProductionEntry.machine_id == filters.machine_id)
        if filters.shift_id is not None:
            stmt = stmt.where(ProductionEntry.shift_id == filters.shift_id)
        if filters.shift is not None:
            stmt = stmt.where(ProductionEntry.shift == filters.shift)
        if filters.entry_from is not None:
            stmt = stmt.where(ProductionEntry.entry_date >= filters.entry_from)
        if filters.entry_to is not None:
            stmt = stmt.where(ProductionEntry.entry_date <= filters.entry_to)
        if filters.output_only:
            stmt = stmt.where(ProductionEntry.output_transaction_id.is_not(None))
        return stmt

    async def list_page_filtered(
        self, filters: ProductionEntryFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ProductionEntry], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, ProductionEntry.entry_date.desc())
        stmt = self._filtered(filters).order_by(order_by, ProductionEntry.id.desc())
        return await self.list_page(params, stmt=stmt)

    async def open_holds_for_order(self, order_id: uuid.UUID) -> list[ProductionEntry]:
        """Bookings whose output is still sitting in QC hold.

        The completion gate: an order cannot COMPLETE while any of its output
        is held. Bounded by the order - a job is booked dozens of times, not
        thousands.
        """
        stmt = (
            self._scoped()
            .where(
                ProductionEntry.production_order_id == order_id,
                ProductionEntry.qc_hold_open.is_(True),
            )
            .order_by(ProductionEntry.entry_date, ProductionEntry.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_by_inspection(self, inspection_id: uuid.UUID) -> ProductionEntry | None:
        """The booking that raised one inspection, row-locked.

        The quality verdict callback finds its entry by the inspection, not by
        the reference: a booking at a QC-gated operation raises its inspection
        against the work order, while any other booking raises it against the
        entry itself, and the callback must clear the hold either way.
        """
        stmt = (
            self._scoped()
            .where(ProductionEntry.qc_inspection_id == inspection_id)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def awaiting_quality_hold(
        self, *, after: uuid.UUID | None, limit: int
    ) -> list[ProductionEntry]:
        """Bookings still holding output for QC, one keyset page.

        The owner reconciliation sweep's work list: each page scans bookings by
        id, so a sweep walks every held booking once even when most of them are
        still under inspection.
        """
        stmt = self._scoped().where(ProductionEntry.qc_hold_open.is_(True))
        if after is not None:
            stmt = stmt.where(ProductionEntry.id > after)
        stmt = stmt.order_by(ProductionEntry.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "MAX_WORK_CENTRE_ROWS",
    "NETTABLE_PRODUCTION_STATUSES",
    "OPEN_WORK_ORDER_STATUSES",
    "CapacityWindow",
    "ComponentAvailabilityRepository",
    "IssuedComponentTotals",
    "MachineConflict",
    "MaterialIssueLineRepository",
    "MaterialIssueRepository",
    "PlantLookupRepository",
    "ProductionEntryRepository",
    "ProductionItem",
    "ProductionItemRepository",
    "ProductionOrderComponentRepository",
    "ProductionOrderRepository",
    "ReservationCoverageRepository",
    "StorageLocationLookupRepository",
    "WarehouseLookupRepository",
    "WipOrderAggregate",
    "WorkCentreCapacityFacts",
    "WorkCentreLoad",
    "WorkCentreRateRepository",
    "WorkOrderRepository",
]
