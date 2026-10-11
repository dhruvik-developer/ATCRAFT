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
   ``QC_PENDING`` once it is fully received, and the quality module is asked for
   an inspection through a guarded import. When quality is not deployed the hold
   is still recorded on the receipt and is still queryable - the stock is held
   either way, which is the half that matters.

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
* **Processed output is received at the output item's standard cost** (the
  inventory engine's documented fallback when no unit cost is supplied). The
  processing charge is expensed through the vendor ledger rather than
  capitalised into the output item's stock value; capitalising it would require
  a costing policy the contract does not state.
* **Batch-tracked and serial-tracked items are refused** on both the input and
  the output side. The inventory engine requires a batch on every movement of a
  batch-tracked item and a serial on every movement of a serialised one, and
  this module has no contract for choosing a batch or minting a serial.
  Refusing at order entry fails before anything is committed to, rather than
  after the material has left the building.
* **``requires_qc`` is a per-order flag**, not an item-master property. ``items``
  carries no inspection policy and inventing one would put the rule in two
  places that could disagree; ``goods_receipt_lines.requires_qc`` and
  ``assembly_orders.requires_qc`` make the same choice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

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
from src.core.logging import get_logger
from src.core.messages import date_before, inactive_record
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity
from src.modules.allocation.models import DemandType, ReservationType
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.service import MovementRequest, change_stock_state, post_movements
from src.modules.job_work.models import (
    MAX_LINES_PER_ORDER,
    JobWorkChallan,
    JobWorkChallanLine,
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkReceipt,
    JobWorkReceiptLine,
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
)
from src.modules.organization.service import require_cost_centre

log = get_logger(__name__)

AUDIT_JOB_WORK_ORDER = "job_work_order"
AUDIT_JOB_WORK_CHALLAN = "job_work_challan"
AUDIT_JOB_WORK_RECEIPT = "job_work_receipt"

#: What the ledger rows this module posts point back at. A bare string, matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
MOVEMENT_REFERENCE_JOB_WORK = "job_work_order"

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
CLEARABLE_UPDATE_FIELDS = frozenset({"process_description", "notes", "cost_centre_id"})


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


def _assert_trackable(item: JobWorkItem, *, role: str) -> None:
    """Refuse tracking modes this module has no contract to satisfy.

    See the module's Assumptions. Checked at order entry so the order never
    reaches a vendor it cannot be received from.
    """
    if item.is_batch_tracked:
        raise BusinessRuleError(
            f"{item.code} is batch tracked and cannot be a job work {role}: there is no "
            "contract for choosing a batch on this path.",
            code=ErrorCode.BATCH_REQUIRED,
            details={"item_code": item.code, "role": role},
        )
    if item.is_serial_tracked:
        raise BusinessRuleError(
            f"{item.code} is serial tracked and cannot be a job work {role}: there is no "
            "contract for naming a serial number on this path.",
            code=ErrorCode.SERIAL_REQUIRED,
            details={"item_code": item.code, "role": role},
        )


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
        _assert_trackable(source, role="input")

        output_id = line.expected_output_item_id or line.item_id
        output = items.get(output_id)
        if output is None:
            raise NotFoundError("Item", output_id)
        if not output.is_active:
            raise BusinessRuleError(
                inactive_record("Item", output.code, "cannot receive processed stock"),
                details={"item_code": output.code, "line_number": line.line_number},
            )
        if output.id != source.id:
            _assert_trackable(output, role="output")

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
        overrides: dict[int, Decimal] = {}
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
            overrides[entry.line_number] = quantity

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
        issuing: list[tuple[JobWorkOrderLine, Decimal]] = []
        movements: list[MovementRequest] = []
        for line in lines:
            quantity = overrides.get(line.line_number, to_quantity(Decimal(line.quantity_to_issue)))
            if quantity <= ZERO:
                continue
            issuing.append((line, quantity))
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=quantity,
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
            (line, quantity, Decimal(txn.unit_cost), to_money(quantity * Decimal(txn.unit_cost)))
            for (line, quantity), txn in zip(issuing, posted, strict=True)
        ]
        total_quantity = to_quantity(sum((quantity for _, quantity, _, _ in declared), start=ZERO))
        total_value = to_money(sum((value for _, _, _, value in declared), start=ZERO))

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
        for line, quantity, unit_value, line_value in declared:
            challan_lines.append(
                challan_line_repo.add(
                    JobWorkChallanLine(
                        challan_id=challan.id,
                        job_work_order_line_id=line.id,
                        line_number=line.line_number,
                        item_id=line.item_id,
                        uom_id=line.uom_id,
                        quantity=quantity,
                        unit_value=unit_value,
                        total_value=line_value,
                    )
                )
            )
            line.issued_quantity = to_quantity(Decimal(line.issued_quantity) + quantity)
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
                "issued": {str(line.line_number): quantity for line, quantity in issuing},
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


def _build_receipt_movements(
    planned: Sequence[PlannedLine],
    *,
    order: JobWorkOrder,
    reason: str,
    occurred_at: datetime | None,
) -> tuple[list[MovementRequest], dict[int, int]]:
    """Every ledger row one receipt writes, as one batch.

    Four movement shapes, and each says something different about where a
    quantity went:

    * ``JOB_WORK_RECEIPT`` into ``AVAILABLE`` (or ``QC_HOLD``) at the return
      warehouse - the processed output arriving as its own item;
    * ``JOB_WORK_RECEIPT`` into ``DAMAGED`` - output the vendor made badly and
      sent back; present and counted, not promisable;
    * ``JOB_WORK_RECEIPT`` from ``JOB_WORK`` into ``AVAILABLE`` at the source
      warehouse - input handed back untouched;
    * ``MATERIAL_ISSUE`` with ``stock_state=JOB_WORK`` - the input the process
      consumed, drawn straight out of the job-work pool without ever becoming
      available again. The closed section-83 vocabulary has no "job work
      consume"; this is the same word production uses for components eaten into
      a different finished item, which is what happened;
    * ``SCRAP`` from ``JOB_WORK`` - the process loss, out of the pool and gone.

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

    for entry in planned:
        line, plan = entry.order_line, entry.plan
        common: dict[str, Any] = {
            "reference_type": MOVEMENT_REFERENCE_JOB_WORK,
            "reference_id": order.id,
            "reference_code": order.code,
            "reason": reason,
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
                    **common,
                )
            )
        if plan.rejected_quantity > ZERO:
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
                    **common,
                )
            )
        if plan.input_for_output > ZERO:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=plan.input_for_output,
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    # Draws the job-work pool down without touching on-hand: the
                    # material never comes back as itself, it comes back as the
                    # output item posted above.
                    stock_state=StockState.JOB_WORK,
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
        excess_total = assert_excess_loss_authorised(
            principal,
            order_code=order.code,
            planned=planned,
            approve_excess_loss=payload.approve_excess_loss,
        )

        receipt_date = payload.receipt_date or today()
        movement_reason = f"Job work {order.code} received: {payload.reason}"[:255]
        movements, scrap_index = _build_receipt_movements(
            planned, order=order, reason=movement_reason, occurred_at=payload.occurred_at
        )
        posted = await post_movements(session, tenant, movements)
        # The debit is priced from the cost the ledger actually used for the
        # scrap, not from a price list - so the vendor is charged what the
        # company was carrying for the material it lost.
        scrap_costs = {
            line_number: Decimal(posted[index].unit_cost)
            for line_number, index in scrap_index.items()
        }

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
        # per-line vendor query and the header agree to the last paisa. The final
        # line absorbs the rounding remainder rather than leaving a stray
        # fraction unattributed.
        charge_remaining = charge
        apportioned: list[tuple[PlannedLine, Decimal, Decimal]] = []
        debit_total = ZERO
        for position, row in enumerate(planned):
            is_last = position == len(planned) - 1
            line_charge = (
                charge_remaining
                if is_last
                else to_money(
                    charge * row.plan.output_quantity / good_output if good_output > ZERO else ZERO
                )
            )
            charge_remaining = to_money(charge_remaining - line_charge)
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
        if held_for_qc and quality_port is not None:
            # The lot is inspected as one, and the inspection names the item the
            # bulk of it came back as. An order returning several output codes
            # is rare; when it happens the first line with good output names the
            # inspection and the rest are covered by the same hold, which the
            # verdict then releases per item.
            first_output_item = next(
                row.order_line.expected_output_item_id
                for row in planned
                if row.plan.output_quantity > ZERO
            )
            inspection = await quality_port(
                session,
                tenant,
                item_id=first_output_item,
                quantity=receipt.total_output_quantity,
                warehouse_id=order.return_warehouse_id,
                reference_type=MOVEMENT_REFERENCE_JOB_WORK,
                reference_id=receipt.id,
                reference_code=receipt.code,
            )
            receipt.qc_inspection_id = getattr(inspection, "id", None)
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
        if fully_received and order.requires_qc:
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
        planned.append(
            PlannedLine(
                order_line=line,
                entry=entry,
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


async def record_qc_result(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: QcResultRequest,
) -> JobWorkOrder:
    """Record the inspection verdict on held output and release the hold.

    Accepted output moves ``QC_HOLD -> AVAILABLE`` and becomes promisable;
    rejected output moves ``QC_HOLD -> DAMAGED`` and stays on the books,
    counted, but out of the available pool. The verdict has to account for every
    held unit of every held item - releasing part of a lot and leaving the rest
    in limbo is how stock becomes permanently unusable without anybody deciding
    that it should be.
    """
    repo = JobWorkOrderRepository(session, tenant)
    receipt_repo = JobWorkReceiptRepository(session, tenant)
    receipt_line_repo = JobWorkReceiptLineRepository(session, tenant)

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


#: Why planning cannot raise a job work order on its own. Must read the same as
#: ``mrp.service.MANUAL_CONVERSION_REASONS[JOB_WORK_ORDER]`` - the planner is
#: told one sentence whether the hook is present or absent.
MRP_CONVERSION_REFUSED = (
    "A job work order needs a vendor and a process, which planning does not choose; "
    "raise it by hand from this suggestion and then ignore the suggestion."
)


async def create_order_from_mrp(
    session: AsyncSession, tenant: TenantContext, request: Any
) -> tuple[uuid.UUID, str]:
    """Planning's conversion hook (``mrp.service.CONVERSION_HOOKS``) - refused.

    Resolved by name at call time by the MRP module, which is why the request is
    typed loosely here. A job work order needs a vendor, a process and a return
    date, none of which a planning run chooses; picking a vendor here would be
    an invented business rule (CLAUDE.md). The hook exists so the refusal is
    this module's own statement rather than planning's guess about it, and so
    the suggestion stays OPEN for the planner to action by hand.
    """
    raise BusinessRuleError(
        MRP_CONVERSION_REFUSED,
        details={
            "suggestion_id": str(request.suggestion_id),
            "item_code": str(request.item_code),
        },
    )


__all__ = [
    "AUDIT_JOB_WORK_CHALLAN",
    "AUDIT_JOB_WORK_ORDER",
    "AUDIT_JOB_WORK_RECEIPT",
    "CHARGES_VENDOR_LIMIT",
    "EXCESS_RECEIPT_TOLERANCE_PERCENT",
    "JOB_WORK_ORDER_LIFECYCLE",
    "MOVEMENT_REFERENCE_JOB_WORK",
    "MRP_CONVERSION_REFUSED",
    "PENDING_AT_VENDOR_LIMIT",
    "AllocationPort",
    "AllocationService",
    "LineReceiptPlan",
    "QualityPort",
    "approve_order",
    "cancel_order",
    "challan_detail",
    "close_order",
    "create_order",
    "create_order_from_mrp",
    "delete_draft_order",
    "estimate_charges",
    "get_order_detail",
    "input_equivalent",
    "issue_order",
    "job_work_charges_for_period",
    "list_challans",
    "list_orders",
    "list_pending_at_vendor",
    "list_receipts",
    "loss_percentage",
    "order_detail",
    "pending_at_vendor",
    "plan_line_receipt",
    "receipt_charge",
    "receipt_detail",
    "receive",
    "reconciliation",
    "record_qc_result",
    "resolve_quality_port",
    "update_order",
    "yield_ratio",
]
