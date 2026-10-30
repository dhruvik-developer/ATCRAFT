"""Material requirements planning: the run, its demand, its netting and its output.

Four tables, each one a stage of the calculation described in database.md
section 6, persisted so a plan can be defended after the fact. Three modelling
decisions here are load-bearing.

**A run is a document, not a cache.** ``mrp_runs`` carries a business code, a
status, the parameters it was given and the summary it produced. A planner asked
"why did we order 4,000 screws in March?" has to be able to answer from the
database three months later, which a recomputation cannot do - the stock and the
open orders it netted against have all moved since.

**The common-part merge is a database constraint, not a convention.**
``mrp_requirements`` is unique on ``(run_id, item_id)``. The single most
important behaviour in master rules section 14 is that two sales orders both
needing ``SCREW-M4`` produce *one* requirement for the summed quantity, and a
unique index is the only form of that statement which cannot be quietly broken
by a later change to the engine. ``mrp_suggestions`` carries the same constraint
for the same reason: one suggestion per item per run, or a buyer raises two
purchase orders for one shortage.

**Demand is snapshotted, not referenced.** ``mrp_demands`` stores the quantity
and date the run actually planned against, and points at its sales order by bare
UUID rather than by foreign key. The order may be amended or cancelled the next
morning; the plan must keep saying what it was based on. This is the same
reasoning the inventory ledger uses for ``reference_id`` (database.md section
4.2), and it is also what lets a manual demand line - which has no source row at
all - live in the same table.

Requirement rows keep their own ``item_code``: the item master may be renamed or
retired, and a plan that no longer prints the part number it was built from is
not auditable.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity, sql_in_list
from src.core.types import GUID, Quantity
from src.modules.inventory.models import StatusEnum

#: Planning horizon bounds. A horizon of zero plans nothing; one beyond three
#: years is asking the engine to net against purchase orders and stock that do
#: not exist yet, which produces confident nonsense.
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 1095
DEFAULT_HORIZON_DAYS = 90

#: Upper bound on the demand a single run will accept. Past this the run is a
#: data problem (an unfiltered horizon over a decade of orders) rather than a
#: plan, and the engine says so instead of spending twenty minutes proving it.
MAX_DEMAND_LINES = 20_000

#: How many contributing demands are listed on a requirement or suggestion. The
#: quantity is always exact; only the *listing* is capped, and the row also
#: stores the true count so a truncated list cannot be mistaken for the whole
#: story. Same treatment as ``source_paths`` in the BOM engine.
MAX_SOURCE_DEMANDS_LISTED = 50


class MrpRunStatus(StrEnum):
    """Lifecycle of one planning run (database.md section 6).

    ``QUEUED`` exists only for runs handed to Celery. A run executed inline goes
    straight to ``RUNNING``, so "queued" always means "a worker has not picked
    this up yet" rather than "something is happening somewhere".
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Statuses in which a run still owns the company's planning lock. Read by the
#: concurrency guard and by the partial index built for it, so the tuple and the
#: index have to change together.
ACTIVE_RUN_STATUSES: tuple[MrpRunStatus, ...] = (
    MrpRunStatus.QUEUED,
    MrpRunStatus.RUNNING,
)


class MrpRunMode(StrEnum):
    """How the run was executed.

    Recorded rather than inferred from ``celery_task_id``: a run dispatched to a
    worker that never started still needs to say it was meant to be asynchronous.
    """

    SYNC = "SYNC"
    ASYNC = "ASYNC"


class MrpDemandSource(StrEnum):
    """Where one demand line came from.

    Every member is a rule the product can point at: an open sales order, a
    planner's manual line, or the item master's reorder point (E3b section 6).
    Forecast is deliberately absent - it is a separate planning input master
    rules section 14 does not describe, and inventing it here would let a plan
    claim a provenance the system cannot substantiate.
    """

    SALES_ORDER = "SALES_ORDER"
    MANUAL = "MANUAL"
    #: ``source_id`` is the item whose reorder point fired, not a document.
    REORDER = "REORDER"


class MrpSuggestionType(StrEnum):
    """What the planner is being told to do about a shortage.

    Transfers are emitted only between warehouses inside the run's explicit
    scope. A transfer suggestion names one source/destination route; a separate
    supply suggestion may also exist for the item's remaining aggregate shortage.
    """

    MANUFACTURE = "MANUFACTURE"
    PURCHASE = "PURCHASE"
    JOB_WORK = "JOB_WORK"
    TRANSFER = "TRANSFER"
    REALLOCATION = "REALLOCATION"


class MrpSuggestionStatus(StrEnum):
    """Whether a suggestion has been acted on.

    ``IGNORED`` is a decision, not a deletion: the row stays so the next run's
    output can be compared against what a planner already rejected.
    """

    OPEN = "OPEN"
    CONVERTED = "CONVERTED"
    IGNORED = "IGNORED"


class MrpScheduleSettings(TenantEntity):
    """Sparse per-company configuration for nightly MRP runs."""

    __tablename__ = "mrp_schedule_settings"

    mrp_scheduled_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    horizon_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_HORIZON_DAYS,
        server_default=str(DEFAULT_HORIZON_DAYS),
    )
    #: NULL means every active warehouse in the company.
    warehouse_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(GUID), nullable=True)
    respect_safety_stock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", name="uq_mrp_schedule_settings_company"),
        CheckConstraint(
            f"horizon_days BETWEEN {MIN_HORIZON_DAYS} AND {MAX_HORIZON_DAYS}",
            name="horizon_days_bounded",
        ),
    )


class MrpRun(TenantEntity):
    """One execution of the planning engine, with everything it was given."""

    __tablename__ = "mrp_runs"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[MrpRunStatus] = mapped_column(
        StatusEnum(MrpRunStatus),
        nullable=False,
        default=MrpRunStatus.QUEUED,
        server_default=MrpRunStatus.QUEUED.value,
    )
    mode: Mapped[MrpRunMode] = mapped_column(
        StatusEnum(MrpRunMode, length=16),
        nullable=False,
        default=MrpRunMode.SYNC,
        server_default=MrpRunMode.SYNC.value,
    )

    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_HORIZON_DAYS)
    #: Warehouses the run netted against. NULL or empty means every warehouse in
    #: the company. An array rather than a join table because it is written once
    #: with the run and only ever read back whole - there is no query that asks
    #: "which runs covered warehouse X?" that is worth a second table.
    warehouse_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(GUID), nullable=True)

    #: Set only for ASYNC runs, and only once the broker has accepted the task.
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    demand_line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    suggestion_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: What the run was asked to do, exactly as resolved by the service. Kept so
    #: a re-run can be reproduced and so a change of defaults cannot silently
    #: restate what an old plan was based on.
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Counts, timings and the netting inputs that were unavailable. The second
    #: part matters: if the procurement module could not be reached, the plan did
    #: not net against incoming purchase orders and must say so rather than
    #: quietly over-ordering.
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_mrp_runs_company_id_code"),
        # The run list, newest first - the only way this table is browsed.
        Index("ix_mrp_runs_company_created", "company_id", "created_at"),
        # The concurrency guard: "is a run already in flight for this company?"
        # Partial, because on a mature system every row but one is finished.
        Index(
            "ix_mrp_runs_active",
            "company_id",
            "created_at",
            postgresql_where=text(f"status IN ({sql_in_list(ACTIVE_RUN_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(MrpRunStatus)})", name="status_known"),
        CheckConstraint(f"mode IN ({sql_in_list(MrpRunMode)})", name="mode_known"),
        CheckConstraint(
            f"horizon_days BETWEEN {MIN_HORIZON_DAYS} AND {MAX_HORIZON_DAYS}",
            name="horizon_in_range",
        ),
        CheckConstraint("demand_line_count >= 0", name="demand_line_count_non_negative"),
        CheckConstraint("suggestion_count >= 0", name="suggestion_count_non_negative"),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NOT NULL",
            name="completion_implies_start",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_after_started",
        ),
        # A failed run without a reason is an incident nobody can investigate.
        CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name="failed_runs_carry_a_reason",
        ),
    )


class MrpDemand(TenantEntity):
    """One line of demand the run planned against, as it stood at run time."""

    __tablename__ = "mrp_demands"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("mrp_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    demand_source: Mapped[MrpDemandSource] = mapped_column(
        StatusEnum(MrpDemandSource), nullable=False
    )
    #: Bare UUIDs, no foreign key: the source may be amended or cancelled after
    #: the plan was made, and a manual line has no source row at all.
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    source_line_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Human-readable document number, denormalised so a shortage report can name
    #: the order that caused it without joining a table the reader may not be
    #: permitted to see.
    source_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    #: NULL means "any warehouse the run was scoped to" - a manual demand line
    #: need not name one.
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    required_date: Mapped[date] = mapped_column(Date, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")

    #: When the item's merged requirement is expected to be fully covered,
    #: copied from the requirement once the run has planned it. Written at
    #: completion (not creation) because the date depends on the plan; NULL on
    #: runs planned before the E3a migration, and on demand whose item the plan
    #: could not cover at all.
    expected_available_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # The engine reads a run's demand grouped by item; the report reads it
        # by run. One composite index serves both.
        Index("ix_mrp_demands_run_item", "company_id", "run_id", "item_id"),
        # "Which plans were built on this sales order?" - asked whenever an order
        # is amended and somebody needs to know what it invalidated.
        Index("ix_mrp_demands_source", "company_id", "source_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        CheckConstraint(
            f"demand_source IN ({sql_in_list(MrpDemandSource)})", name="demand_source_known"
        ),
        # Manual demand has no source row; sales-order demand names its order,
        # and reorder demand names the item whose reorder point fired.
        CheckConstraint(
            "(demand_source = 'MANUAL') = (source_id IS NULL)",
            name="source_id_matches_demand_source",
        ),
    )


class MrpRequirement(TenantEntity):
    """The merged, netted requirement for one item in one run.

    One row per item per run - the common-part merge, enforced by the unique
    index below rather than trusted to the engine. Every quantity that went into
    the netting is stored beside the answer, because "we are 40 short" is not
    reviewable and "gross 200, free 60, on order 100, shortage 40" is.
    """

    __tablename__ = "mrp_requirements"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("mrp_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    #: Snapshot of the part number as it was planned. The item may be renamed.
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 0 for demand raised directly on the item (a sale, a manual line); 1 and
    #: above for the shallowest BOM level the item was reached at. An item that
    #: is both sold and used as a component is level 0 - the direct demand is
    #: what a planner recognises it by.
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: The deepest level the item appears at across the structures in scope -
    #: the level the planner actually planned it at (E3a). Nullable because rows
    #: written before that migration do not carry one.
    low_level_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- demand -------------------------------------------------------------
    #: Demand raised on the item itself, after finished-goods netting.
    direct_demand_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Demand inherited from every exploded BOM branch, already consolidated.
    dependent_demand_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The two above, summed. Stored rather than derived so a report does not
    #: have to re-add columns to show the number the plan was built on.
    gross_requirement: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- supply the requirement was netted against --------------------------
    on_hand_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Stock promised to another demand. Reported separately because it is the
    #: usual explanation for "we have 100 on hand, why are we ordering more?".
    reserved_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: What the run could actually plan against: on hand less reservations, QC
    #: holds, damage and (when the run protects it) safety stock.
    available_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    incoming_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    in_production_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    assembly_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    job_work_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- supply the requirement was NOT netted against ----------------------
    # Inbound supply inside the run's horizon that arrives *after*
    # ``earliest_required_date``, so the time phasing excluded it and the
    # shortage beside it stands. Persisted rather than left to the run's warning
    # text: a requirements table has to be able to render "100 on order, six
    # weeks late" per row, and parsing that back out of an English sentence is
    # neither reliable nor translatable. The run summary's
    # ``late_supply_item_count`` counts the *short* items among these; every
    # row carries its own figures whether or not it ended up short.
    late_incoming_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    late_in_production_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    late_in_assembly_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    late_job_work_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The date each feed's late quantity starts landing on - one column per
    #: feed, beside the quantity it describes, so a table can render "5000
    #: arriving 2026-11-12" and "0.0001 arriving 2026-07-16" as two facts. NULL
    #: when that feed excluded nothing, which is the ordinary case. Nullable
    #: rather than defaulted to the required date: "no late supply" and "late
    #: supply arriving today" are different facts and a sentinel would merge
    #: them.
    first_late_incoming_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_late_in_production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_late_in_assembly_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_late_job_work_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: When the LAST late tranche of each feed lands. ``late_*_quantity`` is a
    #: SUM over that feed's late tranches while ``first_late_*_date`` is the
    #: MINIMUM over the same tranches, so the two together describe the whole
    #: quantity only when the feed had one tranche. With several, the first date
    #: understates how late the bulk is, which reads as "nearly here" - the
    #: dangerous direction. These close the interval, so a consumer can state
    #: "the first of 5010 on 2026-07-30, all of it by 2026-12-15".
    last_late_incoming_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_late_in_production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_late_in_assembly_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_late_job_work_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: The earliest across all four feeds - the soonest anything at all lands. Kept
    #: because it is the run-level question ("could this be relieved without a
    #: new order, and when at the earliest?") and because clients already read
    #: it. It is deliberately NOT the date for any quantity above: one date
    #: shared by independent schedules belongs to at most one of them, and
    #: reading it beside the larger quantity reported a four-month-late purchase
    #: order as arriving tomorrow. ``ck_..._first_late_supply_is_the_earlier``
    #: holds it to the per-feed columns so it cannot drift from them.
    first_late_supply_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- answer -------------------------------------------------------------
    #: Gross less free stock, before incoming supply is considered.
    net_requirement: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Quantity assigned to PENDING reallocation suggestions; not yet executed.
    planned_reallocation_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: What is still missing after every netting stage. Zero means covered.
    shortage_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: The earliest date any contributing demand needs it. The whole requirement
    #: is planned to that date: a merged part cannot be half early and half late.
    earliest_required_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: When the gross requirement is fully covered - by stock, by dated supply,
    #: by the order this plan raises, and never earlier than the item's own
    #: components plus its lead time (E3a). Nullable for rows written before
    #: that migration.
    expected_available_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    source_demand_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_demands: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # THE common-part merge. Two demands for the same screw are one row.
        UniqueConstraint("run_id", "item_id", name="uq_mrp_requirements_run_id_item_id"),
        # The shortage report: this run's short items, most urgent first.
        Index(
            "ix_mrp_requirements_shortage",
            "company_id",
            "run_id",
            "earliest_required_date",
            postgresql_where=text("shortage_quantity > 0"),
        ),
        # "What has this item been short of, historically?"
        Index("ix_mrp_requirements_item", "company_id", "item_id", "created_at"),
        CheckConstraint("level >= 0", name="level_non_negative"),
        CheckConstraint("direct_demand_quantity >= 0", name="direct_demand_non_negative"),
        CheckConstraint("dependent_demand_quantity >= 0", name="dependent_demand_non_negative"),
        CheckConstraint("gross_requirement >= 0", name="gross_requirement_non_negative"),
        CheckConstraint("on_hand_quantity >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved_quantity >= 0", name="reserved_non_negative"),
        CheckConstraint("available_quantity >= 0", name="available_non_negative"),
        CheckConstraint("incoming_quantity >= 0", name="incoming_non_negative"),
        CheckConstraint("in_production_quantity >= 0", name="in_production_non_negative"),
        CheckConstraint("assembly_quantity >= 0", name="assembly_non_negative"),
        CheckConstraint("job_work_quantity >= 0", name="job_work_non_negative"),
        CheckConstraint("late_incoming_quantity >= 0", name="late_incoming_non_negative"),
        CheckConstraint("late_in_production_quantity >= 0", name="late_in_production_non_negative"),
        CheckConstraint("late_in_assembly_quantity >= 0", name="late_in_assembly_non_negative"),
        CheckConstraint("late_job_work_quantity >= 0", name="late_job_work_non_negative"),
        # "Late" means exactly one thing: it lands after the date this
        # requirement needs it. A row claiming otherwise is a phasing bug, and
        # the database is the only place that statement cannot be quietly
        # broken by a later change to the engine. Asserted of each feed's own
        # date as well as of the summary - the summary is the earlier of the
        # two, so it alone passing would not prove the pair did.
        CheckConstraint(
            "first_late_supply_date IS NULL OR first_late_supply_date > earliest_required_date",
            name="late_supply_arrives_after_required",
        ),
        CheckConstraint(
            "first_late_incoming_date IS NULL OR first_late_incoming_date > earliest_required_date",
            name="late_incoming_arrives_after_required",
        ),
        CheckConstraint(
            "first_late_in_production_date IS NULL "
            "OR first_late_in_production_date > earliest_required_date",
            name="late_in_production_arrives_after_required",
        ),
        CheckConstraint(
            "first_late_in_assembly_date IS NULL "
            "OR first_late_in_assembly_date > earliest_required_date",
            name="late_in_assembly_arrives_after_required",
        ),
        CheckConstraint(
            "first_late_job_work_date IS NULL OR first_late_job_work_date > earliest_required_date",
            name="late_job_work_arrives_after_required",
        ),
        # The summary date is the earliest of all feed dates. Two
        # descriptions of one fact that can disagree is the defect these columns
        # were split to fix, one level up; this is that statement made
        # unbreakable rather than merely intended.
        #
        # ``LEAST`` ignores NULLs, so a row with one late feed reduces to that
        # feed's date. The first branch admits the rows revision ``ad9f573e5e59``
        # wrote, which carry a summary date and no per-feed dates because the
        # engine did not compute them yet and nothing can honestly say now which
        # feed that one date came from. "All per-feed dates NULL" therefore has to stay
        # legal, which means a per-feed date is only *guaranteed* present on
        # rows planned from ``66b1c0e2d1f4`` onwards. On an older row NULL reads
        # as "not recorded"; re-planning is how a current answer is obtained.
        CheckConstraint(
            "(first_late_incoming_date IS NULL "
            "AND first_late_in_production_date IS NULL "
            "AND first_late_in_assembly_date IS NULL "
            "AND first_late_job_work_date IS NULL) "
            "OR first_late_supply_date "
            "= LEAST(first_late_incoming_date, first_late_in_production_date, "
            "first_late_in_assembly_date, first_late_job_work_date)",
            name="first_late_supply_is_the_earlier",
        ),
        # A quantity with no date is the half-fact this column set exists to
        # replace. The converse is deliberately not asserted: a tranche smaller
        # than the 4dp quantum rounds to zero while still being a real, dated
        # arrival, and rejecting that row would be worse than reporting it.
        CheckConstraint(
            "late_incoming_quantity + late_in_production_quantity + "
            "late_in_assembly_quantity + late_job_work_quantity = 0 "
            "OR first_late_supply_date IS NOT NULL",
            name="late_quantity_names_its_date",
        ),
        # A closing date that precedes its own opening date, or exists without
        # one, would be a worse half-fact than the single date these replace.
        CheckConstraint(
            "last_late_incoming_date IS NULL "
            "OR (first_late_incoming_date IS NOT NULL "
            "AND last_late_incoming_date >= first_late_incoming_date)",
            name="late_incoming_interval_ordered",
        ),
        CheckConstraint(
            "last_late_in_production_date IS NULL "
            "OR (first_late_in_production_date IS NOT NULL "
            "AND last_late_in_production_date >= first_late_in_production_date)",
            name="late_production_interval_ordered",
        ),
        CheckConstraint(
            "last_late_in_assembly_date IS NULL "
            "OR (first_late_in_assembly_date IS NOT NULL "
            "AND last_late_in_assembly_date >= first_late_in_assembly_date)",
            name="late_assembly_interval_ordered",
        ),
        CheckConstraint(
            "last_late_job_work_date IS NULL "
            "OR (first_late_job_work_date IS NOT NULL "
            "AND last_late_job_work_date >= first_late_job_work_date)",
            name="late_job_work_interval_ordered",
        ),
        CheckConstraint("net_requirement >= 0", name="net_requirement_non_negative"),
        CheckConstraint("shortage_quantity >= 0", name="shortage_non_negative"),
        # The merge arithmetic, checked by the database. If these ever disagree
        # the plan is wrong in a way no report would reveal.
        CheckConstraint(
            "gross_requirement = direct_demand_quantity + dependent_demand_quantity",
            name="gross_is_direct_plus_dependent",
        ),
        # Netting only ever removes requirement, so each stage is bounded by the
        # one before it.
        CheckConstraint("net_requirement <= gross_requirement", name="net_within_gross"),
        CheckConstraint("shortage_quantity <= net_requirement", name="shortage_within_net"),
        CheckConstraint(
            "planned_reallocation_quantity >= 0", name="planned_reallocation_non_negative"
        ),
        CheckConstraint("source_demand_count >= 0", name="source_demand_count_non_negative"),
    )


class MrpSuggestion(TenantEntity):
    """What to do about one item's shortage, and what happened to that advice."""

    __tablename__ = "mrp_suggestions"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("mrp_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Snapshot of the effective default ASSEMBLY BOM behind this suggestion.
    #: The bare UUID keeps the plan independent of the BOM schema lifecycle.
    bom_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    suggestion_type: Mapped[MrpSuggestionType] = mapped_column(
        StatusEnum(MrpSuggestionType), nullable=False
    )

    #: The shortage as calculated - what is genuinely missing.
    required_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: What to actually order: the shortage raised to the minimum order quantity
    #: and rounded up to the supplier's pack multiple. Never below
    #: ``required_quantity``.
    suggested_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    shortage_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    available_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    incoming_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    required_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: ``required_date`` less the lead time. May be in the past, which is the
    #: point: the plan says the order needed placing last week.
    suggested_order_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Set when ``suggested_order_date`` has already passed. Stored rather than
    #: computed on read so the flag means "was late when planned" and does not
    #: silently change meaning as the run ages.
    is_late: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: The plan's own low-level code for the item (E3a) - the level it was
    #: netted at, so the work queue and the requirement table read side by side.
    #: Nullable for rows written before that migration.
    low_level_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``max(as_of, suggested_order_date) + lead_time_days``: the earliest date
    #: this order can physically land. Nullable for rows written before E3a.
    expected_available_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    from_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    to_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    from_reservation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    from_demand_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    from_demand_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    from_demand_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_demand_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_demand_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    to_demand_line_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    to_demand_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_demand_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source_demands: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[MrpSuggestionStatus] = mapped_column(
        StatusEnum(MrpSuggestionStatus),
        nullable=False,
        default=MrpSuggestionStatus.OPEN,
        server_default=MrpSuggestionStatus.OPEN.value,
    )
    #: The document class a converted suggestion became
    #: (``purchase_requisition``, ``production_order``, ...). Set even when the
    #: owning module is not deployed yet - see ``service.convert_suggestion``.
    converted_to_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: Bare UUID: the target lives in one of several tables owned by modules that
    #: are built independently of this one.
    converted_to_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why a planner rejected the advice. Required by the service, because
    #: "ignored" with no reason teaches the next run's reader nothing.
    ignored_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # A merged shortage has one supply action per item, while distinct
        # source/destination routes remain independently actionable.
        Index(
            "uq_mrp_suggestions_supply_run_item",
            "run_id",
            "item_id",
            unique=True,
            postgresql_where=text("suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK')"),
            sqlite_where=text("suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK')"),
        ),
        Index(
            "uq_mrp_suggestions_transfer_route",
            "run_id",
            "item_id",
            "from_warehouse_id",
            "to_warehouse_id",
            unique=True,
            postgresql_where=text("suggestion_type = 'TRANSFER'"),
            sqlite_where=text("suggestion_type = 'TRANSFER'"),
        ),
        Index(
            "uq_mrp_suggestions_reallocation_route",
            "run_id",
            "item_id",
            "from_reservation_id",
            "to_demand_id",
            "to_demand_line_id",
            unique=True,
            postgresql_where=text("suggestion_type = 'REALLOCATION'"),
            sqlite_where=text("suggestion_type = 'REALLOCATION'"),
        ),
        # Sales fulfilment resolves both direct facts and canonical BOM roots
        # with JSONB containment; tenant filtering remains part of every query.
        Index("ix_mrp_suggestions_source_demands", "source_demands", postgresql_using="gin"),
        # The suggestion list: a run filtered by status and type, urgent first.
        Index(
            "ix_mrp_suggestions_run_status",
            "company_id",
            "run_id",
            "status",
            "suggested_order_date",
        ),
        # The buyer's working queue across runs: everything still open, late
        # first. Partial, because a converted suggestion is history.
        Index(
            "ix_mrp_suggestions_open",
            "company_id",
            "suggestion_type",
            "suggested_order_date",
            postgresql_where=text("status = 'OPEN'"),
        ),
        CheckConstraint(
            f"suggestion_type IN ({sql_in_list(MrpSuggestionType)})", name="suggestion_type_known"
        ),
        CheckConstraint(
            "(suggestion_type = 'TRANSFER' AND from_warehouse_id IS NOT NULL "
            "AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id "
            "AND from_reservation_id IS NULL AND from_demand_type IS NULL "
            "AND from_demand_id IS NULL AND from_demand_code IS NULL "
            "AND to_demand_type IS NULL AND to_demand_id IS NULL "
            "AND to_demand_line_id IS NULL AND to_demand_code IS NULL "
            "AND to_demand_priority IS NULL) "
            "OR (suggestion_type = 'REALLOCATION' AND from_warehouse_id IS NOT NULL "
            "AND to_warehouse_id = from_warehouse_id AND from_reservation_id IS NOT NULL "
            "AND from_demand_type IN ('SALES_ORDER', 'PRODUCTION_ORDER', 'ASSEMBLY_ORDER', "
            "'JOB_WORK_ORDER', 'SPARE', 'TRANSFER') AND from_demand_id IS NOT NULL "
            "AND to_demand_type = 'SALES_ORDER' AND to_demand_id IS NOT NULL "
            "AND to_demand_line_id IS NOT NULL AND to_demand_priority IS NOT NULL "
            "AND to_demand_priority >= 0) "
            "OR (suggestion_type NOT IN ('TRANSFER', 'REALLOCATION') "
            "AND from_warehouse_id IS NULL AND to_warehouse_id IS NULL "
            "AND from_reservation_id IS NULL AND from_demand_type IS NULL "
            "AND from_demand_id IS NULL AND from_demand_code IS NULL "
            "AND to_demand_type IS NULL AND to_demand_id IS NULL "
            "AND to_demand_line_id IS NULL AND to_demand_code IS NULL "
            "AND to_demand_priority IS NULL)",
            name="transfer_route_matches_type",
        ),
        CheckConstraint(f"status IN ({sql_in_list(MrpSuggestionStatus)})", name="status_known"),
        CheckConstraint("required_quantity > 0", name="required_quantity_positive"),
        CheckConstraint("suggested_quantity > 0", name="suggested_quantity_positive"),
        CheckConstraint("shortage_quantity > 0", name="shortage_quantity_positive"),
        CheckConstraint("available_quantity >= 0", name="available_non_negative"),
        CheckConstraint("incoming_quantity >= 0", name="incoming_non_negative"),
        CheckConstraint("lead_time_days >= 0", name="lead_time_non_negative"),
        # Order policy may only round a shortage up. Rounding it down would ship
        # a plan that knowingly under-orders.
        CheckConstraint(
            "suggested_quantity >= required_quantity", name="suggested_covers_required"
        ),
        CheckConstraint(
            "suggested_order_date <= required_date", name="order_date_not_after_required"
        ),
        # A converted suggestion names what it became; an ignored one says why.
        CheckConstraint(
            "status <> 'CONVERTED' OR converted_to_type IS NOT NULL",
            name="converted_names_a_target",
        ),
        CheckConstraint(
            "status <> 'IGNORED' OR ignored_reason IS NOT NULL",
            name="ignored_carries_a_reason",
        ),
        CheckConstraint(
            "converted_at IS NULL OR status = 'CONVERTED'",
            name="conversion_timestamp_implies_converted",
        ),
    )


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "DEFAULT_HORIZON_DAYS",
    "MAX_DEMAND_LINES",
    "MAX_HORIZON_DAYS",
    "MAX_SOURCE_DEMANDS_LISTED",
    "MIN_HORIZON_DAYS",
    "MrpDemand",
    "MrpDemandSource",
    "MrpRequirement",
    "MrpRun",
    "MrpRunMode",
    "MrpRunStatus",
    "MrpScheduleSettings",
    "MrpSuggestion",
    "MrpSuggestionStatus",
    "MrpSuggestionType",
]
