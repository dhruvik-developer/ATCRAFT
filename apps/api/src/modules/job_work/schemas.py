"""Request and response schemas for job work.

Conventions applied throughout, each for a specific reason:

* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (master rules
  section 40).
* **Quantities and money are ``Decimal`` with explicit precision**, so Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (section 84).
* **Every action that moves stock carries a mandatory reason.** Section 83
  requires one for every stock event, and issuing to a vendor is the largest one
  this module performs.
* **Nothing that decides where stock goes is optional.** A receipt states what
  came back and what happened to the rest; the service refuses to guess, because
  a guess here is material that quietly stops being traceable.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.job_work.models import (
    VEHICLE_NUMBER_LENGTH,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkSourceType,
    RateBasis,
    RejectionAction,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeRate = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
LossPercentage = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
Notes = Annotated[str, Field(max_length=2000)]
LineNumber = Annotated[int, Field(ge=1, le=100_000)]

#: Upper bound on a single ``pending-at-vendor`` service read. The contract other
#: modules call returns a plain list, so it needs a ceiling of its own - an
#: unbounded result set is forbidden however it is reached (section 31).
PENDING_AT_VENDOR_MAX = 500


# -----------------------------------------------------------------------------
# Order header
# -----------------------------------------------------------------------------


class JobWorkOrderLineCreate(BaseModel):
    """One material to send out and the processed item expected back.

    ``expected_output_item_id`` may be omitted when the process does not change
    what the thing is - polishing a handle returns a handle - in which case the
    input item is used. It is a separate field rather than an assumption because
    for PVD, coating and machining the output genuinely is a different code with
    a different cost.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: LineNumber
    item_id: uuid.UUID
    quantity_to_issue: PositiveQuantity
    #: Defaults to ``item_id``: the same item comes back, processed.
    expected_output_item_id: uuid.UUID | None = None
    #: Defaults to ``quantity_to_issue`` - one piece in, one piece out, which is
    #: true of every process in :class:`JobWorkProcess`. Set it explicitly for a
    #: yield that is not 1:1; the ratio is frozen on the order when it is raised.
    expected_output_quantity: PositiveQuantity | None = None
    expected_loss_percentage: LossPercentage = Decimal("0")
    notes: Notes | None = None


class JobWorkOrderCreate(BaseModel):
    """Raise a draft job work order."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    process_type: JobWorkProcess
    process_description: Notes | None = None
    expected_return_date: date

    #: Material is drawn from here and the job-work pool that holds it while it
    #: is away belongs to this position.
    warehouse_id: uuid.UUID
    #: Where processed output is received. Defaults to ``warehouse_id``.
    return_warehouse_id: uuid.UUID | None = None

    rate_per_unit: NonNegativeRate = Decimal("0")
    rate_basis: RateBasis = RateBasis.PER_PIECE

    #: Whether returned output must be inspected before it becomes usable.
    requires_qc: bool = False

    source_type: JobWorkSourceType = JobWorkSourceType.MANUAL
    source_id: uuid.UUID | None = None

    notes: Notes | None = None

    lines: list[JobWorkOrderLineCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def _lines_are_coherent(self) -> JobWorkOrderCreate:
        numbers = [line.line_number for line in self.lines]
        if len(set(numbers)) != len(numbers):
            raise ValueError("Line numbers must be unique within a job work order.")
        if (self.source_type is JobWorkSourceType.MANUAL) != (self.source_id is None):
            raise ValueError(
                "Name the source document this order covers, or leave the source type MANUAL."
            )
        return self


class JobWorkOrderUpdate(BaseModel):
    """Amend a draft.

    Only a DRAFT is editable: past approval the order has hard stock
    reservations against it and, from ISSUED, material at a vendor. ``vendor_id``
    is absent on purpose - sending the same order to a different processor is a
    different order, not an edit, and the challan would name the wrong party.
    """

    model_config = ConfigDict(extra="forbid")

    process_type: JobWorkProcess | None = None
    process_description: Notes | None = None
    expected_return_date: date | None = None
    warehouse_id: uuid.UUID | None = None
    return_warehouse_id: uuid.UUID | None = None
    rate_per_unit: NonNegativeRate | None = None
    rate_basis: RateBasis | None = None
    requires_qc: bool | None = None
    notes: Notes | None = None
    #: Replaces the whole line set when present. A partial line patch would make
    #: the order's totals depend on the order the edits arrived in.
    lines: list[JobWorkOrderLineCreate] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _line_numbers_are_unique(self) -> JobWorkOrderUpdate:
        if self.lines is not None:
            numbers = [line.line_number for line in self.lines]
            if len(set(numbers)) != len(numbers):
                raise ValueError("Line numbers must be unique within a job work order.")
        return self


class JobWorkOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_work_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    uom_id: uuid.UUID
    quantity_to_issue: Decimal
    issued_quantity: Decimal
    received_quantity: Decimal
    expected_output_item_id: uuid.UUID
    expected_output_quantity: Decimal
    received_output_quantity: Decimal
    rejected_quantity: Decimal
    scrap_quantity: Decimal
    expected_loss_percentage: Decimal
    reservation_id: uuid.UUID | None
    notes: str | None


class JobWorkOrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    process_type: JobWorkProcess
    process_description: str | None
    expected_return_date: date
    warehouse_id: uuid.UUID
    return_warehouse_id: uuid.UUID
    rate_per_unit: Decimal
    rate_basis: RateBasis
    estimated_charges: Decimal
    total_charges: Decimal
    vendor_debit_amount: Decimal
    status: JobWorkOrderStatus
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    issued_at: datetime | None
    closed_at: datetime | None
    receipt_count: int
    requires_qc: bool
    source_type: JobWorkSourceType
    source_id: uuid.UUID | None
    cancellation_reason: str | None
    notes: str | None
    version: int


class JobWorkOrderDetail(JobWorkOrderSummary):
    """Header plus its line set.

    Not paginated, by deliberate exception to section 31: the set is bounded by
    ``MAX_LINES_PER_ORDER`` at write time and a challan is only meaningful whole.
    """

    lines: list[JobWorkOrderLineRead] = Field(default_factory=list)


class JobWorkOrderFilters(BaseModel):
    search: str | None = None
    vendor_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    status: JobWorkOrderStatus | None = None
    process_type: JobWorkProcess | None = None
    #: Only orders with company material physically at a vendor.
    at_vendor_only: bool = False
    #: Only orders that still owe processed stock.
    open_only: bool = False
    #: Due back on or before this date - the overdue chase list.
    due_before: date | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


# -----------------------------------------------------------------------------
# Lifecycle payloads
# -----------------------------------------------------------------------------


class ReasonRequest(BaseModel):
    """The payload every state transition shares: why it happened."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class IssueLineIn(BaseModel):
    """Send less of one line than the order asked for.

    Omitting a line issues its full ``quantity_to_issue``, which is the normal
    case. An explicit quantity exists for the reality of a vendor who can only
    take half the consignment this week.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: LineNumber
    issued_quantity: PositiveQuantity


class IssueRequest(BaseModel):
    """Send the material out under a delivery challan.

    The challan fields are on this payload rather than on the order because they
    describe the journey, not the instruction: the vehicle and the transporter
    are only known on the morning the goods actually leave.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    challan_date: date | None = None
    vehicle_number: Annotated[str, Field(max_length=VEHICLE_NUMBER_LENGTH)] | None = None
    transporter_id: uuid.UUID | None = None
    #: Overrides the order's expected return date for this consignment.
    expected_return_date: date | None = None
    challan_notes: Notes | None = None
    #: Business time of the despatch, when it differs from now.
    occurred_at: datetime | None = None
    lines: list[IssueLineIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_override_per_line(self) -> IssueRequest:
        numbers = [line.line_number for line in self.lines]
        if len(set(numbers)) != len(numbers):
            raise ValueError("A line may only be given one issued quantity.")
        return self


class ReceiptLineIn(BaseModel):
    """What one order line gave back in this lot.

    ``output_quantity`` and ``rejected_quantity`` are both *output* units;
    ``returned_unprocessed_quantity`` is *input* material handed back untouched.
    They are separate fields because they post to three different places, and a
    single "received" number would make it impossible to say which.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: LineNumber
    output_quantity: NonNegativeQuantity = Decimal("0")
    rejected_quantity: NonNegativeQuantity = Decimal("0")
    #: Where the rejected output physically is. Ignored when nothing was
    #: rejected; see :class:`~src.modules.job_work.models.RejectionAction`.
    rejection_action: RejectionAction = RejectionAction.RETURN_TO_STOCK
    returned_unprocessed_quantity: NonNegativeQuantity = Decimal("0")
    #: Close the line: everything still at the vendor that this lot did not
    #: account for is written off as process loss. This is how "I sent 100 and
    #: got 98 back" is keyed - the 2 becomes scrap with a reason rather than an
    #: outstanding balance nobody ever chases.
    is_final: bool = False
    notes: Notes | None = None

    @model_validator(mode="after")
    def _something_came_back(self) -> ReceiptLineIn:
        if (
            self.output_quantity + self.rejected_quantity + self.returned_unprocessed_quantity <= 0
            and not self.is_final
        ):
            raise ValueError(
                "A receipt line must report something returned, or close the line so the "
                "shortfall is written off explicitly."
            )
        return self


class ReceiveRequest(BaseModel):
    """Record one lot returning from the vendor."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    receipt_date: date | None = None
    vendor_challan_number: Annotated[str, Field(max_length=64)] | None = None
    vendor_challan_date: date | None = None
    #: Acknowledge loss beyond the agreed allowance. Without it a receipt whose
    #: loss exceeds ``expected_loss_percentage`` is refused, and the caller is
    #: told how much is over - see ``service.receive``.
    approve_excess_loss: bool = False
    #: Business time of the receipt, when it differs from now.
    occurred_at: datetime | None = None
    notes: Notes | None = None
    lines: list[ReceiptLineIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_entry_per_line(self) -> ReceiveRequest:
        numbers = [line.line_number for line in self.lines]
        if len(set(numbers)) != len(numbers):
            raise ValueError("A line may only appear once in a receipt.")
        return self


class QcVerdictItem(BaseModel):
    """The verdict on one held output item."""

    model_config = ConfigDict(extra="forbid")

    output_item_id: uuid.UUID
    accepted_quantity: NonNegativeQuantity
    rejected_quantity: NonNegativeQuantity = Decimal("0")

    @model_validator(mode="after")
    def _verdict_accounts_for_something(self) -> QcVerdictItem:
        if self.accepted_quantity + self.rejected_quantity <= 0:
            raise ValueError("An inspection verdict must account for at least one unit.")
        return self


class QcResultRequest(BaseModel):
    """The inspection verdict on a job work order's held output.

    Stated per output item, always. An order that sent three castings out for
    three different finishes holds three different item codes, and a single
    accepted/rejected pair could only be split between them by guessing - which
    would release stock of one code and damage stock of another.

    The verdict must account for everything on hold: releasing part of a held lot
    and leaving the rest in limbo is how stock becomes permanently unusable
    without anybody deciding that it should be.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    #: The ``qc_inspections`` row this verdict came from, when quality raised one.
    qc_inspection_id: uuid.UUID | None = None
    items: list[QcVerdictItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_verdict_per_item(self) -> QcResultRequest:
        seen = {item.output_item_id for item in self.items}
        if len(seen) != len(self.items):
            raise ValueError("An output item may only be given one verdict.")
        return self


# -----------------------------------------------------------------------------
# Read models
# -----------------------------------------------------------------------------


class ChallanLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    challan_id: uuid.UUID
    job_work_order_line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    uom_id: uuid.UUID
    quantity: Decimal
    unit_value: Decimal
    total_value: Decimal


class ChallanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    job_work_order_id: uuid.UUID
    vendor_id: uuid.UUID
    challan_date: date
    expected_return_date: date
    vehicle_number: str | None
    transporter_id: uuid.UUID | None
    total_quantity: Decimal
    total_value: Decimal
    notes: str | None
    lines: list[ChallanLineRead] = Field(default_factory=list)


class ReceiptLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    receipt_id: uuid.UUID
    job_work_order_line_id: uuid.UUID
    line_number: int
    output_item_id: uuid.UUID
    output_quantity: Decimal
    rejected_quantity: Decimal
    rejection_action: RejectionAction
    returned_unprocessed_quantity: Decimal
    scrap_quantity: Decimal
    excess_loss_quantity: Decimal
    input_consumed_quantity: Decimal
    charges: Decimal
    vendor_debit_amount: Decimal
    notes: str | None


class ReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    receipt_number: int
    job_work_order_id: uuid.UUID
    vendor_id: uuid.UUID
    receipt_date: date
    vendor_challan_number: str | None
    vendor_challan_date: date | None
    total_output_quantity: Decimal
    total_rejected_quantity: Decimal
    total_returned_unprocessed: Decimal
    total_scrap_quantity: Decimal
    total_input_consumed: Decimal
    total_charges: Decimal
    vendor_debit_amount: Decimal
    has_excess_loss: bool
    qc_held_at: datetime | None
    qc_released_at: datetime | None
    qc_inspection_id: uuid.UUID | None
    reason: str
    notes: str | None
    lines: list[ReceiptLineRead] = Field(default_factory=list)


class IssueResult(BaseModel):
    """What issuing actually did, so the caller does not have to guess."""

    order: JobWorkOrderDetail
    challan: ChallanRead
    #: Number of ``JOB_WORK_ISSUE`` ledger rows written - one per line issued.
    movement_count: int
    #: Total quantity now sitting in the job-work pool at the vendor. Company
    #: property; not available to promise to anybody else.
    quantity_at_vendor: Decimal


class ReceiptResult(BaseModel):
    """What a receipt actually posted."""

    order: JobWorkOrderDetail
    receipt: ReceiptRead
    movement_count: int
    #: True when the output went to QC hold instead of general availability.
    output_held_for_qc: bool
    #: True when quality is deployed and raised an inspection for this lot.
    qc_inspection_created: bool


class PendingAtVendorRow(BaseModel):
    """Company material currently at one vendor, for one item.

    Every quantity here is stock the company still owns and still values; it is
    simply not available to promise. The ageing is measured from the despatch,
    which is why an order that has been open for six months shows up even if its
    expected return date was quietly pushed back.
    """

    model_config = ConfigDict(from_attributes=True)

    vendor_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    outstanding_quantity: Decimal
    order_count: int
    #: Oldest despatch still outstanding for this vendor and item.
    oldest_issued_at: datetime | None
    #: Whole days since ``oldest_issued_at``.
    ageing_days: int
    #: Earliest promised return still outstanding.
    earliest_expected_return: date | None
    #: True when that date has passed.
    is_overdue: bool


class ReconciliationLine(BaseModel):
    """Issued against received, for one order line.

    ``outstanding_quantity`` is the number that has to equal the line's share of
    ``inventory_balances.job_work_quantity``. When they disagree, either a
    movement was posted outside this module or a receipt was recorded without
    one - both are corruption of the traceability the module exists to keep.
    """

    line_number: int
    item_id: uuid.UUID
    output_item_id: uuid.UUID
    quantity_to_issue: Decimal
    issued_quantity: Decimal
    received_quantity: Decimal
    outstanding_quantity: Decimal
    expected_output_quantity: Decimal
    received_output_quantity: Decimal
    rejected_quantity: Decimal
    scrap_quantity: Decimal
    #: Loss as a percentage of the input accounted for, so it can be read
    #: against the line's agreed allowance without arithmetic.
    loss_percentage: Decimal
    expected_loss_percentage: Decimal
    #: Loss beyond the allowance. Positive means the vendor owes an explanation.
    excess_loss_quantity: Decimal
    #: Output still expected for the material that has not come back.
    output_shortfall_quantity: Decimal


class ReconciliationReport(BaseModel):
    """Issued vs received vs loss for one job work order.

    The report a plant manager reads before paying a job worker, and the one an
    auditor reads to satisfy themselves that stock sent out was accounted for.
    """

    order_id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    status: JobWorkOrderStatus
    issued_at: datetime | None
    expected_return_date: date
    total_issued: Decimal
    total_received: Decimal
    total_outstanding: Decimal
    total_output_received: Decimal
    total_rejected: Decimal
    total_scrap: Decimal
    total_excess_loss: Decimal
    total_charges: Decimal
    vendor_debit_amount: Decimal
    #: Job-work-pool quantity the inventory ledger holds for this order's items
    #: at its warehouse. Read live from ``inventory_balances``, never cached
    #: (section 34). It is a whole-position figure: several open orders drawing
    #: on the same item and warehouse share it, so it is greater than or equal
    #: to ``total_outstanding`` rather than always equal to it.
    stock_in_job_work_pool: Decimal
    lines: list[ReconciliationLine] = Field(default_factory=list)


class VendorChargeRow(BaseModel):
    """One vendor's job work charges for a period.

    The contract ``finance`` consumes. Charges accrue on receipt - a vendor is
    paid for good pieces delivered - so the period is applied to
    ``job_work_receipts.receipt_date`` rather than to the order date.
    ``net_charges`` is what is actually payable: gross processing charges less
    the value of company material the vendor lost beyond the agreed allowance.
    """

    vendor_id: uuid.UUID
    gross_charges: Decimal
    vendor_debits: Decimal
    net_charges: Decimal
    receipt_count: int
    order_count: int
    output_quantity: Decimal


__all__ = [
    "PENDING_AT_VENDOR_MAX",
    "ChallanLineRead",
    "ChallanRead",
    "IssueLineIn",
    "IssueRequest",
    "IssueResult",
    "JobWorkOrderCreate",
    "JobWorkOrderDetail",
    "JobWorkOrderFilters",
    "JobWorkOrderLineCreate",
    "JobWorkOrderLineRead",
    "JobWorkOrderSummary",
    "JobWorkOrderUpdate",
    "PendingAtVendorRow",
    "QcResultRequest",
    "QcVerdictItem",
    "ReasonRequest",
    "ReceiptLineIn",
    "ReceiptLineRead",
    "ReceiptRead",
    "ReceiptResult",
    "ReceiveRequest",
    "ReconciliationLine",
    "ReconciliationReport",
    "VendorChargeRow",
]
