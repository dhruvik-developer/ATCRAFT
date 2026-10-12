"""Request and response schemas for production.

Conventions applied throughout, each for a specific reason:

* **Quantities and money are ``Decimal`` with explicit precision.** Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (master rules
  section 84).
* **``company_id`` never appears on a payload.** It comes from the authenticated
  principal (section 40).
* **Components are addressed by their snapshot row id, never by item.** An
  issue line names the requirement it is satisfying, so the issue variance is
  computable and an item that is not on the released structure cannot be pushed
  into the job by accident.
* **Every list has a filter model.** Filters are parsed once, in the router, and
  handed to the service as a typed object rather than as a bag of keywords.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.production.models import (
    DEFAULT_PRODUCTION_PRIORITY,
    MaterialIssueStatus,
    MaterialIssueType,
    ProductionOrderStatus,
    ProductionSourceType,
    WorkOrderStatus,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Priority = Annotated[int, Field(ge=0, le=9999)]
ReasonField = Annotated[str, Field(min_length=3, max_length=255)]

#: A single issue document with more lines than this is a data-entry accident,
#: not a kit. The bound matters because posting locks every affected balance row
#: inside one transaction, and an unbounded document would hold those locks for
#: an unbounded time.
MAX_ISSUE_LINES = 200

#: Upper bound on a single production booking, so a mis-keyed 100000 cannot
#: reach the costing arithmetic or the output receipt.
MAX_BOOKING_QUANTITY = Decimal("100000000")


# -----------------------------------------------------------------------------
# Production order
# -----------------------------------------------------------------------------


class ProductionOrderCreate(BaseModel):
    """Raise a DRAFT order. Nothing is planned against stock yet."""

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    planned_quantity: PositiveQuantity
    warehouse_id: uuid.UUID
    plant_id: uuid.UUID | None = None
    #: The cost centre the job is charged to. Optional; NULL is "unallocated".
    cost_centre_id: uuid.UUID | None = None
    #: Left unset to take the item's default approved structure at release.
    bom_id: uuid.UUID | None = None
    routing_id: uuid.UUID | None = None
    planned_start_date: date
    planned_end_date: date
    priority: Priority = DEFAULT_PRODUCTION_PRIORITY
    source_type: ProductionSourceType = ProductionSourceType.MANUAL
    source_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _dates_ordered(self) -> ProductionOrderCreate:
        if self.planned_end_date < self.planned_start_date:
            raise ValueError("planned_end_date cannot be before planned_start_date")
        return self


class ProductionOrderUpdate(BaseModel):
    """Partial update of a draft. Only the keys actually sent are applied."""

    model_config = ConfigDict(extra="forbid")

    planned_quantity: PositiveQuantity | None = None
    warehouse_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    #: An explicit null clears it.
    cost_centre_id: uuid.UUID | None = None
    bom_id: uuid.UUID | None = None
    routing_id: uuid.UUID | None = None
    planned_start_date: date | None = None
    planned_end_date: date | None = None
    priority: Priority | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ProductionOrderFilters(BaseModel):
    """List filters. ``open_only`` is the shop floor's default view."""

    model_config = ConfigDict(extra="forbid")

    status: ProductionOrderStatus | None = None
    open_only: bool = False
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    source_type: ProductionSourceType | None = None
    source_id: uuid.UUID | None = None
    planned_from: date | None = None
    planned_to: date | None = None
    #: Only orders at least this urgent (priority is ascending urgency).
    max_priority: Priority | None = None
    search: str | None = Field(default=None, max_length=64)


class ProductionOrderSummary(BaseModel):
    """List row. Deliberately narrower than the detail."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: ProductionOrderStatus
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    plant_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    planned_quantity: Decimal
    produced_quantity: Decimal
    rejected_quantity: Decimal
    scrap_quantity: Decimal
    planned_start_date: date
    planned_end_date: date
    actual_start_date: date | None
    actual_end_date: date | None
    priority: int
    source_type: ProductionSourceType
    source_id: uuid.UUID | None
    created_at: datetime


class ProductionOrderRead(ProductionOrderSummary):
    bom_id: uuid.UUID | None
    routing_id: uuid.UUID | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    closure_note: str | None
    total_material_cost: Decimal
    total_operation_cost: Decimal
    total_cost: Decimal
    notes: str | None
    version: int
    updated_at: datetime


class WorkOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    production_order_id: uuid.UUID
    sequence: int
    operation_code: str
    operation_name: str
    work_center_id: uuid.UUID
    machine_id: uuid.UUID | None
    operator_user_id: uuid.UUID | None
    #: The shift's code as it was when assigned - or legacy typed text.
    shift: str | None
    shift_id: uuid.UUID | None
    planned_start_at: datetime | None
    planned_end_at: datetime | None
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    planned_quantity: Decimal
    completed_quantity: Decimal
    rejected_quantity: Decimal
    planned_setup_minutes: Decimal
    planned_run_minutes: Decimal
    process_cost: Decimal
    #: The rates the job is costed at, frozen when it was released.
    work_center_cost_per_hour: Decimal
    work_center_setup_cost: Decimal
    status: WorkOrderStatus
    notes: str | None


class ProductionOrderDetail(ProductionOrderRead):
    """The order with its operations.

    Work orders are included inline rather than paginated because a routing is
    bounded at write time (``MAX_OPERATIONS_PER_ROUTING`` in the routing module)
    and a traveller is only meaningful whole. Components are *not* included:
    a multi-level explosion is not bounded that way, so they have their own
    paginated endpoint.
    """

    work_orders: list[WorkOrderRead] = Field(default_factory=list)
    component_count: int = 0


# -----------------------------------------------------------------------------
# Lifecycle payloads
# -----------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


class ReleaseRequest(BaseModel):
    """Explode the structure, snapshot it, and hold the material."""

    model_config = ConfigDict(extra="forbid")

    #: Store every component is drawn from. Falls back to the item's default
    #: warehouse, then to the order's output warehouse.
    component_warehouse_id: uuid.UUID | None = None
    #: Components issued automatically at completion instead of being picked.
    #: Named per item because backflushing is a policy about the part, not about
    #: this order.
    backflush_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    #: Structures and routings are effective-dated; release resolves them as of
    #: this date. Defaults to the order's planned start.
    as_of: date | None = None
    #: Set false to release without holding stock - used when the material is
    #: already physically staged at the line.
    reserve_material: bool = True
    note: str | None = Field(default=None, max_length=1000)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_start_date: date | None = None
    note: str | None = Field(default=None, max_length=1000)


class CompleteRequest(BaseModel):
    """Finish the job, optionally booking the last output in the same call.

    When ``produced_quantity`` is given, completion books a final production
    entry through the ordinary entry path - which is the only place in this
    module that posts ``PRODUCTION_RECEIPT``. Leaving it unset completes an order
    whose output was already booked shift by shift, and posts nothing.
    """

    model_config = ConfigDict(extra="forbid")

    produced_quantity: Annotated[
        Decimal, Field(ge=0, le=MAX_BOOKING_QUANTITY, max_digits=18, decimal_places=4)
    ] = Decimal("0")
    rejected_quantity: Annotated[
        Decimal, Field(ge=0, le=MAX_BOOKING_QUANTITY, max_digits=18, decimal_places=4)
    ] = Decimal("0")
    rejection_reason: str | None = Field(default=None, max_length=255)
    #: Planned output written off as unrecoverable. Records the variance; it
    #: never posts a movement, because rejected output is never received.
    scrap_quantity: NonNegativeQuantity = Decimal("0")
    entry_date: date | None = None
    #: The shift of the final booking, when one is made; see ``ProductionEntryCreate``.
    shift_id: uuid.UUID | None = None
    work_order_id: uuid.UUID | None = None
    operator_user_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    actual_end_date: date | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _rejection_has_a_reason(self) -> CompleteRequest:
        if self.rejected_quantity > 0 and not self.rejection_reason:
            raise ValueError("rejection_reason is required when rejecting output")
        return self


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closure_note: str | None = Field(default=None, max_length=4000)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReasonField


# -----------------------------------------------------------------------------
# Components and shortage
# -----------------------------------------------------------------------------


class ComponentRead(BaseModel):
    """One snapshotted requirement with its live coverage.

    ``reserved_quantity`` and ``available_quantity`` are read from allocation and
    inventory at request time rather than stored: a stored copy would be stale
    the moment anybody else moved the part, and availability is never cached
    (master rules section 34).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    production_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str
    uom_id: uuid.UUID
    bom_level: int
    warehouse_id: uuid.UUID
    is_backflushed: bool
    required_quantity: Decimal
    issued_quantity: Decimal
    returned_quantity: Decimal
    consumed_quantity: Decimal
    #: Issued minus returned: what the job is actually holding.
    net_issued_quantity: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    available_quantity: Decimal = Decimal("0")
    #: What still has to be found: required, less what is issued, less what is
    #: already held for this order.
    shortage_quantity: Decimal = Decimal("0")


class ComponentShortage(BaseModel):
    """One component the release could not fully cover.

    Reported rather than raised: a planner needs the whole list of what is
    missing, not the first thing that failed.
    """

    model_config = ConfigDict(from_attributes=True)

    component_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    warehouse_id: uuid.UUID
    required_quantity: Decimal
    reserved_quantity: Decimal
    shortage_quantity: Decimal
    detail: str | None = None


class ReleaseResult(BaseModel):
    order: ProductionOrderRead
    bom_id: uuid.UUID
    bom_code: str
    routing_id: uuid.UUID | None
    component_count: int
    work_order_count: int
    fully_reserved: bool
    shortages: list[ComponentShortage] = Field(default_factory=list)
    message: str


# -----------------------------------------------------------------------------
# Material issue / return
# -----------------------------------------------------------------------------


class MaterialIssueLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=MAX_ISSUE_LINES)]
    component_id: uuid.UUID
    quantity: PositiveQuantity
    #: Overrides the document's store for this line - a kit routinely draws from
    #: more than one.
    warehouse_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class MaterialIssueCreate(BaseModel):
    """A draft issue or return. Nothing moves until it is posted."""

    model_config = ConfigDict(extra="forbid")

    issue_date: date | None = None
    warehouse_id: uuid.UUID | None = None
    reason: ReasonField
    notes: str | None = Field(default=None, max_length=4000)
    lines: Annotated[list[MaterialIssueLineIn], Field(min_length=1, max_length=MAX_ISSUE_LINES)]

    @model_validator(mode="after")
    def _lines_are_distinct(self) -> MaterialIssueCreate:
        numbers = [line.line_number for line in self.lines]
        if len(set(numbers)) != len(numbers):
            raise ValueError("line_number must be unique within the document")
        components = [line.component_id for line in self.lines]
        if len(set(components)) != len(components):
            # Two lines for one component would need two balance locks on the
            # same position and make the issue variance ambiguous.
            raise ValueError("each component may appear on only one line")
        return self


class MaterialIssuePostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Advance the order to MATERIAL_ISSUED even though mandatory components are
    #: still short. Requires the production approval right; the shortfall is
    #: written to the audit entry.
    allow_partial_issue: bool = False
    override_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _override_is_explained(self) -> MaterialIssuePostRequest:
        if self.allow_partial_issue and not self.override_reason:
            raise ValueError("override_reason is required to force a partial issue")
        return self


class MaterialIssueLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    component_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    location_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    notes: str | None


class MaterialIssueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    production_order_id: uuid.UUID
    issue_type: MaterialIssueType
    status: MaterialIssueStatus
    issue_date: date
    warehouse_id: uuid.UUID
    posted_at: datetime | None
    posted_by_id: uuid.UUID | None
    reason: str
    notes: str | None
    line_count: int
    total_quantity: Decimal
    total_cost: Decimal
    created_at: datetime


class MaterialIssueDetail(MaterialIssueRead):
    lines: list[MaterialIssueLineRead] = Field(default_factory=list)


class PostedMovement(BaseModel):
    """What one posting actually wrote to the ledger."""

    transaction_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    transaction_type: str


class MaterialIssuePostResult(BaseModel):
    issue: MaterialIssueDetail
    order_status: ProductionOrderStatus
    movements: list[PostedMovement] = Field(default_factory=list)
    #: Components still short of their requirement after this posting.
    outstanding_components: list[ComponentShortage] = Field(default_factory=list)
    message: str


class CompleteResult(BaseModel):
    """What completion actually did."""

    order: ProductionOrderRead
    #: The receipt booked by the final entry, when completion booked one.
    output_movement: PostedMovement | None = None
    #: The system-raised issue that backflushed the automatic components.
    backflush_issue_code: str | None = None
    backflush_movements: list[PostedMovement] = Field(default_factory=list)
    consumed_component_count: int = 0
    message: str


class MaterialIssueFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_order_id: uuid.UUID | None = None
    issue_type: MaterialIssueType | None = None
    status: MaterialIssueStatus | None = None
    warehouse_id: uuid.UUID | None = None
    issued_from: date | None = None
    issued_to: date | None = None
    search: str | None = Field(default=None, max_length=64)


# -----------------------------------------------------------------------------
# Shop-floor bookings
# -----------------------------------------------------------------------------


class ProductionEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_order_id: uuid.UUID | None = None
    entry_date: date | None = None
    #: The shift worked, from the master; its code is snapshotted onto the entry.
    shift_id: uuid.UUID | None = None
    operator_user_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    produced_quantity: Annotated[
        Decimal, Field(ge=0, le=MAX_BOOKING_QUANTITY, max_digits=18, decimal_places=4)
    ] = Decimal("0")
    rejected_quantity: Annotated[
        Decimal, Field(ge=0, le=MAX_BOOKING_QUANTITY, max_digits=18, decimal_places=4)
    ] = Decimal("0")
    rejection_reason: str | None = Field(default=None, max_length=255)
    downtime_minutes: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)] = Decimal(
        "0"
    )
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _entry_records_something(self) -> ProductionEntryCreate:
        recorded = self.produced_quantity + self.rejected_quantity + self.downtime_minutes
        if recorded <= 0:
            raise ValueError(
                "a booking must record produced quantity, rejected quantity or downtime"
            )
        if self.rejected_quantity > 0 and not self.rejection_reason:
            raise ValueError("rejection_reason is required when rejecting output")
        return self


class ProductionEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    production_order_id: uuid.UUID
    work_order_id: uuid.UUID | None
    entry_date: date
    shift: str | None
    shift_id: uuid.UUID | None
    operator_user_id: uuid.UUID | None
    machine_id: uuid.UUID | None
    produced_quantity: Decimal
    rejected_quantity: Decimal
    rejection_reason: str | None
    downtime_minutes: Decimal
    notes: str | None
    output_transaction_id: uuid.UUID | None
    created_at: datetime


class ProductionEntryResult(BaseModel):
    entry: ProductionEntryRead
    order: ProductionOrderRead
    #: The output receipt this booking posted, when it booked final output.
    output_movement: PostedMovement | None = None
    message: str


class ProductionEntryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_order_id: uuid.UUID | None = None
    work_order_id: uuid.UUID | None = None
    operator_user_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    shift: str | None = Field(default=None, max_length=16)
    shift_id: uuid.UUID | None = None
    entry_from: date | None = None
    entry_to: date | None = None
    output_only: bool = False


# -----------------------------------------------------------------------------
# Work orders
# -----------------------------------------------------------------------------


class WorkOrderUpdate(BaseModel):
    """Assignment and scheduling. Quantities move through bookings, never here."""

    model_config = ConfigDict(extra="forbid")

    operator_user_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    #: A shift from the master, active when chosen; null clears it and its code
    #: snapshot together. Replaces the free-text ``shift`` this form used to take.
    shift_id: uuid.UUID | None = None
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)


class WorkOrderTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WorkOrderStatus
    #: Recorded moment the operation started or finished. Defaults to now.
    occurred_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=255)


class WorkOrderFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_order_id: uuid.UUID | None = None
    status: WorkOrderStatus | None = None
    work_center_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    operator_user_id: uuid.UUID | None = None
    shift_id: uuid.UUID | None = None
    #: Exact match on the code snapshot - also finds rows typed before the shift master.
    shift: str | None = Field(default=None, max_length=16)
    open_only: bool = False


# -----------------------------------------------------------------------------
# Work in progress
# -----------------------------------------------------------------------------


class WipOrderRow(BaseModel):
    """One live job, as the planner's board shows it."""

    model_config = ConfigDict(from_attributes=True)

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
    balance_quantity: Decimal
    planned_start_date: date
    planned_end_date: date
    actual_start_date: date | None
    #: Positive when the planned finish is already in the past.
    days_late: int
    open_work_orders: int
    total_work_orders: int


class WipWorkCentreRow(BaseModel):
    """Load on one work centre, from the work orders currently on it."""

    work_center_id: uuid.UUID
    pending_work_orders: int
    in_progress_work_orders: int
    planned_quantity: Decimal
    completed_quantity: Decimal
    balance_quantity: Decimal
    #: Chargeable minutes still to run, at the planned rate.
    remaining_minutes: Decimal


class WipFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    work_center_id: uuid.UUID | None = None
    late_only: bool = False


# -----------------------------------------------------------------------------
# Costing
# -----------------------------------------------------------------------------


class ProductionCostMaterialLine(BaseModel):
    component_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    required_quantity: Decimal
    issued_quantity: Decimal
    returned_quantity: Decimal
    net_issued_quantity: Decimal
    #: Weighted average of what the movements were actually valued at.
    unit_cost: Decimal
    total_cost: Decimal


class ProductionCostOperationLine(BaseModel):
    work_order_id: uuid.UUID
    sequence: int
    operation_code: str
    work_center_id: uuid.UUID
    status: WorkOrderStatus
    #: Charged minutes: elapsed actual time where it was clocked, planned
    #: setup + run otherwise.
    charged_minutes: Decimal
    is_actual_time: bool
    cost_per_hour: Decimal
    time_cost: Decimal
    setup_cost: Decimal
    process_cost: Decimal
    total_cost: Decimal


class ProductionCostRead(BaseModel):
    """Actual cost of one job.

    Recomputed live for an open order and read back from the stored columns
    once the order is closed - a closed job's cost must not move when a
    work-centre rate is revised.
    """

    production_order_id: uuid.UUID
    code: str
    status: ProductionOrderStatus
    is_final: bool
    planned_quantity: Decimal
    produced_quantity: Decimal
    material_cost: Decimal
    operation_cost: Decimal
    total_cost: Decimal
    #: ``None`` when nothing has been produced yet: dividing by zero output
    #: would report an infinite unit cost rather than "not known yet".
    cost_per_unit: Decimal | None
    materials: list[ProductionCostMaterialLine] = Field(default_factory=list)
    operations: list[ProductionCostOperationLine] = Field(default_factory=list)
    #: Work orders costed from planned time because nobody clocked them.
    uncosted_operation_count: int = 0


class CloseResult(BaseModel):
    """Closure, with the costing it computed and froze."""

    order: ProductionOrderRead
    cost: ProductionCostRead
    released_reservation_count: int
    message: str


__all__ = [
    "MAX_BOOKING_QUANTITY",
    "MAX_ISSUE_LINES",
    "ApproveRequest",
    "CancelRequest",
    "CloseRequest",
    "CloseResult",
    "CompleteRequest",
    "CompleteResult",
    "ComponentRead",
    "ComponentShortage",
    "MaterialIssueCreate",
    "MaterialIssueDetail",
    "MaterialIssueFilters",
    "MaterialIssueLineIn",
    "MaterialIssueLineRead",
    "MaterialIssuePostRequest",
    "MaterialIssuePostResult",
    "MaterialIssueRead",
    "PostedMovement",
    "ProductionCostMaterialLine",
    "ProductionCostOperationLine",
    "ProductionCostRead",
    "ProductionEntryCreate",
    "ProductionEntryFilters",
    "ProductionEntryRead",
    "ProductionEntryResult",
    "ProductionOrderCreate",
    "ProductionOrderDetail",
    "ProductionOrderFilters",
    "ProductionOrderRead",
    "ProductionOrderSummary",
    "ProductionOrderUpdate",
    "ReleaseRequest",
    "ReleaseResult",
    "StartRequest",
    "WipFilters",
    "WipOrderRow",
    "WipWorkCentreRow",
    "WorkOrderFilters",
    "WorkOrderRead",
    "WorkOrderTransitionRequest",
    "WorkOrderUpdate",
]
