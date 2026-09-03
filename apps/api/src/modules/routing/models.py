"""Production routing masters: where work happens, on what machine, in what order.

Four modelling decisions in this file are load-bearing.

**A routing is versioned like a BOM, not edited like a form.** ``(company_id,
item_id, version)`` is unique and an approved routing is never rewritten: a
production order raised in March must still cost and schedule against the
process that was current in March. A change is a new row created by
``copy-as-new-version``.

**"The" routing for an item is a partial unique index, not application logic.**
At most one row per item may be ``is_default`` *and* ``APPROVED`` at once. MRP
reads that pair to derive the manufacture lead time, production reads it to
raise work orders, and two winners would make the two modules silently plan
against different processes. PostgreSQL arbitrates it, so a concurrent approve
cannot produce two.

**A machine may only be named on an operation at its own work centre.**
``machines`` carries a redundant unique ``(id, work_center_id)`` purely so
``routing_operations`` can point at it with a *composite* foreign key. That
turns "this lathe is not in the heat-treatment cell" from a validation someone
has to remember into something the database refuses. ``machine_id`` is nullable
and PostgreSQL's default ``MATCH SIMPLE`` skips the check when any referencing
column is NULL, so an operation planned at a work centre without naming a
specific machine is still legal.

**The job-work vendor is a bare UUID.** ``routing_operations.job_work_vendor_id``
has no foreign key: a routing is a *template*, and the vendor recorded on it is a
planning default rather than a commitment. The binding vendor is chosen on the
job-work order itself. Same reasoning the inventory ledger uses for
``reference_id`` (database.md section 4.2).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity, TenantMasterEntity
from src.core.types import GUID, Money, Quantity

#: Fallback shift length when a work centre has not stated one. Named rather
#: than inlined because it is the divisor in the manufacture lead time MRP
#: plans from, and a site running two shifts changes it here.
DEFAULT_WORKING_HOURS_PER_DAY = Decimal("8")

#: A calendar day cannot hold more than this. Enforced as a CHECK because a
#: typo here silently halves every lead time in the plan.
MAX_WORKING_HOURS_PER_DAY = Decimal("24")


class MachineStatus(StrEnum):
    """Availability of one machine.

    ``RETIRED`` is terminal: a scrapped machine is kept because historical
    production entries name it, but it can never come back into a plan.
    """

    AVAILABLE = "AVAILABLE"
    RUNNING = "RUNNING"
    MAINTENANCE = "MAINTENANCE"
    BREAKDOWN = "BREAKDOWN"
    RETIRED = "RETIRED"


class RoutingStatus(StrEnum):
    """Lifecycle of one routing version.

    Only ``APPROVED`` routings are visible to planning and production.
    ``OBSOLETE`` is terminal - a superseded process is retained because the
    orders raised against it still reference it.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    OBSOLETE = "OBSOLETE"


class WorkCenter(TenantMasterEntity):
    """A costed capacity centre - a cell, a bench, a line.

    Capacity and rate live here rather than on each operation so a rate revision
    is one edit instead of one per routing. The consequence is deliberate: the
    cost of an *approved* routing moves when the work-centre rate moves, which is
    what a standard-costing model wants. A frozen snapshot of the rate at the
    moment of approval belongs on ``bom_operations``, which is exactly what that
    table stores.
    """

    __tablename__ = "work_centers"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Nullable: a single-site company never creates a plant or a department, and
    # forcing the organisation hierarchy to exist before any routing can be
    # entered would make the module unusable on day one. RESTRICT because a
    # plant that still has work centres under it must not be deletable.
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plants.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    #: Units of output the centre can produce per hour. Zero means "not rated" -
    #: capacity planning skips it rather than dividing by zero.
    capacity_per_hour: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Blended labour + machine + overhead rate. The only rate the routing cost
    #: roll-up multiplies by hours.
    cost_per_hour: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Flat changeover charge for a production run at this centre. Deliberately
    #: NOT part of the routing cost roll-up: the setup *time* is already costed
    #: through ``cost_per_hour``, and charging both would count the same
    #: changeover twice. Production-order costing applies it once per run.
    setup_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Hours this centre is manned per calendar day. The divisor that turns
    #: routing minutes into the manufacture lead time MRP schedules against.
    working_hours_per_day: Mapped[Decimal] = mapped_column(
        Quantity,
        nullable=False,
        default=DEFAULT_WORKING_HOURS_PER_DAY,
        server_default=str(DEFAULT_WORKING_HOURS_PER_DAY),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_work_centers_company_id_code"),
        Index("ix_work_centers_company_active", "company_id", "is_active"),
        CheckConstraint("capacity_per_hour >= 0", name="capacity_non_negative"),
        CheckConstraint("cost_per_hour >= 0", name="cost_per_hour_non_negative"),
        CheckConstraint("setup_cost >= 0", name="setup_cost_non_negative"),
        CheckConstraint(
            f"working_hours_per_day > 0 AND working_hours_per_day <= {MAX_WORKING_HOURS_PER_DAY}",
            name="working_hours_per_day_bounded",
        ),
    )


class Machine(TenantMasterEntity):
    """One machine inside a work centre.

    Its own ``capacity_per_hour`` and ``cost_per_hour`` describe the individual
    asset; the routing cost roll-up charges the *work centre* rate instead,
    because an operation may be planned at the centre without committing to a
    machine and the two answers must not depend on that choice.
    """

    __tablename__ = "machines"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    work_center_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Free text ("CNC lathe", "hydraulic press"). A lookup table would be an
    #: extra master for a field no rule branches on.
    machine_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    capacity_per_hour: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    cost_per_hour: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    status: Mapped[MachineStatus] = mapped_column(
        String(24),
        nullable=False,
        default=MachineStatus.AVAILABLE,
        server_default=MachineStatus.AVAILABLE,
    )

    last_maintenance_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_maintenance_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_machines_company_id_code"),
        # Target for the routing_operations composite foreign key that keeps a
        # machine and its operation in the same work centre. Not a business rule
        # of its own - id is already unique - but a foreign key cannot reference
        # an unconstrained pair of columns.
        UniqueConstraint("id", "work_center_id", name="uq_machines_id_work_center_id"),
        # "What can run in this cell right now?" - the shop-floor board's query.
        Index("ix_machines_company_work_center_status", "company_id", "work_center_id", "status"),
        # Maintenance-due report. Partial: most machines have no scheduled date,
        # and indexing their NULLs would grow the index for a row no one reads.
        Index(
            "ix_machines_next_maintenance_due",
            "company_id",
            "next_maintenance_date",
            postgresql_where=text("next_maintenance_date IS NOT NULL"),
        ),
        CheckConstraint("capacity_per_hour >= 0", name="capacity_non_negative"),
        CheckConstraint("cost_per_hour >= 0", name="cost_per_hour_non_negative"),
        # A service due before the last one was done is a mis-keyed date, and it
        # would make the maintenance report claim the machine is overdue forever.
        CheckConstraint(
            "next_maintenance_date IS NULL OR last_maintenance_date IS NULL "
            "OR next_maintenance_date >= last_maintenance_date",
            name="maintenance_dates_ordered",
        ),
    )


class Routing(TenantEntity):
    """One version of the process that makes one item."""

    __tablename__ = "routings"

    #: Entered by the engineer rather than issued from a document sequence: a
    #: routing is master data quoted on drawings and travellers, so the code is
    #: theirs to choose. Unique per company, normalised on the way in.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    status: Mapped[RoutingStatus] = mapped_column(
        String(24),
        nullable=False,
        default=RoutingStatus.DRAFT,
        server_default=RoutingStatus.DRAFT,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # A process change takes effect on a date, not at the moment somebody
    # clicked approve, so MRP asks "what was effective on the required date?".
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Approval evidence (master rules section 42). All-or-nothing, enforced by
    # the CHECK below: a row claiming APPROVED with no approver cannot be
    # audited afterwards.
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lost-update protection for a document several engineers edit at once
    # (database.md section 1). ``OptimisticLockMixin`` cannot be used: it names
    # its counter ``version``, and that name is taken by the routing's own
    # engineering version. Same mechanism, different column - SQLAlchemy raises
    # StaleDataError, which the handler turns into a 409 rather than letting the
    # second writer silently overwrite the first.
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012 - SQLAlchemy API

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_routings_company_id_code"),
        UniqueConstraint(
            "company_id", "item_id", "version", name="uq_routings_company_id_item_version"
        ),
        # THE rule MRP and production both depend on: one live default process
        # per item. Enforced by PostgreSQL because two modules resolving
        # different routings for one item is a silent, expensive divergence.
        Index(
            "uq_routings_one_default_approved_per_item",
            "company_id",
            "item_id",
            unique=True,
            postgresql_where=text("is_default AND status = 'APPROVED'"),
        ),
        # The lead-time lookup's hot path: "effective approved routings for this
        # set of items". Partial, because only APPROVED rows are ever resolved
        # this way and they are a minority once a few revisions exist.
        Index(
            "ix_routings_effective_lookup",
            "company_id",
            "item_id",
            "effective_from",
            "effective_to",
            postgresql_where=text("status = 'APPROVED'"),
        ),
        Index("ix_routings_company_status", "company_id", "status", "created_at"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effectivity_window_ordered",
        ),
        CheckConstraint(
            "(status <> 'APPROVED') OR (approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_rows_carry_approval_evidence",
        ),
    )


class RoutingOperation(TenantEntity):
    """One step of a routing.

    The four time components are separate columns rather than one total because
    they behave differently: setup is charged once per run, run time scales with
    the quantity, and queue and move are elapsed time that costs nothing but
    still delays the next step. Collapsing them would make the cost roll-up and
    the lead time impossible to derive from the same row.
    """

    __tablename__ = "routing_operations"

    routing_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("routings.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Execution order, and the identity of the step on the shop-floor traveller.
    #: Stable and unique per routing rather than an array position, because
    #: production entries and BOM lines reference it by number.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_code: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_name: Mapped[str] = mapped_column(String(255), nullable=False)

    work_center_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Optional: an operation may be planned at a work centre without committing
    #: to one machine. When it is named, the composite FK below guarantees the
    #: machine actually sits in that work centre.
    machine_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Charged once per production run, whatever the quantity.
    setup_time_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Multiplied by the order quantity.
    run_time_per_unit_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Waiting before the step can start, and moving to the next centre after
    #: it. Elapsed time that costs nothing but is part of the lead time.
    queue_time_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    move_time_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Flat non-time charge for the step - a plating bath minimum, a job-work
    #: piece rate, a die charge. Added to the time-based cost, never instead
    #: of it.
    process_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    is_outsourced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Planning default only - no foreign key. See the module docstring.
    job_work_vendor_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Production may not report this step complete until an inspection has been
    #: recorded against it (master rules section 15).
    qc_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Keeps a named machine inside the operation's own work centre. A NULL
        # machine_id skips the check under PostgreSQL's default MATCH SIMPLE,
        # which is what makes "no machine chosen yet" legal. RESTRICT: a machine
        # named on a routing must not be deletable out from under it.
        ForeignKeyConstraint(
            ["machine_id", "work_center_id"],
            ["machines.id", "machines.work_center_id"],
            name="fk_routing_operations_machine_id_work_center_id_machines",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("routing_id", "sequence", name="uq_routing_operations_routing_sequence"),
        # The costing and lead-time queries load a whole routing's steps in order.
        Index("ix_routing_operations_routing", "company_id", "routing_id", "sequence"),
        # Load analysis, and "what breaks if this centre goes down?".
        Index("ix_routing_operations_work_center", "company_id", "work_center_id"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("setup_time_minutes >= 0", name="setup_time_non_negative"),
        CheckConstraint("run_time_per_unit_minutes >= 0", name="run_time_non_negative"),
        CheckConstraint("queue_time_minutes >= 0", name="queue_time_non_negative"),
        CheckConstraint("move_time_minutes >= 0", name="move_time_non_negative"),
        CheckConstraint("process_cost >= 0", name="process_cost_non_negative"),
        # ASSUMPTION: a job-work vendor may only be named on an operation that is
        # flagged outsourced. The converse is deliberately NOT enforced - a step
        # can be known to be subcontracted long before the vendor is chosen - so
        # an outsourced operation with no vendor stays legal.
        CheckConstraint(
            "job_work_vendor_id IS NULL OR is_outsourced",
            name="job_work_vendor_only_when_outsourced",
        ),
    )


__all__ = [
    "DEFAULT_WORKING_HOURS_PER_DAY",
    "MAX_WORKING_HOURS_PER_DAY",
    "Machine",
    "MachineStatus",
    "Routing",
    "RoutingOperation",
    "RoutingStatus",
    "WorkCenter",
]
