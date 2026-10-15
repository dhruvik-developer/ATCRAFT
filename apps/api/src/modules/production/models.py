"""Production tables: the shop-floor execution of a manufacturing order.

Master rules section 18, database contract sections 7 and 8. Six tables, and the
reason each exists as a separate one is the reason the module is correct:

``production_orders``
    The document. Carries what was planned, what came out, and - once closed -
    what it actually cost. ``OptimisticLockMixin`` because a planner, a store
    keeper and a supervisor all touch the same order within minutes of each
    other, and a lost update here silently changes a quantity somebody else is
    already producing against.

``production_order_components``
    **A snapshot of the exploded BOM taken at release time.** This is the single
    most important design decision in the module. A BOM revised mid-production
    must not retroactively change what an in-flight order needs: the kit was
    picked, the material was issued and the operator is building against the
    structure as it stood when the job was released. If the requirement were
    read live from ``bom_lines`` every time, an engineering change on Tuesday
    would rewrite Monday's issue variance, make the closed cost of a job
    unreproducible, and leave a store keeper holding parts the system says were
    never needed. The snapshot is also what makes the shortage report
    defensible: it compares stock against what *this* order asked for, not
    against what the current drawing says.

``work_orders``
    One row per routing operation, snapshotted the same way and for the same
    reason. Its planned times come from the routing at release; its actual times
    are what closure costs.

``material_issues`` / ``material_issue_lines``
    The document behind a stock movement out of the store and into the job -
    and, with ``issue_type = RETURN``, back again. One table pair rather than
    two because an issue and its reversal share every column, every rule and
    every posting path; only the sign of the movement differs, and that is
    already carried by the transaction type.

``production_entries``
    Daily shop-floor booking. The entry that books the *final* operation posts
    ``PRODUCTION_RECEIPT`` into the output warehouse, which is why it stores the
    id of the ledger row it created.

Why component consumption is not a second stock movement
--------------------------------------------------------
``MATERIAL_ISSUE`` already takes the component out of the warehouse when the
issue is posted (database.md section 8). ``consumed_quantity`` on the component
therefore records *work-in-progress accounting* - that the issued material has
been built into output and is no longer expected back - and never posts a
movement of its own. Posting one at completion would remove the same stock
twice.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models import OptimisticLockMixin, TenantEntity, sql_in_list
from src.core.types import GUID, Money, Quantity, UnitPrice
from src.modules.inventory.models import StatusEnum

# -----------------------------------------------------------------------------
# Vocabulary
# -----------------------------------------------------------------------------

#: Default reservation/scheduling priority. Lower is more urgent, matching the
#: allocation module so the two numbers are directly comparable when a
#: reallocation weighs a production order against a sales order.
DEFAULT_PRODUCTION_PRIORITY = 100

# ASSUMPTION: a job may draw up to 10% more of a component than its snapshotted
# requirement before the issue is refused. Some over-issue is normal - a bar is
# cut short, a casting is spoiled at the first operation - and refusing it
# outright would push storekeepers into raising stock adjustments to get work
# done, which is far worse for traceability. The bound exists so a mis-keyed
# 1000-for-100 is caught before it drains a store. Same shape and same reasoning
# as procurement's over-receipt tolerance; change it here, not per call site.
OVER_ISSUE_TOLERANCE = Decimal("0.10")
OVER_ISSUE_MULTIPLIER = Decimal("1") + OVER_ISSUE_TOLERANCE

# ASSUMPTION: a job may book up to 10% more good output than it planned. Yield
# above plan happens; yield of ten times plan is a decimal point in the wrong
# place, and it would post that stock into the warehouse before anybody noticed.
OVER_PRODUCTION_TOLERANCE = Decimal("0.10")
OVER_PRODUCTION_MULTIPLIER = Decimal("1") + OVER_PRODUCTION_TOLERANCE


class ProductionOrderStatus(StrEnum):
    """Database contract section 7, verbatim.

    ``DRAFT -> APPROVED -> RELEASED -> MATERIAL_ISSUED -> IN_PROCESS ->
    COMPLETED -> CLOSED``, with ``CANCELLED`` reachable only before the job is
    physically under way.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"
    MATERIAL_ISSUED = "MATERIAL_ISSUED"
    IN_PROCESS = "IN_PROCESS"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ProductionSourceType(StrEnum):
    """Where the demand for this order came from.

    Not a foreign key: the source is an MRP suggestion, a sales order or nothing
    at all, and a polymorphic FK is not expressible in SQL. The pair
    ``(source_type, source_id)`` is the reference and is indexed as one.
    """

    MANUAL = "MANUAL"
    MRP_SUGGESTION = "MRP_SUGGESTION"
    SALES_ORDER = "SALES_ORDER"


class WorkOrderStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class MaterialIssueType(StrEnum):
    """Direction of the movement the document posts.

    ``ISSUE`` posts ``MATERIAL_ISSUE`` out of the store; ``RETURN`` posts
    ``MATERIAL_RETURN`` back into it.
    """

    ISSUE = "ISSUE"
    RETURN = "RETURN"


class MaterialIssueStatus(StrEnum):
    """``POSTED`` is terminal on purpose.

    Once stock has moved, the document is history. A mistake is corrected with a
    return document that posts the offsetting movement, never by editing the
    issue - the same rule the inventory ledger itself follows.
    """

    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


#: Statuses in which a production order is live on the shop floor. Backs the
#: partial index the WIP board reads and the "is this order still open?" checks.
OPEN_PRODUCTION_STATUSES: frozenset[ProductionOrderStatus] = frozenset(
    {
        ProductionOrderStatus.RELEASED,
        ProductionOrderStatus.MATERIAL_ISSUED,
        ProductionOrderStatus.IN_PROCESS,
    }
)

#: Statuses in which material may be issued to or returned from an order.
ISSUABLE_PRODUCTION_STATUSES: frozenset[ProductionOrderStatus] = frozenset(
    {
        ProductionOrderStatus.RELEASED,
        ProductionOrderStatus.MATERIAL_ISSUED,
        ProductionOrderStatus.IN_PROCESS,
    }
)

#: Statuses that can only have been reached through approval. Backs the CHECK
#: that refuses a released order with no approver on it.
POST_APPROVAL_STATUSES: tuple[ProductionOrderStatus, ...] = (
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
    ProductionOrderStatus.COMPLETED,
    ProductionOrderStatus.CLOSED,
)


# -----------------------------------------------------------------------------
# The order
# -----------------------------------------------------------------------------


class ProductionOrder(TenantEntity, OptimisticLockMixin):
    """One manufacturing job: make ``planned_quantity`` of ``item_id``."""

    __tablename__ = "production_orders"

    code: Mapped[str] = mapped_column(String(32), nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    #: The structure this order is built against. Nullable while the order is a
    #: draft - a planner may raise the job before choosing between a default
    #: structure and an alternate - and resolved and frozen at release.
    bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("boms.id", ondelete="RESTRICT"), nullable=True
    )
    routing_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("routings.id", ondelete="RESTRICT"), nullable=True
    )

    planned_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Good output actually received into stock. Only a production entry that
    #: posts the output receipt moves this, so it can never disagree with the
    #: ledger.
    produced_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Output made and refused. Never received into stock: it was never good.
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Planned output written off as unrecoverable at completion.
    scrap_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Where the finished goods land.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plants.id", ondelete="RESTRICT"), nullable=True
    )
    #: The cost centre the job is charged to. Material issues carry none of
    #: their own: they reach a cost centre through ``production_order_id``.
    #: NULL is "unallocated".
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("cost_centres.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # Planning is done in whole days; the shop floor's minute-level detail lives
    # on the work orders, which is where a scheduler actually needs it.
    planned_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    actual_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Lower is more urgent. An integer rather than an enum because a planner
    #: re-ranks jobs numerically and reallocation compares two of them.
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_PRODUCTION_PRIORITY,
        server_default=str(DEFAULT_PRODUCTION_PRIORITY),
    )

    status: Mapped[ProductionOrderStatus] = mapped_column(
        StatusEnum(ProductionOrderStatus),
        nullable=False,
        default=ProductionOrderStatus.DRAFT,
        server_default=ProductionOrderStatus.DRAFT,
    )

    #: Good output of this order is booked into QC_HOLD with an in-process
    #: inspection per booking, and the order cannot COMPLETE while any of it is
    #: held. Decided at creation from the routing and the item (§E5-1), and
    #: editable while the order is still a draft.
    requires_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    source_type: Mapped[ProductionSourceType] = mapped_column(
        StatusEnum(ProductionSourceType),
        nullable=False,
        default=ProductionSourceType.MANUAL,
        server_default=ProductionSourceType.MANUAL,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Why the job was closed short, over, or with a variance. Written at CLOSED.
    closure_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Actual cost, computed once at closure from the movements and the work
    # orders. Stored rather than recomputed on read: the work-centre rates and
    # the stock valuation both move afterwards, and a closed job's cost must not.
    total_material_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_operation_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    standard_unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    standard_material_unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    standard_operation_unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    standard_subcontract_unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    actual_subcontract_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    actual_unit_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    material_variance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    operation_variance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    subcontract_variance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    total_variance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    total_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # lazy="raise": an order list must never load components per row. The detail
    # and posting paths load them through the repository with one statement.
    components: Mapped[list[ProductionOrderComponent]] = relationship(
        back_populates="order",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="ProductionOrderComponent.line_number",
    )
    work_orders: Mapped[list[WorkOrder]] = relationship(
        back_populates="order",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="WorkOrder.sequence",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_production_orders_company_id_code"),
        # The shop-floor schedule and the planner's queue: open jobs, most urgent
        # first. Partial because a finished order is never on this board and
        # indexing years of closed jobs to answer "what is running?" is waste.
        Index(
            "ix_production_orders_open",
            "company_id",
            "priority",
            "planned_end_date",
            postgresql_where=text(f"status IN ({sql_in_list(sorted(OPEN_PRODUCTION_STATUSES))})"),
        ),
        Index("ix_production_orders_status", "company_id", "status", "planned_start_date"),
        Index(
            "ix_production_orders_company_status_updated_at", "company_id", "status", "updated_at"
        ),
        Index("ix_production_orders_item", "company_id", "item_id", "status"),
        Index("ix_production_orders_warehouse", "company_id", "warehouse_id", "status"),
        Index("ix_production_orders_plant", "company_id", "plant_id", "status"),
        # "Which order covered this MRP suggestion / sales order?"
        Index("ix_production_orders_source", "company_id", "source_type", "source_id"),
        CheckConstraint("planned_quantity > 0", name="planned_quantity_positive"),
        CheckConstraint("produced_quantity >= 0", name="produced_quantity_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_quantity_non_negative"),
        CheckConstraint("scrap_quantity >= 0", name="scrap_quantity_non_negative"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        CheckConstraint("total_material_cost >= 0", name="material_cost_non_negative"),
        CheckConstraint("total_operation_cost >= 0", name="operation_cost_non_negative"),
        CheckConstraint("total_cost >= 0", name="total_cost_non_negative"),
        CheckConstraint("planned_end_date >= planned_start_date", name="planned_window_ordered"),
        CheckConstraint(
            "actual_end_date IS NULL OR actual_start_date IS NULL "
            "OR actual_end_date >= actual_start_date",
            name="actual_window_ordered",
        ),
        # Approval evidence is all-or-nothing (master rules section 42): a row
        # naming an approver with no timestamp cannot be audited afterwards.
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)", name="approval_evidence_paired"
        ),
        # A job cannot be on the floor without somebody having approved it.
        CheckConstraint(
            f"status NOT IN ({sql_in_list(sorted(POST_APPROVAL_STATUSES))}) "
            "OR approved_by_id IS NOT NULL",
            name="approved_orders_carry_approval_evidence",
        ),
        {"comment": "Manufacturing orders. Costs are written once, at closure."},
    )


class ProductionOrderComponent(TenantEntity):
    """One exploded BOM line, frozen at the moment the order was released.

    See the module docstring: this table is a snapshot and never a live view. A
    BOM revised while the job is running changes the *next* order's components,
    never this one's.
    """

    __tablename__ = "production_order_components"

    production_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("production_orders.id", ondelete="CASCADE"), nullable=False
    )
    #: Stable identity on the printed kit list, and the ordering the store keeper
    #: picks in. Assigned from the explosion's own ordering (level, then code).
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    #: Snapshotted with the requirement. An item code can be corrected in the
    #: master afterwards; the kit list that was picked said this.
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    #: Shallowest BOM level the component appeared at, carried from the
    #: explosion so a multi-level kit can still be read as a structure.
    bom_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Scrap-adjusted requirement for the whole order. There is no "optional"
    #: flag because the explosion never emits optional or substitute lines
    #: (see ``src.modules.bom.engine.is_explodable``) - every row here is
    #: material the job genuinely needs.
    required_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    issued_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    returned_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Issued material accounted as built into the output. A WIP figure, not a
    #: movement - the stock already left the store at issue time.
    consumed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Where this component is drawn from.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    #: Issued automatically at completion in proportion to the output, rather
    #: than picked and booked by hand.
    is_backflushed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    order: Mapped[ProductionOrder] = relationship(back_populates="components", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "production_order_id",
            "line_number",
            name="uq_production_order_components_order_line",
        ),
        # One row per item per order: the explosion consolidates common parts
        # (database.md section 5), so two rows for one item would mean the
        # snapshot was written twice and the requirement double counted.
        UniqueConstraint(
            "production_order_id", "item_id", name="uq_production_order_components_order_item"
        ),
        Index("ix_production_order_components_order", "production_order_id", "line_number"),
        # "Which live jobs need this part?" - the shortage chase.
        Index("ix_production_order_components_item", "company_id", "item_id"),
        CheckConstraint("required_quantity > 0", name="required_quantity_positive"),
        CheckConstraint("issued_quantity >= 0", name="issued_quantity_non_negative"),
        CheckConstraint("returned_quantity >= 0", name="returned_quantity_non_negative"),
        CheckConstraint("consumed_quantity >= 0", name="consumed_quantity_non_negative"),
        # Returning more than was ever issued would manufacture stock out of a
        # clerical error.
        CheckConstraint("returned_quantity <= issued_quantity", name="returned_within_issued"),
        # Consumption is limited to what is still with the job.
        CheckConstraint(
            "consumed_quantity <= issued_quantity - returned_quantity",
            name="consumed_within_net_issued",
        ),
        CheckConstraint("bom_level >= 1", name="bom_level_positive"),
    )


# -----------------------------------------------------------------------------
# Work orders
# -----------------------------------------------------------------------------


class WorkOrder(TenantEntity):
    """One routing operation of one production order, as issued to the floor."""

    __tablename__ = "work_orders"

    code: Mapped[str] = mapped_column(String(32), nullable=False)

    production_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("production_orders.id", ondelete="CASCADE"), nullable=False
    )
    #: Execution order, taken from the routing operation's sequence so the
    #: traveller, the BOM's ``operation_sequence`` and this row all agree.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Snapshot of the routing operation. Deliberately not a foreign key: the
    #: routing may be superseded or made obsolete while the job runs, and the
    #: work order must still describe the step that was actually performed.
    routing_operation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    operation_code: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_name: Mapped[str] = mapped_column(String(255), nullable=False)

    work_center_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=False
    )
    #: Optional: an operation may be planned at a centre without committing to a
    #: machine. The composite foreign key keeps a named machine inside the
    #: operation's own work centre - the same guard ``routing_operations`` uses.
    machine_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    operator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The shift master row this operation is planned on, chosen from the
    #: routing module's governed list. NULL on work orders assigned before the
    #: master existed, which keep their typed text below and nothing invented.
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=True
    )
    #: The shift's CODE as it was when chosen - a snapshot, like
    #: ``operation_code`` and the rate columns on this row, written whenever
    #: ``shift_id`` is set and cleared with it. Reports group by it, so legacy
    #: free-text rows and governed ones sit together. Never written directly.
    shift: Mapped[str | None] = mapped_column(String(16), nullable=True)

    planned_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    planned_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    planned_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    completed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # Planned times snapshotted from the routing so closure can fall back to
    # them when nobody clocked the job, and so re-timing the routing afterwards
    # cannot rewrite what this order was scheduled to take.
    planned_setup_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    planned_run_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Flat non-time charge for the step (plating bath minimum, die charge),
    #: snapshotted because it is a price, not a duration.
    process_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    is_outsourced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # The work centre's rates as they stood when the job was released.
    #
    # Snapshotted, unlike the *routing* cost roll-up, which deliberately reads
    # the live rate so a revision flows through to the standard cost of every
    # approved process. This is not a standard cost: it is what one job actually
    # cost, and work done in April must stay costed at April's rate however many
    # times the figure is asked for afterwards. Without the snapshot, a rate
    # revision would silently move the per-operation breakdown of a closed job
    # away from the total that was stored on it.
    work_center_cost_per_hour: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    work_center_setup_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    status: Mapped[WorkOrderStatus] = mapped_column(
        StatusEnum(WorkOrderStatus),
        nullable=False,
        default=WorkOrderStatus.PENDING,
        server_default=WorkOrderStatus.PENDING,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    order: Mapped[ProductionOrder] = relationship(back_populates="work_orders", lazy="raise")

    __table_args__ = (
        ForeignKeyConstraint(
            ["machine_id", "work_center_id"],
            ["machines.id", "machines.work_center_id"],
            name="fk_work_orders_machine_id_work_center_id_machines",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("company_id", "code", name="uq_work_orders_company_id_code"),
        UniqueConstraint("production_order_id", "sequence", name="uq_work_orders_order_sequence"),
        # The work-centre load board: what is queued or running in this cell.
        Index("ix_work_orders_work_center", "company_id", "work_center_id", "status"),
        Index("ix_work_orders_order", "production_order_id", "sequence"),
        Index("ix_work_orders_operator", "company_id", "operator_user_id", "status"),
        Index("ix_work_orders_machine", "company_id", "machine_id", "status"),
        # The work-order queue's shift filter.
        Index("ix_work_orders_shift", "company_id", "shift_id", "status"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("planned_quantity > 0", name="planned_quantity_positive"),
        CheckConstraint("completed_quantity >= 0", name="completed_quantity_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_quantity_non_negative"),
        CheckConstraint("planned_setup_minutes >= 0", name="setup_minutes_non_negative"),
        CheckConstraint("planned_run_minutes >= 0", name="run_minutes_non_negative"),
        CheckConstraint("process_cost >= 0", name="process_cost_non_negative"),
        CheckConstraint("work_center_cost_per_hour >= 0", name="cost_per_hour_non_negative"),
        CheckConstraint("work_center_setup_cost >= 0", name="setup_cost_non_negative"),
        CheckConstraint(
            "planned_end_at IS NULL OR planned_start_at IS NULL "
            "OR planned_end_at >= planned_start_at",
            name="planned_window_ordered",
        ),
        CheckConstraint(
            "actual_end_at IS NULL OR actual_start_at IS NULL OR actual_end_at >= actual_start_at",
            name="actual_window_ordered",
        ),
        # An operation cannot have finished before it started.
        CheckConstraint(
            "status <> 'COMPLETED' OR actual_start_at IS NOT NULL",
            name="completed_operations_were_started",
        ),
    )


# -----------------------------------------------------------------------------
# Material issue / return
# -----------------------------------------------------------------------------


class MaterialIssue(TenantEntity):
    """A document moving component stock between the store and a job."""

    __tablename__ = "material_issues"

    code: Mapped[str] = mapped_column(String(32), nullable=False)

    #: RESTRICT: an order that has moved stock must never be deletable. Only a
    #: DRAFT order can be deleted, and a draft has no issues.
    production_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("production_orders.id", ondelete="RESTRICT"), nullable=False
    )

    issue_type: Mapped[MaterialIssueType] = mapped_column(
        StatusEnum(MaterialIssueType),
        nullable=False,
        default=MaterialIssueType.ISSUE,
        server_default=MaterialIssueType.ISSUE,
    )
    status: Mapped[MaterialIssueStatus] = mapped_column(
        StatusEnum(MaterialIssueStatus),
        nullable=False,
        default=MaterialIssueStatus.DRAFT,
        server_default=MaterialIssueStatus.DRAFT,
    )

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Source store for an issue, destination store for a return. Lines may name
    #: their own, which is what a multi-store kit needs.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Never blank: master rules section 83 requires a reason on every stock
    #: event, and this document is the reason the movement exists.
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Valued at posting from the movement's own cost, which is the weighted
    #: average the inventory engine applied - not a price this module invented.
    total_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    lines: Mapped[list[MaterialIssueLine]] = relationship(
        back_populates="issue",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="MaterialIssueLine.line_number",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_material_issues_company_id_code"),
        Index("ix_material_issues_order", "company_id", "production_order_id", "issue_date"),
        Index("ix_material_issues_status", "company_id", "status", "issue_date"),
        Index("ix_material_issues_warehouse", "company_id", "warehouse_id", "status"),
        CheckConstraint("line_count >= 0", name="line_count_non_negative"),
        CheckConstraint("total_quantity >= 0", name="total_quantity_non_negative"),
        CheckConstraint("total_cost >= 0", name="total_cost_non_negative"),
        # Posting evidence is all-or-nothing, and only a posted document has it.
        CheckConstraint(
            "(posted_at IS NULL) = (posted_by_id IS NULL)", name="posting_evidence_paired"
        ),
        CheckConstraint(
            "(status = 'POSTED') = (posted_at IS NOT NULL)", name="posted_rows_carry_posting_time"
        ),
    )


class MaterialIssueLine(TenantEntity):
    """One component quantity on a material issue or return."""

    __tablename__ = "material_issue_lines"

    material_issue_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("material_issues.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: RESTRICT: the component row is the requirement this line was issued
    #: against, and the issue variance is meaningless without it. Every line
    #: names one - unplanned material is a stock adjustment or a BOM revision,
    #: not a silent addition to a released job.
    component_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("production_order_components.id", ondelete="RESTRICT"), nullable=False
    )
    #: Denormalised from the component so the posting path builds its movements
    #: without a second lookup, and so reporting can group by item directly.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True
    )

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Written at posting from the inventory transaction, so the job's material
    #: cost is what the stock was actually valued at when it left the store.
    unit_cost: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    issue: Mapped[MaterialIssue] = relationship(back_populates="lines", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "material_issue_id", "line_number", name="uq_material_issue_lines_issue_line"
        ),
        Index("ix_material_issue_lines_issue", "material_issue_id", "line_number"),
        Index("ix_material_issue_lines_component", "company_id", "component_id"),
        Index("ix_material_issue_lines_item", "company_id", "item_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        CheckConstraint("total_cost >= 0", name="total_cost_non_negative"),
    )


# -----------------------------------------------------------------------------
# Shop-floor bookings
# -----------------------------------------------------------------------------


class ProductionEntry(TenantEntity):
    """One shift's booking against a job, and possibly its output receipt.

    Never edited after it is written: the entry that books final output has
    posted stock, and a booking that moved the ledger is history. The service
    exposes creation only.
    """

    __tablename__ = "production_entries"

    production_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("production_orders.id", ondelete="RESTRICT"), nullable=False
    )
    #: The operation the booking is against. Nullable: an order made without a
    #: routing has no work orders, and its output is still booked here.
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("work_orders.id", ondelete="RESTRICT"), nullable=True
    )

    #: Business date of the shift, which is regularly not the date the entry was
    #: keyed - night shift output is booked the following morning.
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The shift this output was booked against. NULL on bookings made before
    #: the shift master existed; see ``WorkOrder.shift_id``.
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=True
    )
    #: The shift's code snapshot - ``WorkOrder.shift`` explains why it is kept.
    shift: Mapped[str | None] = mapped_column(String(16), nullable=True)

    operator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("machines.id", ondelete="RESTRICT"), nullable=True
    )

    produced_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Machine or operator time lost during the shift. Reported for OEE, and
    #: deliberately *not* subtracted from the costed time: the centre was
    #: occupied by this job while it stood idle.
    downtime_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The ``PRODUCTION_RECEIPT`` this entry posted, when it booked final output.
    #: A bare UUID rather than a foreign key: the ledger outlives its source, so
    #: purging an archived booking must not be able to delete stock history.
    output_transaction_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: The in-process inspection this booking raised, when the order requires
    #: QC. A bare UUID for the same reason as above: quality owns the row, and
    #: this module reaches it through the owner callback, never a join.
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Denormalised from the inspection so the completion gate can name the open
    #: inspections without reading another module's table.
    qc_inspection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Set while the booking's output sits in QC_HOLD, cleared by the quality
    #: verdict callback. The order cannot COMPLETE while any entry holds this.
    qc_hold_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_production_entries_order", "company_id", "production_order_id", "entry_date"),
        Index("ix_production_entries_work_order", "company_id", "work_order_id", "entry_date"),
        # The daily production report, and the shift board.
        Index("ix_production_entries_date", "company_id", "entry_date", "shift"),
        Index("ix_production_entries_operator", "company_id", "operator_user_id", "entry_date"),
        # Bookings of one shift over a period; the shift-production report groups
        # by the code snapshot, which ``ix_production_entries_date`` covers.
        Index("ix_production_entries_shift", "company_id", "shift_id", "entry_date"),
        CheckConstraint("produced_quantity >= 0", name="produced_quantity_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_quantity_non_negative"),
        CheckConstraint("downtime_minutes >= 0", name="downtime_non_negative"),
        # A booking that records nothing at all is a mis-keyed form.
        CheckConstraint(
            "produced_quantity > 0 OR rejected_quantity > 0 OR downtime_minutes > 0",
            name="entry_records_something",
        ),
        # Rejection without a reason is exactly the data nobody can act on.
        CheckConstraint(
            "rejected_quantity = 0 OR rejection_reason IS NOT NULL",
            name="rejections_carry_a_reason",
        ),
    )

    @property
    def posted_output(self) -> bool:
        """Whether this booking put finished goods into the output warehouse."""
        return self.output_transaction_id is not None


__all__ = [
    "DEFAULT_PRODUCTION_PRIORITY",
    "ISSUABLE_PRODUCTION_STATUSES",
    "OPEN_PRODUCTION_STATUSES",
    "OVER_ISSUE_MULTIPLIER",
    "OVER_ISSUE_TOLERANCE",
    "OVER_PRODUCTION_MULTIPLIER",
    "OVER_PRODUCTION_TOLERANCE",
    "POST_APPROVAL_STATUSES",
    "MaterialIssue",
    "MaterialIssueLine",
    "MaterialIssueStatus",
    "MaterialIssueType",
    "ProductionEntry",
    "ProductionOrder",
    "ProductionOrderComponent",
    "ProductionOrderStatus",
    "ProductionSourceType",
    "WorkOrder",
    "WorkOrderStatus",
]
