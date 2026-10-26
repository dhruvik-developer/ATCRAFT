"""Job work business rules: sending material out and getting it back.

Master rules section 20. Raw handles go to a plating shop for PVD, castings go
to a machinist, finished parts go out for laser marking. The rules that matter,
and why each lives here rather than in a caller:

1. **MATERIAL AT A VENDOR IS STILL OURS - THIS IS THE WHOLE POINT.** Issuing
   posts ``JOB_WORK_ISSUE``, which moves the quantity out of ``on_hand_quantity``
   and into ``inventory_balances.job_work_quantity``. It is **not** a
   ``DISPATCH``, not a ``PURCHASE_RETURN``, and not an ``ADJUSTMENT_DECREASE``:
   those all say the goods stopped being ours, and a company that books them
   that way loses a warehouse full of stock off its balance sheet the moment it
   leaves the gate. Job-work stock stays on the books, stays in the valuation,
   stays in stock reports and stays attributable to a vendor, a challan and an
   order line. What it stops being is *available* - nobody can be promised it.
   :func:`reconciliation` reports the module's own outstanding figure beside the
   ledger's ``JOB_WORK`` pool so a divergence is visible rather than silent.

2. **Receiving is one posting.** Every movement a lot generates - the output
   received, the input drawn out of the job-work pool, the unprocessed return,
   the process loss written off as scrap - goes into a single
   :func:`~src.modules.inventory.service.post_movements` call inside one
   transaction. That call locks every affected balance row in primary-key order
   before touching any of them, so two receipts sharing an item queue instead of
   deadlocking (database.md section 4.4). Posting them separately would leave a
   window in which the company had received coated handles and still believed
   the raw ones were at the vendor.

3. **The input pool is drawn down explicitly, and the ledger has a word for
   it.** The section-83 vocabulary is closed and has no "job work consume", so
   the input consumed by the process is posted as ``MATERIAL_ISSUE`` with
   ``stock_state=JOB_WORK`` - the documented way to name the pool a decrease
   comes out of. It is the same word production uses when components are
   consumed into a different finished item, which is exactly what happened here;
   the difference is only that the machine belonged to somebody else.

4. **Loss is stated, never implied.** A short return inside the line's
   ``expected_loss_percentage`` posts the shortfall as ``SCRAP`` with a reason.
   Beyond it, the receipt is refused until somebody with authority approves the
   excess, and the value of the material lost is debited to the vendor at the
   position's own moving-average cost. A quantity that simply never comes back
   and is never written off would sit in the job-work pool forever, and the
   stock report would keep counting material that no longer exists.

5. **An over-receipt is refused, not clamped.** Getting back more than went out
   is either a mis-keyed quantity or somebody else's goods; both need a person,
   not a silent adjustment. :data:`EXCESS_RECEIPT_TOLERANCE_PERCENT` allows for
   an agreed overage on the *output* side, and the job-work pool itself is an
   absolute bound on the input side.

6. **Rework keeps the material where it is.** Output the vendor will reprocess
   is recorded and nothing moves: the input stays in the job-work pool, the
   order line stays open, and the quantity keeps appearing in
   ``pending-at-vendor`` with its true ageing. Receiving it into a rejected
   state and inventing a second order to send it back would put the goods
   somewhere they are not.

7. **QC holds the output instead of releasing it.** When the order requires
   inspection the receipt posts with ``stock_state=QC_HOLD``, the order reaches
   ``QC_PENDING`` once it is fully received while a lot is still held, and the
   quality module is asked for one inspection per output item of the lot through
   a guarded import. When quality is not deployed the hold is still recorded on
   the receipt and is still queryable - the stock is held either way, which is
   the half that matters.

8. **One hold, one release path.** Deciding those inspections is what releases
   the held output, and quality calls :func:`record_quality_verdict` back in the
   same transaction to release the lot - even while the rest of the order is
   still at the vendor - and to complete the order once nothing is held, moving
   nothing itself. ``record_qc_result`` releases stock only for lots no
   inspection covers; for covered lots it refuses, or applies the inspections'
   already-recorded figures, and never posts a second movement.

Allocation and quality are reached through ports. The default implementations
delegate to ``allocation.service`` and ``quality.service`` exactly as
database.md sections 4.5 and 8 specify; the seams exist so this module's rules
can be exercised without standing up the reservation engine.

## Assumptions

Each of these is a rule the contract did not fix. The conservative option was
taken, named, and is changeable in one place.

* **Approval is gated on ``JOB_WORK_CLOSE``.** The permission vocabulary has no
  ``JOB_WORK_APPROVE``. ``JOB_WORK_CLOSE`` is the module's senior permission -
  the plant manager holds it and the planner who raises and issues orders does
  not - so gating approval on it keeps raising and approving separate duties.
* **Accepting loss beyond the agreed allowance is gated on ``JOB_WORK_CLOSE``
  too.** ``approve_excess_loss`` is a flag on the receipt payload, and the route
  that carries it needs only ``JOB_WORK_RECEIVE`` - the storekeeper's right. The
  excess debits the vendor ledger, so the flag is honoured only from a caller
  who holds the same senior right that approves the order, and the receipt
  records that caller as the approver. A storekeeper keying the lot still gets
  ``ApprovalRequiredError`` and hands it up (:func:`assert_excess_loss_authorised`).
* **A job work order is 1:1 in the line's unit of measure unless told
  otherwise.** ``expected_output_quantity`` defaults to ``quantity_to_issue``,
  which is true of polishing, coating, PVD, laser marking and machining. A yield
  that is not 1:1 is expressed by setting it, and the ratio is frozen on the
  line so a later edit cannot restate what an issued order was supposed to
  return.
* **Charges accrue on good output received**, at the agreed rate, on the receipt
  date. A vendor is paid for good pieces delivered, so a short or rejected
  delivery costs less without anybody raising a credit note. A ``LOT`` rate is
  accrued once, on the first receipt that returns any output.
* **The vendor debit is the value of company material lost beyond the
  allowance**, priced at the material's own moving-average cost from the scrap
  movement. It is not a penalty and not a re-charge of the processing fee -
  those would both be numbers this module invented.
* **Processed output is received at its capitalised cost** (:func:`capitalised_output_cost`):
  the input the good output consumed, valued at the frozen challan issue cost -
  the ledger's FIFO draw captured on the challan line when the material left -
  plus the receipt charge the line accrued, per good unit. Returned rejects
  carry their proportional share of that input value without a processing
  charge; their material is not also folded into the good units. The price is read
  from challans before posting, so it is exact rather than a second read of
  layers the posting itself consumes. Process loss within the allowance is
  expensed as scrap (as before) rather than absorbed into the good units, and
  the later invoice price variance is expensed too. docs/assumptions.md §E7b-2.
* **Batch identity is preserved only for same-item job work.** One batch is
  selected for each order line on the draft, reserved at that stock grain and
  carried through every challan, receipt and ledger movement; a second batch
  needs a separate line.
  Serial issues name every physical unit, and receipts name disjoint units for
  output, rejection, unprocessed return and scrap. Vendor-held rework remains
  eligible for a later receipt. Serial-only SKU conversion is one-to-one: the
  physical label is retained in a new SKU-scoped serial row, joined to the input
  by an immutable conversion record. Historical ledger identities are never
  rewritten. Batch SKU conversion and mixed tracking still need a lineage
  policy and are refused.
* **``requires_qc`` is a per-order flag**, not an item-master property. ``items``
  carries no inspection policy and inventing one would put the rule in two
  places that could disagree; ``goods_receipt_lines.requires_qc`` and
  ``assembly_orders.requires_qc`` make the same choice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    ApprovalRequiredError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.ids import persisted_actor
from src.core.logging import get_logger
from src.core.messages import (
    date_before,
    held_for_inspection,
    inactive_record,
    verdict_differs_from_inspection,
)
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity, to_unit_price
from src.modules.allocation.models import DemandType, ReservationType
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import StockSerialConversion, StockState, TransactionType
from src.modules.inventory.repository import StockBatchRepository
from src.modules.inventory.service import MovementRequest, change_stock_state, post_movements
from src.modules.job_work.models import (
    ISSUED_OR_LATER_STATUSES,
    JOB_WORK_DEFAULT_GST_RATE,
    JOB_WORK_DEFAULT_SAC,
    MAX_LINES_PER_ORDER,
    JobWorkChallan,
    JobWorkChallanLine,
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkReceipt,
    JobWorkReceiptLine,
    JobWorkSourceType,
    RateBasis,
    RejectionAction,
)
from src.modules.job_work.repository import (
    ItemLookupRepository,
    JobWorkBalanceRepository,
    JobWorkChallanLineRepository,
    JobWorkChallanRepository,
    JobWorkItem,
    JobWorkOrderLineRepository,
    JobWorkOrderRepository,
    JobWorkReceiptLineRepository,
    JobWorkReceiptRepository,
    TransporterLookupRepository,
    VendorLookupRepository,
    WarehouseLookupRepository,
)
from src.modules.job_work.schemas import (
    PENDING_AT_VENDOR_MAX,
    BillableLine,
    ChallanFilters,
    ChallanLineRead,
    ChallanRead,
    IssueRequest,
    IssueResult,
    JobWorkOrderCreate,
    JobWorkOrderDetail,
    JobWorkOrderFilters,
    JobWorkOrderLineCreate,
    JobWorkOrderLineRead,
    JobWorkOrderUpdate,
    PendingAtVendorRow,
    QcResultRequest,
    ReceiptLineIn,
    ReceiptLineRead,
    ReceiptRead,
    ReceiptResult,
    ReceiveRequest,
    ReconciliationLine,
    ReconciliationReport,
    VendorChargeRow,
    VendorLedgerItemRow,
    VendorLedgerReport,
)
from src.modules.job_work.serial_tracking import (
    prepare_conversions,
    receipt_movements,
    validate_receipt_units,
)
from src.modules.organization.models import Company
from src.modules.organization.service import require_cost_centre
from src.modules.products.models import Item, UnitOfMeasure
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.vendors.models import Transporter, Vendor

log = get_logger(__name__)

AUDIT_JOB_WORK_ORDER = "job_work_order"
AUDIT_JOB_WORK_CHALLAN = "job_work_challan"
AUDIT_JOB_WORK_RECEIPT = "job_work_receipt"

#: What the ledger rows this module posts point back at. A bare string, matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
MOVEMENT_REFERENCE_JOB_WORK = "job_work_order"

#: The reference a returned lot's inspection carries: the *receipt* it holds,
#: which is the document the verdict releases. Inspections raised before this
#: existed carry :data:`MOVEMENT_REFERENCE_JOB_WORK` with the receipt's id, and
#: are read under both keys.
QC_REFERENCE_RECEIPT = "job_work_receipt"
QC_REFERENCE_KEYS: tuple[str, ...] = (QC_REFERENCE_RECEIPT, MOVEMENT_REFERENCE_JOB_WORK)

# ASSUMPTION: a vendor may return up to 2% more output than the order expected
# before the receipt is refused. Job work overages are real - a plating line runs
# a few extra pieces to fill a rack - but an unbounded one is a mis-keyed
# quantity or another customer's goods, and either needs a person rather than a
# silent adjustment. Named so raising it is a one-line, reviewable change.
EXCESS_RECEIPT_TOLERANCE_PERCENT = Decimal("2")

#: The largest ``pending-at-vendor`` page a service caller may ask for.
PENDING_AT_VENDOR_LIMIT = PENDING_AT_VENDOR_MAX

#: The largest number of vendors one ``job_work_charges_for_period`` call returns
#: - the bounded, plain-list contract other modules consume. The HTTP surface
#: pages the same query through :func:`list_charges_page` instead, so a company
#: with more job workers than this reads every one of them a page at a time
#: rather than a silently cut extract (section 31).
CHARGES_VENDOR_LIMIT = 500

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
ORDER_AUDITED_FIELDS = [
    "code",
    "vendor_id",
    "process_type",
    "process_description",
    "expected_return_date",
    "warehouse_id",
    "return_warehouse_id",
    "cost_centre_id",
    "rate_per_unit",
    "rate_basis",
    "estimated_charges",
    "total_charges",
    "vendor_debit_amount",
    "tax_rate",
    "sac_code",
    "status",
    "approved_by_id",
    "approved_at",
    "issued_at",
    "closed_at",
    "requires_qc",
    "source_type",
    "source_id",
    "notes",
]

#: Draft fields an explicit ``null`` in a PATCH body legitimately clears. Every
#: other column on the update payload backs a NOT NULL column, where a null is a
#: client mistake rather than an instruction - see :func:`update_order`.
CLEARABLE_UPDATE_FIELDS = frozenset(
    {"process_description", "notes", "cost_centre_id", "tax_rate", "sac_code"}
)


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move job_work_orders.status
# -----------------------------------------------------------------------------

JOB_WORK_ORDER_LIFECYCLE: StateMachine[JobWorkOrderStatus] = StateMachine(
    entity="Job work order",
    initial=JobWorkOrderStatus.DRAFT,
    transitions=(
        Transition(
            JobWorkOrderStatus.DRAFT,
            JobWorkOrderStatus.APPROVED,
            # See the module's Assumptions: there is no JOB_WORK_APPROVE in the
            # permission vocabulary, and CLOSE is the senior job-work right.
            permission=Permission.JOB_WORK_CLOSE,
            label="Approve",
            # Approval takes hard reservations away from every other document
            # competing for the same material.
            is_irreversible=True,
        ),
        Transition(
            JobWorkOrderStatus.APPROVED,
            JobWorkOrderStatus.ISSUED,
            permission=Permission.JOB_WORK_ISSUE,
            label="Issue under a challan",
            is_irreversible=True,
        ),
        Transition(
            JobWorkOrderStatus.ISSUED,
            JobWorkOrderStatus.PARTIALLY_RECEIVED,
            permission=Permission.JOB_WORK_RECEIVE,
            label="Receive a lot",
            is_irreversible=True,
        ),
        # A vendor who returns everything in one delivery never passes through
        # PARTIALLY_RECEIVED, so the skip edge is real rather than a shortcut.
        Transition(
            JobWorkOrderStatus.ISSUED,
            JobWorkOrderStatus.RECEIVED,
            permission=Permission.JOB_WORK_RECEIVE,
            label="Receive in full",
            is_irreversible=True,
        ),
        Transition(
            JobWorkOrderStatus.PARTIALLY_RECEIVED,
            JobWorkOrderStatus.RECEIVED,
            permission=Permission.JOB_WORK_RECEIVE,
            label="Receive the balance",
            is_irreversible=True,
        ),
        Transition(
            JobWorkOrderStatus.RECEIVED,
            JobWorkOrderStatus.QC_PENDING,
            permission=Permission.JOB_WORK_RECEIVE,
            label="Hold for inspection",
        ),
        Transition(
            JobWorkOrderStatus.QC_PENDING,
            JobWorkOrderStatus.COMPLETED,
            permission=Permission.QC_DECIDE,
            label="Record inspection result",
            is_irreversible=True,
        ),
        # The no-inspection branch. Both edges reach COMPLETED; only whether the
        # output had to be looked at first differs.
        Transition(
            JobWorkOrderStatus.RECEIVED,
            JobWorkOrderStatus.COMPLETED,
            permission=Permission.JOB_WORK_CLOSE,
            label="Complete",
        ),
        Transition(
            JobWorkOrderStatus.COMPLETED,
            JobWorkOrderStatus.CLOSED,
            permission=Permission.JOB_WORK_CLOSE,
            label="Close",
        ),
        # Cancellation is reachable only before the material has left. Past that
        # the order is the only record of where company stock went, and there is
        # no edge out of ISSUED except through a receipt - a consignment that
        # never comes back is written off by a final receipt, so the loss is
        # stated rather than implied.
        Transition(
            JobWorkOrderStatus.DRAFT,
            JobWorkOrderStatus.CANCELLED,
            permission=Permission.JOB_WORK_CREATE,
            label="Cancel",
        ),
        Transition(
            JobWorkOrderStatus.APPROVED,
            JobWorkOrderStatus.CANCELLED,
            permission=Permission.JOB_WORK_CREATE,
            label="Cancel",
            # Reservations are given back, which changes what every other
            # document can be promised.
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({JobWorkOrderStatus.CLOSED, JobWorkOrderStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Pure rules - unit tested without a database
# -----------------------------------------------------------------------------


def yield_ratio(quantity_to_issue: Decimal, expected_output_quantity: Decimal) -> Decimal:
    """Output units expected per unit of input, frozen when the order is raised.

    Normally 1: one raw handle goes out, one coated handle comes back. A process
    that yields differently - ten blanks machined out of one bar - is expressed
    by setting ``expected_output_quantity``, and every conversion between the two
    sides of the order goes through this number so they cannot drift apart.
    """
    issued = to_quantity(quantity_to_issue)
    if issued <= ZERO:
        raise ValidationError("A job work line must send out a positive quantity.")
    expected = to_quantity(expected_output_quantity)
    if expected <= ZERO:
        raise ValidationError("A job work line must expect a positive output quantity.")
    return expected / issued


def input_equivalent(output_quantity: Decimal, ratio: Decimal) -> Decimal:
    """How much input material a quantity of output accounts for."""
    if ratio <= ZERO:  # pragma: no cover - yield_ratio refuses a non-positive ratio
        raise ValidationError("The yield ratio must be positive.")
    return to_quantity(Decimal(output_quantity) / ratio)


def estimate_charges(
    *, rate_per_unit: Decimal, rate_basis: RateBasis, expected_output_quantity: Decimal
) -> Decimal:
    """What the order is expected to cost at the agreed rate.

    Frozen on the order so a later rate-card change does not silently restate
    what was approved. ``LOT`` is a flat charge for the consignment, so the
    quantity does not enter it.
    """
    rate = Decimal(rate_per_unit)
    if RateBasis(rate_basis) is RateBasis.LOT:
        return to_money(rate)
    return to_money(rate * to_quantity(expected_output_quantity))


def receipt_charge(
    *,
    rate_per_unit: Decimal,
    rate_basis: RateBasis,
    output_quantity: Decimal,
    lot_already_charged: bool,
) -> Decimal:
    """What one lot accrued.

    See the module's Assumptions: the vendor is paid for good output delivered,
    so rejected pieces and process loss earn nothing and a short delivery costs
    less without anybody raising a credit note. A ``LOT`` rate is earned once, on
    the first lot that actually returns something.
    """
    rate = Decimal(rate_per_unit)
    good = to_quantity(output_quantity)
    if RateBasis(rate_basis) is RateBasis.LOT:
        return ZERO if lot_already_charged or good <= ZERO else to_money(rate)
    return to_money(rate * good)


@dataclass(frozen=True, slots=True)
class LineReceiptPlan:
    """What one order line gives back in one lot, and where each part of it goes.

    The five quantities are not independent - this identity is what makes the
    job-work pool reconcilable::

        input_consumed = input_equivalent(output + rejected_returned)
                       + returned_unprocessed
                       + scrap

    ``rework_quantity`` is deliberately outside it: output the vendor keeps to
    reprocess consumes nothing, so the material stays in the pool and the line
    stays open.
    """

    line_number: int
    output_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    returned_unprocessed_quantity: Decimal
    input_for_output: Decimal
    scrap_quantity: Decimal
    excess_loss_quantity: Decimal
    input_consumed_quantity: Decimal
    #: Rejected output destroyed at the vendor, in input units. Separate from
    #: process loss: it earns neither an automatic recovery nor loss approval.
    rejected_scrap_input_quantity: Decimal = ZERO


def plan_line_receipt(
    *,
    line_number: int,
    outstanding_input: Decimal,
    ratio: Decimal,
    expected_output_quantity: Decimal,
    output_received_so_far: Decimal,
    output_quantity: Decimal,
    rejected_quantity: Decimal,
    rejection_action: RejectionAction,
    returned_unprocessed_quantity: Decimal,
    is_final: bool,
    expected_loss_percentage: Decimal,
) -> LineReceiptPlan:
    """Work out what one receipt line does, and check it against what is out.

    Pure: everything it needs has been fetched already, so the arithmetic and the
    four rules it enforces can be exercised without a database.

    The rules, in the order they fail:

    1. nothing may be received against a line with no material at the vendor;
    2. unprocessed input handed back may never exceed what is at the vendor -
       it is the same item, so there is no yield to argue about;
    3. cumulative output may not exceed what the line expected by more than
       :data:`EXCESS_RECEIPT_TOLERANCE_PERCENT`;
    4. neither may one lot's output imply more input than is left in the pool
       for that line, by more than the same tolerance;
    5. a report that accounts for no material at all is refused rather than
       written as a row that means nothing.

    Inside the tolerance an over-delivery is accepted, and the input drawn from
    the job-work pool is capped at what is actually in it. That is the honest
    representation of a vendor who returned a few more pieces than were sent:
    the surplus output exists and is received, and no input is invented behind
    it. Beyond the tolerance the lot is refused rather than clamped - the
    difference is a mis-keyed quantity or somebody else's goods, and both need a
    person.
    """
    outstanding = to_quantity(outstanding_input)
    if outstanding <= ZERO:
        raise BusinessRuleError(
            f"Line {line_number} has no material at the vendor; nothing can be received "
            "against it.",
            details={"line_number": line_number, "outstanding_quantity": str(outstanding)},
        )

    good = to_quantity(output_quantity)
    rejected = to_quantity(rejected_quantity)
    returned = to_quantity(returned_unprocessed_quantity)
    action = RejectionAction(rejection_action)

    # Unprocessed input is the same item that went out, so there is no yield to
    # interpret and no tolerance to allow: more than was sent cannot come back.
    if returned > outstanding:
        raise BusinessRuleError(
            f"Line {line_number} would return {returned} unprocessed unit(s) but only "
            f"{outstanding} is at the vendor.",
            code=ErrorCode.INSUFFICIENT_STOCK,
            details={
                "line_number": line_number,
                "outstanding_quantity": str(outstanding),
                "returned_quantity": str(returned),
            },
        )

    # Rework leaves the goods with the vendor, so those pieces consume no input
    # and are not counted as delivered output.
    rework = rejected if action is RejectionAction.REWORK_AT_VENDOR else ZERO
    delivered_output = good if action is RejectionAction.REWORK_AT_VENDOR else good + rejected

    ceiling = to_quantity(
        to_quantity(expected_output_quantity)
        * (Decimal("100") + EXCESS_RECEIPT_TOLERANCE_PERCENT)
        / Decimal("100")
    )
    # Rework the vendor is keeping is deliberately outside this total: those
    # pieces have not been delivered, and counting them now would count them
    # again when they finally come back good.
    cumulative = to_quantity(to_quantity(output_received_so_far) + delivered_output)
    if cumulative > ceiling:
        raise BusinessRuleError(
            f"Line {line_number} expected {to_quantity(expected_output_quantity)} unit(s) of "
            f"output; {cumulative} would be received, beyond the "
            f"{EXCESS_RECEIPT_TOLERANCE_PERCENT}% tolerance. Check the quantity or raise a "
            "second order for the excess.",
            details={
                "line_number": line_number,
                "expected_output_quantity": str(to_quantity(expected_output_quantity)),
                "cumulative_output": str(cumulative),
                "tolerance_percent": str(EXCESS_RECEIPT_TOLERANCE_PERCENT),
            },
        )

    raw_for_output = input_equivalent(delivered_output, ratio) if delivered_output > ZERO else ZERO
    input_ceiling = to_quantity(
        outstanding * (Decimal("100") + EXCESS_RECEIPT_TOLERANCE_PERCENT) / Decimal("100")
    )
    if to_quantity(raw_for_output + returned) > input_ceiling:
        raise BusinessRuleError(
            f"Line {line_number} would account for {to_quantity(raw_for_output + returned)} "
            f"unit(s) of input but only {outstanding} is at the vendor.",
            code=ErrorCode.INSUFFICIENT_STOCK,
            details={
                "line_number": line_number,
                "outstanding_quantity": str(outstanding),
                "accounted_quantity": str(to_quantity(raw_for_output + returned)),
                "tolerance_percent": str(EXCESS_RECEIPT_TOLERANCE_PERCENT),
            },
        )

    # The pool is an absolute floor whatever the tolerance allows: it cannot go
    # negative, and the inventory engine would refuse the movement if it tried.
    for_output = min(raw_for_output, to_quantity(outstanding - returned))
    accounted = to_quantity(for_output + returned)
    consumed = outstanding if is_final else accounted
    if consumed <= ZERO:
        raise BusinessRuleError(
            f"Line {line_number} accounts for no material. Rework the vendor is still holding "
            "changes nothing on its own - record it in the receipt notes, or close the line "
            "so the shortfall is written off.",
            details={"line_number": line_number},
        )

    scrap = to_quantity(consumed - accounted)
    allowance = to_quantity(consumed * to_quantity(expected_loss_percentage) / Decimal("100"))
    excess = to_quantity(max(ZERO, scrap - allowance))

    return LineReceiptPlan(
        line_number=line_number,
        output_quantity=good,
        rejected_quantity=rejected if action is not RejectionAction.REWORK_AT_VENDOR else ZERO,
        rework_quantity=rework,
        returned_unprocessed_quantity=returned,
        input_for_output=for_output,
        scrap_quantity=scrap,
        excess_loss_quantity=excess,
        input_consumed_quantity=consumed,
        rejected_scrap_input_quantity=(
            to_quantity(for_output * rejected / delivered_output)
            if action is RejectionAction.SCRAP_AT_VENDOR and delivered_output > ZERO
            else ZERO
        ),
    )


def loss_percentage(*, scrap_quantity: Decimal, input_consumed: Decimal) -> Decimal:
    """Process loss as a percentage of the input it consumed.

    Zero when nothing was consumed, rather than undefined: a line that has not
    been received against has not lost anything, and the reconciliation report
    would otherwise have to carry a null nobody can sort on.
    """
    consumed = to_quantity(input_consumed)
    if consumed <= ZERO:
        return ZERO
    return (Decimal(scrap_quantity) * Decimal("100") / consumed).quantize(Decimal("0.0001"))


def issue_unit_basis(challan_lines: Sequence[JobWorkChallanLine]) -> Decimal | None:
    """One order line's frozen issue cost: the quantity-weighted average of what
    its challans declared.

    Each challan line captured the ledger's FIFO draw when the material left,
    so this is the FIFO value of the input - frozen, auditable, and readable
    without touching the cost layers the issue has long since consumed. None
    when the line has no challan history: the caller then falls back to the
    engine's standard-cost default rather than inventing a price.
    """
    total_quantity = to_quantity(
        sum((to_quantity(Decimal(row.quantity)) for row in challan_lines), start=ZERO)
    )
    if total_quantity <= ZERO:
        return None
    total_value = sum(
        (to_quantity(Decimal(row.quantity)) * Decimal(row.unit_value) for row in challan_lines),
        start=ZERO,
    )
    return total_value / total_quantity


@dataclass(frozen=True, slots=True)
class CapitalisedCost:
    """What one receipt line's output pool is booked at."""

    #: Posted on the output movement: the input it consumed, valued at the
    #: frozen issue cost, plus the processing charge it earned, per output unit.
    unit_cost: Decimal
    #: The input value folded into it (input-for-output at the frozen basis).
    input_value: Decimal
    #: ``input_value + charge - unit_cost * output``: the sub-paisa remainder
    #: the six-place price cannot hold. Recorded on the receipt audit, not
    #: posted anywhere - it is dust, but it is accounted-for dust.
    rounding_residual: Decimal


def capitalised_output_cost(
    *,
    input_for_output: Decimal,
    challan_lines: Sequence[JobWorkChallanLine],
    line_charge: Decimal,
    output_quantity: Decimal,
    delivered_output_quantity: Decimal | None = None,
) -> CapitalisedCost | None:
    """Book an output pool at its share of input cost plus its earned charge.

    ``input_for_output`` covers all delivered output. When good and rejected
    output share that input, ``delivered_output_quantity`` allocates its frozen
    value proportionally between the two pools. Do not round the input shares
    to quantity scale: a fraction of a raw unit still has value. Only good
    output earns a processing charge; rejected output is priced with zero.

    Pure: the arithmetic behind P13.M5, exercised without a database. None when
    this pool has no output - there is nothing to divide by - or when
    the line has no challan history, in which case the movement keeps the
    engine's standard-cost default.
    """
    output = to_quantity(output_quantity)
    if output <= ZERO:
        return None
    basis = issue_unit_basis(challan_lines)
    if basis is None:
        return None
    # ASSUMPTION: each delivered pool carries its share of the input value at
    # the frozen challan issue cost. Only good output earns a receipt charge.
    # Process loss within the allowance is expensed as scrap (as today) rather
    # than absorbed into the good units, and the later invoice price variance
    # is expensed too. docs/assumptions.md §E7b-2.
    delivered = (
        output if delivered_output_quantity is None else to_quantity(delivered_output_quantity)
    )
    material = to_quantity(input_for_output) * basis * output / delivered
    total = material + Decimal(line_charge)
    unit = to_unit_price(total / output)
    return CapitalisedCost(
        unit_cost=unit,
        input_value=material,
        rounding_residual=total - unit * output,
    )


# -----------------------------------------------------------------------------
# Cross-module ports
# -----------------------------------------------------------------------------


class AllocationPort(Protocol):
    """The two allocation calls this module makes (database.md section 4.5)."""

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        batch_id: uuid.UUID | None = None,
        quantity: Decimal,
        demand_type: DemandType,
        demand_id: uuid.UUID,
        reason: str,
        demand_line_id: uuid.UUID | None = None,
        demand_code: str | None = None,
        reservation_type: ReservationType = ReservationType.HARD,
    ) -> Any: ...

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> Any: ...


class AllocationService:
    """Default port: the real reservation engine."""

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        batch_id: uuid.UUID | None = None,
        quantity: Decimal,
        demand_type: DemandType,
        demand_id: uuid.UUID,
        reason: str,
        demand_line_id: uuid.UUID | None = None,
        demand_code: str | None = None,
        reservation_type: ReservationType = ReservationType.HARD,
    ) -> Any:
        from src.modules.allocation.service import reserve as _reserve

        return await _reserve(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            batch_id=batch_id,
            quantity=quantity,
            demand_type=demand_type,
            demand_id=demand_id,
            demand_line_id=demand_line_id,
            demand_code=demand_code,
            reservation_type=reservation_type,
            reason=reason,
        )

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> Any:
        from src.modules.allocation.service import release as _release

        return await _release(
            session, tenant, reservation_id=reservation_id, reason=reason, quantity=quantity
        )


_DEFAULT_ALLOCATION = AllocationService()


def _allocation(port: AllocationPort | None) -> AllocationPort:
    return port if port is not None else _DEFAULT_ALLOCATION


class QualityPort(Protocol):
    """What job work asks of the quality module when it is deployed.

    Deliberately narrow: one call raising one in-process inspection against one
    returned lot. Anything wider would couple receiving to a module that is
    built independently of it.
    """

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        quantity: Decimal,
        warehouse_id: uuid.UUID,
        reference_type: str,
        reference_id: uuid.UUID,
        reference_code: str,
    ) -> Any: ...


def resolve_quality_port() -> QualityPort | None:
    """The quality module's in-process inspection factory, if it is deployed.

    Only the *absence* of the module is tolerated. A failure inside it is not
    swallowed: an inspection that could not be raised must roll the receipt back
    with everything else, rather than leaving stock on QC hold with nobody told
    to look at it.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    factory = getattr(quality_service, "create_in_process_inspection", None)
    return factory if callable(factory) else None


class InspectionReaderPort(Protocol):
    """The quality module's batched read of the inspections documents raised."""

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        references: Sequence[tuple[str, uuid.UUID]],
    ) -> Sequence[Any]: ...


def resolve_inspection_reader_port() -> InspectionReaderPort | None:
    """The quality module's ``inspections_for_references``, if it is deployed.

    Read to learn whether every inspection over a lot is settled before the lot
    is released, and by the owner-side verdict endpoint before it releases
    anything itself - one statement for every lot of an order.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    reader = getattr(quality_service, "inspections_for_references", None)
    return reader if callable(reader) else None


def held_output_by_item(lines: Iterable[tuple[uuid.UUID, Decimal]]) -> dict[uuid.UUID, Decimal]:
    """Good output per output item in one returned lot, in the order first named.

    The lot is inspected one *item* at a time: each item gets its own inspection
    for exactly the quantity of it on hold, so deciding one moves that item's
    stock and nothing else. Lines that returned the same item share one.
    """
    totals: dict[uuid.UUID, Decimal] = {}
    for item_id, quantity in lines:
        if quantity <= ZERO:
            continue
        totals[item_id] = to_quantity(totals.get(item_id, ZERO) + quantity)
    return totals


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------


def order_detail(order: JobWorkOrder, lines: Sequence[JobWorkOrderLine]) -> JobWorkOrderDetail:
    detail = JobWorkOrderDetail.model_validate(order)
    detail.lines = [JobWorkOrderLineRead.model_validate(row) for row in lines]
    return detail


def challan_detail(challan: JobWorkChallan, lines: Sequence[JobWorkChallanLine]) -> ChallanRead:
    detail = ChallanRead.model_validate(challan)
    detail.lines = [ChallanLineRead.model_validate(row) for row in lines]
    return detail


def receipt_detail(receipt: JobWorkReceipt, lines: Sequence[JobWorkReceiptLine]) -> ReceiptRead:
    detail = ReceiptRead.model_validate(receipt)
    detail.lines = [ReceiptLineRead.model_validate(row) for row in lines]
    return detail


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    order: JobWorkOrder,
    target: JobWorkOrderStatus,
    *,
    reason: str,
    repo: JobWorkOrderRepository,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> JobWorkOrder:
    """Move an order's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``job_work_orders.status`` and exactly one place that writes the
    matching audit row (master rules section 73).
    """
    current = JobWorkOrderStatus(order.status)
    JOB_WORK_ORDER_LIFECYCLE.assert_transition(current, target)
    order.status = target
    repo.touch(order)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_JOB_WORK_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return order


async def _assert_warehouses(session: AsyncSession, tenant: TenantContext, *ids: uuid.UUID) -> None:
    """Both warehouses in one statement, and both must be able to hold stock."""
    found = await WarehouseLookupRepository(session, tenant).load(list(ids))
    for warehouse_id in ids:
        row = found.get(warehouse_id)
        if row is None:
            raise NotFoundError("Warehouse", warehouse_id)
        code, is_active = row
        if not is_active:
            raise BusinessRuleError(
                inactive_record("Warehouse", code, "cannot be used by a job work order"),
                details={"warehouse_id": str(warehouse_id), "warehouse_code": code},
            )


async def _assert_vendor(session: AsyncSession, tenant: TenantContext, vendor_id: uuid.UUID) -> str:
    """The processor must exist, be active and be approved.

    Approval is checked here rather than only at issue time: an order raised
    against an unapproved vendor is a commitment somebody will act on, and
    finding out on despatch morning is too late.
    """
    vendor = await VendorLookupRepository(session, tenant).load(vendor_id)
    if vendor is None:
        raise NotFoundError("Vendor", vendor_id)
    if not vendor.is_active:
        raise BusinessRuleError(
            inactive_record("Vendor", vendor.code, "cannot be sent job work"),
            details={"vendor_id": str(vendor_id), "vendor_code": vendor.code},
        )
    if not vendor.is_approved:
        raise BusinessRuleError(
            f"Vendor {vendor.code} is not approved; company material cannot be sent to them.",
            code=ErrorCode.APPROVAL_REQUIRED,
            details={"vendor_id": str(vendor_id), "vendor_code": vendor.code},
        )
    return vendor.code


async def _resolve_lines(
    session: AsyncSession,
    tenant: TenantContext,
    payload_lines: Sequence[JobWorkOrderLineCreate],
) -> list[tuple[JobWorkOrderLineCreate, JobWorkItem, JobWorkItem]]:
    """Validate a line set against the item master in one batched lookup.

    Returns each payload line with its input and output item facts, so the
    caller can build rows without a second round of queries (section 32).
    """
    if len(payload_lines) > MAX_LINES_PER_ORDER:
        raise BusinessRuleError(
            f"A job work order carries at most {MAX_LINES_PER_ORDER} lines; "
            f"{len(payload_lines)} were supplied.",
            details={"line_count": len(payload_lines), "maximum": MAX_LINES_PER_ORDER},
        )

    wanted: list[uuid.UUID] = []
    for line in payload_lines:
        wanted.append(line.item_id)
        wanted.append(line.expected_output_item_id or line.item_id)

    items = await ItemLookupRepository(session, tenant).load(wanted)
    batches = await StockBatchRepository(session, tenant).load(
        [line.batch_id for line in payload_lines if line.batch_id is not None]
    )

    resolved: list[tuple[JobWorkOrderLineCreate, JobWorkItem, JobWorkItem]] = []
    for line in payload_lines:
        source = items.get(line.item_id)
        if source is None:
            raise NotFoundError("Item", line.item_id)
        if not source.is_active:
            raise BusinessRuleError(
                inactive_record("Item", source.code, "cannot be sent for job work"),
                details={"item_code": source.code, "line_number": line.line_number},
            )
        # The capability flag, not the item group - database.md section 3 is
        # explicit that no rule may read the group to decide what is permitted.
        if not source.can_job_work:
            raise BusinessRuleError(
                f"{source.code} is not flagged as usable for job work.",
                details={"item_code": source.code, "line_number": line.line_number},
            )
        if source.is_serial_tracked and (
            line.quantity_to_issue != line.quantity_to_issue.to_integral_value()
            or (
                line.expected_output_quantity is not None
                and line.expected_output_quantity != line.quantity_to_issue
            )
        ):
            raise BusinessRuleError(
                "Serial job work requires whole units and one output per input."
            )
        if source.is_batch_tracked:
            if line.batch_id is None:
                raise BusinessRuleError(
                    f"{source.code} is batch tracked; select the lot to reserve for job work.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"item_code": source.code, "line_number": line.line_number},
                )
            batch = batches.get(line.batch_id)
            if batch is None:
                raise NotFoundError("Stock batch", line.batch_id)
            if batch.item_id != source.id:
                raise BusinessRuleError(
                    f"Batch {batch.batch_number} belongs to another item.",
                    details={
                        "batch_id": str(batch.id),
                        "item_id": str(source.id),
                        "batch_item_id": str(batch.item_id),
                    },
                )
        elif line.batch_id is not None:
            raise BusinessRuleError(
                f"{source.code} is not batch tracked; remove the batch selection.",
                details={"item_code": source.code, "line_number": line.line_number},
            )

        output_id = line.expected_output_item_id or line.item_id
        output = items.get(output_id)
        if output is None:
            raise NotFoundError("Item", output_id)
        if not output.is_active:
            raise BusinessRuleError(
                inactive_record("Item", output.code, "cannot receive processed stock"),
                details={"item_code": output.code, "line_number": line.line_number},
            )
        # ASSUMPTION: a lot identity survives same-item processing, but cannot
        # be transferred to a different SKU without an explicit lineage record.
        if (
            output.id != source.id
            and (
                source.is_batch_tracked
                or source.is_serial_tracked
                or output.is_batch_tracked
                or output.is_serial_tracked
            )
            and not (
                source.is_serial_tracked
                and output.is_serial_tracked
                and not source.is_batch_tracked
                and not output.is_batch_tracked
            )
        ):
            raise BusinessRuleError(
                f"{source.code} cannot be converted to {output.code} in a tracked job-work "
                "order until the input-to-output identity lineage is defined.",
                details={
                    "input_item_code": source.code,
                    "output_item_code": output.code,
                    "line_number": line.line_number,
                },
            )

        resolved.append((line, source, output))
    return resolved


def _persist_lines(
    repo: JobWorkOrderLineRepository,
    order: JobWorkOrder,
    resolved: Sequence[tuple[JobWorkOrderLineCreate, JobWorkItem, JobWorkItem]],
) -> list[JobWorkOrderLine]:
    return [
        repo.add(
            JobWorkOrderLine(
                job_work_order_id=order.id,
                line_number=line.line_number,
                item_id=source.id,
                is_batch_tracked=source.is_batch_tracked,
                is_serial_tracked=source.is_serial_tracked,
                batch_id=line.batch_id,
                uom_id=source.base_uom_id,
                quantity_to_issue=to_quantity(line.quantity_to_issue),
                expected_output_item_id=output.id,
                expected_output_quantity=to_quantity(
                    line.expected_output_quantity
                    if line.expected_output_quantity is not None
                    else line.quantity_to_issue
                ),
                expected_loss_percentage=Decimal(line.expected_loss_percentage),
                notes=line.notes,
            )
        )
        for line, source, output in resolved
    ]


def _expected_output_total(
    resolved: Sequence[tuple[JobWorkOrderLineCreate, JobWorkItem, JobWorkItem]],
) -> Decimal:
    return to_quantity(
        sum(
            (
                to_quantity(
                    line.expected_output_quantity
                    if line.expected_output_quantity is not None
                    else line.quantity_to_issue
                )
                for line, _, _ in resolved
            ),
            start=ZERO,
        )
    )


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: JobWorkOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[JobWorkOrder], OffsetPagination]:
    return await JobWorkOrderRepository(session, tenant).list_page_filtered(filters, params, sort)


async def get_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> JobWorkOrderDetail:
    order = await JobWorkOrderRepository(session, tenant).get_or_404(order_id)
    lines = await JobWorkOrderLineRepository(session, tenant).list_for_order(order.id)
    return order_detail(order, lines)


async def list_challans(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[ChallanRead]:
    """Every challan raised for one order, with its lines.

    The header is fetched first so a missing order 404s rather than returning an
    empty list that looks like an order nothing was ever sent against.
    """
    await JobWorkOrderRepository(session, tenant).get_or_404(order_id)
    challans = await JobWorkChallanRepository(session, tenant).list_for_order(order_id)
    if not challans:
        return []

    grouped = await JobWorkChallanLineRepository(session, tenant).list_for_challans(
        [challan.id for challan in challans]
    )
    return [challan_detail(challan, grouped.get(challan.id, [])) for challan in challans]


async def list_challan_register(
    session: AsyncSession,
    tenant: TenantContext,
    filters: ChallanFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[ChallanRead], OffsetPagination]:
    """Company-wide delivery-challan register, newest despatch first (E7b/P10.M3).

    The per-order ``list_challans`` answers "what travelled under this order";
    this answers "what left the factory, for whom, and when" - the register the
    GST delivery challan is filed from and the ITC-04 return would be built on.
    Lines are omitted: a register row names the consignment, and the full
    document is one ``list_challans`` call away.
    """
    rows, pagination = await JobWorkChallanRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [ChallanRead.model_validate(row) for row in rows], pagination


async def vendor_ledger(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> VendorLedgerReport:
    """One job-work vendor's material account for a period (E7b/P10.M10).

    Per input item: what was already at the vendor when the period opened, what
    the challans sent, what the receipts accounted back (good output, unworked
    returns, process loss), and what is still out - beside the processing
    charges the period accrued, what invoices have billed of those lines, and
    the excess-loss value debited. The reconcilable identity on every row is
    ``closing = opening + issued - consumed``; ``consumed`` is the input drawn
    out of the job-work pool, not the output received, because the two are
    different items whenever the process changes what the thing is.
    """
    _assert_period(date_from, date_to)
    vendor = await VendorLookupRepository(session, tenant).load(vendor_id)
    if vendor is None:
        raise NotFoundError("Vendor", vendor_id)

    issued = await JobWorkChallanRepository(session, tenant).issued_by_item(
        vendor_id, date_from, date_to
    )
    consumed = await JobWorkReceiptRepository(session, tenant).consumed_by_item(
        vendor_id, date_from, date_to
    )
    item_ids = sorted(set(issued) | set(consumed))
    codes = await ItemLookupRepository(session, tenant).load(item_ids)

    rows: list[VendorLedgerItemRow] = []
    for item_id in item_ids:
        issued_before, issued_period = issued.get(item_id, (ZERO, ZERO))
        parts = consumed.get(
            item_id,
            {
                "consumed_before": ZERO,
                "consumed_period": ZERO,
                "output_period": ZERO,
                "returned_period": ZERO,
                "scrap_period": ZERO,
                "charges_period": ZERO,
                "billed_period": ZERO,
                "debits_period": ZERO,
            },
        )
        opening = to_quantity(issued_before - parts["consumed_before"])
        consumed_period = to_quantity(parts["consumed_period"])
        rows.append(
            VendorLedgerItemRow(
                item_id=item_id,
                item_code=codes[item_id].code if item_id in codes else "",
                opening_quantity=opening,
                issued_quantity=to_quantity(issued_period),
                input_consumed_quantity=consumed_period,
                received_output_quantity=to_quantity(parts["output_period"]),
                returned_quantity=to_quantity(parts["returned_period"]),
                scrap_quantity=to_quantity(parts["scrap_period"]),
                closing_quantity=to_quantity(opening + issued_period - consumed_period),
                charges_accrued=to_money(parts["charges_period"]),
                charges_billed=to_money(parts["billed_period"]),
                vendor_debits=to_money(parts["debits_period"]),
            )
        )
    return VendorLedgerReport(vendor_id=vendor_id, date_from=date_from, date_to=date_to, items=rows)


async def list_receipts(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[ReceiptRead]:
    """Every lot received against one order, with its lines."""
    await JobWorkOrderRepository(session, tenant).get_or_404(order_id)
    receipts = await JobWorkReceiptRepository(session, tenant).list_for_order(order_id)
    if not receipts:
        return []

    grouped = await JobWorkReceiptLineRepository(session, tenant).list_for_receipts(
        [receipt.id for receipt in receipts]
    )
    return [receipt_detail(receipt, grouped.get(receipt.id, [])) for receipt in receipts]


async def billable_lines(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[BillableLine]:
    """The receipt lines of one order a job-work invoice may still bill.

    Only lines with unbilled charges are listed, in receipt and line order. A
    DRAFT order has no receipts and a CANCELLED one is dead, so neither has
    anything billable. The order's rate, basis, tax rate and SAC ride along
    because the dialog prefills from them and the match compares against them.
    """
    order = await JobWorkOrderRepository(session, tenant).get_or_404(order_id)
    if JobWorkOrderStatus(order.status) not in ISSUED_OR_LATER_STATUSES:
        raise BusinessRuleError(
            f"{order.code} is {order.status}; only an issued order with receipts can be billed.",
            details={"order_id": str(order.id), "status": str(order.status)},
        )
    receipts = await JobWorkReceiptRepository(session, tenant).list_for_order(order.id)
    if not receipts:
        return []
    grouped = await JobWorkReceiptLineRepository(session, tenant).list_for_receipts(
        [receipt.id for receipt in receipts]
    )

    rows: list[BillableLine] = []
    for receipt in receipts:
        for line in grouped.get(receipt.id, []):
            unbilled = to_money(Decimal(line.charges) - Decimal(line.invoiced_amount))
            if unbilled <= ZERO:
                continue
            rows.append(
                BillableLine(
                    job_work_receipt_line_id=line.id,
                    job_work_receipt_id=receipt.id,
                    receipt_code=receipt.code,
                    receipt_date=receipt.receipt_date,
                    line_number=line.line_number,
                    output_item_id=line.output_item_id,
                    output_quantity=line.output_quantity,
                    charges=line.charges,
                    invoiced_amount=line.invoiced_amount,
                    unbilled_amount=unbilled,
                    vendor_debit_amount=line.vendor_debit_amount,
                    pending_vendor_debit=to_money(
                        Decimal(line.vendor_debit_amount) - Decimal(line.debit_posted_amount)
                    ),
                    rate_per_unit=order.rate_per_unit,
                    rate_basis=RateBasis(order.rate_basis),
                    tax_rate=order.tax_rate,
                    sac_code=order.sac_code,
                )
            )
    return rows


async def pending_at_vendor(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    overdue_only: bool = False,
    limit: int = PENDING_AT_VENDOR_LIMIT,
) -> list[PendingAtVendorRow]:
    """Company material currently at a vendor, by vendor and item, with ageing.

    The feed this module exists to make possible. Every row is stock the company
    still owns, still values and can still be asked to account for by an auditor
    - it is simply at somebody else's premises and not available to promise.

    ``limit`` is capped rather than trusted: this returns a plain list, so the
    bound has to live here (section 31).
    """
    bounded = max(1, min(int(limit), PENDING_AT_VENDOR_LIMIT))
    return await JobWorkOrderRepository(session, tenant).pending_at_vendor(
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        overdue_only=overdue_only,
        limit=bounded,
    )


async def list_pending_at_vendor(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID | None,
    item_id: uuid.UUID | None,
    warehouse_id: uuid.UUID | None,
    overdue_only: bool,
    params: PageParams,
) -> tuple[list[PendingAtVendorRow], OffsetPagination]:
    """Paginated form of the same feed, for the HTTP surface."""
    return await JobWorkOrderRepository(session, tenant).pending_at_vendor_page(
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        overdue_only=overdue_only,
        params=params,
    )


async def reconciliation(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ReconciliationReport:
    """Issued vs received vs loss for one order.

    The report a plant manager reads before paying a job worker and an auditor
    reads to satisfy themselves that stock sent out was accounted for. It puts
    the module's own outstanding figure beside ``inventory_balances``'s
    ``job_work_quantity`` for the same items and warehouse, because a divergence
    between the two means either a movement was posted outside this module or a
    receipt was recorded without one - and neither is visible any other way.
    """
    order = await JobWorkOrderRepository(session, tenant).get_or_404(order_id)
    lines = await JobWorkOrderLineRepository(session, tenant).list_for_order(order.id)

    report_lines: list[ReconciliationLine] = []
    total_issued = total_received = total_output = ZERO
    total_rejected = total_scrap = total_excess = ZERO

    for line in lines:
        issued = to_quantity(Decimal(line.issued_quantity))
        received = to_quantity(Decimal(line.received_quantity))
        outstanding = to_quantity(issued - received)
        ratio = yield_ratio(Decimal(line.quantity_to_issue), Decimal(line.expected_output_quantity))
        scrap = to_quantity(Decimal(line.scrap_quantity))
        allowance = to_quantity(
            received * to_quantity(Decimal(line.expected_loss_percentage)) / Decimal("100")
        )

        report_lines.append(
            ReconciliationLine(
                line_number=line.line_number,
                item_id=line.item_id,
                output_item_id=line.expected_output_item_id,
                quantity_to_issue=to_quantity(Decimal(line.quantity_to_issue)),
                issued_quantity=issued,
                received_quantity=received,
                outstanding_quantity=outstanding,
                expected_output_quantity=to_quantity(Decimal(line.expected_output_quantity)),
                received_output_quantity=to_quantity(Decimal(line.received_output_quantity)),
                rejected_quantity=to_quantity(Decimal(line.rejected_quantity)),
                scrap_quantity=scrap,
                loss_percentage=loss_percentage(scrap_quantity=scrap, input_consumed=received),
                expected_loss_percentage=to_quantity(Decimal(line.expected_loss_percentage)),
                excess_loss_quantity=to_quantity(max(ZERO, scrap - allowance)),
                # What the material still at the vendor is expected to come back
                # as. The number a planner nets against demand.
                output_shortfall_quantity=to_quantity(outstanding * ratio),
            )
        )

        total_issued += issued
        total_received += received
        total_output += to_quantity(Decimal(line.received_output_quantity))
        total_rejected += to_quantity(Decimal(line.rejected_quantity))
        total_scrap += scrap
        total_excess += to_quantity(max(ZERO, scrap - allowance))

    pool = await JobWorkBalanceRepository(session, tenant).job_work_quantity(
        [line.item_id for line in lines], order.warehouse_id
    )

    return ReconciliationReport(
        order_id=order.id,
        code=order.code,
        vendor_id=order.vendor_id,
        status=JobWorkOrderStatus(order.status),
        issued_at=order.issued_at,
        expected_return_date=order.expected_return_date,
        total_issued=to_quantity(total_issued),
        total_received=to_quantity(total_received),
        total_outstanding=to_quantity(total_issued - total_received),
        total_output_received=to_quantity(total_output),
        total_rejected=to_quantity(total_rejected),
        total_scrap=to_quantity(total_scrap),
        total_excess_loss=to_quantity(total_excess),
        total_charges=to_money(Decimal(order.total_charges)),
        vendor_debit_amount=to_money(Decimal(order.vendor_debit_amount)),
        stock_in_job_work_pool=to_quantity(pool),
        lines=report_lines,
    )


async def job_work_charges_for_period(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    date_from: date,
    date_to: date,
    vendor_ids: Sequence[uuid.UUID] | None = None,
    limit: int = CHARGES_VENDOR_LIMIT,
) -> list[VendorChargeRow]:
    """Per-vendor job work charges for a period - the contract finance consumes.

    Charges accrue on receipt, so the period is applied to the receipt date: a
    vendor earns the fee when the processed goods come back, not when the raw
    material went out. ``net_charges`` is gross processing charges less the value
    of company material the vendor lost beyond the agreed allowance, which is
    what is actually payable.

    ``limit`` is capped rather than trusted - the result is a plain list.
    """
    _assert_period(date_from, date_to)
    bounded = max(1, min(int(limit), CHARGES_VENDOR_LIMIT))
    return await JobWorkReceiptRepository(session, tenant).charges_for_period(
        date_from=date_from, date_to=date_to, vendor_ids=vendor_ids, limit=bounded
    )


async def production_order_charges(
    session: AsyncSession, tenant: TenantContext, production_order_id: uuid.UUID
) -> Decimal:
    """Accrued job-work receipt charges for a production order.

    This narrow read seam keeps production from reaching into job-work tables.
    Unreceived estimated charges are not actual cost and are not included.
    """
    return await JobWorkOrderRepository(session, tenant).charges_for_production_order(
        production_order_id
    )


async def list_charges_page(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    date_from: date,
    date_to: date,
    vendor_ids: Sequence[uuid.UUID] | None,
    params: PageParams,
) -> tuple[list[VendorChargeRow], OffsetPagination]:
    """Paginated form of the same figures, for the HTTP surface.

    These are the numbers finance posts to the vendor ledger, so an extract cut
    short with no sign of it is worse than one refused: the endpoint pages
    rather than caps, and ``has_next`` says when there is more to post.
    """
    _assert_period(date_from, date_to)
    return await JobWorkReceiptRepository(session, tenant).charges_page(
        date_from=date_from, date_to=date_to, vendor_ids=vendor_ids, params=params
    )


def _assert_period(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise ValidationError(
            date_before("period end", "period start"),
            details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )


# -----------------------------------------------------------------------------
# Create / update / delete
# -----------------------------------------------------------------------------


async def create_order(
    session: AsyncSession, tenant: TenantContext, payload: JobWorkOrderCreate
) -> tuple[JobWorkOrder, list[JobWorkOrderLine]]:
    """Raise a DRAFT job work order.

    No stock is reserved and none moves: a draft is a plan, and holding material
    against it would let an unapproved order starve an approved one.
    """
    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)

    async with transaction(session):
        vendor_code = await _assert_vendor(session, tenant, payload.vendor_id)
        return_warehouse_id = payload.return_warehouse_id or payload.warehouse_id
        await _assert_warehouses(session, tenant, payload.warehouse_id, return_warehouse_id)
        resolved = await _resolve_lines(session, tenant, payload.lines)

        if payload.expected_return_date < today():
            raise ValidationError(
                "The expected return date is in the past.",
                details={"expected_return_date": payload.expected_return_date.isoformat()},
            )
        # ASSUMPTION: explicit or absent. A job-work order does not inherit the
        # cost centre of the production order it may cover - ``source_id`` is a
        # bare polymorphic id this module never reads. docs/assumptions.md §42.
        if payload.cost_centre_id is not None:
            await require_cost_centre(session, tenant, payload.cost_centre_id)

        code = await next_document_number(session, tenant.company_id, DocTypes.JOB_WORK_ORDER)
        order = repo.add(
            JobWorkOrder(
                code=code,
                vendor_id=payload.vendor_id,
                process_type=payload.process_type,
                process_description=payload.process_description,
                expected_return_date=payload.expected_return_date,
                warehouse_id=payload.warehouse_id,
                return_warehouse_id=return_warehouse_id,
                cost_centre_id=payload.cost_centre_id,
                rate_per_unit=Decimal(payload.rate_per_unit),
                rate_basis=payload.rate_basis,
                estimated_charges=estimate_charges(
                    rate_per_unit=payload.rate_per_unit,
                    rate_basis=payload.rate_basis,
                    expected_output_quantity=_expected_output_total(resolved),
                ),
                # A payload that omits the rate or the SAC takes the documented
                # defaults; an explicit value is the client's own. NULL stays
                # NULL on an old row - it means "never named", not zero.
                # docs/assumptions.md §B3-3.
                tax_rate=(
                    Decimal(payload.tax_rate)
                    if payload.tax_rate is not None
                    else JOB_WORK_DEFAULT_GST_RATE
                ),
                sac_code=(
                    payload.sac_code if payload.sac_code is not None else JOB_WORK_DEFAULT_SAC
                ),
                status=JobWorkOrderStatus.DRAFT,
                requires_qc=payload.requires_qc,
                source_type=payload.source_type,
                source_id=payload.source_id,
                notes=payload.notes,
            )
        )
        await session.flush()

        lines = _persist_lines(line_repo, order, resolved)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_JOB_WORK_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Created {order.code}: {order.process_type} at {vendor_code}, {len(lines)} line(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "vendor_code": vendor_code,
                "line_count": len(lines),
            },
        )

    return order, lines


async def update_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: JobWorkOrderUpdate,
) -> tuple[JobWorkOrder, list[JobWorkOrderLine]]:
    """Amend a draft.

    Only a DRAFT is editable: from APPROVED the order holds hard reservations
    and from ISSUED it holds company material at somebody else's premises.
    Supplying ``lines`` replaces the whole set, so the estimated charge is always
    computed from the line set that is actually stored.
    """
    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.get_or_404(order_id)
        if JobWorkOrderStatus(order.status) is not JobWorkOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only a draft job work order may be edited.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        before = snapshot(order, ORDER_AUDITED_FIELDS)
        fields = payload.model_dump(exclude_unset=True)
        fields.pop("lines", None)

        warehouse_id = fields.get("warehouse_id", order.warehouse_id)
        return_warehouse_id = fields.get("return_warehouse_id", order.return_warehouse_id)
        if {"warehouse_id", "return_warehouse_id"} & fields.keys():
            await _assert_warehouses(session, tenant, warehouse_id, return_warehouse_id)
        # Only a new cost centre is validated: a draft re-saving the one it
        # already carries keeps its reference even after deactivation.
        new_centre = fields.get("cost_centre_id")
        if new_centre is not None and new_centre != order.cost_centre_id:
            await require_cost_centre(session, tenant, new_centre)

        for field, value in fields.items():
            # An explicit null clears a column that is nullable and is ignored
            # for one that is not: PATCH cannot be allowed to write NULL into a
            # NOT NULL column and fail with an integrity error naming a
            # constraint rather than the field.
            if value is None and field not in CLEARABLE_UPDATE_FIELDS:
                continue
            setattr(order, field, value)

        lines: list[JobWorkOrderLine]
        if payload.lines is not None:
            resolved = await _resolve_lines(session, tenant, payload.lines)
            await line_repo.delete_for_order(order.id)
            lines = _persist_lines(line_repo, order, resolved)
            expected_output = _expected_output_total(resolved)
        else:
            lines = await line_repo.list_for_order(order.id)
            expected_output = to_quantity(
                sum(
                    (to_quantity(Decimal(line.expected_output_quantity)) for line in lines),
                    start=ZERO,
                )
            )

        order.estimated_charges = estimate_charges(
            rate_per_unit=Decimal(order.rate_per_unit),
            rate_basis=RateBasis(order.rate_basis),
            expected_output_quantity=expected_output,
        )
        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_JOB_WORK_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated draft {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={"line_count": len(lines), "lines_replaced": payload.lines is not None},
        )

    return order, lines


async def delete_draft_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a draft that was never committed.

    A hard delete, and only from DRAFT: nothing references an unapproved order,
    no stock was reserved against it and no challan names it. Anything past
    DRAFT is cancelled instead, so the trail survives (section 28).
    """
    repo = JobWorkOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.get_or_404(order_id)
        if JobWorkOrderStatus(order.status) is not JobWorkOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status} and must be cancelled rather than deleted, "
                "so the record of it survives.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_JOB_WORK_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Deleted draft {order.code}",
            user_id=tenant.user_id,
            old_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={"reason": reason},
        )
        # Lines go with it through ON DELETE CASCADE.
        await repo.hard_delete(order)


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


async def _release_reservations(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[JobWorkOrderLine],
    *,
    port: AllocationPort,
    reason: str,
) -> int:
    """Hand every held reservation back before any movement is posted.

    The order matters and is not a style choice. ``reserved_quantity`` has to
    come down before ``on_hand_quantity`` does, or the movement engine would see
    a position whose reservation exceeds the stock it is about to reduce and
    would correctly refuse it. Both happen inside the issuing transaction, so no
    other document can slip into the gap.

    The iteration order is fixed - by item, then line - across every caller, so
    two orders sharing an item cannot take the same balance locks in opposite
    orders.
    """
    released = 0
    for line in sorted(lines, key=lambda row: (row.item_id, row.line_number)):
        if line.reservation_id is None:
            continue
        await port.release(session, tenant, reservation_id=line.reservation_id, reason=reason)
        line.reservation_id = None
        released += 1
    await session.flush()
    return released


async def approve_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> tuple[JobWorkOrder, list[JobWorkOrderLine]]:
    """Sign off a draft and reserve every unit it will send out.

    Reserving is what makes approval mean something: from here the material
    belongs to this order and no sales order or production order can be promised
    it. A line that is short refuses the whole approval rather than reserving
    what it can - half a consignment at a plating shop still costs a full setup
    and cannot be finished either way, so the shortage is better surfaced before
    a vendor has been told to expect it.
    """
    port = _allocation(allocation)
    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)
        JOB_WORK_ORDER_LIFECYCLE.assert_transition(
            JobWorkOrderStatus(order.status), JobWorkOrderStatus.APPROVED
        )
        await _assert_vendor(session, tenant, order.vendor_id)

        lines = await line_repo.list_for_order(order.id)
        if not lines:
            raise BusinessRuleError(
                f"{order.code} has no lines; there is nothing to send.",
                details={"order_id": str(order.id)},
            )

        # Fixed reservation order across every caller of this path - see
        # _release_reservations.
        for line in sorted(lines, key=lambda row: (row.item_id, row.line_number)):
            if line.reservation_id is not None:
                continue
            reservation = await port.reserve(
                session,
                tenant,
                item_id=line.item_id,
                warehouse_id=order.warehouse_id,
                batch_id=line.batch_id,
                quantity=to_quantity(Decimal(line.quantity_to_issue)),
                demand_type=DemandType.JOB_WORK_ORDER,
                demand_id=order.id,
                demand_line_id=line.id,
                demand_code=order.code,
                reservation_type=ReservationType.HARD,
                reason=f"{order.code} approved for job work: {reason}"[:255],
            )
            line.reservation_id = reservation.id
            line_repo.touch(line)
        await session.flush()

        order.approved_by_id = tenant.user_id
        order.approved_at = utcnow()
        await _transition(
            session,
            tenant,
            order,
            JobWorkOrderStatus.APPROVED,
            reason=reason,
            repo=repo,
            action=AuditAction.APPROVE,
        )

    return order, lines


async def cancel_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> JobWorkOrder:
    """Cancel an order and give every reserved unit back.

    Releasing inside the same transaction as the status change is the point: a
    cancelled order still holding reservations makes material invisible to every
    other document while belonging to nobody. Only reachable before the material
    has left - see :data:`JOB_WORK_ORDER_LIFECYCLE`.
    """
    port = _allocation(allocation)
    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)
        JOB_WORK_ORDER_LIFECYCLE.assert_transition(
            JobWorkOrderStatus(order.status), JobWorkOrderStatus.CANCELLED
        )

        lines = await line_repo.list_for_order(order.id)
        released = await _release_reservations(
            session, tenant, lines, port=port, reason=f"{order.code} cancelled: {reason}"[:255]
        )

        order.cancellation_reason = reason
        await _transition(
            session,
            tenant,
            order,
            JobWorkOrderStatus.CANCELLED,
            reason=reason,
            repo=repo,
            action=AuditAction.CANCEL,
        )

        if released:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.RESERVATION_CHANGE,
                entity_type=AUDIT_JOB_WORK_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Released {released} reservation(s) held by {order.code}",
                user_id=tenant.user_id,
                new_values={"released_reservations": released},
                context={"reason": reason},
            )
    return order


async def issue_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: IssueRequest,
    *,
    allocation: AllocationPort | None = None,
) -> IssueResult:
    """Send the material out: raise the challan and post the stock.

    One transaction from end to end: reservations released, every
    ``JOB_WORK_ISSUE`` posted in a single batch, the statutory challan written
    from the costs those movements actually used, the line quantities and the
    header stamped, and the audit entries recorded. Either all of it commits or
    none of it does - there is no path that puts goods on a vehicle without a
    challan, or writes a challan for stock that never moved.

    **The movement type is the whole point of this function.** ``JOB_WORK_ISSUE``
    moves the quantity from ``on_hand_quantity`` into ``job_work_quantity``: the
    company still owns it, still values it and can still be asked to account for
    it. Anything that removed it from the books would be wrong.
    """
    port = _allocation(allocation)
    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)
    challan_repo = JobWorkChallanRepository(session, tenant)
    challan_line_repo = JobWorkChallanLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)
        # Checked before anything is released so an order in the wrong state
        # fails without having given a single reservation back.
        JOB_WORK_ORDER_LIFECYCLE.assert_transition(
            JobWorkOrderStatus(order.status), JobWorkOrderStatus.ISSUED
        )

        if payload.transporter_id is not None:
            transporter = await TransporterLookupRepository(session, tenant).load(
                payload.transporter_id
            )
            if transporter is None:
                raise NotFoundError("Transporter", payload.transporter_id)
            if not transporter[1]:
                raise BusinessRuleError(
                    f"Transporter {transporter[0]} is inactive.",
                    details={"transporter_id": str(payload.transporter_id)},
                )

        lines = await line_repo.list_for_order(order.id)
        by_number = {line.line_number: line for line in lines}
        serials_by_number = {entry.line_number: entry.serial_ids for entry in payload.lines}
        selected_serials = [serial for serials in serials_by_number.values() for serial in serials]
        if len(selected_serials) != len(set(selected_serials)):
            raise BusinessRuleError("A serial unit can be issued on only one job-work line.")
        overrides: dict[int, tuple[Decimal, uuid.UUID | None]] = {}
        for entry in payload.lines:
            line = by_number.get(entry.line_number)
            if line is None:
                raise NotFoundError("Job work order line", entry.line_number)
            quantity = to_quantity(entry.issued_quantity)
            if quantity > to_quantity(Decimal(line.quantity_to_issue)):
                raise BusinessRuleError(
                    f"Line {entry.line_number} may issue at most {line.quantity_to_issue}; "
                    "only that much is reserved for it.",
                    details={
                        "line_number": entry.line_number,
                        "ordered": str(line.quantity_to_issue),
                        "requested": str(quantity),
                    },
                )
            batch_id = entry.batch_id if entry.batch_id is not None else line.batch_id
            if line.is_batch_tracked:
                if batch_id is None:
                    raise BusinessRuleError(
                        f"Line {entry.line_number} is batch tracked; name the batch being sent.",
                        code=ErrorCode.BATCH_REQUIRED,
                        details={"line_number": entry.line_number, "item_id": str(line.item_id)},
                    )
                if line.batch_id is not None and batch_id != line.batch_id:
                    raise BusinessRuleError(
                        f"Line {entry.line_number} has already been issued from another batch; "
                        "use a separate job-work line for each batch.",
                        details={
                            "line_number": entry.line_number,
                            "batch_id": str(batch_id),
                            "issued_batch_id": str(line.batch_id),
                        },
                    )
            elif entry.batch_id is not None:
                raise BusinessRuleError(
                    f"Line {entry.line_number} is not batch tracked; remove the batch selection.",
                    details={"line_number": entry.line_number},
                )
            overrides[entry.line_number] = (quantity, batch_id)

        challan_date = payload.challan_date or today()
        expected_return = payload.expected_return_date or order.expected_return_date
        if expected_return < challan_date:
            raise ValidationError(
                "The expected return date is before the challan date.",
                details={
                    "challan_date": challan_date.isoformat(),
                    "expected_return_date": expected_return.isoformat(),
                },
            )

        released = await _release_reservations(
            session, tenant, lines, port=port, reason=f"{order.code} issued: {payload.reason}"[:255]
        )

        movement_reason = f"Job work {order.code} issued to vendor: {payload.reason}"[:255]
        issuing: list[tuple[JobWorkOrderLine, Decimal, uuid.UUID | None]] = []
        movements: list[MovementRequest] = []
        movement_ranges: list[tuple[int, int]] = []
        for line in lines:
            quantity, batch_id = overrides.get(
                line.line_number,
                (to_quantity(Decimal(line.quantity_to_issue)), line.batch_id),
            )
            if quantity <= ZERO:
                continue
            if line.is_batch_tracked and batch_id is None:
                raise BusinessRuleError(
                    f"Line {line.line_number} is batch tracked; name the batch being sent.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"line_number": line.line_number, "item_id": str(line.item_id)},
                )
            issuing.append((line, quantity, batch_id))
            serial_ids = serials_by_number.get(line.line_number, [])
            if line.is_serial_tracked:
                if Decimal(len(serial_ids)) != quantity:
                    raise BusinessRuleError(
                        "Select one serial unit for every issued job-work unit."
                    )
            elif serial_ids:
                raise BusinessRuleError("Remove serial selections from a non-serial job-work line.")
            start = len(movements)
            serial_units: list[uuid.UUID | None] = list(serial_ids) if serial_ids else [None]
            for serial_id in serial_units:
                movements.append(
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=order.warehouse_id,
                        quantity=Decimal("1") if serial_id is not None else quantity,
                        serial_id=serial_id,
                        batch_id=batch_id,
                        # THE line that keeps the material ours. Not DISPATCH, not
                        # PURCHASE_RETURN, not ADJUSTMENT_DECREASE - the stock moves
                        # into job_work_quantity and stays on the company's books.
                        transaction_type=TransactionType.JOB_WORK_ISSUE,
                        reference_type=MOVEMENT_REFERENCE_JOB_WORK,
                        reference_id=order.id,
                        reference_code=order.code,
                        reason=movement_reason,
                        stock_state=StockState.JOB_WORK,
                        occurred_at=payload.occurred_at,
                    )
                )
            movement_ranges.append((start, len(movements)))

        if not movements:
            raise BusinessRuleError(
                f"{order.code} would issue nothing; there is no consignment to send.",
                details={"order_id": str(order.id)},
            )

        posted = await post_movements(session, tenant, movements)

        # The declared values are taken from the ledger rows the issue actually
        # wrote, so the challan agrees with the books rather than with a price
        # list that may have moved since. They are worked out before the header
        # is inserted because a challan for a zero quantity is not a document -
        # the CHECK constraint refuses one, and it is right to.
        declared = [
            (
                line,
                quantity,
                batch_id,
                to_unit_price(
                    sum(
                        (
                            abs(Decimal(txn.quantity)) * Decimal(txn.unit_cost)
                            for txn in posted[start:end]
                        ),
                        ZERO,
                    )
                    / quantity
                ),
                to_money(
                    sum(
                        (
                            abs(Decimal(txn.quantity)) * Decimal(txn.unit_cost)
                            for txn in posted[start:end]
                        ),
                        ZERO,
                    )
                ),
            )
            for (line, quantity, batch_id), (start, end) in zip(
                issuing, movement_ranges, strict=True
            )
        ]
        total_quantity = to_quantity(
            sum((quantity for _, quantity, _, _, _ in declared), start=ZERO)
        )
        total_value = to_money(sum((value for _, _, _, _, value in declared), start=ZERO))

        challan_code = await next_document_number(
            session, tenant.company_id, DocTypes.JOB_WORK_CHALLAN, on=challan_date
        )
        challan = challan_repo.add(
            JobWorkChallan(
                code=challan_code,
                job_work_order_id=order.id,
                vendor_id=order.vendor_id,
                challan_date=challan_date,
                expected_return_date=expected_return,
                vehicle_number=payload.vehicle_number,
                transporter_id=payload.transporter_id,
                total_quantity=total_quantity,
                total_value=total_value,
                notes=payload.challan_notes,
            )
        )
        await session.flush()

        challan_lines: list[JobWorkChallanLine] = []
        for line, quantity, batch_id, unit_value, line_value in declared:
            challan_lines.append(
                challan_line_repo.add(
                    JobWorkChallanLine(
                        challan_id=challan.id,
                        job_work_order_line_id=line.id,
                        line_number=line.line_number,
                        item_id=line.item_id,
                        batch_id=batch_id,
                        uom_id=line.uom_id,
                        quantity=quantity,
                        serial_ids=serials_by_number.get(line.line_number, []),
                        unit_value=unit_value,
                        total_value=line_value,
                    )
                )
            )
            line.issued_quantity = to_quantity(Decimal(line.issued_quantity) + quantity)
            line.serial_ids = serials_by_number.get(line.line_number, [])
            line.batch_id = batch_id
            line_repo.touch(line)

        order.issued_at = payload.occurred_at or utcnow()
        repo.touch(order)
        await session.flush()

        await _transition(
            session, tenant, order, JobWorkOrderStatus.ISSUED, reason=payload.reason, repo=repo
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_JOB_WORK_CHALLAN,
            entity_id=challan.id,
            entity_code=challan.code,
            summary=(
                f"{challan.code} issued {challan.total_quantity} unit(s) of company material "
                f"to the vendor under {order.code}; stock moved to the JOB_WORK state and "
                "remains company owned"
            ),
            user_id=tenant.user_id,
            new_values={
                "job_work_order_code": order.code,
                "challan_date": challan_date,
                "expected_return_date": expected_return,
                "vehicle_number": payload.vehicle_number,
                "transporter_id": payload.transporter_id,
                "total_quantity": challan.total_quantity,
                "total_value": challan.total_value,
                "issued": {
                    str(line.line_number): quantity for line, quantity, _batch_id in issuing
                },
            },
            context={
                "reason": payload.reason,
                "movement_count": len(posted),
                "reservations_released": released,
                "stock_state": str(StockState.JOB_WORK),
            },
        )

    return IssueResult(
        order=order_detail(order, lines),
        challan=challan_detail(challan, challan_lines),
        movement_count=len(posted),
        quantity_at_vendor=to_quantity(
            sum(
                (
                    to_quantity(Decimal(line.issued_quantity) - Decimal(line.received_quantity))
                    for line in lines
                ),
                start=ZERO,
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class PlannedLine:
    """One receipt line: what was asked for, which order line it draws on, and
    the arithmetic :func:`plan_line_receipt` worked out for it."""

    order_line: JobWorkOrderLine
    entry: ReceiptLineIn
    plan: LineReceiptPlan
    batch_id: uuid.UUID | None


def _build_receipt_movements(
    planned: Sequence[PlannedLine],
    *,
    order: JobWorkOrder,
    reason: str,
    occurred_at: datetime | None,
    capitalised_costs: Mapping[int, Decimal] | None = None,
    rejected_costs: Mapping[int, Decimal] | None = None,
    converted_serial_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> tuple[list[MovementRequest], dict[int, int]]:
    """Every ledger row one receipt writes, as one batch.

    These movement shapes each say something different about where a
    quantity went:

    * ``JOB_WORK_RECEIPT`` into ``AVAILABLE`` (or ``QC_HOLD``) at the return
      warehouse - the processed output arriving as its own item, booked at its
      capitalised cost (input at the frozen issue value plus the receipt
      charge) when ``capitalised_costs`` names the line, at the item's
      standard cost otherwise;
    * ``JOB_WORK_RECEIPT`` into ``DAMAGED`` - output the vendor made badly and
      sent back; present and counted, not promisable;
    * ``JOB_WORK_RECEIPT`` from ``JOB_WORK`` into ``AVAILABLE`` at the source
      warehouse - input handed back untouched;
    * ``MATERIAL_ISSUE`` with ``stock_state=JOB_WORK`` - the input the process
      consumed, drawn straight out of the job-work pool without ever becoming
      available again. The closed section-83 vocabulary has no "job work
      consume"; this is the same word production uses for components eaten into
      a different finished item, which is what happened;
    * ``SCRAP`` from ``JOB_WORK`` - process loss or the input share of rejected
      output destroyed at the vendor. Only process loss enters ``scrap_index``
      and the excess-loss recovery calculation.

    They are returned as one list so
    :func:`~src.modules.inventory.service.post_movements` locks every affected
    balance row in primary-key order before changing any of them.

    The second return value maps an order line number to the index of its scrap
    movement in that list. The vendor debit is priced from the cost the ledger
    actually used, and finding that row by counting movements at the call site
    would silently break the moment a movement is added here.
    """
    movements: list[MovementRequest] = []
    scrap_index: dict[int, int] = {}
    output_state = StockState.QC_HOLD if order.requires_qc else StockState.AVAILABLE
    capitalised = capitalised_costs or {}
    rejected = rejected_costs or {}

    for entry in planned:
        line, plan = entry.order_line, entry.plan
        if line.is_serial_tracked:
            unit_movements, unit_scrap_index = receipt_movements(
                entry,
                order=order,
                reason=reason,
                occurred_at=occurred_at,
                capitalised_cost=capitalised.get(line.line_number),
                rejected_cost=rejected.get(line.line_number),
                converted_serial_ids=converted_serial_ids,
            )
            if unit_scrap_index is not None:
                scrap_index[line.line_number] = len(movements) + unit_scrap_index
            movements.extend(unit_movements)
            continue
        common: dict[str, Any] = {
            "reference_type": MOVEMENT_REFERENCE_JOB_WORK,
            "reference_id": order.id,
            "reference_code": order.code,
            "reason": reason,
            "batch_id": entry.batch_id,
            "occurred_at": occurred_at,
        }

        if plan.output_quantity > ZERO:
            movements.append(
                MovementRequest(
                    item_id=line.expected_output_item_id,
                    warehouse_id=order.return_warehouse_id,
                    quantity=plan.output_quantity,
                    transaction_type=TransactionType.JOB_WORK_RECEIPT,
                    stock_state=output_state,
                    # P13.M5: the output carries what it cost - the input it
                    # consumed at the frozen challan issue value plus the
                    # processing charge this lot accrued for it. None keeps the
                    # engine's standard-cost default (no challan history, or a
                    # line older than the costing rule).
                    unit_cost=capitalised.get(line.line_number),
                    **common,
                )
            )
        if (
            plan.rejected_quantity > ZERO
            and entry.entry.rejection_action is RejectionAction.RETURN_TO_STOCK
        ):
            movements.append(
                MovementRequest(
                    item_id=line.expected_output_item_id,
                    warehouse_id=order.return_warehouse_id,
                    quantity=plan.rejected_quantity,
                    transaction_type=TransactionType.JOB_WORK_RECEIPT,
                    # DAMAGED is the rejected pool: on hand, counted in the
                    # valuation, and excluded from available_quantity so nothing
                    # can be promised out of it.
                    stock_state=StockState.DAMAGED,
                    # Returned rejects carry only their share of the frozen
                    # input value. The good output carries the processing fee.
                    unit_cost=rejected.get(line.line_number),
                    **common,
                )
            )
        delivered_input = plan.input_for_output - plan.rejected_scrap_input_quantity
        if delivered_input > ZERO:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=delivered_input,
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    # Draws the job-work pool down without touching on-hand: the
                    # material never comes back as itself, it comes back as the
                    # output item posted above.
                    stock_state=StockState.JOB_WORK,
                    **common,
                )
            )
        if plan.rejected_scrap_input_quantity > ZERO:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=plan.rejected_scrap_input_quantity,
                    transaction_type=TransactionType.SCRAP,
                    from_state=StockState.JOB_WORK,
                    stock_state=StockState.SCRAP,
                    **common,
                )
            )
        if plan.returned_unprocessed_quantity > ZERO:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=plan.returned_unprocessed_quantity,
                    transaction_type=TransactionType.JOB_WORK_RECEIPT,
                    from_state=StockState.JOB_WORK,
                    stock_state=StockState.AVAILABLE,
                    **common,
                )
            )
        if plan.scrap_quantity > ZERO:
            scrap_index[line.line_number] = len(movements)
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=plan.scrap_quantity,
                    transaction_type=TransactionType.SCRAP,
                    from_state=StockState.JOB_WORK,
                    stock_state=StockState.SCRAP,
                    **common,
                )
            )

    return movements, scrap_index


def assert_excess_loss_authorised(
    principal: Principal,
    *,
    order_code: str,
    planned: Sequence[PlannedLine],
    approve_excess_loss: bool,
) -> Decimal:
    """Total the loss beyond the allowance and check who is accepting it.

    Returns the excess as a quantity so the caller can price the vendor debit.
    Zero excess needs nobody's say-so. Any excess refuses the receipt with
    :class:`~src.core.errors.ApprovalRequiredError` until ``approve_excess_loss``
    is set - and the flag is honoured only from a caller holding
    ``JOB_WORK_CLOSE``, because what it accepts is debited to the vendor ledger.
    """
    excess_total = to_quantity(sum((row.plan.excess_loss_quantity for row in planned), start=ZERO))
    if excess_total <= ZERO:
        return excess_total
    if not approve_excess_loss:
        raise ApprovalRequiredError(
            f"{order_code} would write off {excess_total} unit(s) more than the agreed "
            "process loss allows. Approve the excess to post the receipt; the value will "
            "be debited to the vendor.",
            details={
                "order_code": order_code,
                "excess_loss_quantity": str(excess_total),
                "lines": {
                    str(row.order_line.line_number): str(row.plan.excess_loss_quantity)
                    for row in planned
                    if row.plan.excess_loss_quantity > ZERO
                },
            },
        )
    # ASSUMPTION: accepting loss beyond the agreed allowance debits the vendor
    # ledger, so it is the senior JOB_WORK_CLOSE right - the same right that
    # approves the order - not the storekeeper's JOB_WORK_RECEIVE. The receive
    # route is gated on the latter alone, so the flag is checked here rather
    # than trusted. See the module's Assumptions and docs/assumptions.md §4.
    principal.require(Permission.JOB_WORK_CLOSE)
    return excess_total


async def receive(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    payload: ReceiveRequest,
    *,
    quality: QualityPort | None = None,
) -> ReceiptResult:
    """Record one lot returning from the vendor and post everything it moves.

    The operation this module exists for, and it is one transaction from end to
    end: every line planned and checked, every movement posted in a single
    batch, the receipt and its lines written, the order line balances advanced,
    the charge accrued, the status moved, the inspection raised if one is owed,
    and the audit entries recorded.

    Loss inside the line's ``expected_loss_percentage`` is posted as ``SCRAP``
    with the receipt's reason. Loss beyond it refuses the receipt with
    :class:`~src.core.errors.ApprovalRequiredError` until ``approve_excess_loss``
    is set by a ``principal`` holding ``JOB_WORK_CLOSE`` - the caller is the
    approver the receipt records - and the value of the material lost is
    debited to the vendor (:func:`assert_excess_loss_authorised`).
    """
    quality_port = quality if quality is not None else resolve_quality_port()

    repo = JobWorkOrderRepository(session, tenant)
    line_repo = JobWorkOrderLineRepository(session, tenant)
    receipt_repo = JobWorkReceiptRepository(session, tenant)
    receipt_line_repo = JobWorkReceiptLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)
        if JobWorkOrderStatus(order.status) not in (
            JobWorkOrderStatus.ISSUED,
            JobWorkOrderStatus.PARTIALLY_RECEIVED,
        ):
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only an order with material at the vendor "
                "can be received against.",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        lines = await line_repo.list_for_order(order.id)
        by_number = {line.line_number: line for line in lines}

        planned = _plan_receipt(payload.lines, by_number)
        await validate_receipt_units(session, tenant, planned)
        converted_serial_ids = await prepare_conversions(
            session,
            tenant,
            planned,
            order=order,
            occurred_at=payload.occurred_at or utcnow(),
        )
        excess_total = assert_excess_loss_authorised(
            principal,
            order_code=order.code,
            planned=planned,
            approve_excess_loss=payload.approve_excess_loss,
        )

        receipt_date = payload.receipt_date or today()
        movement_reason = f"Job work {order.code} received: {payload.reason}"[:255]

        good_output = to_quantity(sum((row.plan.output_quantity for row in planned), start=ZERO))
        charge = receipt_charge(
            rate_per_unit=Decimal(order.rate_per_unit),
            rate_basis=RateBasis(order.rate_basis),
            output_quantity=good_output,
            lot_already_charged=Decimal(order.total_charges) > ZERO,
        )

        # Everything the header carries is worked out before it is inserted: a
        # receipt row that momentarily says it accounted for nothing is refused
        # by the CHECK constraint, and rightly so - it would be a document with
        # no content.
        totals = dict.fromkeys(("output", "rejected", "returned", "scrap", "consumed"), ZERO)
        for row in planned:
            totals["output"] += row.plan.output_quantity
            totals["rejected"] += row.plan.rejected_quantity
            totals["returned"] += row.plan.returned_unprocessed_quantity
            totals["scrap"] += row.plan.scrap_quantity
            totals["consumed"] += row.plan.input_consumed_quantity

        # The charge is apportioned across the lines that earned it, so a
        # per-line vendor query and the header agree to the last paisa. Round
        # cumulative good-output shares so no rejected-only line earns a charge
        # and accumulated rounding cannot make the final charge negative.
        # This runs before the posting because the
        # capitalised output cost below folds each line's share in.
        allocated_output = ZERO
        allocated_charge = ZERO
        line_charges: list[tuple[PlannedLine, Decimal]] = []
        for row in planned:
            allocated_output += row.plan.output_quantity
            cumulative_charge = to_money(
                charge * allocated_output / good_output if good_output > ZERO else ZERO
            )
            line_charge = cumulative_charge - allocated_charge
            allocated_charge = cumulative_charge
            line_charges.append((row, line_charge))

        # P13.M5: what each line's good output is booked at - the input it
        # consumed, valued at the frozen challan issue cost, plus the charge it
        # earned. Read before posting, from challans that can no longer change,
        # so the price is exact rather than a second read of layers the posting
        # itself is about to consume.
        challan_grouped = await JobWorkChallanRepository(session, tenant).list_for_order_lines(
            [row.order_line.id for row in planned]
        )
        costing: dict[int, CapitalisedCost] = {}
        rejected_costing: dict[int, CapitalisedCost] = {}
        for row, line_charge in line_charges:
            delivered_output = row.plan.output_quantity + row.plan.rejected_quantity
            input_for_output = row.plan.input_for_output
            if row.entry.rejection_action is RejectionAction.SCRAP_AT_VENDOR:
                # A scrap movement must use input quantity scale. The good
                # output carries exactly the remainder drawn as MATERIAL_ISSUE,
                # rather than a second independently rounded input share.
                input_for_output -= row.plan.rejected_scrap_input_quantity
                delivered_output = row.plan.output_quantity
            challan_lines = challan_grouped.get(row.order_line.id, [])
            cost = capitalised_output_cost(
                input_for_output=input_for_output,
                challan_lines=challan_lines,
                line_charge=line_charge,
                output_quantity=row.plan.output_quantity,
                delivered_output_quantity=delivered_output,
            )
            if cost is not None:
                costing[row.order_line.line_number] = cost
            rejected_cost = (
                capitalised_output_cost(
                    input_for_output=row.plan.input_for_output,
                    challan_lines=challan_lines,
                    line_charge=ZERO,
                    output_quantity=row.plan.rejected_quantity,
                    delivered_output_quantity=delivered_output,
                )
                if row.entry.rejection_action is RejectionAction.RETURN_TO_STOCK
                else None
            )
            if rejected_cost is not None:
                rejected_costing[row.order_line.line_number] = rejected_cost

        movements, scrap_index = _build_receipt_movements(
            planned,
            order=order,
            reason=movement_reason,
            occurred_at=payload.occurred_at,
            capitalised_costs={number: cost.unit_cost for number, cost in costing.items()},
            rejected_costs={number: cost.unit_cost for number, cost in rejected_costing.items()},
            converted_serial_ids=converted_serial_ids,
        )
        posted = await post_movements(session, tenant, movements)
        # The debit is priced from the cost the ledger actually used for the
        # scrap, not from a price list - so the vendor is charged what the
        # company was carrying for the material it lost.
        scrap_costs = {
            line_number: Decimal(posted[index].unit_cost)
            for line_number, index in scrap_index.items()
        }

        apportioned: list[tuple[PlannedLine, Decimal, Decimal]] = []
        debit_total = ZERO
        for row, line_charge in line_charges:
            debit = to_money(
                row.plan.excess_loss_quantity * scrap_costs.get(row.order_line.line_number, ZERO)
            )
            debit_total += debit
            apportioned.append((row, line_charge, debit))

        order.receipt_count += 1
        receipt = receipt_repo.add(
            JobWorkReceipt(
                code=f"{order.code}/R{order.receipt_count}"[:80],
                receipt_number=order.receipt_count,
                job_work_order_id=order.id,
                vendor_id=order.vendor_id,
                receipt_date=receipt_date,
                vendor_challan_number=payload.vendor_challan_number,
                vendor_challan_date=payload.vendor_challan_date,
                total_output_quantity=to_quantity(totals["output"]),
                total_rejected_quantity=to_quantity(totals["rejected"]),
                total_returned_unprocessed=to_quantity(totals["returned"]),
                total_scrap_quantity=to_quantity(totals["scrap"]),
                total_input_consumed=to_quantity(totals["consumed"]),
                total_charges=to_money(charge),
                vendor_debit_amount=to_money(debit_total),
                has_excess_loss=excess_total > ZERO,
                excess_loss_approved_by_id=principal.user_id if excess_total > ZERO else None,
                excess_loss_approved_at=utcnow() if excess_total > ZERO else None,
                reason=payload.reason[:255],
                notes=payload.notes,
            )
        )
        await session.flush()

        receipt_lines: list[JobWorkReceiptLine] = []
        for row, line_charge, debit in apportioned:
            line, plan = row.order_line, row.plan
            receipt_lines.append(
                receipt_line_repo.add(
                    JobWorkReceiptLine(
                        receipt_id=receipt.id,
                        job_work_order_line_id=line.id,
                        line_number=line.line_number,
                        output_item_id=line.expected_output_item_id,
                        batch_id=row.batch_id,
                        output_serial_ids=row.entry.output_serial_ids,
                        rejected_serial_ids=row.entry.rejected_serial_ids,
                        returned_unprocessed_serial_ids=row.entry.returned_unprocessed_serial_ids,
                        scrapped_serial_ids=row.entry.scrapped_serial_ids,
                        converted_serial_ids={
                            str(source_id): str(converted_serial_ids[source_id])
                            for source_id in [
                                *row.entry.output_serial_ids,
                                *row.entry.rejected_serial_ids,
                            ]
                            if source_id in converted_serial_ids
                        },
                        output_quantity=plan.output_quantity,
                        rejected_quantity=plan.rejected_quantity,
                        rejection_action=row.entry.rejection_action,
                        returned_unprocessed_quantity=plan.returned_unprocessed_quantity,
                        scrap_quantity=plan.scrap_quantity,
                        excess_loss_quantity=plan.excess_loss_quantity,
                        input_consumed_quantity=plan.input_consumed_quantity,
                        charges=line_charge,
                        vendor_debit_amount=debit,
                        notes=row.entry.notes,
                    )
                )
            )

            line.received_quantity = to_quantity(
                Decimal(line.received_quantity) + plan.input_consumed_quantity
            )
            line.received_output_quantity = to_quantity(
                Decimal(line.received_output_quantity) + plan.output_quantity
            )
            # Rework the vendor is still holding is deliberately not added: it is
            # a transient state, not an outcome, and counting it here would count
            # the same pieces again when they finally come back good.
            line.rejected_quantity = to_quantity(
                Decimal(line.rejected_quantity) + plan.rejected_quantity
            )
            line.scrap_quantity = to_quantity(Decimal(line.scrap_quantity) + plan.scrap_quantity)
            line_repo.touch(line)

        order.total_charges = to_money(Decimal(order.total_charges) + charge)
        order.vendor_debit_amount = to_money(Decimal(order.vendor_debit_amount) + debit_total)

        held_for_qc = order.requires_qc and receipt.total_output_quantity > ZERO
        if held_for_qc:
            receipt.qc_held_at = payload.occurred_at or utcnow()
        repo.touch(order)
        await session.flush()

        inspection_created = False
        for receipt_line in receipt_lines:
            for source_id, output_id in receipt_line.converted_serial_ids.items():
                session.add(
                    StockSerialConversion(
                        company_id=tenant.company_id,
                        created_by_id=persisted_actor(tenant.user_id),
                        source_serial_id=uuid.UUID(source_id),
                        output_serial_id=uuid.UUID(output_id),
                        job_work_receipt_line_id=receipt_line.id,
                    )
                )
        await session.flush()
        if held_for_qc and quality_port is not None:
            # One inspection per output item, each for exactly that item's held
            # quantity. One inspection naming the first item for the whole lot's
            # total could not be decided: it would try to release more of that
            # item than is held, or release another lot held at the same grain.
            held: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
            for receipt_line in receipt_lines:
                if Decimal(receipt_line.output_quantity) > ZERO:
                    key = (receipt_line.output_item_id, receipt_line.batch_id)
                    held[key] = held.get(key, ZERO) + Decimal(receipt_line.output_quantity)
            # One call per output item of the lot - a handful at most, bounded by
            # the order's line count - because an inspection is raised per item.
            for (item_id, batch_id), quantity in held.items():
                serial_ids = [
                    serial_id
                    for receipt_line in receipt_lines
                    if receipt_line.output_item_id == item_id and receipt_line.batch_id == batch_id
                    for serial_id in receipt_line.output_serial_ids
                ]
                serial_ids = [
                    converted_serial_ids.get(serial_id, serial_id) for serial_id in serial_ids
                ]
                inspection = await quality_port(
                    session,
                    tenant,
                    item_id=item_id,
                    quantity=quantity,
                    warehouse_id=order.return_warehouse_id,
                    reference_type=QC_REFERENCE_RECEIPT,
                    reference_id=receipt.id,
                    reference_code=receipt.code,
                    **({"serial_ids": serial_ids} if serial_ids else {}),
                    **({"batch_id": batch_id} if batch_id is not None else {}),
                )
                inspection_id = getattr(inspection, "id", None)
                for receipt_line in receipt_lines:
                    if (
                        receipt_line.output_item_id == item_id
                        and receipt_line.batch_id == batch_id
                        and Decimal(receipt_line.output_quantity) > ZERO
                    ):
                        receipt_line.qc_inspection_id = inspection_id
                if receipt.qc_inspection_id is None:
                    receipt.qc_inspection_id = inspection_id
            inspection_created = receipt.qc_inspection_id is not None
            await session.flush()

        fully_received = all(
            to_quantity(Decimal(row.issued_quantity) - Decimal(row.received_quantity)) <= ZERO
            for row in lines
        )
        target = (
            JobWorkOrderStatus.RECEIVED if fully_received else JobWorkOrderStatus.PARTIALLY_RECEIVED
        )
        if JobWorkOrderStatus(order.status) is not target:
            await _transition(session, tenant, order, target, reason=payload.reason, repo=repo)
        # ASSUMPTION: held for inspection only while a lot is still on hold. Lots
        # inspected and released while the order was still partly at the vendor
        # need nothing more, and an order sent to QC_PENDING with nothing held
        # could never leave it - no decision would ever be taken to release it.
        # Such an order stays RECEIVED and is closed like one that never needed
        # inspecting (docs/assumptions.md §E4-9).
        if (
            fully_received
            and order.requires_qc
            and await receipt_repo.count_unreleased_holds(order.id) > 0
        ):
            await _transition(
                session,
                tenant,
                order,
                JobWorkOrderStatus.QC_PENDING,
                reason="Returned output held for inspection",
                repo=repo,
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_JOB_WORK_RECEIPT,
            entity_id=receipt.id,
            entity_code=receipt.code,
            summary=(
                f"{receipt.code} received {receipt.total_output_quantity} good, "
                f"{receipt.total_rejected_quantity} rejected, "
                f"{receipt.total_scrap_quantity} scrapped against {order.code} "
                f"in {len(posted)} movement(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                "output_quantity": receipt.total_output_quantity,
                "rejected_quantity": receipt.total_rejected_quantity,
                "returned_unprocessed": receipt.total_returned_unprocessed,
                "scrap_quantity": receipt.total_scrap_quantity,
                "input_consumed": receipt.total_input_consumed,
                "charges": receipt.total_charges,
                "vendor_debit_amount": receipt.vendor_debit_amount,
                # P13.M5: what each costed line's output was booked at, and the
                # sub-paisa remainder the six-place price could not hold. A line
                # absent here kept the engine's standard-cost default.
                "costing": {
                    str(number): {
                        "unit_cost": str(cost.unit_cost),
                        "input_value": str(cost.input_value),
                        "rounding_residual": str(cost.rounding_residual),
                    }
                    for number, cost in sorted(costing.items())
                },
                "rejected_costing": {
                    str(number): {
                        "unit_cost": str(cost.unit_cost),
                        "input_value": str(cost.input_value),
                        "rounding_residual": str(cost.rounding_residual),
                    }
                    for number, cost in sorted(rejected_costing.items())
                },
                "rejected_scrap_input": {
                    str(row.order_line.line_number): str(row.plan.rejected_scrap_input_quantity)
                    for row in planned
                    if row.plan.rejected_scrap_input_quantity > ZERO
                },
                "serial_conversions": {
                    str(source_id): str(output_id)
                    for source_id, output_id in converted_serial_ids.items()
                },
            },
            context={
                "reason": payload.reason,
                "job_work_order_code": order.code,
                "movement_count": len(posted),
                "excess_loss_quantity": excess_total,
                "excess_loss_approved": excess_total > ZERO,
                "output_stock_state": str(
                    StockState.QC_HOLD if held_for_qc else StockState.AVAILABLE
                ),
                "qc_inspection_created": inspection_created,
                "vendor_challan_number": payload.vendor_challan_number,
            },
        )

    return ReceiptResult(
        order=order_detail(order, lines),
        receipt=receipt_detail(receipt, receipt_lines),
        movement_count=len(posted),
        output_held_for_qc=held_for_qc,
        qc_inspection_created=inspection_created,
    )


def _plan_receipt(
    entries: Sequence[ReceiptLineIn], by_number: dict[int, JobWorkOrderLine]
) -> list[PlannedLine]:
    """Plan every line of a receipt before any of them posts.

    Separated so the whole lot is validated before the first movement is built:
    a receipt that fails on its fourth line must not have moved the first three.
    """
    planned: list[PlannedLine] = []
    for entry in entries:
        line = by_number.get(entry.line_number)
        if line is None:
            raise NotFoundError("Job work order line", entry.line_number)
        batch_id = entry.batch_id if entry.batch_id is not None else line.batch_id
        if line.is_batch_tracked:
            if line.batch_id is None:
                raise BusinessRuleError(
                    f"Line {line.line_number} is batch tracked but has no issued batch.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"line_number": line.line_number},
                )
            if batch_id != line.batch_id:
                raise BusinessRuleError(
                    f"Line {line.line_number} must be received against the batch that was issued.",
                    details={
                        "line_number": line.line_number,
                        "batch_id": str(batch_id) if batch_id else None,
                        "issued_batch_id": str(line.batch_id),
                    },
                )
        elif entry.batch_id is not None:
            raise BusinessRuleError(
                f"Line {line.line_number} is not batch tracked; remove the batch selection.",
                details={"line_number": line.line_number},
            )
        planned.append(
            PlannedLine(
                order_line=line,
                entry=entry,
                batch_id=batch_id,
                plan=plan_line_receipt(
                    line_number=entry.line_number,
                    outstanding_input=Decimal(line.issued_quantity)
                    - Decimal(line.received_quantity),
                    ratio=yield_ratio(
                        Decimal(line.quantity_to_issue),
                        Decimal(line.expected_output_quantity),
                    ),
                    expected_output_quantity=Decimal(line.expected_output_quantity),
                    output_received_so_far=Decimal(line.received_output_quantity)
                    + Decimal(line.rejected_quantity),
                    output_quantity=entry.output_quantity,
                    rejected_quantity=entry.rejected_quantity,
                    rejection_action=entry.rejection_action,
                    returned_unprocessed_quantity=entry.returned_unprocessed_quantity,
                    is_final=entry.is_final,
                    expected_loss_percentage=Decimal(line.expected_loss_percentage),
                ),
            )
        )
    return planned


def assert_verdict_covers_hold(
    *,
    item_id: uuid.UUID,
    item_code: str | None,
    held_quantity: Decimal,
    accepted: Decimal,
    rejected: Decimal,
) -> None:
    """Refuse a verdict that leaves part of one item's hold undecided.

    The sentence names the item by code: the frontend shows API messages
    verbatim, and a QC decider cannot map a uuid to a part without a second
    lookup. ``item_code`` is ``None`` only for an item retired since the receipt,
    which then falls back to its id - and the id is carried in ``details``
    either way, for the client that wants to link the row.
    """
    if accepted + rejected == held_quantity:
        return
    label = item_code if item_code is not None else str(item_id)
    raise BusinessRuleError(
        f"{held_quantity} unit(s) of {label} are on hold; the verdict accounts "
        f"for {accepted + rejected}.",
        details={
            "item_id": str(item_id),
            "item_code": label,
            "held_quantity": str(held_quantity),
            "accepted_quantity": str(accepted),
            "rejected_quantity": str(rejected),
        },
    )


# -----------------------------------------------------------------------------
# The quality verdict - called back by quality (one release path per hold)
# -----------------------------------------------------------------------------


class InspectionVerdict(Protocol):
    """What job work reads off a decided inspection.

    A protocol rather than quality's own type, so this module does not import
    the module it reaches through a port.
    """

    @property
    def inspection_id(self) -> uuid.UUID: ...
    @property
    def inspection_code(self) -> str: ...
    @property
    def reference_id(self) -> uuid.UUID | None: ...
    @property
    def is_settled(self) -> bool: ...
    @property
    def reason(self) -> str: ...


def _inspection_settled(inspection: Any) -> bool:
    """Decided, with nothing left in rework or hold - read without importing
    quality's types, the way procurement reads an inspection's outcome."""

    def quantity(name: str) -> Decimal:
        return Decimal(getattr(inspection, name, 0) or 0)

    decided = str(getattr(inspection, "result", "PENDING")) != "PENDING"
    return decided and quantity("rework_quantity") + quantity("hold_quantity") <= ZERO


async def _lot_inspections(
    session: AsyncSession, tenant: TenantContext, receipt_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Any]]:
    """Every inspection over each lot, under both reference keys a lot has had.

    One read for the whole set, however many lots the order came back in.
    """
    found: dict[uuid.UUID, list[Any]] = {receipt_id: [] for receipt_id in receipt_ids}
    reader = resolve_inspection_reader_port()
    if reader is None or not receipt_ids:
        return found
    references = [(key, receipt_id) for receipt_id in receipt_ids for key in QC_REFERENCE_KEYS]
    for inspection in await reader(session, tenant, references):
        if inspection.reference_id in found:
            found[inspection.reference_id].append(inspection)
    return found


async def _release_lot(
    session: AsyncSession,
    tenant: TenantContext,
    receipt: JobWorkReceipt,
    inspections: Sequence[Any],
    *,
    reason: str,
    receipt_repo: JobWorkReceiptRepository,
) -> None:
    """Lift one lot's hold: its inspections have already moved the stock."""
    codes = ", ".join(str(row.code) for row in inspections)
    receipt.qc_released_at = utcnow()
    receipt_repo.touch(receipt)
    await session.flush()
    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.QC_DECISION,
        entity_type=AUDIT_JOB_WORK_RECEIPT,
        entity_id=receipt.id,
        entity_code=receipt.code,
        summary=f"{receipt.code} released from inspection hold by {codes}",
        user_id=tenant.user_id,
        new_values={"qc_released_at": receipt.qc_released_at},
        context={
            "reason": reason,
            "qc_inspection_codes": [str(row.code) for row in inspections],
            # Quality moved the held stock when it decided; this only records it.
            "stock_released_by": "qc_inspection",
        },
    )


async def _complete_inspected_order(
    session: AsyncSession,
    tenant: TenantContext,
    order: JobWorkOrder,
    *,
    reason: str,
    repo: JobWorkOrderRepository,
    receipt_repo: JobWorkReceiptRepository,
) -> bool:
    """QC_PENDING -> COMPLETED once no lot of the order is still held."""
    if JobWorkOrderStatus(order.status) is not JobWorkOrderStatus.QC_PENDING:
        return False
    if await receipt_repo.count_unreleased_holds(order.id):
        return False
    await _transition(
        session,
        tenant,
        order,
        JobWorkOrderStatus.COMPLETED,
        reason=reason[:255],
        repo=repo,
        action=AuditAction.QC_DECISION,
    )
    return True


async def lock_for_quality_verdict(
    session: AsyncSession, tenant: TenantContext, reference_id: uuid.UUID
) -> None:
    """Take the order's row lock before quality moves a lot's held output.

    The inspection names the lot; the order is the serialisation point for every
    lot of it - the same lock a receipt against it takes first.
    """
    receipt = await JobWorkReceiptRepository(session, tenant).get(reference_id)
    if receipt is not None:
        await JobWorkOrderRepository(session, tenant).lock_for_update(receipt.job_work_order_id)


async def record_quality_verdict(
    session: AsyncSession, tenant: TenantContext, verdict: InspectionVerdict
) -> bool:
    """Release a returned lot, and complete its order, from quality's verdict.

    Quality has already moved the lot's held output. A lot is released once
    every inspection over it - one per output item - is decided with nothing
    left in rework or hold, whatever state the order is in: a lot back early is
    released while the rest is still at the vendor (PARTIALLY_RECEIVED). An
    order waiting in QC_PENDING completes once none of its lots is held.

    Moves no stock. Idempotent - a lot already released is left alone - and
    tolerant of an inspection that names no lot of this module (one raised by
    hand quoting an order). Returns whether anything changed.
    """
    if verdict.reference_id is None:
        return False
    repo = JobWorkOrderRepository(session, tenant)
    receipt_repo = JobWorkReceiptRepository(session, tenant)

    async with transaction(session):
        receipt = await receipt_repo.get(verdict.reference_id)
        if receipt is None:
            log.info(
                "job_work.verdict_names_no_lot",
                inspection_id=str(verdict.inspection_id),
                reference_id=str(verdict.reference_id),
            )
            return False
        order = await repo.lock_for_update(receipt.job_work_order_id)
        if order is None:  # pragma: no cover - the foreign key guarantees the row
            return False

        changed = False
        if receipt.qc_held_at is not None and receipt.qc_released_at is None:
            inspections = (await _lot_inspections(session, tenant, [receipt.id]))[receipt.id]
            if inspections and all(_inspection_settled(row) for row in inspections):
                await _release_lot(
                    session,
                    tenant,
                    receipt,
                    inspections,
                    reason=f"Inspection {verdict.inspection_code}: {verdict.reason}",
                    receipt_repo=receipt_repo,
                )
                changed = True

        if await _complete_inspected_order(
            session,
            tenant,
            order,
            reason=f"Inspection {verdict.inspection_code} decided; no returned lot is held",
            repo=repo,
            receipt_repo=receipt_repo,
        ):
            changed = True
    return changed


async def awaiting_quality_verdicts(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    after: uuid.UUID | None,
    limit: int,
) -> list[tuple[str, uuid.UUID]]:
    """Held lots behind an inspection - one keyset page for the reconciliation
    sweep. Each is offered under both reference keys a lot's inspection has had."""
    receipt_ids = await JobWorkReceiptRepository(session, tenant).awaiting_quality_verdict(
        after=after, limit=limit
    )
    return [(key, receipt_id) for receipt_id in receipt_ids for key in QC_REFERENCE_KEYS]


async def _settle_from_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    covered: dict[uuid.UUID, list[Any]],
    payload: QcResultRequest,
) -> JobWorkOrder:
    """The owner-side endpoint, for an order whose held lots inspections cover.

    ASSUMPTION: an inspection is the one release path for the lot it covers.
    While any of them is open the request is refused and pointed at it; once all
    are decided the stock has already moved, so the lots are released from the
    inspections' own figures - per item, and only if the request repeats them -
    and nothing moves again (docs/assumptions.md §E4-3).
    """
    repo = JobWorkOrderRepository(session, tenant)
    receipt_repo = JobWorkReceiptRepository(session, tenant)
    order = await repo.get_or_404(order_id)
    order_code = order.code

    inspections = [row for rows in covered.values() for row in rows]
    open_codes = [str(row.code) for row in inspections if not _inspection_settled(row)]
    if open_codes:
        raise BusinessRuleError(
            held_for_inspection(order_code, ", ".join(open_codes)),
            code=ErrorCode.INVALID_STATE_TRANSITION,
            details={"qc_inspection_codes": open_codes},
        )

    accepted: dict[uuid.UUID, Decimal] = {}
    refused: dict[uuid.UUID, Decimal] = {}
    for row in inspections:
        accepted[row.item_id] = to_quantity(
            accepted.get(row.item_id, ZERO) + Decimal(row.accepted_quantity)
        )
        refused[row.item_id] = to_quantity(
            refused.get(row.item_id, ZERO)
            + Decimal(row.rejected_quantity)
            + Decimal(row.scrap_quantity)
        )
    stated = {
        item.output_item_id: (
            to_quantity(item.accepted_quantity),
            to_quantity(item.rejected_quantity),
        )
        for item in payload.items
    }
    recorded = {item_id: (accepted[item_id], refused[item_id]) for item_id in accepted}
    codes = ", ".join(str(row.code) for row in inspections)
    if stated != recorded:
        raise BusinessRuleError(
            verdict_differs_from_inspection(order_code, codes),
            details={
                "qc_inspection_codes": [str(row.code) for row in inspections],
                "recorded": {
                    str(item_id): {"accepted_quantity": str(a), "rejected_quantity": str(r)}
                    for item_id, (a, r) in recorded.items()
                },
            },
        )

    async with transaction(session):
        locked = await repo.lock_for_update(order_id)
        if locked is None:  # pragma: no cover - read a moment ago
            raise NotFoundError("Job work order", order_id)
        for receipt in await receipt_repo.list_for_order(locked.id):
            if receipt.id in covered and receipt.qc_released_at is None:
                await _release_lot(
                    session,
                    tenant,
                    receipt,
                    covered[receipt.id],
                    reason=payload.reason,
                    receipt_repo=receipt_repo,
                )
        await _complete_inspected_order(
            session,
            tenant,
            locked,
            reason=payload.reason,
            repo=repo,
            receipt_repo=receipt_repo,
        )
    return locked


async def record_qc_result(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: QcResultRequest,
) -> JobWorkOrder:
    """Record the inspection verdict on held output and release the hold.

    For held lots no quality inspection covers - quality absent when they came
    back - this is the release path. Accepted output moves ``QC_HOLD ->
    AVAILABLE`` and becomes promisable; rejected output moves ``QC_HOLD ->
    DAMAGED`` and stays on the books, counted, but out of the available pool.
    The verdict has to account for every held unit of every held item -
    releasing part of a lot and leaving the rest in limbo is how stock becomes
    permanently unusable without anybody deciding that it should be.

    When inspections cover the held lots, they are the one release path and this
    defers to them (:func:`_settle_from_inspections`): it never moves the same
    held stock a second time.
    """
    repo = JobWorkOrderRepository(session, tenant)
    receipt_repo = JobWorkReceiptRepository(session, tenant)
    receipt_line_repo = JobWorkReceiptLineRepository(session, tenant)

    # Plain reads first, deliberately without the order's row lock: deferring to
    # an inspection must not hold the order while quality locks the inspection
    # and then the order, which is quality's lock order.
    current = await repo.get_or_404(order_id)
    held_ids = [
        row.id
        for row in await receipt_repo.list_for_order(current.id)
        if row.qc_held_at is not None and row.qc_released_at is None
    ]
    covered = {
        receipt_id: rows
        for receipt_id, rows in (await _lot_inspections(session, tenant, held_ids)).items()
        if rows
    }
    if covered:
        return await _settle_from_inspections(session, tenant, order_id, covered, payload)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)
        JOB_WORK_ORDER_LIFECYCLE.assert_transition(
            JobWorkOrderStatus(order.status), JobWorkOrderStatus.COMPLETED
        )

        receipts = [
            row
            for row in await receipt_repo.list_for_order(order.id)
            if row.qc_held_at is not None and row.qc_released_at is None
        ]
        if not receipts:
            raise BusinessRuleError(
                f"{order.code} has no output on inspection hold.",
                details={"order_id": str(order.id)},
            )

        # One grouped statement for every held lot, not a query per receipt: an
        # order returned in six deliveries would otherwise be six round trips on
        # the write path (section 32).
        held = await receipt_line_repo.held_output_for_order([receipt.id for receipt in receipts])
        # One batched lookup so a refusal can name the item by code rather than
        # id; an item retired since the receipt is absent and falls back to its id.
        items = await ItemLookupRepository(session, tenant).load(list(held.keys()))

        verdicts = {item.output_item_id: item for item in payload.items}
        missing = sorted(str(item_id) for item_id in held.keys() - verdicts.keys())
        if missing:
            raise BusinessRuleError(
                "The verdict does not account for every item on hold.",
                details={"unaccounted_item_ids": missing},
            )
        unexpected = sorted(str(item_id) for item_id in verdicts.keys() - held.keys())
        if unexpected:
            raise BusinessRuleError(
                "The verdict names items that are not on hold for this order.",
                details={"unheld_item_ids": unexpected},
            )

        reason = f"Job work {order.code} inspection: {payload.reason}"[:255]
        for item_id, quantity in sorted(held.items(), key=lambda pair: str(pair[0])):
            verdict = verdicts[item_id]
            accepted = to_quantity(verdict.accepted_quantity)
            rejected = to_quantity(verdict.rejected_quantity)
            item = items.get(item_id)
            assert_verdict_covers_hold(
                item_id=item_id,
                item_code=item.code if item is not None else None,
                held_quantity=quantity,
                accepted=accepted,
                rejected=rejected,
            )
            for target, moving in (
                (StockState.AVAILABLE, accepted),
                (StockState.DAMAGED, rejected),
            ):
                if moving <= ZERO:
                    continue
                await change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=order.return_warehouse_id,
                    quantity=moving,
                    from_state=StockState.QC_HOLD,
                    to_state=target,
                    reason=reason,
                    reference_type=MOVEMENT_REFERENCE_JOB_WORK,
                    reference_id=order.id,
                    reference_code=order.code,
                )

        released_at = utcnow()
        for receipt in receipts:
            receipt.qc_released_at = released_at
            if payload.qc_inspection_id is not None:
                receipt.qc_inspection_id = payload.qc_inspection_id
            receipt_repo.touch(receipt)
        await session.flush()

        await _transition(
            session,
            tenant,
            order,
            JobWorkOrderStatus.COMPLETED,
            reason=payload.reason,
            repo=repo,
            action=AuditAction.QC_DECISION,
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.QC_DECISION,
            entity_type=AUDIT_JOB_WORK_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Inspection verdict recorded for {order.code}",
            user_id=tenant.user_id,
            new_values={
                "accepted": {
                    str(item.output_item_id): item.accepted_quantity for item in payload.items
                },
                "rejected": {
                    str(item.output_item_id): item.rejected_quantity for item in payload.items
                },
            },
            context={
                "reason": payload.reason,
                "receipts_released": len(receipts),
                "qc_inspection_id": payload.qc_inspection_id,
            },
        )
    return order


async def close_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> JobWorkOrder:
    """Close a finished order. Terminal: nothing may be posted against it after.

    An order that has not been fully received cannot get here, and that is
    deliberate: the state machine has no edge out of ``PARTIALLY_RECEIVED``
    except ``RECEIVED``. Material still at a vendor is closed out by a final
    receipt, which writes the shortfall off as scrap with a reason - closing over
    the top of it would leave a quantity in the job-work pool that the stock
    report keeps counting and nobody ever explains.

    A ``RECEIVED`` order that needed no inspection is completed and closed in one
    call, and both transitions are audited: "the goods are all back" and "the
    order is finished" are genuinely two facts.
    """
    repo = JobWorkOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Job work order", order_id)

        if JobWorkOrderStatus(order.status) is JobWorkOrderStatus.RECEIVED:
            await _transition(
                session, tenant, order, JobWorkOrderStatus.COMPLETED, reason=reason, repo=repo
            )

        order.closed_at = utcnow()
        await _transition(
            session, tenant, order, JobWorkOrderStatus.CLOSED, reason=reason, repo=repo
        )
    return order


# -----------------------------------------------------------------------------
# Planning's conversion hook
# -----------------------------------------------------------------------------


#: Inputs the planner must supply; none may be inferred from a shortage.
MRP_CONVERSION_INPUTS_REQUIRED = (
    "Converting a job-work suggestion requires a vendor, process, expected return date, "
    "and issuing warehouse."
)


async def create_order_from_mrp(
    session: AsyncSession, tenant: TenantContext, request: Any
) -> tuple[uuid.UUID, str]:
    """Create a draft job-work order from an explicitly completed MRP request.

    Resolved by name at call time by the MRP module, which is why the request is
    typed loosely here. The regular order service owns all validation, numbering,
    transaction and audit behavior; the hook only maps the suggestion and the
    planner's explicit choices into its established create schema.
    """
    values = {
        "vendor_id": request.vendor_id,
        "process_type": request.process_type,
        "expected_return_date": request.expected_return_date,
        "warehouse_id": request.warehouse_id,
    }
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise BusinessRuleError(
            MRP_CONVERSION_INPUTS_REQUIRED,
            details={
                "suggestion_id": str(request.suggestion_id),
                "item_code": str(request.item_code),
                "missing_fields": missing,
            },
        )

    if request.warehouse_ids and request.warehouse_id not in request.warehouse_ids:
        raise ValidationError(
            "The issuing warehouse must be included in the MRP run's warehouse scope.",
            details={
                "warehouse_id": str(request.warehouse_id),
                "run_warehouse_ids": [str(value) for value in request.warehouse_ids],
            },
        )

    try:
        process_type = JobWorkProcess(request.process_type)
    except ValueError as exc:
        raise ValidationError(
            "The selected job-work process is not supported.",
            details={"process_type": request.process_type},
        ) from exc

    payload = JobWorkOrderCreate(
        vendor_id=request.vendor_id,
        process_type=process_type,
        expected_return_date=request.expected_return_date,
        warehouse_id=request.warehouse_id,
        source_type=JobWorkSourceType.MRP_SUGGESTION,
        source_id=request.suggestion_id,
        notes=request.notes,
        lines=[
            JobWorkOrderLineCreate(
                line_number=1,
                item_id=request.item_id,
                quantity_to_issue=request.quantity,
            )
        ],
    )
    order, _ = await create_order(session, tenant, payload)
    return order.id, order.code


async def open_job_work_output(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_ids: Sequence[uuid.UUID],
    warehouse_ids: Sequence[uuid.UUID] | None = None,
    upto: date,
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """Return the dated good-output balance still at job-work vendors for MRP."""
    return await JobWorkOrderRepository(session, tenant).open_output_by_item(
        item_ids,
        warehouse_ids=warehouse_ids,
        upto=upto,
    )


# -----------------------------------------------------------------------------
# Delivery challan PDF - queued, never rendered in the request (section 22)
# -----------------------------------------------------------------------------


#: Celery task that renders the delivery challan, and the queue it runs on.
JOB_WORK_CHALLAN_TASK_NAME = "reports.generate_job_work_challan"
JOB_WORK_CHALLAN_QUEUE = "reports"

#: What the challan states it is: goods sent out for processing, not sold.
CHALLAN_PURPOSE = "Sent for job work \u2014 not for sale"


def challan_pdf_parameters(challan: JobWorkChallan) -> dict[str, Any]:
    """What a challan PDF job is a render *of*, as stored on its job row.

    The challan is immutable once issued, but the row still names the document
    so a support conversation can say which PDF a job produced.
    """
    return {
        "report_key": str(ReportKey.JOB_WORK_CHALLAN),
        "job_work_challan_id": str(challan.id),
        "job_work_challan_code": challan.code,
    }


async def queue_job_work_challan(
    session: AsyncSession, tenant: TenantContext, challan_id: uuid.UUID
) -> str:
    """Hand the delivery-challan PDF to Celery and return the job id.

    The job id is a ``report_snapshots`` row, not the Celery task id: the row is
    what ``GET /reports/jobs/{id}`` reads, what the worker marks RUNNING,
    COMPLETED or FAILED, and what carries the ``document_id`` the client
    downloads. The row is created and audited in one transaction before the
    task is published, so a broker that refuses the task can mark the job FAILED
    rather than leave the client polling a job that never existed.
    """
    challan = await JobWorkChallanRepository(session, tenant).get_or_404(challan_id)

    snapshots = ReportSnapshotRepository(session, tenant)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.JOB_WORK_CHALLAN, challan_pdf_parameters(challan), now=utcnow()
        )
        snapshots.add(job)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type=AUDIT_JOB_WORK_CHALLAN,
            entity_id=challan.id,
            entity_code=challan.code,
            summary=f"Queued the delivery challan PDF for {challan.code}",
            user_id=tenant.user_id,
            context={"task": JOB_WORK_CHALLAN_TASK_NAME, "job_id": str(job.id)},
        )

    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name=JOB_WORK_CHALLAN_TASK_NAME,
        queue=JOB_WORK_CHALLAN_QUEUE,
        task_kwargs={"job_work_challan_id": str(challan.id)},
        subject="The delivery challan",
    )

    log.info("job_work.challan_pdf_queued", code=challan.code, job_id=str(job.id))
    return str(job.id)


def _party_block(
    *,
    name: str | None,
    legal_name: str | None,
    address_line1: str | None,
    address_line2: str | None,
    city: str | None,
    state: str | None,
    postal_code: str | None,
    gstin: str | None,
) -> dict[str, Any]:
    return {
        "name": name or "",
        "legal_name": legal_name,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "gstin": gstin,
    }


async def challan_pdf_data(
    session: AsyncSession, tenant: TenantContext, challan_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the delivery-challan renderer needs, in six batched statements.

    Assembled here rather than in the worker so the document the PDF shows and
    the document the API shows are produced by one piece of code. The return is
    a plain mapping of primitives: it crosses a process boundary into a
    renderer that must not hold ORM instances or a session.
    """
    challan = await JobWorkChallanRepository(session, tenant).get_or_404(challan_id)
    grouped = await JobWorkChallanLineRepository(session, tenant).list_for_challans([challan.id])
    lines = grouped.get(challan.id, [])
    order = await JobWorkOrderRepository(session, tenant).get_or_404(challan.job_work_order_id)

    vendor = (
        await session.execute(
            select(Vendor).where(
                Vendor.company_id == tenant.company_id, Vendor.id == challan.vendor_id
            )
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise NotFoundError("Vendor", challan.vendor_id)
    company = (
        await session.execute(select(Company).where(Company.id == tenant.company_id))
    ).scalar_one_or_none()

    transporter: Transporter | None = None
    if challan.transporter_id is not None:
        transporter = (
            await session.execute(
                select(Transporter).where(
                    Transporter.company_id == tenant.company_id,
                    Transporter.id == challan.transporter_id,
                )
            )
        ).scalar_one_or_none()

    item_ids = sorted({row.item_id for row in lines})
    items: dict[uuid.UUID, Item] = {}
    uoms: dict[uuid.UUID, str] = {}
    if item_ids:
        items = {
            row.id: row
            for row in (
                await session.execute(
                    select(Item).where(Item.company_id == tenant.company_id, Item.id.in_(item_ids))
                )
            )
            .scalars()
            .all()
        }
        uom_ids = sorted({row.base_uom_id for row in items.values()})
        if uom_ids:
            uoms = {
                row.id: row.code
                for row in (
                    await session.execute(
                        select(UnitOfMeasure).where(
                            UnitOfMeasure.company_id == tenant.company_id,
                            UnitOfMeasure.id.in_(uom_ids),
                        )
                    )
                )
                .scalars()
                .all()
            }

    return {
        "code": challan.code,
        "challan_date": challan.challan_date.isoformat(),
        "expected_return_date": challan.expected_return_date.isoformat(),
        "order_code": order.code,
        "process_type": str(order.process_type),
        "process_description": order.process_description,
        "purpose": CHALLAN_PURPOSE,
        "consignor": _party_block(
            name=company.name if company else None,
            legal_name=company.legal_name if company else None,
            address_line1=company.address_line1 if company else None,
            address_line2=company.address_line2 if company else None,
            city=company.city if company else None,
            state=company.state if company else None,
            postal_code=company.postal_code if company else None,
            gstin=company.gstin if company else None,
        ),
        "consignee": _party_block(
            name=vendor.name,
            legal_name=vendor.legal_name,
            address_line1=vendor.address_line1,
            address_line2=vendor.address_line2,
            city=vendor.city,
            state=vendor.state,
            postal_code=vendor.postal_code,
            gstin=vendor.gstin,
        ),
        "vehicle_number": challan.vehicle_number,
        "transporter": (
            {"code": transporter.code, "name": transporter.name}
            if transporter is not None
            else None
        ),
        "lines": [
            {
                "line_number": row.line_number,
                "item_code": items[row.item_id].code if row.item_id in items else "",
                "item_name": items[row.item_id].name if row.item_id in items else "",
                "hsn_code": items[row.item_id].hsn_code if row.item_id in items else None,
                "uom": (
                    uoms.get(items[row.item_id].base_uom_id, "") if row.item_id in items else ""
                ),
                "quantity": str(row.quantity),
                "unit_value": str(row.unit_value),
                "total_value": str(row.total_value),
            }
            for row in lines
        ],
        "total_quantity": str(challan.total_quantity),
        "total_value": str(challan.total_value),
        "notes": challan.notes,
    }


__all__ = [
    "AUDIT_JOB_WORK_CHALLAN",
    "AUDIT_JOB_WORK_ORDER",
    "AUDIT_JOB_WORK_RECEIPT",
    "CHALLAN_PURPOSE",
    "CHARGES_VENDOR_LIMIT",
    "EXCESS_RECEIPT_TOLERANCE_PERCENT",
    "JOB_WORK_CHALLAN_QUEUE",
    "JOB_WORK_CHALLAN_TASK_NAME",
    "JOB_WORK_ORDER_LIFECYCLE",
    "MOVEMENT_REFERENCE_JOB_WORK",
    "MRP_CONVERSION_INPUTS_REQUIRED",
    "PENDING_AT_VENDOR_LIMIT",
    "QC_REFERENCE_KEYS",
    "QC_REFERENCE_RECEIPT",
    "AllocationPort",
    "AllocationService",
    "CapitalisedCost",
    "InspectionReaderPort",
    "InspectionVerdict",
    "LineReceiptPlan",
    "QualityPort",
    "approve_order",
    "awaiting_quality_verdicts",
    "billable_lines",
    "cancel_order",
    "capitalised_output_cost",
    "challan_detail",
    "challan_pdf_data",
    "challan_pdf_parameters",
    "close_order",
    "create_order",
    "create_order_from_mrp",
    "delete_draft_order",
    "estimate_charges",
    "get_order_detail",
    "held_output_by_item",
    "input_equivalent",
    "issue_order",
    "issue_unit_basis",
    "job_work_charges_for_period",
    "list_challan_register",
    "list_challans",
    "list_orders",
    "list_pending_at_vendor",
    "list_receipts",
    "lock_for_quality_verdict",
    "loss_percentage",
    "open_job_work_output",
    "order_detail",
    "pending_at_vendor",
    "plan_line_receipt",
    "production_order_charges",
    "queue_job_work_challan",
    "receipt_charge",
    "receipt_detail",
    "receive",
    "reconciliation",
    "record_qc_result",
    "record_quality_verdict",
    "resolve_inspection_reader_port",
    "resolve_quality_port",
    "update_order",
    "vendor_ledger",
    "yield_ratio",
]
