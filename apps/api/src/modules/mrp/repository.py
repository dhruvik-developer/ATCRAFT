"""Queries for the planner and for the plans it has already produced.

Two kinds of thing live here, and the distinction is worth stating.

**The run's own tables** (``mrp_runs``, ``mrp_demands``, ``mrp_requirements``,
``mrp_suggestions``) are read the way any document is read: filtered, paginated,
tenant-scoped through :class:`~src.common.repository.BaseRepository`.

**The planning feed** is the interesting half. Demand, item facts, stock and
supplier terms all belong to other modules, and every one of them is read here
for a *set* of ids in a single grouped statement. :class:`DatabaseMrpSource`
assembles those into the :class:`~src.modules.mrp.engine.MrpDataSource` the
engine consumes, which is why the engine itself never sees a session: the
batching contract is enforced by the shape of the protocol, and the unit suite
substitutes an in-memory source that counts calls.

Cross-module reads are by design. The planner joins ``sales_order_lines``,
``items``, ``inventory_balances``, ``boms`` and ``item_vendor_links`` because a
plan is meaningless without all five, and going through ``BaseRepository`` keeps
the tenant and soft-delete predicates automatic rather than something each of
those queries has to remember.

Cross-module supply contracts
-----------------------------
Confirmed incoming purchase orders, in-progress production output, open
assembly output and job-work output are reached through named service contracts
resolved at call time, never by importing their tables and guessing at column
names:

    src.modules.procurement.service.incoming_quantities(
        session, tenant, *, item_ids, warehouse_ids, upto)
            -> dict[UUID, list[tuple[date, Decimal]]]

    src.modules.production.service.in_progress_output(
        session, tenant, *, item_ids, warehouse_ids, upto)
            -> dict[UUID, list[tuple[date, Decimal]]]

    src.modules.assembly.service.open_assembly_output(
        session, tenant, *, item_ids, warehouse_ids, upto)
            -> dict[UUID, list[tuple[date, Decimal]]]

    src.modules.job_work.service.open_job_work_output(
        session, tenant, *, item_ids, warehouse_ids, upto)
            -> dict[UUID, list[tuple[date, Decimal]]]

Both answer with *dated tranches* rather than one summed figure per item, and
that is the whole reason the contract has this shape. The planner nets each
requirement against the supply arriving on or before the date that requirement
is needed; a single figure for the whole horizon let an order landing six weeks
out erase a shortage due in two days, and the run reported success.

When the hook is absent the source says so, and the run records that it did not
net against that supply. It never returns zero silently: a plan that quietly
skipped a netting stage over-orders, and nothing in the output would reveal it.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams, paginate
from src.core.responses import OffsetPagination
from src.core.types import to_quantity
from src.modules.bom import engine as bom_engine
from src.modules.bom.models import BomType
from src.modules.bom.repository import BomLineRepository, BomRepository
from src.modules.inventory.models import InventoryBalance
from src.modules.mrp.engine import (
    BomComponent,
    BomStructure,
    DemandLine,
    PlanningItem,
    StockPosition,
    StockPositions,
    SupplyArrival,
    SupplySchedule,
    SupplyTerms,
)
from src.modules.mrp.models import (
    ACTIVE_RUN_STATUSES,
    DEFAULT_HORIZON_DAYS,
    MAX_DEMAND_LINES,
    MrpDemand,
    MrpDemandSource,
    MrpRequirement,
    MrpRun,
    MrpRunStatus,
    MrpScheduleSettings,
    MrpSuggestion,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.organization.models import Company
from src.modules.products.models import Item, ItemVendorLink
from src.modules.routing import service as routing_service
from src.modules.sales.models import (
    OPEN_DEMAND_STATUSES,
    SalesOrder,
    SalesOrderLine,
    SalesOrderLineStatus,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledMrpConfiguration:
    """Resolved settings needed to dispatch one company's nightly plan."""

    company_id: uuid.UUID
    horizon_days: int
    warehouse_ids: tuple[uuid.UUID, ...] | None
    respect_safety_stock: bool


#: Item-master columns the planner reads. Named explicitly because ``items`` is
#: a very wide table and a run touches every part in every structure - selecting
#: the whole row here is the ``SELECT *`` master rules section 32 forbids.
PLANNING_ITEM_COLUMNS = (
    Item.id,
    Item.code,
    Item.name,
    Item.base_uom_id,
    Item.production_type,
    Item.lead_time_days,
    Item.min_order_quantity,
    Item.safety_stock,
    Item.can_purchase,
    Item.can_manufacture,
    Item.can_job_work,
    Item.is_active,
    Item.is_batch_tracked,
    Item.is_serial_tracked,
)


# -----------------------------------------------------------------------------
# The planning feed
# -----------------------------------------------------------------------------


class SalesDemandRepository(BaseRepository[SalesOrderLine]):
    """Open sales demand, as the planner needs it: one row per order line.

    Distinct from ``sales.demand_summary``, which aggregates per item for a
    dashboard. The planner needs the individual lines because a requirement has
    to be able to name the orders that caused it, and because the earliest date
    across contributing lines is what the whole merged requirement is planned to.
    """

    model = SalesOrderLine

    async def open_demand(
        self,
        *,
        horizon_end: date,
        warehouse_ids: Sequence[uuid.UUID] | None = None,
        limit: int = MAX_DEMAND_LINES + 1,
    ) -> list[DemandLine]:
        """Every open sales-order line falling inside the horizon.

        ``quantity`` is the ordered quantity less what has shipped and less what
        is already reserved for the line. Excluding the reserved part is half of
        "reserved stock is not available": the other half is that
        ``inventory_balances.available_quantity`` excludes reservations too, so a
        reservation is neither counted as free stock nor planned for twice.

        The join is on ``(id, company_id)`` rather than id alone so the tenant
        predicate applies to both sides and PostgreSQL can use the
        tenant-leading partial index on each table.

        One statement, bounded by ``limit``. The caller reads one row more than
        the engine will accept, so "too much demand" is detected rather than
        silently truncated into a plan that under-orders.
        """
        required_date = func.coalesce(SalesOrderLine.required_date, SalesOrder.required_date)
        outstanding = (
            SalesOrderLine.quantity
            - SalesOrderLine.dispatched_quantity
            - SalesOrderLine.allocated_quantity
        )

        stmt = (
            select(
                SalesOrderLine.id.label("line_id"),
                SalesOrderLine.item_id.label("item_id"),
                outstanding.label("open_quantity"),
                required_date.label("required_date"),
                SalesOrder.id.label("order_id"),
                SalesOrder.code.label("order_code"),
                SalesOrder.warehouse_id.label("warehouse_id"),
                SalesOrder.priority.label("priority"),
            )
            .join(
                SalesOrder,
                and_(
                    SalesOrder.id == SalesOrderLine.sales_order_id,
                    SalesOrder.company_id == SalesOrderLine.company_id,
                ),
            )
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrder.status.in_(OPEN_DEMAND_STATUSES),
                SalesOrderLine.status != SalesOrderLineStatus.CANCELLED,
                outstanding > 0,
                required_date <= horizon_end,
            )
            .order_by(required_date, SalesOrder.code, SalesOrderLine.line_number)
            .limit(limit)
        )
        if warehouse_ids:
            stmt = stmt.where(SalesOrder.warehouse_id.in_(list(warehouse_ids)))

        rows = (await self.session.execute(stmt)).all()
        return [
            DemandLine(
                item_id=row.item_id,
                quantity=to_quantity(Decimal(str(row.open_quantity))),
                required_date=row.required_date,
                source=MrpDemandSource.SALES_ORDER,
                source_id=row.order_id,
                source_line_id=row.line_id,
                source_code=row.order_code,
                warehouse_id=row.warehouse_id,
                priority=int(row.priority),
            )
            for row in rows
        ]


class MrpScheduleSettingsRepository(BaseRepository[MrpScheduleSettings]):
    """Tenant-scoped persistence for nightly planning settings."""

    model = MrpScheduleSettings

    async def get_settings(self) -> MrpScheduleSettings | None:
        stmt = self._scoped().limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()


async def scheduled_configurations(
    session: AsyncSession,
) -> list[ScheduledMrpConfiguration]:
    """Resolve enabled active companies, including companies with no settings row.

    This is the one intentional cross-tenant read: the system scheduler must
    discover which tenant contexts to create, then each company is processed
    through the ordinary tenant-scoped service and repositories.
    """
    stmt = (
        select(
            Company.id,
            MrpScheduleSettings.horizon_days,
            MrpScheduleSettings.warehouse_ids,
            MrpScheduleSettings.respect_safety_stock,
        )
        .outerjoin(MrpScheduleSettings, MrpScheduleSettings.company_id == Company.id)
        .where(
            Company.is_active.is_(True),
            Company.deleted_at.is_(None),
            or_(
                MrpScheduleSettings.id.is_(None),
                MrpScheduleSettings.mrp_scheduled_enabled.is_(True),
            ),
        )
        .order_by(Company.id.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        ScheduledMrpConfiguration(
            company_id=row.id,
            horizon_days=(
                row.horizon_days if row.horizon_days is not None else DEFAULT_HORIZON_DAYS
            ),
            warehouse_ids=tuple(row.warehouse_ids) if row.warehouse_ids else None,
            respect_safety_stock=(
                row.respect_safety_stock if row.respect_safety_stock is not None else True
            ),
        )
        for row in rows
    ]


class PlanningItemRepository(BaseRepository[Item]):
    """Item-master facts, BOM availability and supplier terms, always in batches."""

    model = Item

    async def planning_items(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PlanningItem]:
        """The item facts the planner reads, for a whole set of items."""
        if not item_ids:
            return {}
        stmt = select(*PLANNING_ITEM_COLUMNS).where(
            Item.company_id == self.tenant.company_id,
            Item.id.in_(list(dict.fromkeys(item_ids))),
            Item.deleted_at.is_(None),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: PlanningItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                production_type=str(row.production_type),
                lead_time_days=int(row.lead_time_days),
                min_order_quantity=Decimal(str(row.min_order_quantity)),
                safety_stock=Decimal(str(row.safety_stock)),
                can_purchase=bool(row.can_purchase),
                can_manufacture=bool(row.can_manufacture),
                can_job_work=bool(row.can_job_work),
                is_active=bool(row.is_active),
                is_batch_tracked=bool(row.is_batch_tracked),
                is_serial_tracked=bool(row.is_serial_tracked),
            )
            for row in rows
        }

    async def supply_terms(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SupplyTerms]:
        """Ordering policy from each item's preferred vendor.

        A partial unique index guarantees at most one preferred link per item, so
        this cannot return two rows for one item and does not need a tiebreaker.
        Items with no preferred vendor are simply absent; the engine then falls
        back to the item master's own minimum and lead time.
        """
        if not item_ids:
            return {}
        stmt = select(
            ItemVendorLink.item_id,
            ItemVendorLink.lead_time_days,
            ItemVendorLink.min_order_quantity,
            ItemVendorLink.order_multiple,
        ).where(
            ItemVendorLink.company_id == self.tenant.company_id,
            ItemVendorLink.item_id.in_(list(dict.fromkeys(item_ids))),
            ItemVendorLink.is_preferred.is_(True),
            ItemVendorLink.is_active.is_(True),
            ItemVendorLink.deleted_at.is_(None),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.item_id: SupplyTerms(
                lead_time_days=int(row.lead_time_days),
                min_order_quantity=Decimal(str(row.min_order_quantity)),
                order_multiple=Decimal(str(row.order_multiple)),
            )
            for row in rows
        }


class StockPositionRepository(BaseRepository[InventoryBalance]):
    """Stock pools per item and warehouse, plus the item-level totals."""

    model = InventoryBalance

    async def positions(
        self, item_ids: Sequence[uuid.UUID], warehouse_ids: Sequence[uuid.UUID] | None = None
    ) -> StockPositions:
        """One grouped statement for the whole item set. Never cached (§34).

        The four pools are summed rather than the generated ``available_quantity``
        so a requirement row can explain *why* free stock is below on-hand -
        almost always because it is reserved for somebody else. Summing the pools
        and subtracting is arithmetically identical to summing the generated
        column, which is defined as exactly that expression.

        ``warehouse_ids`` empty or omitted means the whole company: a run that
        was not scoped to warehouses plans against all of them.
        """
        if not item_ids:
            return StockPositions(by_item={}, by_warehouse={})
        stmt = (
            select(
                InventoryBalance.item_id,
                InventoryBalance.warehouse_id,
                func.sum(InventoryBalance.on_hand_quantity).label("on_hand"),
                func.sum(InventoryBalance.reserved_quantity).label("reserved"),
                func.sum(InventoryBalance.qc_hold_quantity).label("qc_hold"),
                func.sum(InventoryBalance.damaged_quantity).label("damaged"),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.item_id.in_(list(dict.fromkeys(item_ids))),
            )
            .group_by(InventoryBalance.item_id, InventoryBalance.warehouse_id)
        )
        if warehouse_ids:
            stmt = stmt.where(InventoryBalance.warehouse_id.in_(list(warehouse_ids)))

        rows = (await self.session.execute(stmt)).all()
        by_item: dict[uuid.UUID, StockPosition] = {}
        by_warehouse: dict[tuple[uuid.UUID, uuid.UUID], StockPosition] = {}
        for row in rows:
            position = StockPosition(
                on_hand=Decimal(str(row.on_hand)),
                reserved=Decimal(str(row.reserved)),
                qc_hold=Decimal(str(row.qc_hold)),
                damaged=Decimal(str(row.damaged)),
            )
            by_warehouse[(row.item_id, row.warehouse_id)] = position
            previous = by_item.get(row.item_id, StockPosition())
            by_item[row.item_id] = StockPosition(
                on_hand=previous.on_hand + position.on_hand,
                reserved=previous.reserved + position.reserved,
                qc_hold=previous.qc_hold + position.qc_hold,
                damaged=previous.damaged + position.damaged,
            )
        return StockPositions(by_item=by_item, by_warehouse=by_warehouse)


async def _optional_supply_hook(
    module_name: str,
    function_name: str,
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_ids: Sequence[uuid.UUID],
    warehouse_ids: Sequence[uuid.UUID] | None,
    upto: date,
) -> tuple[SupplySchedule, bool]:
    """Call a netting hook in a module that may not be deployed yet.

    Resolved by name at call time rather than by a top-level import, for two
    reasons. A module that does not exist must not stop the planner importing;
    and a module that *does* exist must be reached through its service, never by
    this module reaching into tables whose columns it would be guessing at.

    Returns ``(schedule, True)`` when the hook ran, and ``({}, False)`` when
    there was nothing to call. The second element is the important one: the run
    records it, so a plan that skipped a netting stage says so instead of
    silently over-ordering.

    The hook answers in primitives - ``{item_id: [(due_date, quantity), ...]}`` -
    so procurement and production never import the planner's types. The
    conversion into :class:`~src.modules.mrp.engine.SupplyArrival` happens here,
    quantised on the way through, and the tranches are sorted by date: the hooks
    order them in SQL, and re-sorting costs nothing against a mapping this small
    while making the engine's input well-defined whatever answered.
    """
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return {}, False

    hook = getattr(module, function_name, None)
    if hook is None:
        return {}, False

    if not item_ids:
        return {}, True

    schedule = await hook(
        session,
        tenant,
        item_ids=list(item_ids),
        warehouse_ids=list(warehouse_ids) if warehouse_ids else None,
        upto=upto,
    )
    return {
        item_id: tuple(
            sorted(
                (
                    SupplyArrival(due_date=due, quantity=to_quantity(Decimal(str(quantity))))
                    for due, quantity in arrivals
                ),
                key=lambda arrival: arrival.due_date,
            )
        )
        for item_id, arrivals in (schedule or {}).items()
    }, True


class DatabaseMrpSource:
    """The production :class:`~src.modules.mrp.engine.MrpDataSource`.

    Holds the run's warehouse scope so the engine never has to carry it: every
    stock and supply read is already restricted to the warehouses the run was
    asked to plan for (master rules section 14, multi-warehouse).
    """

    def __init__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        warehouse_ids: Sequence[uuid.UUID] | None = None,
    ) -> None:
        self._session = session
        self._tenant = tenant
        self._warehouse_ids = list(warehouse_ids) if warehouse_ids else None
        self._items = PlanningItemRepository(session, tenant)
        self._stock = StockPositionRepository(session, tenant)
        self._boms = BomRepository(session, tenant)
        self._bom_lines = BomLineRepository(session, tenant)

    async def planning_items(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PlanningItem]:
        return await self._items.planning_items(item_ids)

    async def stock_positions(self, item_ids: Sequence[uuid.UUID]) -> StockPositions:
        return await self._stock.positions(item_ids, self._warehouse_ids)

    async def bom_structures(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, BomStructure]:
        """Each item's effective structure, one level deep - two statements.

        The effective-BOM ``DISTINCT ON`` and the line read are both set-at-a-
        time, so a whole discovery level costs two statements however many items
        it holds. Lines the planner may not explode are dropped here
        (:func:`bom_engine.is_explodable`): substitutes and optional extras are
        not demand, the same rule the BOM engine's own explosion applies, and
        applying it at the edge means the engine's traversal and this one cannot
        disagree about which lines exist.

        Only ``APPROVED`` structures effective on ``as_of`` are considered -
        planning is never driven by a structure an engineer is still editing
        (database.md section 5).
        """
        if not item_ids:
            return {}
        boms = await self._boms.effective_boms_for_items(item_ids, as_of)
        if not boms:
            return {}
        lines = await self._bom_lines.lines_for_boms([bom.id for bom in boms.values()])
        return {
            item_id: BomStructure(
                output_quantity=Decimal(str(bom.output_quantity)),
                is_default=bool(bom.is_default),
                is_phantom=bom.bom_type == BomType.PHANTOM,
                lines=tuple(
                    BomComponent(
                        item_id=line.component_item_id,
                        quantity_per=Decimal(str(line.quantity_per)),
                        scrap_percentage=Decimal(str(line.scrap_percentage)),
                    )
                    for line in lines.get(bom.id, [])
                    if bom_engine.is_explodable(line)
                ),
                bom_id=bom.id,
                bom_type=BomType(bom.bom_type),
            )
            for item_id, bom in boms.items()
        }

    async def supply_terms(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SupplyTerms]:
        return await self._items.supply_terms(item_ids)

    async def incoming_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        return await _optional_supply_hook(
            "src.modules.procurement.service",
            "incoming_quantities",
            self._session,
            self._tenant,
            item_ids=item_ids,
            warehouse_ids=self._warehouse_ids,
            upto=upto,
        )

    async def in_production_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        return await _optional_supply_hook(
            "src.modules.production.service",
            "in_progress_output",
            self._session,
            self._tenant,
            item_ids=item_ids,
            warehouse_ids=self._warehouse_ids,
            upto=upto,
        )

    async def assembly_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        """Assembly output expected by the due date, through assembly's service."""
        return await _optional_supply_hook(
            "src.modules.assembly.service",
            "open_assembly_output",
            self._session,
            self._tenant,
            item_ids=item_ids,
            warehouse_ids=self._warehouse_ids,
            upto=upto,
        )

    async def job_work_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        return await _optional_supply_hook(
            "src.modules.job_work.service",
            "open_job_work_output",
            self._session,
            self._tenant,
            item_ids=item_ids,
            warehouse_ids=self._warehouse_ids,
            upto=upto,
        )

    async def manufacture_lead_times(
        self, quantities: Mapping[uuid.UUID, Decimal], as_of: date
    ) -> dict[uuid.UUID, int]:
        """Shop days to make each quantity, from the routing module.

        A straight pass-through to the routing module's own batched entry
        point, which reads the effective routings and their costed operations
        for the whole mapping in two statements. The planner never loads an
        operation itself: shop-time knowledge stays in the module that owns it.
        """
        if not quantities:
            return {}
        return await routing_service.manufacture_lead_times(
            self._session, self._tenant, quantities, as_of=as_of
        )


# -----------------------------------------------------------------------------
# The run's own tables
# -----------------------------------------------------------------------------


#: The names these records go by in user-facing messages. One spelling for the
#: ``get_or_404`` path (via ``entity_label``), the lock path and the state
#: machines, so the same missing record never answers "MrpRun not found" from
#: one route and "MRP run not found" from another.
MRP_RUN_ENTITY = "MRP run"
MRP_SUGGESTION_ENTITY = "MRP suggestion"


class MrpRunRepository(BaseRepository[MrpRun]):
    model = MrpRun
    entity_label = MRP_RUN_ENTITY
    searchable_fields = ("code",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": MrpRun.code,
        "status": MrpRun.status,
        "created_at": MrpRun.created_at,
        "started_at": MrpRun.started_at,
        "completed_at": MrpRun.completed_at,
        "demand_line_count": MrpRun.demand_line_count,
        "suggestion_count": MrpRun.suggestion_count,
    }

    async def lock_for_update(self, run_id: uuid.UUID) -> MrpRun | None:
        """Take a row lock on the run before reading the status it will be moved from.

        Every transition on this table is read-check-write: read ``status``, ask
        the state machine whether the move is legal, write the new one. Without
        the lock two actors both read the old value and both pass the check - two
        Celery deliveries of the same task both find ``QUEUED`` and both compute
        and write a full plan, and a worker completing a run can overwrite the
        ``FAILED`` that :func:`_close_abandoned_runs` just committed for it.

        ``populate_existing`` because the caller may already hold a copy of this
        row loaded before the lock was taken; a lock that hands back the stale
        status it was taken to re-read protects nothing.
        """
        stmt = (
            self._scoped()
            .where(MrpRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_runs(self) -> list[MrpRun]:
        """Runs still holding the company's planning slot, locked for update.

        Loaded as entities rather than counted: the caller has to decide whether
        an apparently-active run is genuinely running or was abandoned by a dead
        worker, and that needs its timestamps.

        Locked because that decision is acted on - a stale run is closed off as
        FAILED - and the other writer of these rows is a Celery worker, which
        holds the advisory lock this repository's caller took no part in. Rows
        are locked in ``(created_at, id)`` order, the same order
        :meth:`lock_for_update` would reach them in, so two callers cannot form a
        cycle.
        """
        stmt = (
            self._scoped()
            .where(MrpRun.status.in_(ACTIVE_RUN_STATUSES))
            # created_at is PostgreSQL now() - transaction START time - so every row
            # written in one transaction shares it. The UUIDv7 id is the
            # tiebreaker that recovers true insertion order.
            .order_by(MrpRun.created_at.asc(), MrpRun.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_completed(self) -> MrpRun | None:
        """The most recent finished plan - what the shortage report defaults to."""
        stmt = (
            self._scoped()
            .where(MrpRun.status == MrpRunStatus.COMPLETED)
            .order_by(MrpRun.completed_at.desc(), MrpRun.created_at.desc(), MrpRun.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    def _filtered(
        self,
        *,
        status: MrpRunStatus | None = None,
        created_from: date | None = None,
        created_to: date | None = None,
        search: str | None = None,
    ) -> Select[Any]:
        stmt = self._scoped()
        if status is not None:
            stmt = stmt.where(MrpRun.status == status)
        if created_from is not None:
            stmt = stmt.where(func.date(MrpRun.created_at) >= created_from)
        if created_to is not None:
            stmt = stmt.where(func.date(MrpRun.created_at) <= created_to)
        return self._apply_search(stmt, search)

    async def list_page_filtered(
        self,
        params: PageParams,
        sort: SortParams,
        *,
        status: MrpRunStatus | None = None,
        created_from: date | None = None,
        created_to: date | None = None,
        search: str | None = None,
    ) -> tuple[list[MrpRun], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, MrpRun.created_at.desc())
        stmt = self._filtered(
            status=status, created_from=created_from, created_to=created_to, search=search
        )
        return await self.list_page(params, stmt=stmt, order_by=order_by)


class MrpDemandRepository(BaseRepository[MrpDemand]):
    model = MrpDemand
    entity_label = "MRP demand"

    async def list_for_run(
        self, run_id: uuid.UUID, params: PageParams
    ) -> tuple[list[MrpDemand], OffsetPagination]:
        """One run's demand snapshot, most urgent first.

        ``paginate`` is called directly rather than through ``list_page`` because
        the ordering needs a deterministic tiebreaker as well as the sort key:
        without one, rows sharing a required date can repeat or vanish between
        pages.
        """
        stmt = (
            self._scoped()
            .where(MrpDemand.run_id == run_id)
            .order_by(MrpDemand.required_date.asc(), MrpDemand.id.asc())
        )
        return await paginate(self.session, stmt, params)

    async def all_for_run(self, run_id: uuid.UUID) -> list[MrpDemand]:
        """The whole demand snapshot, for the engine rather than for a screen.

        Deliberately not paginated - the engine plans against the complete set or
        it plans the wrong thing - but still bounded by ``MAX_DEMAND_LINES``,
        which is the same ceiling the run refused to be created above. One
        statement.
        """
        stmt = (
            self._scoped()
            .where(MrpDemand.run_id == run_id)
            .order_by(MrpDemand.required_date.asc(), MrpDemand.id.asc())
            .limit(MAX_DEMAND_LINES)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_completed_run_for_sales_order(self, order_id: uuid.UUID) -> MrpRun | None:
        """The newest completed run whose snapshot contained this order.

        Newest by when it finished, not when it was created: a run queued earlier
        but completed later is the newer plan. The lookup starts from the demand
        snapshot rather than from the run list because a run that did not see the
        order is not evidence about it - a newer plan scoped away from this
        order's warehouse must not blank out the last dates computed for it.
        """
        covered = select(MrpDemand.run_id).where(
            MrpDemand.company_id == self.tenant.company_id,
            MrpDemand.demand_source == MrpDemandSource.SALES_ORDER,
            MrpDemand.source_id == order_id,
        )
        stmt = (
            select(MrpRun)
            .where(
                MrpRun.company_id == self.tenant.company_id,
                MrpRun.status == MrpRunStatus.COMPLETED,
                MrpRun.id.in_(covered),
            )
            .order_by(MrpRun.completed_at.desc().nullslast(), MrpRun.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sales_order_lines_for_run(
        self, run_id: uuid.UUID, order_id: uuid.UUID
    ) -> list[MrpDemand]:
        """The snapshot rows for one sales order in one run.

        Not paginated: an order is bounded by ``MAX_LINES_PER_ORDER`` and the
        caller needs every line to answer for the document it is showing.
        """
        stmt = (
            self._scoped()
            .where(
                MrpDemand.run_id == run_id,
                MrpDemand.demand_source == MrpDemandSource.SALES_ORDER,
                MrpDemand.source_id == order_id,
            )
            .order_by(MrpDemand.source_line_id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_expected_availability(
        self, run_id: uuid.UUID, dates: dict[uuid.UUID, date]
    ) -> None:
        """Stamp post-plan expected availability onto the run's demand snapshot.

        The snapshot rows are written when the run is *created*, before the plan
        exists; the date material for each item can physically land is only known
        once the engine has run, so it is filled in by the same transaction that
        writes the requirements and suggestions. One statement whatever the
        number of lines - a loop of UPDATEs would be a per-line statement on the
        run-completion path, exactly what the batching contract forbids - and a
        CASE rather than a VALUES join so it works identically on every backend
        the tests use.
        """
        if not dates:
            return
        stmt = (
            update(MrpDemand)
            .where(
                MrpDemand.company_id == self.tenant.company_id,
                MrpDemand.run_id == run_id,
            )
            .values(
                expected_available_date=case(
                    *[(MrpDemand.item_id == item_id, when) for item_id, when in dates.items()],
                    else_=MrpDemand.expected_available_date,
                )
            )
        )
        await self.session.execute(stmt)


class MrpRequirementRepository(BaseRepository[MrpRequirement]):
    model = MrpRequirement
    entity_label = "MRP requirement"
    searchable_fields = ("item_code",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "item_code": MrpRequirement.item_code,
        "level": MrpRequirement.level,
        "low_level_code": MrpRequirement.low_level_code,
        "gross_requirement": MrpRequirement.gross_requirement,
        # Both are columns on the row and both are offered as sortable by the
        # shortage table, and neither was listed here - so the two questions a
        # planner most wants to ask that screen ("what am I shortest of" and
        # "what is already coming") answered 422 and emptied the table.
        "available_quantity": MrpRequirement.available_quantity,
        "incoming_quantity": MrpRequirement.incoming_quantity,
        "shortage_quantity": MrpRequirement.shortage_quantity,
        "earliest_required_date": MrpRequirement.earliest_required_date,
        "expected_available_date": MrpRequirement.expected_available_date,
    }

    async def list_page_filtered(
        self,
        run_id: uuid.UUID,
        params: PageParams,
        sort: SortParams,
        *,
        item_id: uuid.UUID | None = None,
        shortage_only: bool = True,
        search: str | None = None,
    ) -> tuple[list[MrpRequirement], OffsetPagination]:
        stmt = self._scoped().where(MrpRequirement.run_id == run_id)
        if item_id is not None:
            stmt = stmt.where(MrpRequirement.item_id == item_id)
        if shortage_only:
            # Matches the partial index built for exactly this predicate.
            stmt = stmt.where(MrpRequirement.shortage_quantity > 0)
        stmt = self._apply_search(stmt, search)
        order_by = sort.resolve(self.sortable_fields, MrpRequirement.earliest_required_date.asc())
        # The id tiebreaker is appended after the caller's sort key so paging
        # stays stable when many rows share a date or a quantity.
        stmt = stmt.order_by(order_by, MrpRequirement.id.asc())
        return await paginate(self.session, stmt, params)


class MrpSuggestionRepository(BaseRepository[MrpSuggestion]):
    model = MrpSuggestion
    entity_label = MRP_SUGGESTION_ENTITY
    searchable_fields = ("item_code",)
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "item_code": MrpSuggestion.item_code,
        "suggestion_type": MrpSuggestion.suggestion_type,
        "status": MrpSuggestion.status,
        "required_date": MrpSuggestion.required_date,
        "suggested_order_date": MrpSuggestion.suggested_order_date,
        "suggested_quantity": MrpSuggestion.suggested_quantity,
        "low_level_code": MrpSuggestion.low_level_code,
        "expected_available_date": MrpSuggestion.expected_available_date,
    }

    async def list_page_filtered(
        self,
        params: PageParams,
        sort: SortParams,
        *,
        run_id: uuid.UUID | None = None,
        suggestion_type: MrpSuggestionType | None = None,
        status: MrpSuggestionStatus | None = None,
        item_id: uuid.UUID | None = None,
        late_only: bool = False,
        search: str | None = None,
    ) -> tuple[list[MrpSuggestion], OffsetPagination]:
        stmt = self._scoped()
        if run_id is not None:
            stmt = stmt.where(MrpSuggestion.run_id == run_id)
        if suggestion_type is not None:
            stmt = stmt.where(MrpSuggestion.suggestion_type == suggestion_type)
        if status is not None:
            stmt = stmt.where(MrpSuggestion.status == status)
        if item_id is not None:
            stmt = stmt.where(MrpSuggestion.item_id == item_id)
        if late_only:
            stmt = stmt.where(MrpSuggestion.is_late.is_(True))
        stmt = self._apply_search(stmt, search)
        order_by = sort.resolve(self.sortable_fields, MrpSuggestion.suggested_order_date.asc())
        stmt = stmt.order_by(order_by, MrpSuggestion.id.asc())
        return await paginate(self.session, stmt, params)

    async def lock_for_update(self, suggestion_id: uuid.UUID) -> MrpSuggestion | None:
        """Take a row lock before deciding one suggestion's fate.

        Conversion is read-check-write: read ``status``, ask the state machine
        whether OPEN -> CONVERTED is legal, create the document, write the new
        status. Two simultaneous conversions of the same row both read OPEN, both
        pass the check and both raise a document - two purchase requisitions for
        one shortage. The ``(run_id, item_id)`` unique index cannot catch that:
        it constrains the suggestion, not the documents raised from it.

        ``populate_existing`` because the caller may already hold a copy loaded
        before the lock; a lock that returns the stale status protects nothing.
        """
        stmt = (
            self._scoped()
            .where(MrpSuggestion.id == suggestion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def load_for_bulk_decision(
        self, suggestion_ids: Sequence[uuid.UUID]
    ) -> list[MrpSuggestion]:
        """Lock several suggestions for a bulk decision, in one statement.

        Every requested row, whatever its status: the caller has to be able to
        tell "already converted" apart from "not in this tenant", and reporting
        both as skipped without knowing which is which would be a worse answer.

        Locked, not merely read: the caller decides on the status it sees here
        and writes it back, so an unlocked read lets a concurrent conversion slip
        between the two. Ordered by id so two concurrent bulk operations touching
        an overlapping set take their row locks in the same order and cannot
        deadlock each other - the same rule the inventory engine follows for
        balance rows. Must be called inside the caller's transaction; a lock
        taken outside one is released before it is used.
        """
        if not suggestion_ids:
            return []
        stmt = (
            self._scoped()
            .where(MrpSuggestion.id.in_(list(dict.fromkeys(suggestion_ids))))
            .order_by(MrpSuggestion.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_status(self, run_id: uuid.UUID) -> dict[str, int]:
        """Suggestion counts per status for one run - the run detail header."""
        stmt = (
            select(MrpSuggestion.status, func.count(MrpSuggestion.id))
            .where(
                MrpSuggestion.company_id == self.tenant.company_id,
                MrpSuggestion.run_id == run_id,
            )
            .group_by(MrpSuggestion.status)
        )
        return {str(status): int(count) for status, count in (await self.session.execute(stmt))}


__all__ = [
    "PLANNING_ITEM_COLUMNS",
    "DatabaseMrpSource",
    "MrpDemandRepository",
    "MrpRequirementRepository",
    "MrpRunRepository",
    "MrpScheduleSettingsRepository",
    "MrpSuggestionRepository",
    "PlanningItemRepository",
    "SalesDemandRepository",
    "ScheduledMrpConfiguration",
    "StockPositionRepository",
    "scheduled_configurations",
]
