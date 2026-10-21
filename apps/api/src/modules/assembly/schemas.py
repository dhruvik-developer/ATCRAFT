"""Request and response schemas for assembly orders.

Conventions applied throughout, each for a specific reason:

* **Component lines are never accepted from the client.** They are exploded
  from the approved ``ASSEMBLY`` bill of materials on the server. A payload that
  could name its own components would let stock be consumed against a recipe
  nobody approved (master rules section 19).
* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (section 40).
* **Quantities are ``Decimal`` with explicit precision**, so Pydantic rejects a
  float before it can reach a ``NUMERIC`` column (section 84).
* **Every irreversible action carries a mandatory reason.** Section 83 requires
  one for every stock event, and completion is the largest stock event this
  module performs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.assembly.models import (
    ASSEMBLY_LINE_LENGTH,
    DEFAULT_ASSEMBLY_PRIORITY,
    AssemblyOrderStatus,
    AssemblySourceType,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
AssemblyLine = Annotated[str, Field(min_length=1, max_length=ASSEMBLY_LINE_LENGTH)]
Notes = Annotated[str, Field(max_length=2000)]
#: Lower is more urgent - the production order's bounds, on the allocation scale.
Priority = Annotated[int, Field(ge=0, le=9999)]

#: Upper bound on a single ``ready-for-packing`` handover read. The service-level
#: contract other modules call returns a plain list, so it needs a ceiling of its
#: own - an unbounded result set is forbidden however it is reached (section 31).
READY_FOR_PACKING_MAX = 200


# -----------------------------------------------------------------------------
# Order header
# -----------------------------------------------------------------------------


class AssemblyOrderCreate(BaseModel):
    """Raise a draft assembly order."""

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    #: Omit to use the item's default approved structure, which must be an
    #: ``ASSEMBLY`` or ``PRODUCTION`` one. Naming one explicitly picks between
    #: several approved structures of those two types.
    bom_id: uuid.UUID | None = None
    planned_quantity: PositiveQuantity

    #: Where components are drawn from.
    warehouse_id: uuid.UUID
    #: Where the finished assembly is received. Defaults to ``warehouse_id``.
    output_warehouse_id: uuid.UUID | None = None

    assembly_line: AssemblyLine | None = None
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    #: Carried onto every component reservation at release, with the planned
    #: start as the reservation's required date.
    priority: Priority = DEFAULT_ASSEMBLY_PRIORITY

    #: Whether the finished assembly must be inspected before it becomes
    #: saleable. Completion holds the output rather than releasing it.
    requires_qc: bool = False

    source_type: AssemblySourceType = AssemblySourceType.MANUAL
    source_id: uuid.UUID | None = None

    notes: Notes | None = None

    @model_validator(mode="after")
    def _schedule_is_ordered(self) -> AssemblyOrderCreate:
        if (
            self.planned_start is not None
            and self.planned_end is not None
            and self.planned_end < self.planned_start
        ):
            raise ValueError("The planned end cannot be before the planned start.")
        if (self.source_type is AssemblySourceType.MANUAL) != (self.source_id is None):
            raise ValueError(
                "Name the source document this order covers, or leave the source type MANUAL."
            )
        return self


class AssemblyOrderUpdate(BaseModel):
    """Amend a draft.

    Only a DRAFT is editable: past approval the order has been committed to a
    schedule and, from RELEASED, to stock. ``item_id`` and ``bom_id`` are absent
    on purpose - changing what is being built is a different order, not an edit,
    because the exploded component set would no longer match the header.
    """

    model_config = ConfigDict(extra="forbid")

    planned_quantity: PositiveQuantity | None = None
    warehouse_id: uuid.UUID | None = None
    output_warehouse_id: uuid.UUID | None = None
    assembly_line: AssemblyLine | None = None
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    priority: Priority | None = None
    requires_qc: bool | None = None
    notes: Notes | None = None


class AssemblyComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    assembly_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    uom_id: uuid.UUID
    required_quantity: Decimal
    issued_quantity: Decimal
    consumed_quantity: Decimal
    is_serial_tracked_component: bool
    is_batch_tracked_component: bool = False
    issued_batch_id: uuid.UUID | None = None
    consumed_batch_id: uuid.UUID | None = None
    consumed_serial_ids: list[uuid.UUID]
    reservation_id: uuid.UUID | None
    notes: str | None


class AssemblyOrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    item_id: uuid.UUID
    bom_id: uuid.UUID
    planned_quantity: Decimal
    assembled_quantity: Decimal
    rejected_quantity: Decimal
    warehouse_id: uuid.UUID
    output_warehouse_id: uuid.UUID
    assembly_line: str | None
    planned_start: datetime | None
    planned_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    priority: int
    status: AssemblyOrderStatus
    requires_qc: bool
    qc_held_at: datetime | None
    qc_inspection_id: uuid.UUID | None
    output_batch_id: uuid.UUID | None = None
    output_serial_ids: list[uuid.UUID] | None = None
    packing_order_id: uuid.UUID | None
    packed_at: datetime | None
    source_type: AssemblySourceType
    source_id: uuid.UUID | None
    cancellation_reason: str | None
    notes: str | None
    version: int


class AssemblyOrderDetail(AssemblyOrderSummary):
    """Header plus its component set.

    Not paginated, by deliberate exception to section 31: the component set is
    bounded by ``MAX_COMPONENTS_PER_ORDER`` at write time and a kitting list is
    only meaningful whole.
    """

    components: list[AssemblyComponentRead] = Field(default_factory=list)


class AssemblyOrderFilters(BaseModel):
    search: str | None = None
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    output_warehouse_id: uuid.UUID | None = None
    status: AssemblyOrderStatus | None = None
    assembly_line: str | None = None
    open_only: bool = False
    requires_qc: bool | None = None
    planned_from: datetime | None = None
    planned_to: datetime | None = None


# -----------------------------------------------------------------------------
# Lifecycle payloads
# -----------------------------------------------------------------------------


class ReasonRequest(BaseModel):
    """The payload every state transition shares: why it happened."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ComponentIssueIn(BaseModel):
    """How much of one component has been handed to the line, in total.

    Omitting a line issues its full ``required_quantity``, which is the normal
    case. An explicit quantity exists for issuing short because the rest
    arrives later; a later top-up states the line's new total, which may not be
    below what is already on the bench nor above the reserved requirement.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    issued_quantity: PositiveQuantity
    #: The lot handed over. Required for a batch-tracked component once the
    #: service has the item master; ignored for every other line.
    batch_id: uuid.UUID | None = None


class IssueComponentsRequest(BaseModel):
    """Hand components to the bench - first from RELEASED, or as a top-up.

    The first issue moves the order to COMPONENTS_ISSUED. A top-up while the
    order is COMPONENTS_ISSUED or IN_PROGRESS raises the issued totals and
    leaves the status where it is.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    lines: list[ComponentIssueIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_override_per_line(self) -> IssueComponentsRequest:
        seen = {line.line_number for line in self.lines}
        if len(seen) != len(self.lines):
            raise ValueError("A component line may only be given one issued quantity.")
        return self


class ComponentConsumptionIn(BaseModel):
    """The serialised units one component contributed to this assembly.

    Required for every serial-tracked component and rejected for every other
    one: a serial identifies a single physical unit, so the count has to equal
    the quantity consumed, and a non-serialised part has nothing to name.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    serial_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10_000)
    #: The lot consumed. Defaults to the lot issued to the line; naming a
    #: different one records a correction rather than hiding it.
    batch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _serials_are_distinct(self) -> ComponentConsumptionIn:
        if len(set(self.serial_ids)) != len(self.serial_ids):
            raise ValueError("The same serial cannot be consumed twice by one assembly.")
        return self


class CompleteRequest(BaseModel):
    """Report the output of an assembly run.

    ``assembled_quantity`` and ``rejected_quantity`` both consumed components:
    a lock set that failed final inspection ate its cylinder just as a good one
    did. Their sum is what the consumption is scaled from.
    """

    model_config = ConfigDict(extra="forbid")

    assembled_quantity: NonNegativeQuantity
    rejected_quantity: NonNegativeQuantity = Decimal("0")
    reason: Reason
    #: Business time of the assembly, when it differs from now - a night shift
    #: keyed in the following morning.
    occurred_at: datetime | None = None
    #: The finished lot and units. Required when the output item is tracked.
    output_batch_id: uuid.UUID | None = None
    output_serial_numbers: list[str] = Field(default_factory=list, max_length=10_000)
    components: list[ComponentConsumptionIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _something_was_built(self) -> CompleteRequest:
        if self.assembled_quantity + self.rejected_quantity <= 0:
            raise ValueError(
                "Report the quantity assembled or rejected; a run that produced nothing "
                "is cancelled or closed, not completed."
            )
        seen = {line.line_number for line in self.components}
        if len(seen) != len(self.components):
            raise ValueError("A component line may only be given one serial set.")
        cleaned = ["".join(value.split()).upper() for value in self.output_serial_numbers]
        if any(not value or len(value) > 64 for value in cleaned):
            raise ValueError("Every output serial must be 1-64 characters.")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("An output serial is repeated.")
        if cleaned and Decimal(len(cleaned)) != self.assembled_quantity:
            raise ValueError("Name one output serial per assembled unit.")
        self.output_serial_numbers = cleaned
        return self


class QcResultRequest(BaseModel):
    """The inspection verdict on an assembly's held output.

    The two quantities must account for everything on hold: releasing part of a
    held lot and leaving the rest in limbo is how stock becomes permanently
    unusable without anybody deciding that it should be.

    Only for a hold no quality inspection covers. When the order names an open
    inspection the verdict belongs on that inspection and this is refused; when
    that inspection is already decided, these figures must repeat its own, and
    the order completes from it without moving stock again.
    """

    model_config = ConfigDict(extra="forbid")

    accepted_quantity: NonNegativeQuantity
    rejected_quantity: NonNegativeQuantity = Decimal("0")
    reason: Reason
    #: The ``qc_inspections`` row this verdict came from, when quality raised one.
    qc_inspection_id: uuid.UUID | None = None


class PackingHandoverRequest(BaseModel):
    """Claim a completed assembly for a packing order."""

    model_config = ConfigDict(extra="forbid")

    packing_order_id: uuid.UUID
    reason: Reason


# -----------------------------------------------------------------------------
# Read models
# -----------------------------------------------------------------------------


class CompletionResult(BaseModel):
    """What completion actually posted, so the caller does not have to guess."""

    order: AssemblyOrderDetail
    #: Number of ``ASSEMBLY_CONSUME`` ledger rows written. Higher than the
    #: component count when serialised parts were consumed: a serial identifies
    #: one unit, so each gets its own movement.
    consume_movement_count: int
    output_quantity: Decimal
    #: True when the output went to QC hold instead of general availability.
    output_held_for_qc: bool
    #: True when quality is deployed and raised an inspection for the output.
    qc_inspection_created: bool


class ReadyForPackingRow(BaseModel):
    """A completed assembly whose output is packable.

    ``available_quantity`` is read from ``inventory_balances`` at the output
    position and is never cached (section 34): it is the number that decides
    whether a packing order can actually be filled.
    """

    model_config = ConfigDict(from_attributes=True)

    assembly_order_id: uuid.UUID
    code: str
    item_id: uuid.UUID
    output_warehouse_id: uuid.UUID
    assembled_quantity: Decimal
    available_quantity: Decimal
    completed_at: datetime | None
    requires_qc: bool
    #: The demand the assembly was raised for. A ``SALES_ORDER`` source names the
    #: sales order whose customer a packing order raised from this row is for.
    source_type: AssemblySourceType
    source_id: uuid.UUID | None


class AssemblyComponentAvailability(BaseModel):
    """One component's stock position, read live for a release decision.

    ``available_quantity`` is promisable stock at the position a component
    reservation holds - the order's component warehouse, no bin, no batch - and
    is never cached (section 34). ``shortage_quantity`` is what releasing would
    still lack: zero once the line is reserved.
    """

    line_number: int
    item_id: uuid.UUID
    required_quantity: Decimal
    issued_quantity: Decimal
    is_reserved: bool
    available_quantity: Decimal
    shortage_quantity: Decimal


class AssemblyBoardOrder(BaseModel):
    """One open assembly order on the board: plan against reality.

    ``is_late`` means the planned end has passed while the order still owes
    finished stock; ``start_overdue`` means the planned start has passed and the
    bench has not begun. Both are worked out on the server at read time.
    """

    id: uuid.UUID
    code: str
    item_id: uuid.UUID
    status: AssemblyOrderStatus
    assembly_line: str | None
    warehouse_id: uuid.UUID
    output_warehouse_id: uuid.UUID
    priority: int
    planned_quantity: Decimal
    assembled_quantity: Decimal
    rejected_quantity: Decimal
    planned_start: datetime | None
    planned_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    requires_qc: bool
    is_late: bool
    start_overdue: bool
    #: Whole days past the planned end; zero when not late.
    days_late: int


class AssemblyLineLoad(BaseModel):
    """What one assembly line (or bench) has open, aggregated in the database.

    ``assembly_line`` is ``None`` for open orders nobody has put on a line yet.
    """

    assembly_line: str | None
    open_orders: int
    not_started_orders: int
    in_progress_orders: int
    qc_pending_orders: int
    late_orders: int
    planned_quantity: Decimal
    earliest_planned_start: datetime | None
    latest_planned_end: datetime | None


class AssemblyBoardFilters(BaseModel):
    """Board filters. ``unassigned_line`` and ``assembly_line`` exclude each other."""

    model_config = ConfigDict(extra="forbid")

    assembly_line: str | None = None
    #: Only open orders that are on no line.
    unassigned_line: bool = False
    warehouse_id: uuid.UUID | None = None
    #: Only orders whose planned end has passed.
    late_only: bool = False

    @model_validator(mode="after")
    def _one_line_selector(self) -> AssemblyBoardFilters:
        if self.unassigned_line and self.assembly_line is not None:
            raise ValueError("Ask for one assembly line or for the unassigned orders, not both.")
        return self


__all__ = [
    "READY_FOR_PACKING_MAX",
    "AssemblyBoardFilters",
    "AssemblyBoardOrder",
    "AssemblyComponentAvailability",
    "AssemblyComponentRead",
    "AssemblyLineLoad",
    "AssemblyOrderCreate",
    "AssemblyOrderDetail",
    "AssemblyOrderFilters",
    "AssemblyOrderSummary",
    "AssemblyOrderUpdate",
    "CompleteRequest",
    "CompletionResult",
    "ComponentConsumptionIn",
    "ComponentIssueIn",
    "IssueComponentsRequest",
    "PackingHandoverRequest",
    "QcResultRequest",
    "ReadyForPackingRow",
    "ReasonRequest",
]
