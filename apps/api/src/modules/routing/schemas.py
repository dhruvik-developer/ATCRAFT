"""Request and response schemas for routings, work centres and machines.

Conventions applied throughout, each for a specific reason:

* **Codes are normalised on the way in.** ``rm 01``, ``RM-01`` and ``rm01`` must
  not become three work centres, so every code field runs through
  :func:`src.common.codes.normalise_code` before the uniqueness check sees it.
* **Times and money are ``Decimal`` with explicit precision.** Pydantic rejects
  a float before it can reach a ``NUMERIC`` column (master rules section 84).
* **Operations are addressed by ``sequence``, not by id.** On a create the steps
  have no primary keys yet, and the traveller printed on the shop floor names
  the sequence anyway.
* **``company_id`` never appears on a payload.** It comes from the authenticated
  principal (section 40).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common.codes import normalise_code
from src.modules.routing.models import (
    DEFAULT_WORKING_HOURS_PER_DAY,
    MAX_WORKING_HOURS_PER_DAY,
    MachineStatus,
    RoutingStatus,
)

# --- reusable constrained scalars --------------------------------------------

NonNegativeMinutes = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
WorkingHours = Annotated[
    Decimal, Field(gt=0, le=MAX_WORKING_HOURS_PER_DAY, max_digits=18, decimal_places=4)
]

CodeField = Annotated[str, Field(min_length=1, max_length=32)]
RoutingCodeField = Annotated[str, Field(min_length=1, max_length=64)]
NameField = Annotated[str, Field(min_length=1, max_length=255)]
ReasonField = Annotated[str, Field(min_length=3, max_length=255)]

#: A routing with more steps than this is a data-entry accident, not a process.
#: The bound matters because create and update both replace the whole operation
#: set inside one transaction.
MAX_OPERATIONS_PER_ROUTING = 200

#: Upper bound on the quantity a costing or lead-time query may be run for.
#: Without it a caller could ask for 10^30 units and make the service multiply
#: arbitrarily large decimals on a request path.
MAX_COSTING_QUANTITY = Decimal("100000000")


def _normalise(value: str | None) -> str | None:
    """Shared code normaliser.

    Tolerates ``None`` so the same function can back the optional ``code`` on a
    partial update: an explicit ``"code": null`` must fail validation as a
    missing value, not crash the validator.
    """
    return normalise_code(value) if value is not None else None


# -----------------------------------------------------------------------------
# Work centres
# -----------------------------------------------------------------------------


class WorkCenterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: CodeField
    name: NameField
    description: str | None = Field(default=None, max_length=2000)
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    #: The cost pool this centre is charged to. Validated in tenant and active.
    cost_centre_id: uuid.UUID | None = None
    capacity_per_hour: NonNegativeQuantity = Decimal("0")
    cost_per_hour: NonNegativeMoney = Decimal("0")
    setup_cost: NonNegativeMoney = Decimal("0")
    working_hours_per_day: WorkingHours = DEFAULT_WORKING_HOURS_PER_DAY
    is_active: bool = True

    _normalise_code = field_validator("code")(_normalise)


class WorkCenterUpdate(BaseModel):
    """Partial update. Only the keys actually sent are applied."""

    model_config = ConfigDict(extra="forbid")

    code: CodeField | None = None
    name: NameField | None = None
    description: str | None = Field(default=None, max_length=2000)
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    #: An explicit null clears it.
    cost_centre_id: uuid.UUID | None = None
    capacity_per_hour: NonNegativeQuantity | None = None
    cost_per_hour: NonNegativeMoney | None = None
    setup_cost: NonNegativeMoney | None = None
    working_hours_per_day: WorkingHours | None = None

    _normalise_code = field_validator("code")(_normalise)


class WorkCenterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    plant_id: uuid.UUID | None
    department_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    capacity_per_hour: Decimal
    cost_per_hour: Decimal
    setup_cost: Decimal
    working_hours_per_day: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WorkCenterFilters(BaseModel):
    search: str | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    is_active: bool | None = None
    include_deleted: bool = False


# -----------------------------------------------------------------------------
# Machines
# -----------------------------------------------------------------------------


class MachineCreate(BaseModel):
    """Payload for a new machine.

    Neither ``status`` nor ``is_active`` is accepted. A new machine starts
    ``AVAILABLE`` and active; availability moves through the machine state
    machine afterwards, and ``is_active`` is cleared only by retiring it. Letting
    a create set either one would make it possible to file a machine that is
    simultaneously available and inactive.
    """

    model_config = ConfigDict(extra="forbid")

    code: CodeField
    name: NameField
    work_center_id: uuid.UUID
    machine_type: str | None = Field(default=None, max_length=64)
    capacity_per_hour: NonNegativeQuantity = Decimal("0")
    cost_per_hour: NonNegativeMoney = Decimal("0")
    last_maintenance_date: date | None = None
    next_maintenance_date: date | None = None

    _normalise_code = field_validator("code")(_normalise)

    @model_validator(mode="after")
    def _maintenance_dates_ordered(self) -> MachineCreate:
        if (
            self.last_maintenance_date is not None
            and self.next_maintenance_date is not None
            and self.next_maintenance_date < self.last_maintenance_date
        ):
            raise ValueError("The next service cannot fall before the last one.")
        return self


class MachineUpdate(BaseModel):
    """Partial update.

    ``status`` is absent by design: availability moves through the machine state
    machine on its own endpoint, so every change carries a reason and an audit
    row rather than arriving as one field of a bulk edit.
    """

    model_config = ConfigDict(extra="forbid")

    code: CodeField | None = None
    name: NameField | None = None
    work_center_id: uuid.UUID | None = None
    machine_type: str | None = Field(default=None, max_length=64)
    capacity_per_hour: NonNegativeQuantity | None = None
    cost_per_hour: NonNegativeMoney | None = None
    last_maintenance_date: date | None = None
    next_maintenance_date: date | None = None

    _normalise_code = field_validator("code")(_normalise)


class MachineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    work_center_id: uuid.UUID
    machine_type: str | None
    capacity_per_hour: Decimal
    cost_per_hour: Decimal
    status: MachineStatus
    last_maintenance_date: date | None
    next_maintenance_date: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MachineFilters(BaseModel):
    search: str | None = None
    work_center_id: uuid.UUID | None = None
    status: MachineStatus | None = None
    is_active: bool | None = None
    #: "What is due for service before this date?" - the maintenance board.
    maintenance_due_before: date | None = None
    include_deleted: bool = False


class MachineStatusChange(BaseModel):
    """Move a machine to another availability state."""

    model_config = ConfigDict(extra="forbid")

    status: MachineStatus
    reason: ReasonField
    #: Optional on any transition, but the reason the payload exists: completing
    #: maintenance is the moment the next service date is known.
    next_maintenance_date: date | None = None


# -----------------------------------------------------------------------------
# Routing operations
# -----------------------------------------------------------------------------


class RoutingOperationIn(BaseModel):
    """One step on a create or update payload."""

    model_config = ConfigDict(extra="forbid")

    sequence: Annotated[int, Field(ge=1, le=100_000)]
    operation_code: CodeField
    operation_name: NameField
    work_center_id: uuid.UUID
    #: Must belong to ``work_center_id``; the service checks it and a composite
    #: foreign key enforces it.
    machine_id: uuid.UUID | None = None
    setup_time_minutes: NonNegativeMinutes = Decimal("0")
    run_time_per_unit_minutes: NonNegativeMinutes = Decimal("0")
    queue_time_minutes: NonNegativeMinutes = Decimal("0")
    move_time_minutes: NonNegativeMinutes = Decimal("0")
    process_cost: NonNegativeMoney = Decimal("0")
    is_outsourced: bool = False
    job_work_vendor_id: uuid.UUID | None = None
    qc_required: bool = False
    notes: str | None = Field(default=None, max_length=2000)

    _normalise_code = field_validator("operation_code")(_normalise)

    @model_validator(mode="after")
    def _vendor_implies_outsourced(self) -> RoutingOperationIn:
        # Mirrors the CHECK constraint so the client gets a message naming the
        # step instead of an integrity error translated into a generic 409.
        if self.job_work_vendor_id is not None and not self.is_outsourced:
            raise ValueError(
                "A job-work vendor can only be named on an operation marked outsourced."
            )
        return self

    @model_validator(mode="after")
    def _outsourced_steps_have_no_machine(self) -> RoutingOperationIn:
        # An outsourced step runs on the vendor's equipment. Naming one of our
        # machines on it would put phantom load on a work centre that is idle.
        if self.is_outsourced and self.machine_id is not None:
            raise ValueError("An outsourced operation cannot be assigned to an in-house machine.")
        return self


class RoutingOperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    routing_id: uuid.UUID
    sequence: int
    operation_code: str
    operation_name: str
    work_center_id: uuid.UUID
    machine_id: uuid.UUID | None
    setup_time_minutes: Decimal
    run_time_per_unit_minutes: Decimal
    queue_time_minutes: Decimal
    move_time_minutes: Decimal
    process_cost: Decimal
    is_outsourced: bool
    job_work_vendor_id: uuid.UUID | None
    qc_required: bool
    notes: str | None


#: A routing with no steps is not a process. Declared here rather than inline so
#: create and update cannot drift apart on the bound.
OperationSet = Annotated[
    list[RoutingOperationIn], Field(min_length=1, max_length=MAX_OPERATIONS_PER_ROUTING)
]


def _validate_operation_set(operations: list[RoutingOperationIn]) -> list[RoutingOperationIn]:
    """Reject duplicate sequences and duplicate operation codes.

    Checked here as well as in the database so the response names the offending
    step rather than surfacing a constraint name.
    """
    sequences = [operation.sequence for operation in operations]
    duplicates = sorted({s for s in sequences if sequences.count(s) > 1})
    if duplicates:
        raise ValueError(f"Duplicate operation sequences: {duplicates}")

    codes = [operation.operation_code for operation in operations]
    repeated = sorted({c for c in codes if codes.count(c) > 1})
    if repeated:
        raise ValueError(f"Duplicate operation codes: {repeated}")
    return operations


# -----------------------------------------------------------------------------
# Routings
# -----------------------------------------------------------------------------


class RoutingCreate(BaseModel):
    """Payload for a new DRAFT routing."""

    model_config = ConfigDict(extra="forbid")

    code: RoutingCodeField
    name: str | None = Field(default=None, max_length=255)
    item_id: uuid.UUID
    is_default: bool = False
    #: Defaults to today. Backdating is allowed - a process is often recorded
    #: after it has been running for a while.
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    operations: OperationSet

    _normalise_code = field_validator("code")(_normalise)

    @field_validator("operations")
    @classmethod
    def _check_operations(cls, value: list[RoutingOperationIn]) -> list[RoutingOperationIn]:
        return _validate_operation_set(value)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> RoutingCreate:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("The effectivity window would end before it starts.")
        return self


class RoutingUpdate(BaseModel):
    """Partial update of a DRAFT routing.

    ``operations`` replaces the whole set when present and is left untouched
    when absent - a partial merge would need stable client-side ids for rows the
    client never saw.
    """

    model_config = ConfigDict(extra="forbid")

    code: RoutingCodeField | None = None
    name: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    operations: OperationSet | None = None

    _normalise_code = field_validator("code")(_normalise)

    @field_validator("operations")
    @classmethod
    def _check_operations(
        cls, value: list[RoutingOperationIn] | None
    ) -> list[RoutingOperationIn] | None:
        return _validate_operation_set(value) if value is not None else None


class RoutingSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str | None
    item_id: uuid.UUID
    version: int
    status: RoutingStatus
    is_default: bool
    effective_from: date
    effective_to: date | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RoutingDetail(RoutingSummary):
    notes: str | None
    operations: list[RoutingOperationRead]


class RoutingFilters(BaseModel):
    search: str | None = None
    item_id: uuid.UUID | None = None
    status: RoutingStatus | None = None
    is_default: bool | None = None
    work_center_id: uuid.UUID | None = None
    effective_on: date | None = None


class ReasonRequest(BaseModel):
    """A state change that carries nothing but its justification.

    The reason is mandatory rather than optional: master rules section 83 wants
    every status change explicable from the audit log alone, and "why did this
    cell go inactive in March?" is exactly the question the log has to answer.
    """

    model_config = ConfigDict(extra="forbid")

    reason: ReasonField


class RoutingStatusChange(ReasonRequest):
    """Approve, obsolete: the routing transitions."""


class CopyAsNewVersionRequest(BaseModel):
    """Create the next DRAFT version from an existing routing."""

    model_config = ConfigDict(extra="forbid")

    code: RoutingCodeField
    name: str | None = Field(default=None, max_length=255)
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    _normalise_code = field_validator("code")(_normalise)


# -----------------------------------------------------------------------------
# Costing and lead time
# -----------------------------------------------------------------------------


class OperationCostRead(BaseModel):
    """One step's contribution to the routing cost, at a stated quantity."""

    sequence: int
    operation_code: str
    operation_name: str
    work_center_id: uuid.UUID
    #: setup + run x quantity + queue + move.
    total_minutes: Decimal
    #: The part of ``total_minutes`` the work centre is actually charged for.
    #: Queue and move are elapsed time at no cost, so this is smaller.
    charged_minutes: Decimal
    work_center_cost_per_hour: Decimal
    time_cost: Decimal
    process_cost: Decimal
    total_cost: Decimal
    is_outsourced: bool


class RoutingCostRead(BaseModel):
    """Costed routing for a quantity, with the lead time it implies."""

    routing_id: uuid.UUID
    routing_code: str
    item_id: uuid.UUID
    quantity: Decimal
    total_minutes: Decimal
    charged_minutes: Decimal
    time_cost: Decimal
    process_cost: Decimal
    total_cost: Decimal
    #: Cost of one unit at this quantity. Falls as the quantity rises, because
    #: setup is amortised over the run.
    cost_per_unit: Decimal
    working_hours_per_day: Decimal
    lead_time_days: int
    operations: list[OperationCostRead]


class LeadTimeRead(BaseModel):
    """What MRP asks for: how long making this quantity takes, in whole days."""

    item_id: uuid.UUID
    quantity: Decimal
    #: ``None`` when the item has no effective approved routing - the caller
    #: falls back to ``items.lead_time_days`` rather than assuming zero.
    routing_id: uuid.UUID | None
    routing_code: str | None
    total_minutes: Decimal
    working_hours_per_day: Decimal
    lead_time_days: int


__all__ = [
    "MAX_COSTING_QUANTITY",
    "MAX_OPERATIONS_PER_ROUTING",
    "CopyAsNewVersionRequest",
    "LeadTimeRead",
    "MachineCreate",
    "MachineFilters",
    "MachineRead",
    "MachineStatusChange",
    "MachineUpdate",
    "OperationCostRead",
    "ReasonRequest",
    "RoutingCostRead",
    "RoutingCreate",
    "RoutingDetail",
    "RoutingFilters",
    "RoutingOperationIn",
    "RoutingOperationRead",
    "RoutingStatusChange",
    "RoutingSummary",
    "RoutingUpdate",
    "WorkCenterCreate",
    "WorkCenterFilters",
    "WorkCenterRead",
    "WorkCenterUpdate",
]
