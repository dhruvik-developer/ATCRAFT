"""Reporting rules: what each report means, and how big it is allowed to get.

Master rules section 25. Four decisions carry this module.

**A report is a definition, not a handler.** Nineteen reports sharing one runner
means the rules that matter - the bounded period, the row ceiling, the cache
policy, the audit entry - are applied nineteen times by construction rather than
nineteen times by discipline. A new report is a :class:`ReportDefinition` and a
route; it cannot forget to paginate, because it never gets the chance to.

**Arithmetic lives in pure functions.** Every percentage, bucket and days-overdue
figure in this file is computed by a module-level function that takes numbers and
returns numbers. That is not tidiness: an ageing bucket that is off by one at its
boundary, or a utilisation figure that divides by a zero-hour day, is a wrong
number on a management screen that nobody can reproduce afterwards. Written this
way they are tested directly, without a database.

**Size decides where the work happens, and the server decides, not the client.**
The runner counts the result - bounded, so counting a huge report is cheap - and
anything past :data:`REPORT_INLINE_ROW_LIMIT` becomes a Celery job answered with
``202`` rather than a request held open while PostgreSQL streams a hundred
thousand rows into a JSON array.

**Cache the historical, never the promisable.** A completed period is a fact and
is cached per tenant with a TTL. Stock availability and valuation are not cached
at any TTL: section 34 is explicit that a stale available quantity which lets two
orders reserve the same stock is a correctness bug, not a performance win.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cache import CacheNamespace, cache_get, cache_set, make_key
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError, ErrorCode, NotFoundError, ValidationError
from src.core.logging import get_logger
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination, api_path
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.reports.models import (
    DOCUMENT_ONLY_REPORT_KEYS,
    REPORT_JOB_LIFECYCLE,
    ReportJobStatus,
    ReportKey,
    ReportSnapshot,
)
from src.modules.reports.repository import (
    SUMMARY_REFRESH_BATCH,
    ReportQueryRepository,
    ReportSnapshotRepository,
    count_rows,
    paginate_rows,
)
from src.modules.reports.schemas import (
    CostCentreExpenseRow,
    CourierTrackingRow,
    DashboardRead,
    DeadStockRow,
    DispatchRow,
    InventoryValuationRow,
    ItemMovementRow,
    JobWorkPendingRow,
    MachineUtilisationRow,
    MrpShortageRow,
    OrderDelayRow,
    ProductionEfficiencyRow,
    ProductionPendingRow,
    ProductProfitabilityRow,
    PurchasePendingRow,
    QcRejectionRow,
    ReportColumnRead,
    ReportDescriptorRead,
    ReportFilters,
    ReportJobRead,
    ReportPeriod,
    ReportRowBase,
    SalesOrderStatusRow,
    StockAvailabilityRow,
    VendorDispatchHistoryRow,
    VendorPerformanceRow,
)

log = get_logger(__name__)

# -----------------------------------------------------------------------------
# Policy constants
# -----------------------------------------------------------------------------

#: Past this many rows a report is generated in the background instead of being
#: returned inline. Chosen as the point where the *whole* result stops being
#: something a person reads and starts being something they open in a
#: spreadsheet: serialising it into a paginated JSON response would hold a
#: connection open to produce something nobody scrolls through.
REPORT_INLINE_ROW_LIMIT = 5000

#: Hard ceiling on a background report. A worker that tried to materialise an
#: unbounded result would exhaust its memory and be killed, which looks to the
#: user exactly like the report silently never arriving. Truncating at a stated
#: limit and saying so in the artefact is the honest failure.
REPORT_MAX_EXPORT_ROWS = 200_000

#: How long a generated artefact is reused before the same request regenerates
#: it. Long enough that a team sharing a link does not queue five identical
#: exports, short enough that "yesterday's numbers" is never a surprise.
REPORT_SNAPSHOT_TTL_HOURS = 24

#: Default age at which stock is called dead. ASSUMPTION: the master rules name
#: a dead-stock report but fix no threshold, so the conservative industry default
#: of six months is used and is overridable per request via ``min_days``. It is a
#: named constant rather than a literal precisely because it is a policy choice
#: somebody will want to change.
DEAD_STOCK_DAYS = 180

#: Celery task name. Must match the ``@async_task(name=...)`` in
#: ``src.workers.tasks.reports``; the API dispatches by name so it never imports
#: the worker module.
REPORT_TASK_NAME = "reports.generate"

#: Queue the generic report job lands on. Report rendering is slow and must not
#: sit in front of a 200 ms notification.
REPORT_QUEUE = "reports"

#: Where a client polls for a background report, and where it fetches the file.
#: Relative to the mounted API prefix: the routers are included under
#: ``settings.api_v1_prefix`` and an operator may change it, so the absolute
#: form is built at call time by :func:`job_status_url` and
#: :func:`document_download_url` rather than baked in here. Every 202 body and
#: notification link goes through those two functions, which is what keeps the
#: URLs from drifting away from the routes that serve them.
JOB_STATUS_URL_TEMPLATE = "/reports/jobs/{job_id}"
DOCUMENT_DOWNLOAD_URL_TEMPLATE = "/documents/{document_id}/download"

AUDIT_ENTITY = "report_snapshot"
SUMMARY_AUDIT_ENTITY = "inventory_summary"

PERCENT_QUANTUM = Decimal("0.01")
QUANTITY_QUANTUM = Decimal("0.0001")


# -----------------------------------------------------------------------------
# URLs returned in bodies - built under the mounted prefix, at call time
# -----------------------------------------------------------------------------


def _api_path(path: str) -> str:
    """Absolute API path under the mounted prefix - :func:`src.core.responses.api_path`."""
    return api_path(path)


def job_status_url(job_id: uuid.UUID | str) -> str:
    """Where a client polls one background job: ``GET /reports/jobs/{id}``."""
    return _api_path(JOB_STATUS_URL_TEMPLATE.format(job_id=job_id))


def document_download_url(document_id: uuid.UUID | str) -> str:
    """Where a client fetches a generated artefact from the document store."""
    return _api_path(DOCUMENT_DOWNLOAD_URL_TEMPLATE.format(document_id=document_id))


# -----------------------------------------------------------------------------
# Pure arithmetic - no session, no ORM, no clock of its own
# -----------------------------------------------------------------------------


def percent(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    """``numerator / denominator`` as a percentage, to two places.

    A zero denominator yields zero rather than raising. Every caller here is
    asking "what proportion of nothing happened", and the truthful answer on a
    report is 0.00%, not a 500 - an item nobody inspected has no rejection rate,
    and a machine with no scheduled hours has no utilisation.
    """
    denominator = Decimal(denominator)
    if denominator == 0:
        return Decimal("0.00")
    value = Decimal(numerator) / denominator * 100
    return value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    """Quantity ratio to four places, zero-safe on the same reasoning."""
    denominator = Decimal(denominator)
    if denominator == 0:
        return Decimal("0")
    return (Decimal(numerator) / denominator).quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_UP)


#: Ageing bands, upper bound inclusive. One definition shared by every ageing
#: column in the module: purchase overdue, job-work overdue, order delay and dead
#: stock all bucket the same way, and two of them disagreeing would make the
#: reports incomparable to each other.
AGEING_BUCKETS: tuple[tuple[int, str], ...] = (
    (30, "1-30 days"),
    (60, "31-60 days"),
    (90, "61-90 days"),
    (180, "91-180 days"),
    (365, "181-365 days"),
)
CURRENT_BUCKET = "Current"
OVER_AGE_BUCKET = "Over 365 days"
NEVER_MOVED_BUCKET = "Never moved"


def ageing_bucket(days: int | None) -> str:
    """Which ageing band a number of days falls in.

    ``None`` means "no movement has ever been recorded", which is not the same as
    zero days old and must not be bucketed as current - it is the oldest state a
    position can be in. Zero or negative days is genuinely current: something due
    today is not overdue, and treating it as one day late would put every day's
    work on the overdue list at midnight.
    """
    if days is None:
        return NEVER_MOVED_BUCKET
    if days <= 0:
        return CURRENT_BUCKET
    for upper, label in AGEING_BUCKETS:
        if days <= upper:
            return label
    return OVER_AGE_BUCKET


def overdue_days(due: date | None, as_of: date) -> int:
    """How many days past ``due`` we are, never negative.

    A document with no due date cannot be overdue: inventing one would put a
    permanent false positive on every list that reads this.
    """
    if due is None:
        return 0
    return max(0, (as_of - due).days)


def days_between(start: datetime | None, end: datetime) -> int | None:
    """Whole days from ``start`` to ``end``; ``None`` in, ``None`` out."""
    if start is None:
        return None
    return max(0, (end - start).days)


#: Urgency bands for a shortage, in days until it bites.
URGENCY_OVERDUE = "Overdue"
URGENCY_THIS_WEEK = "Within 7 days"
URGENCY_THIS_MONTH = "Within 30 days"
URGENCY_LATER = "Beyond 30 days"


def urgency_bucket(days_to_required: int) -> str:
    """How soon a shortage has to be covered.

    Separate from :func:`ageing_bucket` on purpose: ageing looks backwards at how
    long something has been late, urgency looks forward at how long there is to
    fix it, and a single scale would make one of the two read backwards.
    """
    if days_to_required < 0:
        return URGENCY_OVERDUE
    if days_to_required <= 7:
        return URGENCY_THIS_WEEK
    if days_to_required <= 30:
        return URGENCY_THIS_MONTH
    return URGENCY_LATER


def delivery_delay_days(expected: date | None, actual: date | None, as_of: date) -> int:
    """Days late a consignment is, or was.

    An undelivered consignment is measured against today - its delay is still
    growing - and a delivered one against the day it actually arrived, so the
    number stops moving once the lorry has been unloaded.
    """
    if expected is None:
        return 0
    return max(0, ((actual or as_of) - expected).days)


def available_minutes(working_hours_per_day: Decimal, days: int) -> Decimal:
    """Capacity of one machine over a period, in minutes.

    The denominator of utilisation. Calendar days, not working days: the master
    rules define no shift calendar, and inventing one would silently inflate
    every utilisation figure by whatever weekend assumption was chosen.
    ASSUMPTION recorded in the module notes.
    """
    return (Decimal(working_hours_per_day) * 60 * days).quantize(
        QUANTITY_QUANTUM, rounding=ROUND_HALF_UP
    )


def vendor_performance_score(on_time: Decimal, quality: Decimal) -> Decimal:
    """A single 0-100 figure combining delivery and quality.

    ASSUMPTION: equally weighted. The master rules require a vendor performance
    report but fix no weighting, and a 50/50 split is the choice that cannot be
    accused of hiding one dimension behind the other. It is a named function so
    changing the policy is one edit with one test, not a formula copied into
    three screens.
    """
    return ((Decimal(on_time) + Decimal(quality)) / 2).quantize(
        PERCENT_QUANTUM, rounding=ROUND_HALF_UP
    )


# -----------------------------------------------------------------------------
# Report definitions
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Everything a report needs beyond the tenant: when, and narrowed how."""

    now: datetime
    as_of: date
    filters: ReportFilters
    period: ReportPeriod | None = None

    @property
    def period_days(self) -> int:
        """Days in the period; 1 for a position report, so per-day maths is safe."""
        return self.period.days if self.period else 1

    def require_period(self) -> ReportPeriod:
        """Return the validated period for a period-bound report definition."""
        if self.period is None:
            raise RuntimeError("A period-bound report was executed without a period")
        return self.period

    def dead_stock_cutoff(self) -> datetime:
        threshold = self.filters.min_days if self.filters.min_days is not None else DEAD_STOCK_DAYS
        return self.now - timedelta(days=threshold)


Builder = Callable[[ReportQueryRepository, ReportContext], Select[Any]]
Deriver = Callable[[dict[str, Any], ReportContext], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    """One report: its query, its row shape and its policy."""

    key: ReportKey
    title: str
    description: str
    row_model: type[ReportRowBase]
    build: Builder
    requires_period: bool
    #: Whether the result may be served from Redis. False for anything derived
    #: from stock on hand - section 34.
    cacheable: bool
    derive: Deriver | None = None


def _column_kind(annotation: Any) -> str:
    """Map a row-model field type to a rendering hint for a generic grid.

    Derived from the schema rather than hand-listed so a column added to a row
    model cannot be missing from the descriptor - the two would drift apart
    within a week otherwise. The money/quantity distinction is carried by the
    field name (``_amount``, ``_quantity``), which is why both arrive as
    ``number``: the value is an exact Decimal either way, and how it is
    formatted is a presentation decision.
    """
    text = str(annotation)
    if "UUID" in text:
        return "id"
    if "bool" in text:
        return "bool"
    if "Decimal" in text:
        return "number"
    if "datetime" in text:
        return "datetime"
    if "date" in text:
        return "date"
    if "int" in text:
        return "int"
    return "text"


def columns_for(model: type[ReportRowBase]) -> tuple[ReportColumnRead, ...]:
    """The ordered column list of a report, taken from its row schema."""
    return tuple(
        ReportColumnRead(
            name=name,
            label=name.replace("_", " ").capitalize(),
            kind=_column_kind(field.annotation),
        )
        for name, field in model.model_fields.items()
    )


# --- derivations -------------------------------------------------------------


def _derive_sales_order_status(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    ordered = Decimal(row["ordered_quantity"])
    dispatched = Decimal(row["dispatched_quantity"])
    row["pending_quantity"] = max(Decimal("0"), ordered - dispatched)
    row["fulfilment_percent"] = percent(dispatched, ordered)
    return row


def _derive_stock_availability(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    reorder = Decimal(row["reorder_level"])
    row["below_reorder_level"] = reorder > 0 and Decimal(row["available"]) <= reorder
    return row


def _derive_mrp_shortage(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    gap = (row["earliest_required_date"] - ctx.as_of).days
    row["days_to_required"] = gap
    row["urgency_bucket"] = urgency_bucket(gap)
    return row


def _derive_purchase_pending(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    late = overdue_days(row["expected_delivery_date"], ctx.as_of)
    row["days_overdue"] = late
    row["ageing_bucket"] = ageing_bucket(late)
    return row


def _derive_production_pending(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    late = overdue_days(row["planned_end_date"], ctx.as_of)
    row["days_overdue"] = late
    row["ageing_bucket"] = ageing_bucket(late)
    return row


def _derive_job_work_pending(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    late = overdue_days(row["expected_return_date"], ctx.as_of)
    row["days_overdue"] = late
    row["ageing_bucket"] = ageing_bucket(late)
    return row


def _derive_qc_rejection(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    refused = Decimal(row["rejected_quantity"]) + Decimal(row["scrap_quantity"])
    row["rejection_rate_percent"] = percent(refused, row["inspected_quantity"])
    return row


def _derive_vendor_performance(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    on_time = percent(row["on_time_receipt_count"], row["receipt_count"])
    rejection = percent(row["rejected_quantity"], row["received_quantity"])
    row["on_time_percent"] = on_time
    row["rejection_rate_percent"] = rejection
    row["performance_score"] = vendor_performance_score(on_time, Decimal("100") - rejection)
    return row


def _derive_product_profitability(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    margin = Decimal(row["revenue_amount"]) - Decimal(row["cost_amount"])
    row["margin_amount"] = margin
    row["margin_percent"] = percent(margin, row["revenue_amount"])
    return row


def _derive_item_movement(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    usage = ratio(row["issued_quantity"], ctx.period_days)
    row["average_daily_usage"] = usage
    # Days of cover is undefined, not infinite, when nothing moves: dividing by
    # zero usage would either raise or produce a made-up number that a planner
    # would read as "this will last forever".
    row["days_of_cover"] = ratio(row["on_hand"], usage) if usage > 0 else None
    return row


def _derive_dead_stock(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    idle = days_between(row["last_movement_at"], ctx.now)
    row["days_since_movement"] = idle
    row["ageing_bucket"] = ageing_bucket(idle)
    return row


def _derive_order_delay(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    ordered = Decimal(row["ordered_quantity"])
    dispatched = Decimal(row["dispatched_quantity"])
    late = overdue_days(row["required_date"], ctx.as_of)
    row["pending_quantity"] = max(Decimal("0"), ordered - dispatched)
    row["days_overdue"] = late
    row["ageing_bucket"] = ageing_bucket(late)
    return row


def _derive_production_efficiency(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    produced = Decimal(row["produced_quantity"])
    attempted = produced + Decimal(row["rejected_quantity"]) + Decimal(row["scrap_quantity"])
    row["yield_percent"] = percent(produced, attempted)
    # Planned over actual, so 100% is "took exactly as long as planned" and more
    # than 100% is faster than planned. The inverse reads backwards on a screen.
    row["schedule_efficiency_percent"] = percent(row["planned_minutes"], row["actual_minutes"])
    finished = row["actual_end_date"]
    row["completed_on_time"] = finished is not None and finished <= row["planned_end_date"]
    return row


def _derive_machine_utilisation(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    capacity = available_minutes(row["working_hours_per_day"], ctx.period_days)
    row["available_minutes"] = capacity
    row["utilisation_percent"] = percent(row["busy_minutes"], capacity)
    row["downtime_percent"] = percent(row["downtime_minutes"], capacity)
    return row


def _derive_inventory_valuation(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    row["unit_value"] = ratio(row["valuation_amount"], row["on_hand"])
    return row


def _derive_dispatch(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    row["delay_days"] = delivery_delay_days(
        row["expected_delivery_date"], row["actual_delivery_date"], ctx.as_of
    )
    return row


def _derive_courier_tracking(row: dict[str, Any], ctx: ReportContext) -> dict[str, Any]:
    row["days_in_transit"] = max(0, (ctx.as_of - row["dispatch_date"]).days)
    return row


# --- the catalogue -----------------------------------------------------------

REPORT_CATALOGUE: dict[ReportKey, ReportDefinition] = {
    ReportKey.SALES_ORDER_STATUS: ReportDefinition(
        key=ReportKey.SALES_ORDER_STATUS,
        title="Sales order status",
        description="Every order raised in the period with what has been allocated and shipped.",
        row_model=SalesOrderStatusRow,
        build=lambda repo, ctx: repo.sales_order_status(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_sales_order_status,
    ),
    ReportKey.STOCK_AVAILABILITY: ReportDefinition(
        key=ReportKey.STOCK_AVAILABILITY,
        title="Stock availability",
        description=(
            "On-hand, reserved and available stock per item and warehouse, from the "
            "refreshed summary. Indicative: an allocation decision re-reads live stock."
        ),
        row_model=StockAvailabilityRow,
        build=lambda repo, ctx: repo.stock_availability(ctx.filters),
        requires_period=False,
        # Section 34: availability is never served from cache.
        cacheable=False,
        derive=_derive_stock_availability,
    ),
    ReportKey.MRP_SHORTAGE: ReportDefinition(
        key=ReportKey.MRP_SHORTAGE,
        title="MRP shortage",
        description="Net shortages from planning runs completed in the period.",
        row_model=MrpShortageRow,
        build=lambda repo, ctx: repo.mrp_shortage(ctx.require_period(), ctx.filters),
        requires_period=True,
        # Derived from availability at run time, and a buyer acts on it directly.
        cacheable=False,
        derive=_derive_mrp_shortage,
    ),
    ReportKey.PURCHASE_PENDING: ReportDefinition(
        key=ReportKey.PURCHASE_PENDING,
        title="Purchase pending",
        description="Committed purchase orders still awaiting delivery, with ageing.",
        row_model=PurchasePendingRow,
        build=lambda repo, ctx: repo.purchase_pending(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_purchase_pending,
    ),
    ReportKey.PRODUCTION_PENDING: ReportDefinition(
        key=ReportKey.PRODUCTION_PENDING,
        title="Production pending",
        description="Production orders approved or under way but not yet complete.",
        row_model=ProductionPendingRow,
        build=lambda repo, ctx: repo.production_pending(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_production_pending,
    ),
    ReportKey.JOB_WORK_PENDING: ReportDefinition(
        key=ReportKey.JOB_WORK_PENDING,
        title="Job-work pending",
        description="Material with job-work vendors and not yet fully returned.",
        row_model=JobWorkPendingRow,
        build=lambda repo, ctx: repo.job_work_pending(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_job_work_pending,
    ),
    ReportKey.QC_REJECTION: ReportDefinition(
        key=ReportKey.QC_REJECTION,
        title="QC rejection",
        description="Inspections in the period that rejected or scrapped material.",
        row_model=QcRejectionRow,
        build=lambda repo, ctx: repo.qc_rejection(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_qc_rejection,
    ),
    ReportKey.VENDOR_PERFORMANCE: ReportDefinition(
        key=ReportKey.VENDOR_PERFORMANCE,
        title="Vendor performance",
        description="On-time delivery and rejection rate per vendor over the period.",
        row_model=VendorPerformanceRow,
        build=lambda repo, ctx: repo.vendor_performance(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_vendor_performance,
    ),
    ReportKey.PRODUCT_PROFITABILITY: ReportDefinition(
        key=ReportKey.PRODUCT_PROFITABILITY,
        title="Product profitability",
        description="Revenue net of discount against standard cost, per item.",
        row_model=ProductProfitabilityRow,
        build=lambda repo, ctx: repo.product_profitability(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_product_profitability,
    ),
    ReportKey.FAST_MOVING: ReportDefinition(
        key=ReportKey.FAST_MOVING,
        title="Fast-moving items",
        description="Items consumed or shipped most heavily in the period.",
        row_model=ItemMovementRow,
        build=lambda repo, ctx: repo.fast_moving(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_item_movement,
    ),
    ReportKey.SLOW_MOVING: ReportDefinition(
        key=ReportKey.SLOW_MOVING,
        title="Slow-moving items",
        description="Items held in stock with the least movement in the period.",
        row_model=ItemMovementRow,
        build=lambda repo, ctx: repo.slow_moving(ctx.require_period(), ctx.filters),
        requires_period=True,
        # Reads on-hand alongside movement, so it is a stock position too.
        cacheable=False,
        derive=_derive_item_movement,
    ),
    ReportKey.DEAD_STOCK: ReportDefinition(
        key=ReportKey.DEAD_STOCK,
        title="Dead stock",
        description=(
            "Positions holding stock with no movement for longer than the ageing "
            f"threshold (default {DEAD_STOCK_DAYS} days)."
        ),
        row_model=DeadStockRow,
        build=lambda repo, ctx: repo.dead_stock(ctx.filters, cutoff=ctx.dead_stock_cutoff()),
        requires_period=False,
        cacheable=False,
        derive=_derive_dead_stock,
    ),
    ReportKey.ORDER_DELAY: ReportDefinition(
        key=ReportKey.ORDER_DELAY,
        title="Order delay",
        description="Open sales orders whose required date has already passed.",
        row_model=OrderDelayRow,
        build=lambda repo, ctx: repo.order_delay(
            ctx.require_period(), ctx.filters, as_of=ctx.as_of
        ),
        requires_period=True,
        cacheable=True,
        derive=_derive_order_delay,
    ),
    ReportKey.PRODUCTION_EFFICIENCY: ReportDefinition(
        key=ReportKey.PRODUCTION_EFFICIENCY,
        title="Production efficiency",
        description="Yield and schedule performance of orders finished in the period.",
        row_model=ProductionEfficiencyRow,
        build=lambda repo, ctx: repo.production_efficiency(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_production_efficiency,
    ),
    ReportKey.MACHINE_UTILISATION: ReportDefinition(
        key=ReportKey.MACHINE_UTILISATION,
        title="Machine utilisation",
        description="Busy minutes against available capacity per machine.",
        row_model=MachineUtilisationRow,
        build=lambda repo, ctx: repo.machine_utilisation(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_machine_utilisation,
    ),
    ReportKey.INVENTORY_VALUATION: ReportDefinition(
        key=ReportKey.INVENTORY_VALUATION,
        title="Inventory valuation",
        description="Stock value per item and warehouse from the refreshed summary.",
        row_model=InventoryValuationRow,
        build=lambda repo, ctx: repo.inventory_valuation(ctx.filters),
        requires_period=False,
        cacheable=False,
        derive=_derive_inventory_valuation,
    ),
    ReportKey.DISPATCH: ReportDefinition(
        key=ReportKey.DISPATCH,
        title="Dispatch",
        description="Consignments sent in the period, with freight and delivery dates.",
        row_model=DispatchRow,
        build=lambda repo, ctx: repo.dispatch(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_dispatch,
    ),
    ReportKey.COURIER_TRACKING: ReportDefinition(
        key=ReportKey.COURIER_TRACKING,
        title="Courier and transport tracking",
        description="Consignments still on the road, with their latest tracking event.",
        row_model=CourierTrackingRow,
        build=lambda repo, ctx: repo.courier_tracking(ctx.require_period(), ctx.filters),
        requires_period=True,
        # A tracking board is watched while it changes; a five-minute-old event
        # is the thing it exists to avoid showing.
        cacheable=False,
        derive=_derive_courier_tracking,
    ),
    ReportKey.VENDOR_DISPATCH_HISTORY: ReportDefinition(
        key=ReportKey.VENDOR_DISPATCH_HISTORY,
        title="Vendor dispatch history",
        description="Everything sent to vendors in the period, by vendor.",
        row_model=VendorDispatchHistoryRow,
        build=lambda repo, ctx: repo.vendor_dispatch_history(ctx.require_period(), ctx.filters),
        requires_period=True,
        cacheable=True,
        derive=_derive_dispatch,
    ),
    # ASSUMPTION: read from posted ledger legs, net = debit - credit on every row
    # whatever the account class, retired centres included, no roll-up of
    # children into parents. docs/assumptions.md §B1-6.
    ReportKey.COST_CENTRE_EXPENSE: ReportDefinition(
        key=ReportKey.COST_CENTRE_EXPENSE,
        title="Cost centre statement",
        description=(
            "Posted income and expense per cost centre and account over the period. Net is "
            "debit minus credit: positive is cost, negative is income."
        ),
        row_model=CostCentreExpenseRow,
        build=lambda repo, ctx: repo.cost_centre_expense(ctx.require_period(), ctx.filters),
        requires_period=True,
        # Not cached: a period still open takes postings - an approved bill, a
        # reversal - and a statement that lags the ledger it is read from would
        # disagree with the account statement for the same day.
        cacheable=False,
    ),
}

# Every member of the catalogue enum has exactly one definition, and no
# document-only key has one at all. Checked at import so a report added to the
# enum and forgotten here fails at startup, not at the first request from the
# one person who uses it - and so a definition written for a key that is served
# by a dedicated task cannot quietly turn a PDF job into a tabular route.
_missing = set(ReportKey) - set(REPORT_CATALOGUE) - DOCUMENT_ONLY_REPORT_KEYS
if _missing:  # pragma: no cover - a coding error, caught before the app serves
    raise RuntimeError(f"reports: no definition for {sorted(str(k) for k in _missing)}")
_document_only_defined = DOCUMENT_ONLY_REPORT_KEYS & set(REPORT_CATALOGUE)
if _document_only_defined:  # pragma: no cover - a coding error, caught before the app serves
    raise RuntimeError(
        "reports: document-only keys must not have a catalogue definition: "
        f"{sorted(str(k) for k in _document_only_defined)}"
    )


def definition_for(key: ReportKey) -> ReportDefinition:
    return REPORT_CATALOGUE[key]


def descriptors() -> list[ReportDescriptorRead]:
    """The report catalogue, for a client that renders the picker generically."""
    return [
        ReportDescriptorRead(
            key=definition.key,
            title=definition.title,
            description=definition.description,
            requires_period=definition.requires_period,
            columns=list(columns_for(definition.row_model)),
        )
        for definition in REPORT_CATALOGUE.values()
    ]


# -----------------------------------------------------------------------------
# Running a report
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InlineReport:
    """A report small enough to answer in the response."""

    definition: ReportDefinition
    rows: list[ReportRowBase]
    pagination: OffsetPagination
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedReport:
    """A report handed to Celery. The caller polls ``snapshot.id``."""

    definition: ReportDefinition
    snapshot: ReportSnapshot


def build_context(
    definition: ReportDefinition,
    period: ReportPeriod | None,
    filters: ReportFilters,
    *,
    now: datetime | None = None,
) -> ReportContext:
    """Validate the request shape and freeze the clock for this report.

    One ``now`` for the whole report matters more than it looks: ageing, urgency
    and days-in-transit are all computed per row, and reading the clock inside
    the loop would let a report that crosses midnight bucket its first rows one
    day differently from its last.

    A period report without a period is refused here rather than silently run
    over all time (master rules section 25).
    """
    if definition.requires_period and period is None:
        raise ValidationError(
            f"'{definition.title}' covers a period, so date_from and date_to are required.",
            details={"report_key": str(definition.key), "required": ["date_from", "date_to"]},
        )
    moment = now or utcnow()
    return ReportContext(
        now=moment,
        as_of=moment.date(),
        filters=filters,
        # A period passed to a position report is ignored rather than rejected -
        # it changes no row, so it must also not change the parameter hash.
        period=period if definition.requires_period else None,
    )


def parameters_of(definition: ReportDefinition, ctx: ReportContext) -> dict[str, Any]:
    """The stored, hashable description of one report request.

    Normalised: unset filters are dropped so ``?vendor_id=`` and an omitted
    ``vendor_id`` describe the same report and share one snapshot, and a period
    is absent entirely for a report that does not take one.
    """
    parameters: dict[str, Any] = {
        "report_key": str(definition.key),
        "filters": ctx.filters.model_dump(mode="json", exclude_none=True),
    }
    if ctx.period is not None:
        parameters["period"] = {
            "date_from": ctx.period.date_from.isoformat(),
            "date_to": ctx.period.date_to.isoformat(),
        }
    return parameters


def parameters_hash(parameters: dict[str, Any]) -> str:
    """Stable digest of a parameter set.

    ``sort_keys`` is what makes it stable: without it two dictionaries with the
    same content but a different insertion order would hash apart and defeat the
    reuse the hash exists for.
    """
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def context_from_parameters(
    definition: ReportDefinition, parameters: dict[str, Any], *, now: datetime
) -> ReportContext:
    """Rebuild a context from a stored snapshot, for the background worker."""
    period = None
    stored = parameters.get("period")
    if stored:
        period = ReportPeriod(
            date_from=date.fromisoformat(stored["date_from"]),
            date_to=date.fromisoformat(stored["date_to"]),
        )
    filters = ReportFilters.model_validate(parameters.get("filters") or {})
    return build_context(definition, period, filters, now=now)


def _shape(
    definition: ReportDefinition, rows: list[Any], ctx: ReportContext
) -> list[ReportRowBase]:
    """Turn result rows into validated report rows.

    ``dict(row._mapping)`` rather than a hand-written mapper per report: the
    statement labels its columns with the row model's field names, so a renamed
    column fails validation immediately instead of serialising as null.
    """
    shaped: list[ReportRowBase] = []
    for row in rows:
        values = dict(row._mapping)
        if definition.derive is not None:
            values = definition.derive(values, ctx)
        shaped.append(definition.row_model.model_validate(values))
    return shaped


def _cache_key(
    tenant: TenantContext, definition: ReportDefinition, digest: str, params: PageParams
) -> str:
    namespace, _ = CacheNamespace.REPORT
    return make_key(
        namespace, tenant.company_id, str(definition.key), digest, params.page, params.limit
    )


async def run_report(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    key: ReportKey,
    period: ReportPeriod | None,
    filters: ReportFilters,
    params: PageParams,
    may_export: bool,
    now: datetime | None = None,
) -> InlineReport | QueuedReport:
    """Run one report, inline or in the background - the server decides which.

    The row count is taken first, bounded at the inline limit, because that one
    cheap question answers both "can this be served now?" and "what total does
    the pagination block report?". Counting it twice would double the cost of
    every report page.
    """
    definition = definition_for(key)
    ctx = build_context(definition, period, filters, now=now)
    repo = ReportQueryRepository(session, tenant)
    stmt = definition.build(repo, ctx)

    total = await count_rows(session, stmt, cap=REPORT_INLINE_ROW_LIMIT)
    if total > REPORT_INLINE_ROW_LIMIT:
        return await _queue_report(session, tenant, definition, ctx, may_export=may_export)

    parameters = parameters_of(definition, ctx)
    digest = parameters_hash(parameters)

    if definition.cacheable:
        cache_key = _cache_key(tenant, definition, digest, params)
        cached = await cache_get(cache_key)
        if cached is not None:
            _, ttl = CacheNamespace.REPORT
            return InlineReport(
                definition=definition,
                rows=[definition.row_model.model_validate(r) for r in cached["rows"]],
                pagination=OffsetPagination.model_validate(cached["pagination"]),
                generated_at=datetime.fromisoformat(cached["generated_at"]),
            )

    rows, pagination = await paginate_rows(session, stmt, params, total=total)
    shaped = _shape(definition, rows, ctx)

    if definition.cacheable:
        _, ttl = CacheNamespace.REPORT
        await cache_set(
            _cache_key(tenant, definition, digest, params),
            {
                "rows": [row.model_dump(mode="json") for row in shaped],
                "pagination": pagination.model_dump(mode="json"),
                "generated_at": ctx.now.isoformat(),
            },
            ttl=ttl,
        )

    return InlineReport(
        definition=definition, rows=shaped, pagination=pagination, generated_at=ctx.now
    )


async def _queue_report(
    session: AsyncSession,
    tenant: TenantContext,
    definition: ReportDefinition,
    ctx: ReportContext,
    *,
    may_export: bool,
) -> QueuedReport:
    """Create or reuse the background job for a report too large to serve inline."""
    if not may_export:
        raise BusinessRuleError(
            f"'{definition.title}' returns more than {REPORT_INLINE_ROW_LIMIT:,} rows for "
            "these parameters. Narrow the date range or the filters, or ask for the "
            "report-export permission to have it generated as a file.",
            details={"row_limit": REPORT_INLINE_ROW_LIMIT},
        )

    parameters = parameters_of(definition, ctx)
    digest = parameters_hash(parameters)
    snapshots = ReportSnapshotRepository(session, tenant)

    existing = await snapshots.find_reusable(definition.key, digest, now=ctx.now)
    if existing is not None:
        # Five people opening the same link must not queue five identical
        # exports; the later callers poll the job the first one started.
        log.info(
            "reports.snapshot_reused",
            report_key=str(definition.key),
            snapshot_id=str(existing.id),
            status=str(existing.status),
        )
        return QueuedReport(definition=definition, snapshot=existing)

    async with transaction(session):
        snapshot = ReportSnapshot(
            report_key=definition.key,
            parameters_hash=digest,
            parameters=parameters,
            status=ReportJobStatus.QUEUED,
            expires_at=ctx.now + timedelta(hours=REPORT_SNAPSHOT_TTL_HOURS),
        )
        snapshots.add(snapshot)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type=AUDIT_ENTITY,
            entity_id=snapshot.id,
            entity_code=str(definition.key),
            summary=f"Queued '{definition.title}' for background generation",
            user_id=tenant.user_id,
            new_values={"report_key": str(definition.key), "parameters": parameters},
        )

    await dispatch_job(session, tenant, snapshot, task_name=REPORT_TASK_NAME, queue=REPORT_QUEUE)
    return QueuedReport(definition=definition, snapshot=snapshot)


def new_job_snapshot(
    report_key: ReportKey, parameters: dict[str, Any], *, now: datetime
) -> ReportSnapshot:
    """A QUEUED job row for one background artefact, not yet added to a session.

    The one constructor for every job the ``/reports/jobs/{id}`` route can be
    asked about - the catalogue runner's own exports and the document-only jobs
    other modules queue (the packing list, the full valuation workbook). Built
    here so the hash, the initial status and the retention window are decided in
    one place; the caller adds it inside its own transaction, alongside whatever
    audit row names the thing being generated.
    """
    return ReportSnapshot(
        report_key=report_key,
        parameters_hash=parameters_hash(parameters),
        parameters=parameters,
        status=ReportJobStatus.QUEUED,
        expires_at=now + timedelta(hours=REPORT_SNAPSHOT_TTL_HOURS),
    )


async def dispatch_job(
    session: AsyncSession,
    tenant: TenantContext,
    snapshot: ReportSnapshot,
    *,
    task_name: str,
    queue: str,
    task_kwargs: dict[str, Any] | None = None,
    subject: str = "The report",
) -> str:
    """Hand a job row to Celery and record the task id. Returns the task id.

    The task is reached by *name* so the API process never imports the worker
    package, and the broker is imported inside the function rather than at
    module scope: serving a report inline must not require a broker connection,
    and a broker that is down has to fail *this* request rather than
    application startup.

    The publish runs on a worker thread. ``send_task`` opens a socket to the
    broker with kombu's connect-and-retry, and doing that inline would block the
    event loop that is serving every other request for the whole of the
    connection timeout - which, while the broker is unreachable, is exactly
    when every request is already slow. The kwargs are assembled here, in the
    coroutine, so nothing on the worker thread reads a session or a context
    variable.

    A broker that refuses the task marks the job FAILED, so a client polling it
    is told rather than left waiting, and the request answers 503.
    """
    from src.core.logging import get_request_id
    from src.workers.celery_app import celery_app

    kwargs = {
        **(task_kwargs or {}),
        "snapshot_id": str(snapshot.id),
        "company_id": str(tenant.company_id),
        "user_id": str(tenant.user_id),
        "request_id": get_request_id(),
    }
    message = f"{subject} could not be queued for background generation."
    try:
        task = await asyncio.to_thread(celery_app.send_task, task_name, kwargs=kwargs, queue=queue)
    except Exception as exc:
        log.error(
            "reports.dispatch_failed",
            snapshot_id=str(snapshot.id),
            report_key=str(snapshot.report_key),
            task=task_name,
            error=str(exc),
        )
        await fail_snapshot(session, tenant, snapshot_id=snapshot.id, error=message)
        raise AppError(message, code=ErrorCode.SERVICE_UNAVAILABLE, status_code=503) from exc

    task_id = str(task.id)
    async with transaction(session):
        snapshot.celery_task_id = task_id
        ReportSnapshotRepository(session, tenant).touch(snapshot)
    log.info(
        "reports.queued",
        snapshot_id=str(snapshot.id),
        report_key=str(snapshot.report_key),
        task=task_name,
        task_id=task_id,
    )
    return task_id


# -----------------------------------------------------------------------------
# Background job lifecycle
# -----------------------------------------------------------------------------


def job_read(snapshot: ReportSnapshot) -> ReportJobRead:
    """Serialise a snapshot, with the download link only once there is a file."""
    return ReportJobRead(
        id=snapshot.id,
        report_key=snapshot.report_key,
        status=snapshot.status,
        parameters=snapshot.parameters,
        row_count=snapshot.row_count,
        generated_at=snapshot.generated_at,
        expires_at=snapshot.expires_at,
        document_id=snapshot.document_id,
        download_url=(
            document_download_url(snapshot.document_id) if snapshot.document_id else None
        ),
        error_message=snapshot.error_message,
        created_at=snapshot.created_at,
    )


async def get_job(session: AsyncSession, tenant: TenantContext, job_id: uuid.UUID) -> ReportJobRead:
    snapshot = await ReportSnapshotRepository(session, tenant).get(job_id)
    if snapshot is None:
        raise NotFoundError("Report job", job_id, code=ErrorCode.JOB_NOT_FOUND)
    return job_read(snapshot)


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    snapshot: ReportSnapshot,
    target: ReportJobStatus,
    *,
    reason: str | None = None,
) -> None:
    """Move a job's status through the state machine, audited (section 73)."""
    previous = snapshot.status
    REPORT_JOB_LIFECYCLE.assert_transition(previous, target)
    snapshot.status = target
    ReportSnapshotRepository(session, tenant).touch(snapshot)
    await session.flush()
    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_ENTITY,
        entity_id=snapshot.id,
        entity_code=str(snapshot.report_key),
        from_status=str(previous),
        to_status=str(target),
        user_id=tenant.user_id,
        reason=reason,
    )


async def start_snapshot(
    session: AsyncSession, tenant: TenantContext, snapshot_id: uuid.UUID
) -> ReportSnapshot:
    """Claim a queued job. A re-delivered task finds it no longer QUEUED.

    The state machine is what makes ``task_acks_late`` survivable: the second
    delivery of a task whose job has already started raises
    :class:`~src.core.errors.InvalidStateTransitionError`, which the worker
    treats as "already handled" rather than as a failure.
    """
    repo = ReportSnapshotRepository(session, tenant)
    snapshot = await repo.get_or_404(snapshot_id)
    async with transaction(session):
        await _transition(session, tenant, snapshot, ReportJobStatus.RUNNING)
    return snapshot


async def snapshot_rows(
    session: AsyncSession, tenant: TenantContext, snapshot: ReportSnapshot
) -> tuple[ReportDefinition, list[ReportRowBase]]:
    """Materialise a background report's rows, up to the export ceiling.

    Bounded rather than streamed: the artefact is written in one pass by
    openpyxl, so the memory is spent either way, and a stated ceiling is a
    better answer than a worker killed halfway through writing a file the user
    is waiting for.
    """
    definition = definition_for(snapshot.report_key)
    ctx = context_from_parameters(definition, snapshot.parameters or {}, now=utcnow())
    stmt = definition.build(ReportQueryRepository(session, tenant), ctx)
    rows = list((await session.execute(stmt.limit(REPORT_MAX_EXPORT_ROWS))).all())
    return definition, _shape(definition, rows, ctx)


async def complete_snapshot(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    snapshot_id: uuid.UUID,
    row_count: int,
    document_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ReportSnapshot:
    """Record the finished artefact against its job, in one transaction."""
    repo = ReportSnapshotRepository(session, tenant)
    snapshot = await repo.get_or_404(snapshot_id)
    moment = utcnow()

    async with transaction(session):
        snapshot.row_count = row_count
        snapshot.document_id = document_id
        snapshot.payload = payload
        snapshot.generated_at = moment
        await _transition(session, tenant, snapshot, ReportJobStatus.COMPLETED)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DATA_EXPORT,
            entity_type=AUDIT_ENTITY,
            entity_id=snapshot.id,
            entity_code=str(snapshot.report_key),
            summary=f"Generated '{snapshot.report_key}' with {row_count} rows",
            user_id=tenant.user_id,
            new_values={"row_count": row_count, "document_id": document_id},
        )
    return snapshot


async def fail_snapshot(
    session: AsyncSession, tenant: TenantContext, *, snapshot_id: uuid.UUID, error: str
) -> ReportSnapshot:
    """Mark a job failed with a message safe to show the person waiting."""
    repo = ReportSnapshotRepository(session, tenant)
    snapshot = await repo.get_or_404(snapshot_id)

    async with transaction(session):
        snapshot.error_message = error[:1000]
        await _transition(session, tenant, snapshot, ReportJobStatus.FAILED, reason=error[:255])
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_FAILED,
            entity_type=AUDIT_ENTITY,
            entity_id=snapshot.id,
            entity_code=str(snapshot.report_key),
            summary=f"Report '{snapshot.report_key}' failed to generate",
            user_id=tenant.user_id,
            new_values={"error": error[:255]},
        )
    return snapshot


async def purge_expired_snapshots(
    session: AsyncSession, tenant: TenantContext, *, limit: int = 200
) -> int:
    """Delete one bounded batch of lapsed snapshots. Returns how many went.

    Only the snapshot row: the artefact it points at is a ``documents`` row with
    its own retention, and deleting a file out from under that module's
    bookkeeping would leave it with a row pointing at nothing.
    """
    repo = ReportSnapshotRepository(session, tenant)
    expired = await repo.list_expired(utcnow(), limit=limit)
    if not expired:
        return 0
    async with transaction(session):
        removed = await repo.bulk_delete([row.id for row in expired])
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_ENTITY,
            summary=f"Purged {removed} expired report snapshots",
            user_id=tenant.user_id,
            new_values={"removed": removed},
        )
    return removed


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------


async def dashboard(
    session: AsyncSession, tenant: TenantContext, *, now: datetime | None = None
) -> DashboardRead:
    """The management tile set, from one statement and a short-lived cache.

    Cached for the dashboard TTL because every tile here is a headline a person
    reads, not a number a decision is taken from - and because the alternative is
    ten indexed counts every time somebody leaves the page open on a wall screen.
    The stock figure comes from the summary table and carries its own refresh
    time, so a stale summary is visible rather than disguised.
    """
    moment = now or utcnow()
    namespace, ttl = CacheNamespace.DASHBOARD
    cache_key = make_key(namespace, tenant.company_id, "tiles")

    cached = await cache_get(cache_key)
    if cached is not None:
        return DashboardRead.model_validate(cached)

    stmt = ReportQueryRepository(session, tenant).dashboard(as_of=moment.date())
    row = (await session.execute(stmt)).one()
    tiles = DashboardRead.model_validate({**dict(row._mapping), "generated_at": moment})

    await cache_set(cache_key, tiles.model_dump(mode="json"), ttl=ttl)
    return tiles


# -----------------------------------------------------------------------------
# Summary refresh (maintenance.refresh_summary_views)
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SummaryRefreshResult:
    company_id: uuid.UUID
    position_count: int
    removed_count: int
    stock_value: Decimal
    refreshed_at: datetime


async def refresh_inventory_summary(
    session: AsyncSession, tenant: TenantContext, *, now: datetime | None = None
) -> SummaryRefreshResult:
    """Rebuild one tenant's stock summary from ``inventory_balances``.

    The whole refresh is one transaction with one stamp. That matters twice: a
    reader never sees half of the new position and half of the old one, and the
    stamp is what identifies the rows this pass did *not* touch - which are
    exactly the positions whose balance has gone and which must be removed rather
    than left reporting stock that no longer exists.
    """
    moment = now or utcnow()
    repo = ReportQueryRepository(session, tenant)

    written = 0
    removed = 0
    stock_value = Decimal("0")

    async with transaction(session):
        result = await session.stream(repo.summary_source_stmt())
        batch: list[dict[str, Any]] = []
        async for row in result:
            values = dict(row._mapping)
            stock_value += Decimal(values["valuation_amount"] or 0)
            batch.append(values)
            if len(batch) >= SUMMARY_REFRESH_BATCH:
                written += await repo.upsert_summary(batch, refreshed_at=moment)
                batch.clear()
        if batch:
            written += await repo.upsert_summary(batch, refreshed_at=moment)

        removed = await repo.drop_stale_summary(refreshed_at=moment)

        if written or removed:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=SUMMARY_AUDIT_ENTITY,
                summary=(
                    f"Refreshed the inventory summary: {written} positions, {removed} removed"
                ),
                user_id=None,
                actor_label="system",
                new_values={
                    "positions": written,
                    "removed": removed,
                    "stock_value": stock_value,
                },
            )

    log.info(
        "reports.summary_refreshed",
        company_id=str(tenant.company_id),
        positions=written,
        removed=removed,
    )
    return SummaryRefreshResult(
        company_id=tenant.company_id,
        position_count=written,
        removed_count=removed,
        stock_value=stock_value,
        refreshed_at=moment,
    )


async def companies_with_stock(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants that have any stock position at all.

    The beat sweep has no principal and therefore no tenant, so it starts from a
    deliberately cross-tenant read and partitions by company before writing
    anything - the same shape ``dispatch.scan_delays`` uses.
    """
    from sqlalchemy import select

    from src.modules.inventory.models import InventoryBalance

    stmt = select(InventoryBalance.company_id).distinct()
    return list((await session.execute(stmt)).scalars().all())


async def companies_with_expired_snapshots(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants holding at least one lapsed report snapshot."""
    from sqlalchemy import select

    stmt = (
        select(ReportSnapshot.company_id)
        .where(
            ReportSnapshot.expires_at.is_not(None),
            ReportSnapshot.expires_at <= utcnow(),
        )
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "AGEING_BUCKETS",
    "AUDIT_ENTITY",
    "CURRENT_BUCKET",
    "DEAD_STOCK_DAYS",
    "DOCUMENT_DOWNLOAD_URL_TEMPLATE",
    "JOB_STATUS_URL_TEMPLATE",
    "NEVER_MOVED_BUCKET",
    "OVER_AGE_BUCKET",
    "REPORT_CATALOGUE",
    "REPORT_INLINE_ROW_LIMIT",
    "REPORT_MAX_EXPORT_ROWS",
    "REPORT_QUEUE",
    "REPORT_SNAPSHOT_TTL_HOURS",
    "REPORT_TASK_NAME",
    "URGENCY_LATER",
    "URGENCY_OVERDUE",
    "URGENCY_THIS_MONTH",
    "URGENCY_THIS_WEEK",
    "InlineReport",
    "QueuedReport",
    "ReportContext",
    "ReportDefinition",
    "SummaryRefreshResult",
    "ageing_bucket",
    "available_minutes",
    "build_context",
    "columns_for",
    "companies_with_expired_snapshots",
    "companies_with_stock",
    "complete_snapshot",
    "context_from_parameters",
    "dashboard",
    "days_between",
    "definition_for",
    "delivery_delay_days",
    "descriptors",
    "dispatch_job",
    "document_download_url",
    "fail_snapshot",
    "get_job",
    "job_read",
    "job_status_url",
    "new_job_snapshot",
    "overdue_days",
    "parameters_hash",
    "parameters_of",
    "percent",
    "purge_expired_snapshots",
    "ratio",
    "refresh_inventory_summary",
    "run_report",
    "snapshot_rows",
    "start_snapshot",
    "urgency_bucket",
    "vendor_performance_score",
]
