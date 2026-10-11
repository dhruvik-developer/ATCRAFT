"""Query shapes for every report, and persistence for snapshots and the summary.

Three rules govern everything in this file.

**Nothing here aggregates a hot table without a bound.** Every period report is
filtered to a validated date range before it groups anything, and every position
report reads ``inventory_summary`` rather than ``inventory_balances`` or the
ledger (master rules section 25). The two reports that genuinely have to touch
``inventory_transactions`` - fast- and slow-moving - do so through the
``(company_id, transaction_type, occurred_at DESC)`` index with both ends of the
range pinned, and they are the first reports the service escalates to Celery.

**No SELECT \\*, and no N+1.** Each statement names the columns it needs, and
every per-parent total arrives as a joined grouped subquery rather than a query
per row. A report that needed one query per line would be the slowest code in the
system by a wide margin.

**Rows, not entities.** These statements return column tuples, so they cannot go
through :func:`src.core.pagination.paginate`, which reduces a result to
``.scalars()`` and would silently keep only the first column. :func:`paginate_rows`
below is the row-shaped equivalent - same keyset-free offset strategy, same
``limit + 1`` probe for ``has_next``, and the total is passed in because the
service has already had to count the rows to decide whether the report can be
served inline at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Numeric, Row, Select, and_, case, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.ids import new_id
from src.core.pagination import COUNT_CEILING, PageParams
from src.core.responses import OffsetPagination
from src.modules.customers.models import Customer
from src.modules.dispatch.models import (
    IN_FLIGHT_STATUSES,
    Dispatch,
    DispatchTrackingEvent,
)
from src.modules.finance.models import COST_CENTRE_ACCOUNT_TYPES, LedgerAccount, LedgerEntry
from src.modules.inventory.models import InventoryBalance, InventoryTransaction, TransactionType
from src.modules.job_work.models import JobWorkOrder, JobWorkOrderLine, JobWorkOrderStatus
from src.modules.mrp.models import MrpRequirement, MrpRun, MrpRunStatus
from src.modules.organization.models import CostCentre
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from src.modules.production.models import (
    ProductionEntry,
    ProductionOrder,
    ProductionOrderStatus,
    WorkOrder,
)
from src.modules.products.models import Item
from src.modules.quality.models import InspectionStatus, QcInspection
from src.modules.reports.models import InventorySummary, ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.routing.models import Machine, WorkCenter
from src.modules.sales.models import (
    OPEN_DEMAND_STATUSES,
    SalesOrder,
    SalesOrderLine,
    SalesOrderStatus,
)
from src.modules.vendors.models import Transporter, Vendor
from src.modules.warehouses.models import Warehouse

# -----------------------------------------------------------------------------
# Status vocabularies the reports are defined in terms of
# -----------------------------------------------------------------------------

#: A purchase order still owed to us: approved and in the vendor's hands, but not
#: yet fully received. DRAFT and PENDING_APPROVAL are excluded on purpose - they
#: are not commitments, and counting them as "pending purchases" would inflate
#: the buyer's exposure with orders nobody has agreed to place.
PENDING_PURCHASE_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
)

#: Production the shop floor still owes. CLOSED and COMPLETED are finished work;
#: DRAFT has not been approved and may never run.
PENDING_PRODUCTION_STATUSES: tuple[ProductionOrderStatus, ...] = (
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
)

#: Work actually under way, for the dashboard tile.
IN_PROGRESS_PRODUCTION_STATUSES: tuple[ProductionOrderStatus, ...] = (
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
)

#: Material sitting with a job-work vendor, or back but not yet cleared.
PENDING_JOB_WORK_STATUSES: tuple[JobWorkOrderStatus, ...] = (
    JobWorkOrderStatus.APPROVED,
    JobWorkOrderStatus.ISSUED,
    JobWorkOrderStatus.PARTIALLY_RECEIVED,
    JobWorkOrderStatus.QC_PENDING,
)

#: An inspection nobody has concluded yet.
PENDING_QC_STATUSES: tuple[InspectionStatus, ...] = (
    InspectionStatus.PENDING,
    InspectionStatus.IN_PROGRESS,
)

#: Orders that never became revenue. A draft was never promised to anybody and a
#: cancelled order was un-promised, so counting either as a sale would credit
#: margin to a transaction that did not happen.
NON_REVENUE_SALES_STATUSES: tuple[SalesOrderStatus, ...] = (
    SalesOrderStatus.DRAFT,
    SalesOrderStatus.CANCELLED,
)

#: What "movement" means for the fast/slow-moving reports: stock that left the
#: business or was consumed by it. Transfers are excluded deliberately - moving
#: a pallet between two of our own warehouses is not demand, and counting it
#: would make a warehouse reshuffle look like a sales spike.
CONSUMPTION_TYPES: tuple[TransactionType, ...] = (
    TransactionType.MATERIAL_ISSUE,
    TransactionType.DISPATCH,
    TransactionType.ASSEMBLY_CONSUME,
    TransactionType.JOB_WORK_ISSUE,
)

#: How many summary rows are upserted per statement. Large enough that a company
#: with 20 000 positions is a handful of round trips, small enough that one
#: statement never carries a multi-megabyte parameter list.
SUMMARY_REFRESH_BATCH = 1000

#: Minutes, as exact numeric. ``extract(epoch ...)`` is a double on older
#: PostgreSQL; casting before the division keeps the result off binary floating
#: point, which section 84 requires of anything that reaches a report.
_WORK_ORDER_MINUTES = (
    cast(
        func.extract("epoch", WorkOrder.actual_end_at - WorkOrder.actual_start_at),
        Numeric(24, 6),
    )
    / 60
)


def period_bounds(period: ReportPeriod) -> tuple[datetime, datetime]:
    """Half-open UTC instant range for a report period.

    Half-open, not inclusive-at-both-ends: a timestamp column compared with
    ``<= date_to`` silently drops everything that happened after midnight on the
    closing day, which is a whole day of movements missing from a report that
    looks complete. ``< date_to + 1 day`` cannot make that mistake.
    """
    start = datetime.combine(period.date_from, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(period.date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return start, end


async def count_rows(session: AsyncSession, stmt: Select[Any], *, cap: int) -> int:
    """How many rows the report would produce, counted no further than ``cap``.

    The cap is the point: the service only needs to know whether the result is
    small enough to serve inline, and counting three hundred thousand rows to
    learn that it is not would cost more than the report. ``ORDER BY`` is
    stripped because it is meaningless inside a count and forces a sort of rows
    PostgreSQL is only going to tally.
    """
    counted = select(func.count()).select_from(stmt.order_by(None).limit(cap + 1).subquery())
    return int((await session.execute(counted)).scalar_one())


async def paginate_rows(
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
    *,
    total: int | None = None,
) -> tuple[list[Row[Any]], OffsetPagination]:
    """Offset-paginate a row-shaped SELECT.

    ``limit + 1`` is fetched so ``has_next`` is known without a second query.
    ``total`` is supplied by the caller when it has already been counted; when it
    has not, the count is bounded by ``COUNT_CEILING`` exactly as the core
    paginator does, because past that the exact number is not worth a scan.
    """
    rows = list((await session.execute(stmt.limit(params.limit + 1).offset(params.offset))).all())
    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    if total is None and params.with_total:
        total = await count_rows(session, stmt, cap=COUNT_CEILING)

    total_pages = max(1, -(-total // params.limit)) if total is not None else None
    return rows, OffsetPagination(
        page=params.page,
        limit=params.limit,
        total=total,
        total_pages=total_pages,
        has_next=has_next,
        has_previous=params.page > 1,
    )


class ReportSnapshotRepository(BaseRepository[ReportSnapshot]):
    """Persistence for generated reports and their job rows."""

    model = ReportSnapshot
    sortable_fields = {  # noqa: RUF012 - SQLAlchemy API expects a plain dict
        "created_at": ReportSnapshot.created_at,
        "generated_at": ReportSnapshot.generated_at,
        "report_key": ReportSnapshot.report_key,
    }

    async def find_reusable(
        self,
        report_key: ReportKey,
        parameters_hash: str,
        *,
        now: datetime,
    ) -> ReportSnapshot | None:
        """The newest live artefact for exactly this request, if there is one.

        "Live" means queued, running, or completed and not yet expired. Returning
        a running job rather than starting a second one is what stops five people
        opening the same dashboard link from queuing five identical exports - the
        second caller polls the first caller's job.
        """
        stmt = (
            self._scoped()
            .where(
                ReportSnapshot.report_key == report_key,
                ReportSnapshot.parameters_hash == parameters_hash,
                or_(
                    ReportSnapshot.status.in_((ReportJobStatus.QUEUED, ReportJobStatus.RUNNING)),
                    and_(
                        ReportSnapshot.status == ReportJobStatus.COMPLETED,
                        or_(
                            ReportSnapshot.expires_at.is_(None),
                            ReportSnapshot.expires_at > now,
                        ),
                    ),
                ),
            )
            .order_by(ReportSnapshot.created_at.desc(), ReportSnapshot.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_expired(self, now: datetime, *, limit: int) -> list[ReportSnapshot]:
        """One bounded batch of lapsed snapshots, for the retention sweep."""
        stmt = (
            self._scoped()
            .where(ReportSnapshot.expires_at.is_not(None), ReportSnapshot.expires_at <= now)
            .order_by(ReportSnapshot.expires_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ReportQueryRepository(BaseRepository[InventorySummary]):
    """Every report's statement, plus the summary table this module owns.

    The declared model is ``InventorySummary`` because that is the one table this
    repository persists - the refresh below writes it, and the position reports
    read it through :meth:`_scoped`. The document-backed reports build their own
    tenant-filtered selects over other modules' tables: they are reads across
    aggregates this repository does not own, so they state the ``company_id``
    predicate explicitly on every table they touch rather than inheriting it from
    one.
    """

    model = InventorySummary

    # --- summary maintenance ------------------------------------------------

    def summary_source_stmt(self) -> Select[Any]:
        """Roll ``inventory_balances`` up to the (item, warehouse) grain.

        Grouping away location and batch here is what makes the summary an order
        of magnitude smaller than its source, and it is the only aggregation over
        a hot table this module performs - once every fifteen minutes, in a
        worker, not on a request.
        """
        return (
            select(
                InventoryBalance.item_id.label("item_id"),
                InventoryBalance.warehouse_id.label("warehouse_id"),
                func.sum(InventoryBalance.on_hand_quantity).label("on_hand"),
                func.sum(InventoryBalance.reserved_quantity).label("reserved"),
                func.sum(InventoryBalance.available_quantity).label("available"),
                func.sum(InventoryBalance.stock_value).label("valuation_amount"),
                func.max(InventoryBalance.last_movement_at).label("last_movement_at"),
            )
            .where(InventoryBalance.company_id == self.tenant.company_id)
            .group_by(InventoryBalance.item_id, InventoryBalance.warehouse_id)
        )

    async def upsert_summary(
        self, rows: Sequence[dict[str, Any]], *, refreshed_at: datetime
    ) -> int:
        """Insert or update one batch of summary rows. Returns rows written.

        ``ON CONFLICT DO UPDATE`` rather than delete-then-insert: a truncate and
        rebuild would leave the table empty for the duration of the refresh, and
        a dashboard rendered in that window would report zero stock. Updating in
        place means a reader always sees a complete position, either the previous
        one or the new one.
        """
        if not rows:
            return 0

        statement = pg_insert(InventorySummary).values(
            [
                {
                    "id": new_id(),
                    "company_id": self.tenant.company_id,
                    "item_id": row["item_id"],
                    "warehouse_id": row["warehouse_id"],
                    "on_hand": row["on_hand"],
                    "reserved": row["reserved"],
                    "available": row["available"],
                    "valuation_amount": row["valuation_amount"],
                    "last_movement_at": row["last_movement_at"],
                    "refreshed_at": refreshed_at,
                    "created_at": refreshed_at,
                    "updated_at": refreshed_at,
                }
                for row in rows
            ]
        )
        statement = statement.on_conflict_do_update(
            index_elements=["company_id", "item_id", "warehouse_id"],
            set_={
                "on_hand": statement.excluded.on_hand,
                "reserved": statement.excluded.reserved,
                "available": statement.excluded.available,
                "valuation_amount": statement.excluded.valuation_amount,
                "last_movement_at": statement.excluded.last_movement_at,
                "refreshed_at": statement.excluded.refreshed_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
        result = await self.session.execute(statement)
        return affected_rows(result)

    async def drop_stale_summary(self, *, refreshed_at: datetime) -> int:
        """Remove positions the refresh did not touch.

        A row whose balance has gone - the item was retired, or the last of it
        moved out of that warehouse and the balance row was removed - would
        otherwise sit in the summary forever, reporting stock that does not
        exist. Anything the current pass did not stamp is by definition no longer
        derivable from ``inventory_balances``.
        """
        statement = delete(InventorySummary).where(
            InventorySummary.company_id == self.tenant.company_id,
            InventorySummary.refreshed_at < refreshed_at,
        )
        return affected_rows(await self.session.execute(statement))

    async def summary_refreshed_at(self) -> datetime | None:
        """When this tenant's summary was last rebuilt, for the staleness label."""
        stmt = select(func.max(InventorySummary.refreshed_at)).where(
            InventorySummary.company_id == self.tenant.company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # --- helpers ------------------------------------------------------------

    @property
    def _company(self) -> uuid.UUID:
        return self.tenant.company_id

    def _summary_base(self, filters: ReportFilters) -> Select[Any]:
        """Summary joined to its item and warehouse, with the shared filters."""
        stmt = (
            select(
                InventorySummary.item_id.label("item_id"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                InventorySummary.warehouse_id.label("warehouse_id"),
                Warehouse.code.label("warehouse_code"),
                Warehouse.name.label("warehouse_name"),
                InventorySummary.on_hand.label("on_hand"),
                InventorySummary.reserved.label("reserved"),
                InventorySummary.available.label("available"),
                InventorySummary.valuation_amount.label("valuation_amount"),
                Item.reorder_level.label("reorder_level"),
                InventorySummary.last_movement_at.label("last_movement_at"),
                InventorySummary.refreshed_at.label("refreshed_at"),
            )
            .join(Item, Item.id == InventorySummary.item_id)
            .join(Warehouse, Warehouse.id == InventorySummary.warehouse_id)
            .where(InventorySummary.company_id == self._company)
        )
        if filters.item_id:
            stmt = stmt.where(InventorySummary.item_id == filters.item_id)
        if filters.warehouse_id:
            stmt = stmt.where(InventorySummary.warehouse_id == filters.warehouse_id)
        if filters.category_id:
            stmt = stmt.where(Item.category_id == filters.category_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))
        return stmt

    # --- 1. sales order status ---------------------------------------------

    def sales_order_status(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        totals = (
            select(
                SalesOrderLine.sales_order_id.label("sales_order_id"),
                func.sum(SalesOrderLine.quantity).label("ordered_quantity"),
                func.sum(SalesOrderLine.allocated_quantity).label("allocated_quantity"),
                func.sum(SalesOrderLine.dispatched_quantity).label("dispatched_quantity"),
            )
            # Joined to the header rather than filtered on the line table alone:
            # without it this subquery would group every line the company has
            # ever raised, and the date range on the outer query would not
            # narrow it.
            .join(SalesOrder, SalesOrder.id == SalesOrderLine.sales_order_id)
            .where(
                SalesOrderLine.company_id == self._company,
                SalesOrder.order_date.between(period.date_from, period.date_to),
            )
            .group_by(SalesOrderLine.sales_order_id)
            .subquery()
        )

        stmt = (
            select(
                SalesOrder.id.label("sales_order_id"),
                SalesOrder.code.label("code"),
                SalesOrder.order_date.label("order_date"),
                SalesOrder.required_date.label("required_date"),
                SalesOrder.status.label("status"),
                Customer.code.label("customer_code"),
                Customer.name.label("customer_name"),
                SalesOrder.total_amount.label("total_amount"),
                func.coalesce(totals.c.ordered_quantity, 0).label("ordered_quantity"),
                func.coalesce(totals.c.allocated_quantity, 0).label("allocated_quantity"),
                func.coalesce(totals.c.dispatched_quantity, 0).label("dispatched_quantity"),
            )
            .join(Customer, Customer.id == SalesOrder.customer_id)
            .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
            .where(
                SalesOrder.company_id == self._company,
                SalesOrder.order_date.between(period.date_from, period.date_to),
            )
            .order_by(SalesOrder.order_date.desc(), SalesOrder.code.desc())
        )
        if filters.customer_id:
            stmt = stmt.where(SalesOrder.customer_id == filters.customer_id)
        if filters.warehouse_id:
            stmt = stmt.where(SalesOrder.warehouse_id == filters.warehouse_id)
        if filters.status:
            stmt = stmt.where(SalesOrder.status == filters.status)
        if filters.search:
            stmt = stmt.where(SalesOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 2. stock availability ---------------------------------------------

    def stock_availability(self, filters: ReportFilters) -> Select[Any]:
        return self._summary_base(filters).order_by(Item.code.asc(), Warehouse.code.asc())

    # --- 3. MRP shortage ----------------------------------------------------

    def mrp_shortage(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        start, end = period_bounds(period)
        stmt = (
            select(
                MrpRequirement.item_id.label("item_id"),
                MrpRequirement.item_code.label("item_code"),
                MrpRun.code.label("run_code"),
                MrpRun.completed_at.label("run_completed_at"),
                MrpRequirement.gross_requirement.label("gross_requirement"),
                MrpRequirement.available_quantity.label("available_quantity"),
                MrpRequirement.incoming_quantity.label("incoming_quantity"),
                MrpRequirement.shortage_quantity.label("shortage_quantity"),
                MrpRequirement.earliest_required_date.label("earliest_required_date"),
            )
            .join(MrpRun, MrpRun.id == MrpRequirement.run_id)
            .where(
                MrpRequirement.company_id == self._company,
                MrpRun.status == MrpRunStatus.COMPLETED,
                MrpRun.completed_at >= start,
                MrpRun.completed_at < end,
                MrpRequirement.shortage_quantity > 0,
            )
            .order_by(
                MrpRequirement.earliest_required_date.asc(),
                MrpRequirement.shortage_quantity.desc(),
                MrpRequirement.item_code.asc(),
            )
        )
        if filters.item_id:
            stmt = stmt.where(MrpRequirement.item_id == filters.item_id)
        if filters.search:
            stmt = stmt.where(MrpRequirement.item_code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 4. purchase pending ------------------------------------------------

    def purchase_pending(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        totals = (
            select(
                PurchaseOrderLine.purchase_order_id.label("purchase_order_id"),
                func.sum(PurchaseOrderLine.quantity).label("ordered_quantity"),
                func.sum(PurchaseOrderLine.received_quantity).label("received_quantity"),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(
                PurchaseOrderLine.company_id == self._company,
                PurchaseOrder.po_date.between(period.date_from, period.date_to),
                PurchaseOrder.status.in_(PENDING_PURCHASE_STATUSES),
            )
            .group_by(PurchaseOrderLine.purchase_order_id)
            .subquery()
        )

        ordered = func.coalesce(totals.c.ordered_quantity, 0)
        received = func.coalesce(totals.c.received_quantity, 0)

        stmt = (
            select(
                PurchaseOrder.id.label("purchase_order_id"),
                PurchaseOrder.code.label("code"),
                PurchaseOrder.po_date.label("po_date"),
                PurchaseOrder.expected_delivery_date.label("expected_delivery_date"),
                PurchaseOrder.status.label("status"),
                Vendor.code.label("vendor_code"),
                Vendor.name.label("vendor_name"),
                PurchaseOrder.total_amount.label("total_amount"),
                ordered.label("ordered_quantity"),
                received.label("received_quantity"),
                (ordered - received).label("pending_quantity"),
            )
            .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
            .outerjoin(totals, totals.c.purchase_order_id == PurchaseOrder.id)
            .where(
                PurchaseOrder.company_id == self._company,
                PurchaseOrder.po_date.between(period.date_from, period.date_to),
                PurchaseOrder.status.in_(PENDING_PURCHASE_STATUSES),
            )
            .order_by(
                PurchaseOrder.expected_delivery_date.asc().nulls_last(),
                PurchaseOrder.code.asc(),
            )
        )
        if filters.vendor_id:
            stmt = stmt.where(PurchaseOrder.vendor_id == filters.vendor_id)
        if filters.warehouse_id:
            stmt = stmt.where(PurchaseOrder.warehouse_id == filters.warehouse_id)
        if filters.status:
            stmt = stmt.where(PurchaseOrder.status == filters.status)
        if filters.search:
            stmt = stmt.where(PurchaseOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 5. production pending ----------------------------------------------

    def production_pending(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        pending = ProductionOrder.planned_quantity - ProductionOrder.produced_quantity
        stmt = (
            select(
                ProductionOrder.id.label("production_order_id"),
                ProductionOrder.code.label("code"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                ProductionOrder.status.label("status"),
                ProductionOrder.priority.label("priority"),
                ProductionOrder.planned_quantity.label("planned_quantity"),
                ProductionOrder.produced_quantity.label("produced_quantity"),
                pending.label("pending_quantity"),
                ProductionOrder.planned_start_date.label("planned_start_date"),
                ProductionOrder.planned_end_date.label("planned_end_date"),
            )
            .join(Item, Item.id == ProductionOrder.item_id)
            .where(
                ProductionOrder.company_id == self._company,
                ProductionOrder.planned_start_date.between(period.date_from, period.date_to),
                ProductionOrder.status.in_(PENDING_PRODUCTION_STATUSES),
            )
            .order_by(
                ProductionOrder.planned_end_date.asc(),
                ProductionOrder.priority.asc(),
                ProductionOrder.code.asc(),
            )
        )
        if filters.item_id:
            stmt = stmt.where(ProductionOrder.item_id == filters.item_id)
        if filters.warehouse_id:
            stmt = stmt.where(ProductionOrder.warehouse_id == filters.warehouse_id)
        if filters.status:
            stmt = stmt.where(ProductionOrder.status == filters.status)
        if filters.search:
            stmt = stmt.where(ProductionOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 6. job-work pending ------------------------------------------------

    def job_work_pending(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        totals = (
            select(
                JobWorkOrderLine.job_work_order_id.label("job_work_order_id"),
                func.sum(JobWorkOrderLine.issued_quantity).label("issued_quantity"),
                func.sum(JobWorkOrderLine.received_output_quantity).label("received_quantity"),
            )
            .join(JobWorkOrder, JobWorkOrder.id == JobWorkOrderLine.job_work_order_id)
            .where(
                JobWorkOrderLine.company_id == self._company,
                JobWorkOrder.expected_return_date.between(period.date_from, period.date_to),
                JobWorkOrder.status.in_(PENDING_JOB_WORK_STATUSES),
            )
            .group_by(JobWorkOrderLine.job_work_order_id)
            .subquery()
        )

        issued = func.coalesce(totals.c.issued_quantity, 0)
        received = func.coalesce(totals.c.received_quantity, 0)

        stmt = (
            select(
                JobWorkOrder.id.label("job_work_order_id"),
                JobWorkOrder.code.label("code"),
                Vendor.code.label("vendor_code"),
                Vendor.name.label("vendor_name"),
                JobWorkOrder.process_type.label("process_type"),
                JobWorkOrder.status.label("status"),
                JobWorkOrder.expected_return_date.label("expected_return_date"),
                issued.label("issued_quantity"),
                received.label("received_quantity"),
                (issued - received).label("pending_quantity"),
                JobWorkOrder.total_charges.label("total_charges"),
            )
            .join(Vendor, Vendor.id == JobWorkOrder.vendor_id)
            .outerjoin(totals, totals.c.job_work_order_id == JobWorkOrder.id)
            .where(
                JobWorkOrder.company_id == self._company,
                JobWorkOrder.expected_return_date.between(period.date_from, period.date_to),
                JobWorkOrder.status.in_(PENDING_JOB_WORK_STATUSES),
            )
            .order_by(JobWorkOrder.expected_return_date.asc(), JobWorkOrder.code.asc())
        )
        if filters.vendor_id:
            stmt = stmt.where(JobWorkOrder.vendor_id == filters.vendor_id)
        if filters.status:
            stmt = stmt.where(JobWorkOrder.status == filters.status)
        if filters.search:
            stmt = stmt.where(JobWorkOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 7. QC rejection ----------------------------------------------------

    def qc_rejection(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        start, end = period_bounds(period)
        stmt = (
            select(
                QcInspection.id.label("inspection_id"),
                QcInspection.code.label("code"),
                QcInspection.qc_stage.label("qc_stage"),
                QcInspection.result.label("result"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                Vendor.name.label("vendor_name"),
                QcInspection.inspected_quantity.label("inspected_quantity"),
                QcInspection.accepted_quantity.label("accepted_quantity"),
                QcInspection.rejected_quantity.label("rejected_quantity"),
                QcInspection.rework_quantity.label("rework_quantity"),
                QcInspection.scrap_quantity.label("scrap_quantity"),
                QcInspection.rejection_reason.label("rejection_reason"),
                QcInspection.inspected_at.label("inspected_at"),
            )
            .join(Item, Item.id == QcInspection.item_id)
            .outerjoin(Vendor, Vendor.id == QcInspection.vendor_id)
            .where(
                QcInspection.company_id == self._company,
                QcInspection.created_at >= start,
                QcInspection.created_at < end,
                # A rejection report lists rejections. Scrap counts: it is
                # material the inspection refused, written off rather than
                # returned.
                or_(QcInspection.rejected_quantity > 0, QcInspection.scrap_quantity > 0),
            )
            .order_by(QcInspection.created_at.desc(), QcInspection.code.desc())
        )
        if filters.item_id:
            stmt = stmt.where(QcInspection.item_id == filters.item_id)
        if filters.vendor_id:
            stmt = stmt.where(QcInspection.vendor_id == filters.vendor_id)
        if filters.warehouse_id:
            stmt = stmt.where(QcInspection.warehouse_id == filters.warehouse_id)
        if filters.status:
            stmt = stmt.where(QcInspection.status == filters.status)
        if filters.search:
            stmt = stmt.where(QcInspection.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 8. vendor performance ---------------------------------------------

    def vendor_performance(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        #: On time means the goods arrived on or before the date the order
        #: promised. An order with no promised date cannot be judged, so it
        #: counts in neither the numerator nor the denominator - see
        #: ``service.on_time_percent``.
        on_time = case(
            (
                and_(
                    PurchaseOrder.expected_delivery_date.is_not(None),
                    GoodsReceipt.receipt_date <= PurchaseOrder.expected_delivery_date,
                ),
                GoodsReceipt.id,
            ),
            else_=None,
        )
        judgeable = case(
            (PurchaseOrder.expected_delivery_date.is_not(None), GoodsReceipt.id), else_=None
        )

        stmt = (
            select(
                Vendor.id.label("vendor_id"),
                Vendor.code.label("vendor_code"),
                Vendor.name.label("vendor_name"),
                func.count(func.distinct(judgeable)).label("receipt_count"),
                func.count(func.distinct(on_time)).label("on_time_receipt_count"),
                func.coalesce(func.sum(GoodsReceiptLine.received_quantity), 0).label(
                    "received_quantity"
                ),
                func.coalesce(func.sum(GoodsReceiptLine.accepted_quantity), 0).label(
                    "accepted_quantity"
                ),
                func.coalesce(func.sum(GoodsReceiptLine.rejected_quantity), 0).label(
                    "rejected_quantity"
                ),
            )
            .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
            .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
            .join(Vendor, Vendor.id == GoodsReceipt.vendor_id)
            .where(
                GoodsReceiptLine.company_id == self._company,
                GoodsReceipt.receipt_date.between(period.date_from, period.date_to),
                # A cancelled or still-draft receipt has not happened; scoring a
                # vendor on it would punish them for our paperwork.
                GoodsReceipt.status == GoodsReceiptStatus.POSTED,
            )
            .group_by(Vendor.id, Vendor.code, Vendor.name)
            .order_by(Vendor.name.asc(), Vendor.id.asc())
        )
        if filters.vendor_id:
            stmt = stmt.where(Vendor.id == filters.vendor_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Vendor.code.ilike(pattern), Vendor.name.ilike(pattern)))
        return stmt

    # --- 9. product profitability ------------------------------------------

    def product_profitability(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        # Revenue is taken net of discount and *before* tax: GST is collected on
        # the customer's behalf and is not margin. Using the tax-inclusive line
        # total would overstate profitability by the whole GST rate.
        net_line = SalesOrderLine.quantity * SalesOrderLine.unit_price - (
            SalesOrderLine.discount_amount
        )
        stmt = (
            select(
                Item.id.label("item_id"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                func.coalesce(func.sum(SalesOrderLine.quantity), 0).label("quantity_sold"),
                func.coalesce(func.sum(net_line), 0).label("revenue_amount"),
                func.coalesce(func.sum(SalesOrderLine.quantity * Item.standard_cost), 0).label(
                    "cost_amount"
                ),
            )
            .join(SalesOrder, SalesOrder.id == SalesOrderLine.sales_order_id)
            .join(Item, Item.id == SalesOrderLine.item_id)
            .where(
                SalesOrderLine.company_id == self._company,
                SalesOrder.order_date.between(period.date_from, period.date_to),
                # A draft is not revenue and a cancelled order is not either.
                SalesOrder.status.not_in(NON_REVENUE_SALES_STATUSES),
            )
            .group_by(Item.id, Item.code, Item.name)
            .order_by(func.coalesce(func.sum(net_line), 0).desc(), Item.code.asc())
        )
        if filters.item_id:
            stmt = stmt.where(Item.id == filters.item_id)
        if filters.category_id:
            stmt = stmt.where(Item.category_id == filters.category_id)
        if filters.customer_id:
            stmt = stmt.where(SalesOrder.customer_id == filters.customer_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))
        return stmt

    # --- 10/11. fast- and slow-moving --------------------------------------

    def _movement_subquery(self, period: ReportPeriod) -> Any:
        """Consumption per item over the period, from the indexed ledger slice."""
        start, end = period_bounds(period)
        return (
            select(
                InventoryTransaction.item_id.label("item_id"),
                # Outbound quantities are stored negative (database.md 4.2);
                # negating here makes "issued" a positive figure a reader can
                # compare across reports without remembering the sign convention.
                func.sum(-InventoryTransaction.quantity).label("issued_quantity"),
                func.count(InventoryTransaction.id).label("movement_count"),
                func.max(InventoryTransaction.occurred_at).label("last_movement_at"),
            )
            .where(
                InventoryTransaction.company_id == self._company,
                InventoryTransaction.transaction_type.in_(CONSUMPTION_TYPES),
                InventoryTransaction.occurred_at >= start,
                InventoryTransaction.occurred_at < end,
            )
            .group_by(InventoryTransaction.item_id)
            .subquery()
        )

    def _stock_subquery(self) -> Any:
        """On-hand per item, from the summary rather than from the balances."""
        return (
            select(
                InventorySummary.item_id.label("item_id"),
                func.sum(InventorySummary.on_hand).label("on_hand"),
            )
            .where(InventorySummary.company_id == self._company)
            .group_by(InventorySummary.item_id)
            .subquery()
        )

    def fast_moving(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        movement = self._movement_subquery(period)
        stock = self._stock_subquery()
        stmt = (
            select(
                Item.id.label("item_id"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                func.coalesce(stock.c.on_hand, 0).label("on_hand"),
                movement.c.issued_quantity.label("issued_quantity"),
                movement.c.movement_count.label("movement_count"),
                movement.c.last_movement_at.label("last_movement_at"),
            )
            .join(Item, Item.id == movement.c.item_id)
            .outerjoin(stock, stock.c.item_id == Item.id)
            .where(Item.company_id == self._company, Item.deleted_at.is_(None))
            .order_by(movement.c.issued_quantity.desc(), Item.code.asc())
        )
        return self._apply_item_filters(stmt, filters)

    def slow_moving(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        movement = self._movement_subquery(period)
        stock = self._stock_subquery()
        # Driven from stock held, not from the item master: an item nobody stocks
        # is not slow-moving, it is simply not stocked, and listing the whole
        # catalogue would bury the rows that cost money to hold.
        stmt = (
            select(
                Item.id.label("item_id"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                stock.c.on_hand.label("on_hand"),
                func.coalesce(movement.c.issued_quantity, 0).label("issued_quantity"),
                func.coalesce(movement.c.movement_count, 0).label("movement_count"),
                movement.c.last_movement_at.label("last_movement_at"),
            )
            .join(Item, Item.id == stock.c.item_id)
            .outerjoin(movement, movement.c.item_id == Item.id)
            .where(
                Item.company_id == self._company,
                Item.deleted_at.is_(None),
                stock.c.on_hand > 0,
            )
            .order_by(func.coalesce(movement.c.issued_quantity, 0).asc(), Item.code.asc())
        )
        return self._apply_item_filters(stmt, filters)

    def _apply_item_filters(self, stmt: Select[Any], filters: ReportFilters) -> Select[Any]:
        if filters.item_id:
            stmt = stmt.where(Item.id == filters.item_id)
        if filters.category_id:
            stmt = stmt.where(Item.category_id == filters.category_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))
        return stmt

    # --- 12. dead stock -----------------------------------------------------

    def dead_stock(self, filters: ReportFilters, *, cutoff: datetime) -> Select[Any]:
        return (
            self._summary_base(filters)
            .where(
                InventorySummary.on_hand > 0,
                # NULL is not "recent": a position that has never moved is the
                # oldest dead stock there is, and a bare ``<`` comparison would
                # drop exactly the rows the report exists to surface.
                or_(
                    InventorySummary.last_movement_at.is_(None),
                    InventorySummary.last_movement_at < cutoff,
                ),
            )
            .order_by(InventorySummary.valuation_amount.desc(), Item.code.asc())
        )

    # --- 13. order delay ----------------------------------------------------

    def order_delay(
        self, period: ReportPeriod, filters: ReportFilters, *, as_of: date
    ) -> Select[Any]:
        totals = (
            select(
                SalesOrderLine.sales_order_id.label("sales_order_id"),
                func.sum(SalesOrderLine.quantity).label("ordered_quantity"),
                func.sum(SalesOrderLine.dispatched_quantity).label("dispatched_quantity"),
            )
            .join(SalesOrder, SalesOrder.id == SalesOrderLine.sales_order_id)
            .where(
                SalesOrderLine.company_id == self._company,
                SalesOrder.required_date.between(period.date_from, period.date_to),
            )
            .group_by(SalesOrderLine.sales_order_id)
            .subquery()
        )

        stmt = (
            select(
                SalesOrder.id.label("sales_order_id"),
                SalesOrder.code.label("code"),
                Customer.code.label("customer_code"),
                Customer.name.label("customer_name"),
                SalesOrder.status.label("status"),
                SalesOrder.order_date.label("order_date"),
                SalesOrder.required_date.label("required_date"),
                SalesOrder.promised_date.label("promised_date"),
                SalesOrder.total_amount.label("total_amount"),
                func.coalesce(totals.c.ordered_quantity, 0).label("ordered_quantity"),
                func.coalesce(totals.c.dispatched_quantity, 0).label("dispatched_quantity"),
            )
            .join(Customer, Customer.id == SalesOrder.customer_id)
            .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
            .where(
                SalesOrder.company_id == self._company,
                SalesOrder.required_date.between(period.date_from, period.date_to),
                # Still owed, and the date we promised has passed. An order due
                # tomorrow is not late, however little of it is allocated.
                SalesOrder.status.in_(OPEN_DEMAND_STATUSES),
                SalesOrder.required_date < as_of,
            )
            .order_by(SalesOrder.required_date.asc(), SalesOrder.code.asc())
        )
        if filters.customer_id:
            stmt = stmt.where(SalesOrder.customer_id == filters.customer_id)
        if filters.search:
            stmt = stmt.where(SalesOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 14. production efficiency -----------------------------------------

    def production_efficiency(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        effort = (
            select(
                WorkOrder.production_order_id.label("production_order_id"),
                func.coalesce(
                    func.sum(WorkOrder.planned_setup_minutes + WorkOrder.planned_run_minutes), 0
                ).label("planned_minutes"),
                func.coalesce(func.sum(_WORK_ORDER_MINUTES), 0).label("actual_minutes"),
            )
            .join(ProductionOrder, ProductionOrder.id == WorkOrder.production_order_id)
            .where(
                WorkOrder.company_id == self._company,
                ProductionOrder.actual_end_date.between(period.date_from, period.date_to),
            )
            .group_by(WorkOrder.production_order_id)
            .subquery()
        )

        stmt = (
            select(
                ProductionOrder.id.label("production_order_id"),
                ProductionOrder.code.label("code"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                ProductionOrder.planned_quantity.label("planned_quantity"),
                ProductionOrder.produced_quantity.label("produced_quantity"),
                ProductionOrder.rejected_quantity.label("rejected_quantity"),
                ProductionOrder.scrap_quantity.label("scrap_quantity"),
                func.coalesce(effort.c.planned_minutes, 0).label("planned_minutes"),
                func.coalesce(effort.c.actual_minutes, 0).label("actual_minutes"),
                ProductionOrder.planned_end_date.label("planned_end_date"),
                ProductionOrder.actual_end_date.label("actual_end_date"),
            )
            .join(Item, Item.id == ProductionOrder.item_id)
            .outerjoin(effort, effort.c.production_order_id == ProductionOrder.id)
            .where(
                ProductionOrder.company_id == self._company,
                ProductionOrder.actual_end_date.between(period.date_from, period.date_to),
                ProductionOrder.status.in_(
                    (ProductionOrderStatus.COMPLETED, ProductionOrderStatus.CLOSED)
                ),
            )
            .order_by(ProductionOrder.actual_end_date.desc(), ProductionOrder.code.asc())
        )
        if filters.item_id:
            stmt = stmt.where(ProductionOrder.item_id == filters.item_id)
        if filters.search:
            stmt = stmt.where(ProductionOrder.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 15. machine utilisation -------------------------------------------

    def machine_utilisation(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        start, end = period_bounds(period)

        downtime = (
            select(
                ProductionEntry.machine_id.label("machine_id"),
                func.coalesce(func.sum(ProductionEntry.downtime_minutes), 0).label(
                    "downtime_minutes"
                ),
            )
            .where(
                ProductionEntry.company_id == self._company,
                ProductionEntry.machine_id.is_not(None),
                ProductionEntry.entry_date.between(period.date_from, period.date_to),
            )
            .group_by(ProductionEntry.machine_id)
            .subquery()
        )

        stmt = (
            select(
                Machine.id.label("machine_id"),
                Machine.code.label("machine_code"),
                Machine.name.label("machine_name"),
                WorkCenter.code.label("work_center_code"),
                WorkCenter.working_hours_per_day.label("working_hours_per_day"),
                func.count(WorkOrder.id).label("work_order_count"),
                func.coalesce(func.sum(_WORK_ORDER_MINUTES), 0).label("busy_minutes"),
                func.coalesce(func.max(downtime.c.downtime_minutes), 0).label("downtime_minutes"),
                func.coalesce(func.sum(WorkOrder.completed_quantity), 0).label(
                    "completed_quantity"
                ),
                func.coalesce(func.sum(WorkOrder.rejected_quantity), 0).label("rejected_quantity"),
            )
            .join(Machine, Machine.id == WorkOrder.machine_id)
            .join(WorkCenter, WorkCenter.id == Machine.work_center_id)
            .outerjoin(downtime, downtime.c.machine_id == Machine.id)
            .where(
                WorkOrder.company_id == self._company,
                # Only finished runs: an operation still open has no elapsed time
                # yet, and treating its NULL end as "now" would credit a machine
                # with hours it has not worked.
                WorkOrder.actual_start_at.is_not(None),
                WorkOrder.actual_end_at.is_not(None),
                WorkOrder.actual_start_at >= start,
                WorkOrder.actual_start_at < end,
            )
            .group_by(
                Machine.id,
                Machine.code,
                Machine.name,
                WorkCenter.code,
                WorkCenter.working_hours_per_day,
            )
            .order_by(func.coalesce(func.sum(_WORK_ORDER_MINUTES), 0).desc(), Machine.code.asc())
        )
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Machine.code.ilike(pattern), Machine.name.ilike(pattern)))
        return stmt

    # --- 16. inventory valuation -------------------------------------------

    def inventory_valuation(self, filters: ReportFilters) -> Select[Any]:
        return (
            self._summary_base(filters)
            .where(or_(InventorySummary.on_hand > 0, InventorySummary.valuation_amount != 0))
            .order_by(InventorySummary.valuation_amount.desc(), Item.code.asc())
        )

    # --- 17. dispatch -------------------------------------------------------

    def dispatch(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        stmt = (
            select(
                Dispatch.id.label("dispatch_id"),
                Dispatch.code.label("code"),
                Dispatch.dispatch_type.label("dispatch_type"),
                Dispatch.status.label("status"),
                Dispatch.dispatch_date.label("dispatch_date"),
                Dispatch.expected_delivery_date.label("expected_delivery_date"),
                Dispatch.actual_delivery_date.label("actual_delivery_date"),
                Customer.name.label("customer_name"),
                Vendor.name.label("vendor_name"),
                Transporter.name.label("transporter_name"),
                Dispatch.total_boxes.label("total_boxes"),
                Dispatch.total_weight_kg.label("total_weight_kg"),
                Dispatch.freight_amount.label("freight_amount"),
                Dispatch.tracking_id.label("tracking_id"),
                Dispatch.lr_number.label("lr_number"),
            )
            .outerjoin(Customer, Customer.id == Dispatch.customer_id)
            .outerjoin(Vendor, Vendor.id == Dispatch.vendor_id)
            .outerjoin(Transporter, Transporter.id == Dispatch.transporter_id)
            .where(
                Dispatch.company_id == self._company,
                Dispatch.dispatch_date.between(period.date_from, period.date_to),
            )
            .order_by(Dispatch.dispatch_date.desc(), Dispatch.code.desc())
        )
        if filters.customer_id:
            stmt = stmt.where(Dispatch.customer_id == filters.customer_id)
        if filters.vendor_id:
            stmt = stmt.where(Dispatch.vendor_id == filters.vendor_id)
        if filters.warehouse_id:
            stmt = stmt.where(Dispatch.warehouse_id == filters.warehouse_id)
        if filters.status:
            stmt = stmt.where(Dispatch.status == filters.status)
        if filters.search:
            stmt = stmt.where(Dispatch.code.ilike(f"%{filters.search.strip()}%"))
        return stmt

    # --- 18. courier / transport tracking ----------------------------------

    def courier_tracking(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        # ``row_number`` rather than a join on ``max(event_time)``: two events
        # recorded in the same millisecond - a courier feed and a manual entry -
        # would both match the maximum and duplicate the consignment on the
        # board. Ranking on (time, id) is a total order, so exactly one wins.
        ranked = (
            select(
                DispatchTrackingEvent.dispatch_id.label("dispatch_id"),
                DispatchTrackingEvent.event_status.label("event_status"),
                DispatchTrackingEvent.event_time.label("event_time"),
                DispatchTrackingEvent.location.label("location"),
                func.row_number()
                .over(
                    partition_by=DispatchTrackingEvent.dispatch_id,
                    order_by=(
                        DispatchTrackingEvent.event_time.desc(),
                        DispatchTrackingEvent.id.desc(),
                    ),
                )
                .label("recency"),
            )
            .where(DispatchTrackingEvent.company_id == self._company)
            .subquery()
        )
        latest = select(ranked).where(ranked.c.recency == 1).subquery()

        stmt = (
            select(
                Dispatch.id.label("dispatch_id"),
                Dispatch.code.label("code"),
                Dispatch.status.label("status"),
                Dispatch.dispatch_date.label("dispatch_date"),
                Dispatch.expected_delivery_date.label("expected_delivery_date"),
                Dispatch.courier_name.label("courier_name"),
                Dispatch.transport_name.label("transport_name"),
                Transporter.name.label("transporter_name"),
                Dispatch.tracking_id.label("tracking_id"),
                Dispatch.lr_number.label("lr_number"),
                Dispatch.vehicle_number.label("vehicle_number"),
                latest.c.event_status.label("last_event_status"),
                latest.c.event_time.label("last_event_at"),
                latest.c.location.label("last_event_location"),
            )
            .outerjoin(Transporter, Transporter.id == Dispatch.transporter_id)
            .outerjoin(latest, latest.c.dispatch_id == Dispatch.id)
            .where(
                Dispatch.company_id == self._company,
                Dispatch.dispatch_date.between(period.date_from, period.date_to),
                Dispatch.status.in_(IN_FLIGHT_STATUSES),
            )
            .order_by(
                Dispatch.expected_delivery_date.asc().nulls_last(),
                Dispatch.code.asc(),
            )
        )
        if filters.customer_id:
            stmt = stmt.where(Dispatch.customer_id == filters.customer_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(
                or_(Dispatch.code.ilike(pattern), Dispatch.tracking_id.ilike(pattern))
            )
        return stmt

    # --- 19. vendor dispatch history ---------------------------------------

    def vendor_dispatch_history(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        stmt = (
            select(
                Dispatch.id.label("dispatch_id"),
                Dispatch.code.label("code"),
                Vendor.code.label("vendor_code"),
                Vendor.name.label("vendor_name"),
                Dispatch.dispatch_type.label("dispatch_type"),
                Dispatch.status.label("status"),
                Dispatch.dispatch_date.label("dispatch_date"),
                Dispatch.expected_delivery_date.label("expected_delivery_date"),
                Dispatch.actual_delivery_date.label("actual_delivery_date"),
                Dispatch.total_boxes.label("total_boxes"),
                Dispatch.total_weight_kg.label("total_weight_kg"),
                Dispatch.freight_amount.label("freight_amount"),
                Dispatch.tracking_id.label("tracking_id"),
            )
            .join(Vendor, Vendor.id == Dispatch.vendor_id)
            .where(
                Dispatch.company_id == self._company,
                Dispatch.dispatch_date.between(period.date_from, period.date_to),
            )
            .order_by(Dispatch.dispatch_date.desc(), Dispatch.code.desc())
        )
        if filters.vendor_id:
            stmt = stmt.where(Dispatch.vendor_id == filters.vendor_id)
        if filters.status:
            stmt = stmt.where(Dispatch.status == filters.status)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Vendor.name.ilike(pattern), Dispatch.code.ilike(pattern)))
        return stmt

    # --- 20. cost centre statement -----------------------------------------

    def cost_centre_expense(self, period: ReportPeriod, filters: ReportFilters) -> Select[Any]:
        """Posted income and expense per (cost centre, account) over the period.

        Read from ``ledger_entries`` - the posted, immutable history - rather
        than from documents, so it agrees with the ledger by construction and
        includes every route money took into it: invoices, direct journals and
        their reversals. One grouped scan through the partial
        ``(company_id, cost_centre_id, entry_date)`` index, bounded by the
        validated period like every other period report.

        A retired cost centre still appears: posted history keeps its centre.
        Only INCOME and EXPENSE accounts are read, which is also the only place
        ``post_journal`` lets a centre be carried.
        """
        debit = func.sum(LedgerEntry.debit_amount)
        credit = func.sum(LedgerEntry.credit_amount)
        stmt = (
            select(
                CostCentre.id.label("cost_centre_id"),
                CostCentre.code.label("cost_centre_code"),
                CostCentre.name.label("cost_centre_name"),
                LedgerAccount.id.label("account_id"),
                LedgerAccount.code.label("account_code"),
                LedgerAccount.name.label("account_name"),
                LedgerAccount.account_type.label("account_type"),
                debit.label("debit_amount"),
                credit.label("credit_amount"),
                (debit - credit).label("net_amount"),
            )
            .select_from(LedgerEntry)
            .join(
                CostCentre,
                and_(
                    CostCentre.id == LedgerEntry.cost_centre_id,
                    CostCentre.company_id == LedgerEntry.company_id,
                ),
            )
            .join(
                LedgerAccount,
                and_(
                    LedgerAccount.id == LedgerEntry.account_id,
                    LedgerAccount.company_id == LedgerEntry.company_id,
                ),
            )
            .where(
                LedgerEntry.company_id == self._company,
                LedgerEntry.cost_centre_id.is_not(None),
                LedgerEntry.entry_date.between(period.date_from, period.date_to),
                LedgerAccount.account_type.in_(sorted(COST_CENTRE_ACCOUNT_TYPES)),
            )
            .group_by(
                CostCentre.id,
                CostCentre.code,
                CostCentre.name,
                LedgerAccount.id,
                LedgerAccount.code,
                LedgerAccount.name,
                LedgerAccount.account_type,
            )
            # Codes are unique per company, so this order is total.
            .order_by(CostCentre.code.asc(), LedgerAccount.code.asc())
        )
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(CostCentre.code.ilike(pattern), CostCentre.name.ilike(pattern)))
        return stmt

    # --- dashboard ----------------------------------------------------------

    def dashboard(self, *, as_of: date) -> Select[Any]:
        """Every management tile in one statement.

        Eight scalar subqueries in a single SELECT rather than eight round trips:
        each is an indexed count or a sum over the summary table, and issuing
        them together means the dashboard costs one network round trip and one
        planning pass instead of eight of each (section 30).
        """
        company = self._company

        open_orders = (
            select(func.count(SalesOrder.id))
            .where(
                SalesOrder.company_id == company,
                SalesOrder.status.in_(OPEN_DEMAND_STATUSES),
            )
            .scalar_subquery()
        )
        overdue_orders = (
            select(func.count(SalesOrder.id))
            .where(
                SalesOrder.company_id == company,
                SalesOrder.status.in_(OPEN_DEMAND_STATUSES),
                SalesOrder.required_date < as_of,
            )
            .scalar_subquery()
        )
        stock_value = (
            select(func.coalesce(func.sum(InventorySummary.valuation_amount), 0))
            .where(InventorySummary.company_id == company)
            .scalar_subquery()
        )
        stock_refreshed = (
            select(func.max(InventorySummary.refreshed_at))
            .where(InventorySummary.company_id == company)
            .scalar_subquery()
        )
        pending_pos = (
            select(func.count(PurchaseOrder.id))
            .where(
                PurchaseOrder.company_id == company,
                PurchaseOrder.status.in_(PENDING_PURCHASE_STATUSES),
            )
            .scalar_subquery()
        )
        pending_po_value = (
            select(func.coalesce(func.sum(PurchaseOrder.total_amount), 0))
            .where(
                PurchaseOrder.company_id == company,
                PurchaseOrder.status.in_(PENDING_PURCHASE_STATUSES),
            )
            .scalar_subquery()
        )
        production_wip = (
            select(func.count(ProductionOrder.id))
            .where(
                ProductionOrder.company_id == company,
                ProductionOrder.status.in_(IN_PROGRESS_PRODUCTION_STATUSES),
            )
            .scalar_subquery()
        )
        qc_pending = (
            select(func.count(QcInspection.id))
            .where(
                QcInspection.company_id == company,
                QcInspection.status.in_(PENDING_QC_STATUSES),
            )
            .scalar_subquery()
        )
        in_transit = (
            select(func.count(Dispatch.id))
            .where(Dispatch.company_id == company, Dispatch.status.in_(IN_FLIGHT_STATUSES))
            .scalar_subquery()
        )
        overdue_deliveries = (
            select(func.count(Dispatch.id))
            .where(
                Dispatch.company_id == company,
                Dispatch.status.in_(IN_FLIGHT_STATUSES),
                Dispatch.expected_delivery_date.is_not(None),
                Dispatch.expected_delivery_date < as_of,
            )
            .scalar_subquery()
        )

        return select(
            open_orders.label("open_sales_orders"),
            overdue_orders.label("overdue_sales_orders"),
            stock_value.label("stock_value"),
            pending_pos.label("pending_purchase_orders"),
            pending_po_value.label("pending_purchase_value"),
            production_wip.label("production_in_progress"),
            qc_pending.label("qc_pending"),
            in_transit.label("dispatches_in_transit"),
            overdue_deliveries.label("overdue_deliveries"),
            stock_refreshed.label("stock_refreshed_at"),
        )


__all__ = [
    "CONSUMPTION_TYPES",
    "IN_PROGRESS_PRODUCTION_STATUSES",
    "NON_REVENUE_SALES_STATUSES",
    "PENDING_JOB_WORK_STATUSES",
    "PENDING_PRODUCTION_STATUSES",
    "PENDING_PURCHASE_STATUSES",
    "PENDING_QC_STATUSES",
    "SUMMARY_REFRESH_BATCH",
    "ReportQueryRepository",
    "ReportSnapshotRepository",
    "count_rows",
    "paginate_rows",
    "period_bounds",
]
