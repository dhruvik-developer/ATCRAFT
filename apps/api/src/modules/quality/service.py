"""Quality control business rules.

Master rules section 21. Six rules carry this module, and each lives here rather
than in a caller for a stated reason.

1. **Deciding an inspection is a stock movement.** ACCEPTED releases the
   quantity from ``QC_HOLD`` to ``AVAILABLE``; REJECTED moves it to ``DAMAGED``
   and raises the return flow; SCRAP writes it off to ``SCRAP``; REWORK and HOLD
   deliberately move nothing and leave the quantity on QC hold. All of it goes
   through ``inventory.change_stock_state`` inside one ``transaction(...)``
   together with the status change and the ``QC_DECISION`` audit entry. There is
   no path that records a verdict without moving the stock it decided, and none
   that moves the stock without recording who decided what and why.

2. **A critical parameter out of specification rejects the lot.** Whatever every
   other reading says, and even when mandatory readings are still missing - once
   a thread does not engage, no further measurement can save the goods. The rule
   is binding, not advisory: a decision that tried to accept or rework any part
   of such a lot is refused (:func:`assert_critical_override`).

3. **The disposition is stated in full and accounts for the whole lot.**
   accepted + rejected + rework + hold + scrap must equal ``inspected_quantity``.
   A partial disposition would leave quantity on QC hold underneath a decided
   document - the single most common way stock becomes permanently unusable
   without anybody deciding that it should be.

4. **A decision may be revisited while its quantity is still held.** Material put
   into REWORK or HOLD has not left the QC pool, so it is re-decided later - that
   is what "flagged for rework" means operationally. Re-deciding posts only the
   *delta* against what has already been applied, and the quantities that have
   already left (accepted, rejected, scrapped) can only grow. Recomputing the
   movements from scratch would release the same pieces twice.

5. **Creating an inspection does not hold stock.** The goods receipt, the job
   work return and the assembly completion each post their own ``QC_HOLD``
   movement, because only they know the batch, the cost and the ledger reference
   of what arrived. Holding it again here would take the quantity out of
   availability twice. An inspection raised by hand is the one exception, and it
   says so explicitly with ``hold_stock``.

6. **A template that has been used is frozen.** ``qc_inspection_results`` points
   at a parameter row, so editing a tolerance in place would restate every past
   pass and fail. Editing a used template forks a new ``version`` instead.

## Assumptions

Each of these is a rule the contract did not fix. The conservative option was
taken, named, and is changeable in one place.

* **``result`` is the dominant disposition, with ties broken towards severity**
  (:data:`SEVERITY_ORDER`). A lot of 100 with 95 accepted and 5 rejected reads
  ACCEPTED; a 50/50 split reads REJECTED. The vocabulary has no "PARTIAL"
  member, and the rejection *rate* every report quotes is computed from the
  quantities, never from this label - so the label is chosen to be useful on a
  list screen rather than to carry arithmetic.
* **A critical failure forbids accepting or reworking any part of the lot.** The
  contract says a critical failure forces REJECTED "regardless of the others";
  routing part of the lot to SCRAP instead is permitted because scrap is a
  strictly harsher disposition than rejection, never a softer one.
* **A non-critical deviation recommends REWORK, not ACCEPTED.** The
  recommendation is advisory - the inspector decides - but defaulting a
  deviating lot towards acceptance would make the check sheet decorative.
* **A BOOLEAN parameter passes on ``true``.** There is no column to say which way
  round a boolean check reads, and "conforms / does not conform" is how every
  check sheet in the master rules is worded.
* **A NUMERIC parameter with no minimum and no maximum is informational**, never
  a failure. A target with no tolerance band cannot be met by a real measurement,
  so treating a reading that missed it as a failure would reject every lot.
* **No template resolves to 100% inspection.** An item nobody has written a check
  sheet for is inspected in full rather than sampled, because a sample size
  invented for an unknown specification is a guess presented as a statistic.
* **Quantity still on QC hold keeps the document open**, whatever the verdict on
  the goods (:func:`status_for`). A lot 60 released and 40 sent for rework has a
  ``result`` of ACCEPTED and a ``status`` of REWORK, because those 40 pieces
  still owe a second decision and a decided status would close their only way
  out of the pool.
* **``return_required`` is a flag, not a purchase return.** Rejected incoming
  material owes the vendor a return or a replacement, but raising it is
  procurement's decision, numbering series and approval. Quality states the
  requirement, notifies the buyers and leaves the document to the module that
  owns it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import StockState
from src.modules.inventory.service import change_stock_state
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify_role
from src.modules.quality import owners
from src.modules.quality.models import (
    DECIDED_STATUSES,
    OPEN_STATUSES,
    REOPENABLE_STATUSES,
    InspectionResult,
    InspectionStatus,
    ParameterType,
    QcInspection,
    QcInspectionResult,
    QcStage,
    QcTemplate,
    QcTemplateParameter,
    SamplingPlan,
)
from src.modules.quality.repository import (
    InspectableItem,
    ItemLookupRepository,
    QcInspectionRepository,
    QcInspectionResultRepository,
    QcTemplateParameterRepository,
    QcTemplateRepository,
    QualityTotals,
    WarehouseLookupRepository,
    decided_inspection_companies,
)
from src.modules.quality.schemas import (
    DEFAULT_REPORT_WINDOW_DAYS,
    MAX_REPORT_WINDOW_DAYS,
    VENDOR_ITEM_BREAKDOWN_LIMIT,
    DecideRequest,
    DecisionResponse,
    InspectionCreate,
    InspectionDetail,
    InspectionFilters,
    InspectionResultRead,
    ItemQualityHistory,
    MeasurementIn,
    ParameterEvaluationRead,
    QualityPeriodRow,
    RecordResultsRequest,
    RecordResultsResponse,
    TemplateCreate,
    TemplateDetail,
    TemplateFilters,
    TemplateParameterIn,
    TemplateParameterRead,
    TemplateUpdate,
    TemplateVersionRequest,
    VendorQualityItemRow,
    VendorQualitySummary,
)

log = get_logger(__name__)

AUDIT_QC_TEMPLATE = "qc_template"
AUDIT_QC_INSPECTION = "qc_inspection"

#: What the ledger rows this module posts point back at. A bare string matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
MOVEMENT_REFERENCE_QC = "qc_inspection"

#: Which stage an automatically raised inspection belongs to, keyed on the
#: ``reference_type`` the calling module passes. Explicit rather than inferred
#: from the string, so a new caller either appears here or falls to the
#: IN_PROCESS default rather than silently inventing a stage nobody reports on.
STAGE_BY_REFERENCE_TYPE: dict[str, QcStage] = {
    "goods_receipt": QcStage.INCOMING,
    "purchase_order": QcStage.INCOMING,
    "job_work_order": QcStage.JOB_WORK,
    "job_work_receipt": QcStage.JOB_WORK,
    "assembly_order": QcStage.ASSEMBLY,
    "production_order": QcStage.IN_PROCESS,
    "production_entry": QcStage.IN_PROCESS,
    "purchase_return": QcStage.RETURN,
    "sales_return": QcStage.RETURN,
    "customer_return": QcStage.RETURN,
    "dispatch": QcStage.FINAL,
}
DEFAULT_AUTO_STAGE = QcStage.IN_PROCESS

#: The reference type procurement files incoming inspections under. A rejection
#: on one of these is also reported to the supplying vendor's portal users.
GOODS_RECEIPT_REFERENCE = "goods_receipt"


def _plain_quantity(value: Decimal) -> str:
    """A quantity as a sentence states it: ``5``, not ``5.0000``."""
    text = format(Decimal(value).normalize(), "f")
    return "0" if text == "-0" else text


#: Who is told when a lot fails, and who is told a vendor return is owed. Role
#: codes rather than user ids so the recipient list is derived from the role
#: assignment at the moment it happens and stays correct when somebody joins,
#: leaves or changes job (see ``notifications.service.notify_role``).
QC_FAILURE_ROLE_CODES: tuple[str, ...] = ("QC_INSPECTOR", "PLANT_MANAGER", "STORE_KEEPER")
QC_RETURN_ROLE_CODES: tuple[str, ...] = ("PURCHASE_MANAGER", "PURCHASE_EXECUTIVE")

#: ASSUMPTION: severity order used to break a tie when two dispositions hold
#: equal quantity - see the module's Assumptions.
#:
#: Later is more severe: a lot split evenly between accepted and rejected reads
#: REJECTED, because the optimistic reading of an even split is the one that puts
#: doubtful material into a customer's hands.
SEVERITY_ORDER: tuple[InspectionResult, ...] = (
    InspectionResult.ACCEPTED,
    InspectionResult.REWORK,
    InspectionResult.HOLD,
    InspectionResult.REJECTED,
    InspectionResult.SCRAP,
)

#: ISO 2859-1 / ANSI Z1.4 general inspection level II, single sampling: the upper
#: bound of each lot-size band paired with the sample it calls for. This is the
#: table the standard defines; it is transcribed rather than approximated because
#: a "roughly right" sampling plan is not a sampling plan at all - the whole
#: point is that the risk of accepting a bad lot is a known number.
AQL_LOT_BANDS: tuple[tuple[int, int], ...] = (
    (8, 2),
    (15, 3),
    (25, 5),
    (50, 8),
    (90, 13),
    (150, 20),
    (280, 32),
    (500, 50),
    (1200, 80),
    (3200, 125),
    (10_000, 200),
    (35_000, 315),
    (150_000, 500),
    (500_000, 800),
)
#: Sample for any lot larger than the last band above.
AQL_MAX_SAMPLE = 1250

#: Fields captured before and after an edit so the audit entry records exactly
#: what changed. Explicit rather than "every column": ``created_at`` and the
#: actor columns are not business changes.
_TEMPLATE_AUDITED_FIELDS = [
    "code",
    "name",
    "description",
    "item_id",
    "item_category_id",
    "qc_stage",
    "sampling_plan",
    "sample_percentage",
    "sample_size",
    "is_active",
    "version",
]

_INSPECTION_AUDITED_FIELDS = [
    "status",
    "result",
    "accepted_quantity",
    "rejected_quantity",
    "rework_quantity",
    "hold_quantity",
    "scrap_quantity",
    "rejection_reason",
    "return_required",
]


# =============================================================================
# The state machine (database.md section 7)
# =============================================================================

_DECISION_STATES: tuple[InspectionStatus, ...] = DECIDED_STATUSES


def _decision_transitions() -> tuple[Transition[InspectionStatus], ...]:
    """Build the decision edges.

    Written as a generator rather than forty literal rows because every decision
    edge carries the same permission and the same irreversibility: they all move
    stock. Spelling them out by hand is how one of them ends up with the wrong
    permission.
    """
    edges: list[Transition[InspectionStatus]] = []

    for source in (InspectionStatus.PENDING, InspectionStatus.IN_PROGRESS):
        edges.extend(
            Transition(
                source,
                target,
                permission=Permission.QC_DECIDE,
                label=f"Decide {target.value.lower()}",
                is_irreversible=True,
            )
            for target in _DECISION_STATES
        )

    # Quantity left in REWORK or HOLD never left the QC pool, so those decisions
    # are revisited once the rework is done or the question is answered. The
    # self-edges cover a partial re-decision - ten of twenty reworked and
    # accepted, ten still on the bench - which leaves the document where it is.
    for source in REOPENABLE_STATUSES:
        edges.extend(
            Transition(
                source,
                target,
                permission=Permission.QC_DECIDE,
                label=f"Re-decide {target.value.lower()}",
                is_irreversible=True,
            )
            for target in _DECISION_STATES
        )

    edges.extend(
        Transition(
            source,
            InspectionStatus.CLOSED,
            permission=Permission.QC_DECIDE,
            label="Close",
        )
        for source in _DECISION_STATES
    )
    return tuple(edges)


QC_INSPECTION_LIFECYCLE: StateMachine[InspectionStatus] = StateMachine(
    entity="QC inspection",
    initial=InspectionStatus.PENDING,
    transitions=(
        Transition(
            InspectionStatus.PENDING,
            InspectionStatus.IN_PROGRESS,
            permission=Permission.QC_INSPECT,
            label="Start inspection",
        ),
        *_decision_transitions(),
    ),
    terminal=frozenset({InspectionStatus.CLOSED}),
)


# =============================================================================
# Pure logic - no database, no session. Exercised directly by the unit tests.
# =============================================================================


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One characteristic's specification, detached from the ORM.

    The evaluator takes these rather than model instances so the rules that
    decide a lot's fate can be tested without a database, and so a specification
    read from a frozen template version and one read from a live row are judged
    by identical code.
    """

    id: uuid.UUID
    name: str
    parameter_type: ParameterType
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    target_value: Decimal | None = None
    allowed_values: tuple[str, ...] = ()
    is_critical: bool = False
    is_mandatory: bool = True

    @classmethod
    def from_model(cls, row: QcTemplateParameter) -> ParameterSpec:
        return cls(
            id=row.id,
            name=row.parameter_name,
            parameter_type=ParameterType(row.parameter_type),
            min_value=None if row.min_value is None else Decimal(row.min_value),
            max_value=None if row.max_value is None else Decimal(row.max_value),
            target_value=None if row.target_value is None else Decimal(row.target_value),
            allowed_values=tuple(row.allowed_values or ()),
            is_critical=row.is_critical,
            is_mandatory=row.is_mandatory,
        )


@dataclass(frozen=True, slots=True)
class Reading:
    """One measurement, in whichever of the three forms the parameter calls for."""

    numeric: Decimal | None = None
    text: str | None = None
    boolean: bool | None = None

    @classmethod
    def from_payload(cls, measurement: MeasurementIn) -> Reading:
        return cls(
            numeric=measurement.measured_value_numeric,
            text=measurement.measured_value_text,
            boolean=measurement.measured_value_bool,
        )


@dataclass(frozen=True, slots=True)
class Disposition:
    """Where every piece of an inspected lot ends up."""

    accepted: Decimal = ZERO
    rejected: Decimal = ZERO
    rework: Decimal = ZERO
    hold: Decimal = ZERO
    scrap: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.accepted + self.rejected + self.rework + self.hold + self.scrap

    @property
    def still_held(self) -> Decimal:
        """Quantity this disposition leaves sitting in the QC hold pool."""
        return self.rework + self.hold

    @classmethod
    def from_request(cls, payload: DecideRequest) -> Disposition:
        return cls(
            accepted=to_quantity(payload.accepted_quantity),
            rejected=to_quantity(payload.rejected_quantity),
            rework=to_quantity(payload.rework_quantity),
            hold=to_quantity(payload.hold_quantity),
            scrap=to_quantity(payload.scrap_quantity),
        )

    @classmethod
    def from_inspection(cls, inspection: QcInspection) -> Disposition:
        return cls(
            accepted=to_quantity(Decimal(inspection.accepted_quantity)),
            rejected=to_quantity(Decimal(inspection.rejected_quantity)),
            rework=to_quantity(Decimal(inspection.rework_quantity)),
            hold=to_quantity(Decimal(inspection.hold_quantity)),
            scrap=to_quantity(Decimal(inspection.scrap_quantity)),
        )


@dataclass(frozen=True, slots=True)
class ParameterEvaluation:
    """What the recorded readings imply about the lot."""

    recommended_result: InspectionResult
    is_conclusive: bool
    critical_failures: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    missing_mandatory: tuple[str, ...] = ()
    measured_count: int = 0
    in_spec_count: int = 0

    @property
    def has_critical_failure(self) -> bool:
        return bool(self.critical_failures)


def aql_sample_size(lot_size: int) -> int:
    """Sample called for by ISO 2859-1 general level II at this lot size."""
    if lot_size <= 1:
        # A lot of one is inspected, not sampled: there is nothing to generalise
        # from, and the standard's smallest band already starts at two.
        return max(lot_size, 0)
    for upper_bound, sample in AQL_LOT_BANDS:
        if lot_size <= upper_bound:
            return sample
    return AQL_MAX_SAMPLE


def compute_sample_size(
    *,
    plan: SamplingPlan,
    received_quantity: Decimal,
    sample_percentage: Decimal | None = None,
    sample_size: int | None = None,
) -> Decimal:
    """How much of a received lot this plan says to measure.

    Never zero and never larger than the lot. Rounding is always upward: a plan
    that says "inspect 10%" of 25 pieces means three, not two, because rounding a
    sample down is the one direction that weakens the guarantee the plan exists
    to give. Where the lot is a fractional quantity - 0.5 kg of powder - the
    round-up is capped back to the lot, so the answer stays inspectable.
    """
    lot = to_quantity(Decimal(received_quantity))
    if lot <= ZERO:
        raise BusinessRuleError("A lot with no quantity cannot be sampled.")

    if plan is SamplingPlan.ALL:
        return lot

    if plan is SamplingPlan.PERCENTAGE:
        if sample_percentage is None or sample_percentage <= ZERO:
            raise BusinessRuleError("A PERCENTAGE sampling plan needs a sample percentage.")
        raw = lot * Decimal(sample_percentage) / Decimal("100")
        candidate = raw.to_integral_value(rounding=ROUND_CEILING)
    elif plan is SamplingPlan.FIXED_SAMPLE:
        if sample_size is None or sample_size < 1:
            raise BusinessRuleError("A FIXED_SAMPLE sampling plan needs a sample size.")
        candidate = Decimal(sample_size)
    else:
        # AQL. The standard is defined over whole pieces, so a fractional lot is
        # rounded up to the piece count it would occupy before the table is read.
        lot_pieces = int(lot.to_integral_value(rounding=ROUND_CEILING))
        candidate = Decimal(aql_sample_size(lot_pieces))

    return to_quantity(min(max(candidate, Decimal("1")), lot))


def evaluate_reading(spec: ParameterSpec, reading: Reading) -> bool:
    """Whether one measurement meets its parameter's specification.

    Raises :class:`ValidationError` when the reading is in the wrong form for the
    parameter: a numeric measurement typed into the text field would be compared
    as a string, and that is how "10" comes out below "9".
    """
    if spec.parameter_type is ParameterType.NUMERIC:
        if reading.numeric is None:
            raise ValidationError(
                f"'{spec.name}' is a NUMERIC parameter and needs a numeric reading.",
                details={"parameter_id": str(spec.id)},
            )
        # ASSUMPTION: a parameter with neither bound is informational, never a
        # failure. A target with no tolerance band cannot be met by a real
        # measurement, so judging against it would reject every lot.
        value = Decimal(reading.numeric)
        if spec.min_value is not None and value < spec.min_value:
            return False
        return not (spec.max_value is not None and value > spec.max_value)

    if spec.parameter_type is ParameterType.BOOLEAN:
        if reading.boolean is None:
            raise ValidationError(
                f"'{spec.name}' is a BOOLEAN parameter and needs a true/false reading.",
                details={"parameter_id": str(spec.id)},
            )
        # ASSUMPTION: a BOOLEAN parameter passes on true. No column says which
        # way round the check reads, and "conforms / does not conform" is how
        # every check sheet in the master rules is worded.
        return reading.boolean

    if reading.text is None:
        raise ValidationError(
            f"'{spec.name}' is a {spec.parameter_type} parameter and needs a text reading.",
            details={"parameter_id": str(spec.id)},
        )
    value_text = reading.text.strip()
    if spec.parameter_type is ParameterType.SELECT or spec.allowed_values:
        # Exact match after trimming, deliberately not case-folded: the value is
        # picked from a list the template supplied, and folding could merge two
        # grades a specification distinguishes.
        return value_text in spec.allowed_values
    return True


def evaluate_inspection(
    specs: Sequence[ParameterSpec], recorded: dict[uuid.UUID, bool]
) -> ParameterEvaluation:
    """Turn a set of recorded pass/fail flags into a recommendation.

    ``recorded`` maps parameter id to whether the reading met specification.

    The critical override is the rule this function exists for: a critical
    parameter out of specification rejects the whole lot regardless of every
    other reading, and regardless of whether other mandatory readings are still
    missing. Once that characteristic has failed, no further measurement can
    change the answer, so demanding the rest of the sheet first would only delay
    a verdict that is already settled.
    """
    known = {spec.id for spec in specs}
    relevant = {key: value for key, value in recorded.items() if key in known}

    critical_failures = tuple(
        spec.name for spec in specs if spec.is_critical and relevant.get(spec.id) is False
    )
    failures = tuple(
        spec.name for spec in specs if not spec.is_critical and relevant.get(spec.id) is False
    )
    missing = tuple(spec.name for spec in specs if spec.is_mandatory and spec.id not in relevant)

    measured_count = len(relevant)
    in_spec_count = sum(1 for passed in relevant.values() if passed)

    if critical_failures:
        recommended, conclusive = InspectionResult.REJECTED, True
    elif missing:
        recommended, conclusive = InspectionResult.PENDING, False
    elif failures:
        # ASSUMPTION: a deviation nobody classified as critical recommends
        # REWORK, not ACCEPTED. Advisory only - the inspector decides - but
        # defaulting a deviating lot towards acceptance would make the whole
        # check sheet decorative.
        recommended, conclusive = InspectionResult.REWORK, True
    else:
        recommended, conclusive = InspectionResult.ACCEPTED, True

    return ParameterEvaluation(
        recommended_result=recommended,
        is_conclusive=conclusive,
        critical_failures=critical_failures,
        failures=failures,
        missing_mandatory=missing,
        measured_count=measured_count,
        in_spec_count=in_spec_count,
    )


def derive_result(disposition: Disposition) -> InspectionResult:
    """The label that best describes a disposition.

    The bucket holding the most quantity wins; an exact tie goes to the more
    severe of the two (:data:`SEVERITY_ORDER`). See the module's Assumptions.
    """
    quantities: dict[InspectionResult, Decimal] = {
        InspectionResult.ACCEPTED: disposition.accepted,
        InspectionResult.REWORK: disposition.rework,
        InspectionResult.HOLD: disposition.hold,
        InspectionResult.REJECTED: disposition.rejected,
        InspectionResult.SCRAP: disposition.scrap,
    }
    if all(value <= ZERO for value in quantities.values()):
        raise BusinessRuleError("A decision has to disposition some quantity.")
    return max(
        SEVERITY_ORDER,
        key=lambda member: (quantities[member], SEVERITY_ORDER.index(member)),
    )


def status_for(disposition: Disposition, result: InspectionResult) -> InspectionStatus:
    """Where a decision leaves the *document*, which is not the same question as
    what it concluded about the *goods*.

    ASSUMPTION: any quantity left on QC hold keeps the document in a re-openable
    state, and REWORK outranks HOLD when both carry quantity.

    ``result`` is the verdict; ``status`` is what is left to do. A lot of 100 with
    60 released and 40 sent for rework has a verdict of ACCEPTED - most of it was
    good - but the document is not finished, because 40 pieces are still sitting
    in the QC hold pool owing somebody a second decision. Stamping it ACCEPTED
    would close the only door those 40 pieces have out of the pool.

    So any quantity left on hold keeps the document in a re-openable state, and
    the verdict only becomes the status once nothing is held. REWORK wins over
    HOLD when both carry quantity: rework is work somebody has been asked to do,
    where hold is a question somebody has been asked to answer, and the work list
    is the more actionable of the two.
    """
    if disposition.rework > ZERO:
        return InspectionStatus.REWORK
    if disposition.hold > ZERO:
        return InspectionStatus.HOLD
    return InspectionStatus(result.value)


def validate_disposition(inspected_quantity: Decimal, disposition: Disposition) -> None:
    """Every piece must be accounted for, and none invented.

    Enforced here as well as by the database's ``closed_disposition_balances``
    check so the caller is told *which* numbers do not add up, rather than
    receiving an integrity error naming a constraint.
    """
    inspected = to_quantity(Decimal(inspected_quantity))
    if any(
        value < ZERO
        for value in (
            disposition.accepted,
            disposition.rejected,
            disposition.rework,
            disposition.hold,
            disposition.scrap,
        )
    ):
        raise BusinessRuleError("A disposition quantity cannot be negative.")

    total = to_quantity(disposition.total)
    if total != inspected:
        raise BusinessRuleError(
            f"The disposition accounts for {total} of {inspected} inspected. Accepted, "
            "rejected, rework, hold and scrap together must equal the inspected quantity - "
            "anything left over is stock sitting on QC hold that no decision explains.",
            details={
                "inspected_quantity": str(inspected),
                "dispositioned_quantity": str(total),
                "accepted_quantity": str(disposition.accepted),
                "rejected_quantity": str(disposition.rejected),
                "rework_quantity": str(disposition.rework),
                "hold_quantity": str(disposition.hold),
                "scrap_quantity": str(disposition.scrap),
            },
        )


def assert_critical_override(evaluation: ParameterEvaluation, disposition: Disposition) -> None:
    """A critical failure forbids accepting or reworking any of the lot.

    ASSUMPTION: routing part of such a lot to SCRAP stays permitted. The contract
    forces REJECTED "regardless of the others", and scrap is a strictly harsher
    disposition than rejection, never a softer one.
    """
    if not evaluation.has_critical_failure:
        return
    if disposition.accepted > ZERO or disposition.rework > ZERO:
        raise BusinessRuleError(
            "A critical characteristic is out of specification ("
            + ", ".join(evaluation.critical_failures)
            + "), so none of this lot can be accepted or sent for rework. Reject or scrap it.",
            details={
                "critical_failures": list(evaluation.critical_failures),
                "accepted_quantity": str(disposition.accepted),
                "rework_quantity": str(disposition.rework),
            },
        )


def disposition_delta(applied: Disposition, requested: Disposition) -> Disposition:
    """What still has to leave QC hold to get from one disposition to another.

    Accepted, rejected and scrapped quantities have physically left the QC pool
    and cannot be taken back - the released pieces may already have been issued,
    sold or consumed. So those three may only grow, and the delta is what to post
    now. Rework and hold never moved, so they are simply restated.
    """
    for label, before, after in (
        ("accepted", applied.accepted, requested.accepted),
        ("rejected", applied.rejected, requested.rejected),
        ("scrapped", applied.scrap, requested.scrap),
    ):
        if after < before:
            raise BusinessRuleError(
                f"{before} has already been {label} on this inspection and cannot be reduced to "
                f"{after}: that quantity has left QC hold and may already have been used.",
                details={"bucket": label, "already_applied": str(before), "requested": str(after)},
            )
    return Disposition(
        accepted=to_quantity(requested.accepted - applied.accepted),
        rejected=to_quantity(requested.rejected - applied.rejected),
        scrap=to_quantity(requested.scrap - applied.scrap),
        rework=requested.rework,
        hold=requested.hold,
    )


def rejection_rate(totals: QualityTotals) -> Decimal:
    """Rejected plus scrapped, over inspected, as a percentage.

    Scrap counts as a rejection: material written off at inspection failed just
    as surely as material sent back, and excluding it would let a supplier's rate
    improve by having their defects destroyed rather than returned.
    """
    if totals.inspected_quantity <= ZERO:
        return ZERO
    refused = totals.rejected_quantity + totals.scrap_quantity
    return (refused / totals.inspected_quantity * Decimal("100")).quantize(Decimal("0.0001"))


# =============================================================================
# Shared helpers
# =============================================================================


def _resolve_window(window_days: int | None) -> tuple[datetime, datetime, int]:
    """Clamp and materialise a reporting window.

    A window is validated rather than silently truncated: a caller who asked for
    five years of history and quietly received two would draw the wrong
    conclusion from the chart, which is worse than an error (section 25).
    """
    days = DEFAULT_REPORT_WINDOW_DAYS if window_days is None else int(window_days)
    if days < 1 or days > MAX_REPORT_WINDOW_DAYS:
        raise ValidationError(
            f"The reporting window must be between 1 and {MAX_REPORT_WINDOW_DAYS} days; "
            "anything longer is an export, not a screen.",
            details={"window_days": days, "max_window_days": MAX_REPORT_WINDOW_DAYS},
        )
    until = utcnow()
    return until - timedelta(days=days), until, days


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    inspection: QcInspection,
    target: InspectionStatus,
    *,
    reason: str,
    repo: QcInspectionRepository,
    action: AuditAction = AuditAction.STATUS_CHANGE,
    result: InspectionResult | None = None,
) -> QcInspection:
    """Move an inspection's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``qc_inspections.status`` and exactly one that writes the matching
    audit row (master rules section 73). QC status changes must be audited - the
    master rules say so explicitly - which is why the two are not separable.

    ``result`` is written here too, rather than by the caller, because the
    database requires the pair to agree: a row whose status says decided while
    its result still says PENDING violates ``status_and_result_agree``. Setting
    them in two steps would trip that constraint on whichever flush came first,
    so the one function that owns the status column owns the verdict beside it.
    """
    current = InspectionStatus(inspection.status)
    QC_INSPECTION_LIFECYCLE.assert_transition(current, target)
    inspection.status = target
    if result is not None:
        inspection.result = result
    repo.touch(inspection)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_QC_INSPECTION,
        entity_id=inspection.id,
        entity_code=inspection.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return inspection


def template_detail(
    template: QcTemplate, parameters: Sequence[QcTemplateParameter]
) -> TemplateDetail:
    detail = TemplateDetail.model_validate(template)
    detail.parameters = [TemplateParameterRead.model_validate(row) for row in parameters]
    return detail


def inspection_detail(
    inspection: QcInspection,
    parameters: Sequence[QcTemplateParameter],
    results: Sequence[QcInspectionResult],
) -> InspectionDetail:
    detail = InspectionDetail.model_validate(inspection)
    detail.parameters = [TemplateParameterRead.model_validate(row) for row in parameters]
    detail.results = [InspectionResultRead.model_validate(row) for row in results]
    return detail


def evaluation_read(evaluation: ParameterEvaluation) -> ParameterEvaluationRead:
    return ParameterEvaluationRead(
        recommended_result=evaluation.recommended_result,
        is_conclusive=evaluation.is_conclusive,
        has_critical_failure=evaluation.has_critical_failure,
        critical_failures=list(evaluation.critical_failures),
        failures=list(evaluation.failures),
        missing_mandatory=list(evaluation.missing_mandatory),
        measured_count=evaluation.measured_count,
        in_spec_count=evaluation.in_spec_count,
    )


async def _load_sheet(
    session: AsyncSession, tenant: TenantContext, inspection: QcInspection
) -> tuple[list[QcTemplateParameter], list[QcInspectionResult]]:
    """The check sheet and what has been recorded against it - two statements.

    Both are read whole rather than paged: a verdict computed from part of the
    sheet would miss the critical parameter that rejects the lot. The sets are
    bounded by ``MAX_PARAMETERS_PER_TEMPLATE``.
    """
    parameters: list[QcTemplateParameter] = []
    if inspection.template_id is not None:
        parameters = await QcTemplateParameterRepository(session, tenant).list_for_template(
            inspection.template_id
        )
    results = await QcInspectionResultRepository(session, tenant).list_for_inspection(inspection.id)
    return parameters, results


def _evaluate(
    parameters: Sequence[QcTemplateParameter], results: Sequence[QcInspectionResult]
) -> ParameterEvaluation:
    specs = [ParameterSpec.from_model(row) for row in parameters]
    recorded = {row.parameter_id: row.is_within_spec for row in results}
    return evaluate_inspection(specs, recorded)


# =============================================================================
# Templates
# =============================================================================


def _write_parameters(
    session: AsyncSession,
    tenant: TenantContext,
    template: QcTemplate,
    parameters: Iterable[TemplateParameterIn],
) -> list[QcTemplateParameter]:
    """Stage a template's whole check sheet. The caller flushes."""
    rows: list[QcTemplateParameter] = []
    for parameter in parameters:
        row = QcTemplateParameter(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            template_id=template.id,
            sequence=parameter.sequence,
            parameter_name=parameter.parameter_name.strip(),
            parameter_type=parameter.parameter_type,
            unit=parameter.unit,
            min_value=parameter.min_value,
            max_value=parameter.max_value,
            target_value=parameter.target_value,
            allowed_values=list(parameter.allowed_values) if parameter.allowed_values else None,
            is_critical=parameter.is_critical,
            is_mandatory=parameter.is_mandatory,
        )
        session.add(row)
        rows.append(row)
    return rows


async def _assert_scope_is_free(
    repo: QcTemplateRepository,
    *,
    item_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
    stage: QcStage,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Refuse a second active template claiming the same scope.

    The partial unique indexes are the real guarantee; this check exists so the
    caller is told which template already owns the scope instead of receiving an
    integrity error naming an index.
    """
    existing = await repo.active_for_scope(
        item_id=item_id, category_id=category_id, stage=stage, exclude_id=exclude_id
    )
    if existing is not None:
        raise DuplicateError(
            f"{existing.code} v{existing.version} is already the active {stage} template for "
            "this scope. Retire it, or raise a new version of it.",
            details={"template_id": str(existing.id), "code": existing.code},
        )


async def create_template(
    session: AsyncSession, tenant: TenantContext, payload: TemplateCreate
) -> tuple[QcTemplate, list[QcTemplateParameter]]:
    """Create version 1 of an inspection specification."""
    repo = QcTemplateRepository(session, tenant)

    async with transaction(session):
        if await repo.get_by_code_version(payload.code, 1) is not None:
            raise DuplicateError(
                f"A QC template with code {payload.code} already exists.",
                details={"code": payload.code},
            )
        if payload.is_active:
            await _assert_scope_is_free(
                repo,
                item_id=payload.item_id,
                category_id=payload.item_category_id,
                stage=payload.qc_stage,
            )

        template = QcTemplate(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            item_id=payload.item_id,
            item_category_id=payload.item_category_id,
            qc_stage=payload.qc_stage,
            sampling_plan=payload.sampling_plan,
            sample_percentage=payload.sample_percentage,
            sample_size=payload.sample_size,
            is_active=payload.is_active,
            version=1,
        )
        repo.add(template)
        await session.flush()

        parameters = _write_parameters(session, tenant, template, payload.parameters)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_QC_TEMPLATE,
            entity_id=template.id,
            entity_code=template.code,
            summary=f"QC template {template.code} v1 created with {len(parameters)} parameter(s)",
            user_id=tenant.user_id,
            new_values=snapshot(template, _TEMPLATE_AUDITED_FIELDS),
        )
    return template, parameters


async def update_template(
    session: AsyncSession, tenant: TenantContext, template_id: uuid.UUID, payload: TemplateUpdate
) -> tuple[QcTemplate, list[QcTemplateParameter]]:
    """Amend a template.

    Presentation and activation are always editable. Anything that changes what
    the template *means* - its scope, its sampling plan or its parameters - is
    editable only while no inspection has applied it; afterwards the change forks
    a new version through :func:`create_template_version`, so a past pass or fail
    stays reproducible against the specification it was judged by.
    """
    repo = QcTemplateRepository(session, tenant)
    parameter_repo = QcTemplateParameterRepository(session, tenant)
    inspection_repo = QcInspectionRepository(session, tenant)

    meaning_fields = {
        "item_id",
        "item_category_id",
        "qc_stage",
        "sampling_plan",
        "sample_percentage",
        "sample_size",
        "parameters",
    }

    async with transaction(session):
        template = await repo.lock_for_update(template_id)
        if template is None:
            raise NotFoundError("QC template", template_id)
        before = snapshot(template, _TEMPLATE_AUDITED_FIELDS)

        changing_meaning = bool(payload.model_fields_set & meaning_fields)
        if changing_meaning:
            used = await inspection_repo.count_for_template(template.id)
            if used:
                raise BusinessRuleError(
                    f"{template.code} v{template.version} has been applied to {used} "
                    "inspection(s), so its specification is frozen. Raise a new version instead - "
                    "editing it in place would restate whether those inspections passed.",
                    code=ErrorCode.REFERENCED_RECORD_IN_USE,
                    details={"template_id": str(template.id), "inspection_count": used},
                )

        for field in ("name", "description", "is_active"):
            if field in payload.model_fields_set:
                setattr(template, field, getattr(payload, field))
        for field in (
            "item_id",
            "item_category_id",
            "qc_stage",
            "sampling_plan",
            "sample_percentage",
            "sample_size",
        ):
            if field in payload.model_fields_set:
                setattr(template, field, getattr(payload, field))

        _assert_plan_is_complete(template)
        if template.is_active:
            await _assert_scope_is_free(
                repo,
                item_id=template.item_id,
                category_id=template.item_category_id,
                stage=QcStage(template.qc_stage),
                exclude_id=template.id,
            )

        if payload.parameters is not None:
            await parameter_repo.delete_for_template(template.id)
            _write_parameters(session, tenant, template, payload.parameters)

        repo.touch(template)
        await session.flush()
        parameters = await parameter_repo.list_for_template(template.id)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_QC_TEMPLATE,
            entity_id=template.id,
            entity_code=template.code,
            summary=f"QC template {template.code} v{template.version} updated",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(template, _TEMPLATE_AUDITED_FIELDS),
        )
    return template, parameters


def _assert_plan_is_complete(template: QcTemplate) -> None:
    """A sampling plan without the number it needs is not a plan."""
    plan = SamplingPlan(template.sampling_plan)
    if plan is SamplingPlan.PERCENTAGE and not template.sample_percentage:
        raise BusinessRuleError("A PERCENTAGE sampling plan needs a sample percentage.")
    if plan is SamplingPlan.FIXED_SAMPLE and not template.sample_size:
        raise BusinessRuleError("A FIXED_SAMPLE sampling plan needs a sample size.")
    if plan is not SamplingPlan.PERCENTAGE:
        template.sample_percentage = None
    if plan is not SamplingPlan.FIXED_SAMPLE:
        template.sample_size = None


async def create_template_version(
    session: AsyncSession,
    tenant: TenantContext,
    template_id: uuid.UUID,
    payload: TemplateVersionRequest,
) -> tuple[QcTemplate, list[QcTemplateParameter]]:
    """Fork a template into a new revision.

    The new version copies the current one's check sheet unless a replacement is
    supplied, so changing one tolerance does not mean retyping forty rows. The
    previous version is retired by default: two active templates claiming the
    same scope is refused by the partial unique indexes anyway, and leaving both
    live is only meaningful during a deliberate changeover.
    """
    repo = QcTemplateRepository(session, tenant)
    parameter_repo = QcTemplateParameterRepository(session, tenant)

    async with transaction(session):
        source = await repo.lock_for_update(template_id)
        if source is None:
            raise NotFoundError("QC template", template_id)

        existing_parameters = await parameter_repo.list_for_template(source.id)
        next_version = await repo.latest_version(source.code) + 1

        if payload.retire_previous:
            source.is_active = False
            repo.touch(source)
            await session.flush()

        template = QcTemplate(
            code=source.code,
            name=payload.name or source.name,
            description=(
                payload.description if payload.description is not None else source.description
            ),
            item_id=source.item_id,
            item_category_id=source.item_category_id,
            qc_stage=source.qc_stage,
            sampling_plan=payload.sampling_plan or source.sampling_plan,
            sample_percentage=(
                payload.sample_percentage
                if payload.sampling_plan is not None
                else source.sample_percentage
            ),
            sample_size=(
                payload.sample_size if payload.sampling_plan is not None else source.sample_size
            ),
            is_active=True,
            version=next_version,
        )
        repo.add(template)
        _assert_plan_is_complete(template)
        await session.flush()

        if not payload.retire_previous:
            await _assert_scope_is_free(
                repo,
                item_id=template.item_id,
                category_id=template.item_category_id,
                stage=QcStage(template.qc_stage),
                exclude_id=template.id,
            )

        if payload.parameters is not None:
            _write_parameters(session, tenant, template, payload.parameters)
        else:
            _write_parameters(
                session,
                tenant,
                template,
                [
                    TemplateParameterIn(
                        sequence=row.sequence,
                        parameter_name=row.parameter_name,
                        parameter_type=ParameterType(row.parameter_type),
                        unit=row.unit,
                        min_value=row.min_value,
                        max_value=row.max_value,
                        target_value=row.target_value,
                        allowed_values=list(row.allowed_values) if row.allowed_values else None,
                        is_critical=row.is_critical,
                        is_mandatory=row.is_mandatory,
                    )
                    for row in existing_parameters
                ],
            )
        await session.flush()
        parameters = await parameter_repo.list_for_template(template.id)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_QC_TEMPLATE,
            entity_id=template.id,
            entity_code=template.code,
            summary=(
                f"QC template {template.code} v{next_version} raised from v{source.version}: "
                f"{payload.reason}"
            ),
            user_id=tenant.user_id,
            new_values=snapshot(template, _TEMPLATE_AUDITED_FIELDS),
            context={"superseded_template_id": str(source.id), "reason": payload.reason},
        )
    return template, parameters


async def retire_template(
    session: AsyncSession, tenant: TenantContext, template_id: uuid.UUID, *, reason: str
) -> QcTemplate:
    """Retire a template so it stops being resolved.

    Soft deletion, never a hard one: inspections name the template they followed,
    and a row that vanished would take the meaning of that record with it
    (section 28).
    """
    repo = QcTemplateRepository(session, tenant)

    async with transaction(session):
        template = await repo.lock_for_update(template_id)
        if template is None:
            raise NotFoundError("QC template", template_id)
        before = snapshot(template, _TEMPLATE_AUDITED_FIELDS)

        template.is_active = False
        await repo.soft_delete(template)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_QC_TEMPLATE,
            entity_id=template.id,
            entity_code=template.code,
            summary=f"QC template {template.code} v{template.version} retired: {reason}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(template, _TEMPLATE_AUDITED_FIELDS),
            context={"reason": reason},
        )
    return template


async def list_templates(
    session: AsyncSession,
    tenant: TenantContext,
    filters: TemplateFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[QcTemplate], OffsetPagination]:
    return await QcTemplateRepository(session, tenant).list_templates(filters, params, sort)


async def get_template_detail(
    session: AsyncSession, tenant: TenantContext, template_id: uuid.UUID
) -> TemplateDetail:
    repo = QcTemplateRepository(session, tenant)
    template = await repo.get_or_404(template_id)
    parameters = await QcTemplateParameterRepository(session, tenant).list_for_template(template.id)
    return template_detail(template, parameters)


# =============================================================================
# Raising inspections
# =============================================================================


async def _prepare(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    stage: QcStage,
    template_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
) -> tuple[InspectableItem, QcTemplate | None, Decimal]:
    """Validate the position and work out how much of it to measure.

    Three batched lookups, never one per field: the item projection, the
    warehouse projection and the template resolution. The batch requirement is
    checked here rather than at decision time because an inspection raised
    against a batch-tracked item with no batch could never be decided - the
    inventory engine will not move such a quantity - and failing at creation
    tells the receiving module immediately instead of stranding the stock.
    """
    items = await ItemLookupRepository(session, tenant).load([item_id])
    item = items.get(item_id)
    if item is None:
        raise NotFoundError("Item", item_id)
    if not item.is_active:
        raise BusinessRuleError(
            f"Item {item.code} is not active and cannot be inspected.",
            details={"item_id": str(item_id)},
        )

    warehouses = await WarehouseLookupRepository(session, tenant).load([warehouse_id])
    warehouse = warehouses.get(warehouse_id)
    if warehouse is None:
        raise NotFoundError("Warehouse", warehouse_id)
    if not warehouse[1]:
        raise BusinessRuleError(
            f"Warehouse {warehouse[0]} is not active.", details={"warehouse_id": str(warehouse_id)}
        )

    if item.is_batch_tracked and batch_id is None:
        raise BusinessRuleError(
            f"Item {item.code} is batch tracked, so the inspection must name the lot it covers - "
            "without one, the decision could not move the quantity out of QC hold.",
            code=ErrorCode.BATCH_REQUIRED,
            details={"item_id": str(item_id)},
        )

    template_repo = QcTemplateRepository(session, tenant)
    if template_id is not None:
        template = await template_repo.get(template_id)
        if template is None:
            raise NotFoundError("QC template", template_id)
    else:
        template = await template_repo.resolve(
            item_id=item_id, category_id=item.category_id, stage=stage
        )

    if template is None:
        # ASSUMPTION: no specification on file means inspect the lot in full
        # rather than sample it. A sample size invented for an unknown
        # specification is a guess presented as a statistic.
        sample = to_quantity(Decimal(quantity))
    else:
        sample = compute_sample_size(
            plan=SamplingPlan(template.sampling_plan),
            received_quantity=quantity,
            sample_percentage=template.sample_percentage,
            sample_size=template.sample_size,
        )
    return item, template, sample


async def _raise_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    stage: QcStage,
    reference_type: str,
    reference_id: uuid.UUID | None,
    reference_code: str | None,
    vendor_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
    template_id: uuid.UUID | None,
    remarks: str | None,
    hold_stock: bool,
    reason: str,
) -> QcInspection:
    """The one code path that creates an inspection.

    Every caller - the goods receipt, the job work return, the assembly
    completion and the manual endpoint - lands here, so the document number, the
    sampling decision, the QC hold policy and the audit entry cannot diverge
    between them.
    """
    quantity = to_quantity(Decimal(quantity))
    if quantity <= ZERO:
        raise BusinessRuleError("An inspection needs a quantity to inspect.")

    repo = QcInspectionRepository(session, tenant)

    async with transaction(session):
        item, template, sample = await _prepare(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            stage=stage,
            template_id=template_id,
            batch_id=batch_id,
        )

        code = await next_document_number(session, tenant.company_id, DocTypes.QC_INSPECTION)
        inspection = QcInspection(
            code=code,
            qc_stage=stage,
            item_id=item_id,
            warehouse_id=warehouse_id,
            batch_id=batch_id,
            template_id=None if template is None else template.id,
            template_version=None if template is None else template.version,
            vendor_id=vendor_id,
            reference_type=reference_type,
            reference_id=reference_id,
            reference_code=reference_code,
            inspected_quantity=quantity,
            sample_size=sample,
            result=InspectionResult.PENDING,
            status=InspectionStatus.PENDING,
            remarks=remarks,
        )
        repo.add(inspection)
        await session.flush()

        if hold_stock:
            # Only the manual path asks for this. An inspection raised by a
            # receipt is inspecting stock that receipt already moved to QC_HOLD,
            # and holding it again would take the quantity out of availability a
            # second time - see the module docstring.
            await change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=quantity,
                from_state=StockState.AVAILABLE,
                to_state=StockState.QC_HOLD,
                reason=f"{code}: {reason}",
                batch_id=batch_id,
                reference_type=MOVEMENT_REFERENCE_QC,
                reference_id=inspection.id,
                reference_code=code,
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_QC_INSPECTION,
            entity_id=inspection.id,
            entity_code=code,
            summary=(
                f"{stage} inspection {code} raised for {quantity} {item.code}"
                + (f" against {reference_code}" if reference_code else "")
            ),
            user_id=tenant.user_id,
            new_values={
                "item_id": item_id,
                "warehouse_id": warehouse_id,
                "batch_id": batch_id,
                "vendor_id": vendor_id,
                "inspected_quantity": quantity,
                "sample_size": sample,
                "template_id": inspection.template_id,
                "template_version": inspection.template_version,
                "qc_stage": stage,
                "held_stock": hold_stock,
            },
            context={"reason": reason, "reference_code": reference_code},
        )

    log.info(
        "quality.inspection_raised",
        inspection_id=str(inspection.id),
        code=code,
        stage=str(stage),
        item_id=str(item_id),
        quantity=str(quantity),
        sample_size=str(sample),
        template_id=str(inspection.template_id) if inspection.template_id else None,
    )
    return inspection


async def create_incoming_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    quantity: Decimal,
    warehouse_id: uuid.UUID,
    reference_type: str,
    reference_id: uuid.UUID,
    reference_code: str,
    vendor_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> QcInspection:
    """Raise an incoming inspection for goods a receipt has put on QC hold.

    The cross-module entry point procurement calls after posting a GRN
    (database.md section 8). The signature is fixed by that contract. The receipt
    has already posted the quantity into ``QC_HOLD``, so nothing is held here -
    this records what has to be inspected and against which specification.
    """
    return await _raise_inspection(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        stage=QcStage.INCOMING,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_code=reference_code,
        vendor_id=vendor_id,
        batch_id=batch_id,
        template_id=None,
        remarks=None,
        hold_stock=False,
        reason=f"Incoming inspection raised by {reference_code}",
    )


async def create_in_process_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    quantity: Decimal,
    warehouse_id: uuid.UUID,
    reference_type: str,
    reference_id: uuid.UUID,
    reference_code: str,
    vendor_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> QcInspection:
    """Raise an inspection for output a production step has put on QC hold.

    The cross-module entry point the assembly and job work modules call on
    completion. The stage is derived from ``reference_type`` through
    :data:`STAGE_BY_REFERENCE_TYPE`, so a job work return is reported as JOB_WORK
    and an assembly as ASSEMBLY without either caller having to know this
    module's vocabulary. As with the incoming path, the caller has already held
    the stock.
    """
    return await _raise_inspection(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        stage=STAGE_BY_REFERENCE_TYPE.get(reference_type, DEFAULT_AUTO_STAGE),
        reference_type=reference_type,
        reference_id=reference_id,
        reference_code=reference_code,
        vendor_id=vendor_id,
        batch_id=batch_id,
        template_id=None,
        remarks=None,
        hold_stock=False,
        reason=f"In-process inspection raised by {reference_code}",
    )


async def create_inspection(
    session: AsyncSession, tenant: TenantContext, payload: InspectionCreate
) -> QcInspection:
    """Raise an inspection by hand - a supervisor pulling a lot off the shelf."""
    return await _raise_inspection(
        session,
        tenant,
        item_id=payload.item_id,
        warehouse_id=payload.warehouse_id,
        quantity=payload.inspected_quantity,
        stage=payload.qc_stage,
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
        reference_code=payload.reference_code,
        vendor_id=payload.vendor_id,
        batch_id=payload.batch_id,
        template_id=payload.template_id,
        remarks=payload.remarks,
        hold_stock=payload.hold_stock,
        reason=payload.reason,
    )


# =============================================================================
# Recording measurements
# =============================================================================


async def record_results(
    session: AsyncSession,
    tenant: TenantContext,
    inspection_id: uuid.UUID,
    payload: RecordResultsRequest,
) -> RecordResultsResponse:
    """Record or correct measurements against the inspection's check sheet.

    Each reading is judged against its parameter's specification on the server
    and the pass/fail is stored, never accepted from the client - a caller that
    could assert its own ``is_within_spec`` would make the check sheet
    decorative. A second reading for the same parameter replaces the first: a
    correction, not an extra row, because two rows would make "did it pass?"
    ambiguous.
    """
    repo = QcInspectionRepository(session, tenant)
    result_repo = QcInspectionResultRepository(session, tenant)

    async with transaction(session):
        inspection = await repo.lock_for_update(inspection_id)
        if inspection is None:
            raise NotFoundError("QC inspection", inspection_id)

        status = InspectionStatus(inspection.status)
        if status not in (*OPEN_STATUSES, *REOPENABLE_STATUSES):
            raise BusinessRuleError(
                f"{inspection.code} is {status}; its stock has already left QC hold, so the "
                "readings behind that decision can no longer be changed.",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                details={"status": str(status)},
            )
        if inspection.template_id is None:
            raise BusinessRuleError(
                f"{inspection.code} was raised without a QC template, so it has no parameters to "
                "measure. Decide it on the inspector's judgement, or raise a template for this "
                "item and re-raise the inspection.",
                details={"inspection_id": str(inspection.id)},
            )

        parameters = await QcTemplateParameterRepository(session, tenant).list_for_template(
            inspection.template_id
        )
        by_id = {row.id: row for row in parameters}
        unknown = [
            str(measurement.parameter_id)
            for measurement in payload.measurements
            if measurement.parameter_id not in by_id
        ]
        if unknown:
            raise ValidationError(
                "Those parameters do not belong to this inspection's check sheet.",
                details={"unknown_parameter_ids": unknown},
            )

        existing = await result_repo.map_for_inspection(inspection.id)
        recorded_names: list[str] = []
        failed_names: list[str] = []

        for measurement in payload.measurements:
            spec = ParameterSpec.from_model(by_id[measurement.parameter_id])
            within_spec = evaluate_reading(spec, Reading.from_payload(measurement))
            recorded_names.append(spec.name)
            if not within_spec:
                failed_names.append(spec.name)

            row = existing.get(measurement.parameter_id)
            if row is None:
                row = QcInspectionResult(
                    company_id=tenant.company_id,
                    created_by_id=tenant.user_id,
                    inspection_id=inspection.id,
                    parameter_id=measurement.parameter_id,
                )
                session.add(row)
                existing[measurement.parameter_id] = row
            row.measured_value_numeric = measurement.measured_value_numeric
            row.measured_value_text = measurement.measured_value_text
            row.measured_value_bool = measurement.measured_value_bool
            row.is_within_spec = within_spec
            row.remark = measurement.remark
            row.photo_document_id = measurement.photo_document_id
            row.updated_by_id = tenant.user_id

        if payload.remarks is not None:
            inspection.remarks = payload.remarks
        repo.touch(inspection)
        await session.flush()

        if status is InspectionStatus.PENDING:
            await _transition(
                session,
                tenant,
                inspection,
                InspectionStatus.IN_PROGRESS,
                reason=f"First measurements recorded on {inspection.code}",
                repo=repo,
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_QC_INSPECTION,
            entity_id=inspection.id,
            entity_code=inspection.code,
            summary=(
                f"{len(recorded_names)} measurement(s) recorded on {inspection.code}"
                + (f"; out of specification: {', '.join(failed_names)}" if failed_names else "")
            ),
            user_id=tenant.user_id,
            new_values={"parameters": recorded_names, "out_of_specification": failed_names},
        )

        results = await result_repo.list_for_inspection(inspection.id)
        evaluation = _evaluate(parameters, results)

    return RecordResultsResponse(
        inspection=inspection_detail(inspection, parameters, results),
        evaluation=evaluation_read(evaluation),
    )


# =============================================================================
# The decision - the stock consequence
# =============================================================================


#: What each disposition bucket does to the held quantity. Declared as data so
#: the mapping the master rules specify is stated once and read by both the
#: posting loop and the tests, rather than being spread through an if-chain.
STATE_FOR_DISPOSITION: dict[InspectionResult, StockState | None] = {
    InspectionResult.ACCEPTED: StockState.AVAILABLE,
    InspectionResult.REJECTED: StockState.DAMAGED,
    InspectionResult.SCRAP: StockState.SCRAP,
    # Rework and hold move nothing: the quantity stays exactly where it is, in
    # the QC hold pool, and is re-decided once the rework is done or the
    # question is answered.
    InspectionResult.REWORK: None,
    InspectionResult.HOLD: None,
}


def _assert_decidable(inspection: QcInspection) -> None:
    """Refuse a verdict on a document that can no longer take one.

    Checked before any arithmetic so the caller is told the real reason - this
    inspection's stock has already left QC hold - rather than a complaint that
    the quantities do not reconcile, which is a true statement about a question
    nobody should have been allowed to ask.
    """
    current = InspectionStatus(inspection.status)
    reachable = set(QC_INSPECTION_LIFECYCLE.allowed_targets(current))
    if not reachable & set(DECIDED_STATUSES):
        raise InvalidStateTransitionError(
            entity=QC_INSPECTION_LIFECYCLE.entity,
            current=str(current),
            requested="a new verdict",
            allowed=[str(state) for state in reachable],
        )


async def decide_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    inspection_id: uuid.UUID,
    payload: DecideRequest,
) -> DecisionResponse:
    """Apply a verdict and move the stock it decided. One transaction.

    In order:

    1. Lock the inspection, so two decisions cannot both compute their movements
       from the same quantities.
    2. Load the check sheet and evaluate it. A critical parameter out of
       specification forces REJECTED and forbids accepting or reworking any of
       the lot; a mandatory reading that is still missing blocks the decision
       altogether unless a critical failure has already settled it.
    3. Validate that the disposition accounts for the whole lot, and that nothing
       already released is being taken back.
    4. Post the delta out of ``QC_HOLD`` - to ``AVAILABLE``, ``DAMAGED`` and
       ``SCRAP`` - through ``inventory.change_stock_state``.
    5. Write the verdict, move the status through the state machine, flag a
       vendor return if one is owed, and record the ``QC_DECISION`` audit entry.

    All of it commits together or none of it does. There is no path that records
    a verdict without moving the stock, and none that moves the stock without
    recording who decided what and why.
    """
    repo = QcInspectionRepository(session, tenant)

    async with transaction(session):
        inspection = await repo.lock_for_update(inspection_id)
        if inspection is None:
            raise NotFoundError("QC inspection", inspection_id)
        before = snapshot(inspection, _INSPECTION_AUDITED_FIELDS)
        _assert_decidable(inspection)
        # The owning document's lock comes before any balance row, so a decision
        # locks inspection -> owner -> balances: the order the owner's own
        # posting takes them in (owner -> balances). See ``owners``.
        await owners.lock_owner(session, tenant, inspection)

        parameters, results = await _load_sheet(session, tenant, inspection)
        evaluation = _evaluate(parameters, results)

        if not evaluation.is_conclusive and not evaluation.has_critical_failure:
            raise BusinessRuleError(
                f"{inspection.code} cannot be decided yet: "
                + ", ".join(evaluation.missing_mandatory)
                + " still needs a reading.",
                details={"missing_mandatory": list(evaluation.missing_mandatory)},
            )

        requested = Disposition.from_request(payload)
        validate_disposition(Decimal(inspection.inspected_quantity), requested)
        assert_critical_override(evaluation, requested)

        applied = Disposition.from_inspection(inspection)
        delta = disposition_delta(applied, requested)

        # A critical failure settles the label whatever the split of the lot -
        # this is the "regardless of the others" rule the contract states.
        result = (
            InspectionResult.REJECTED
            if evaluation.has_critical_failure
            else derive_result(requested)
        )
        target = status_for(requested, result)
        # Fail before any stock moves: the state machine is the authority on
        # whether this inspection can be decided at all.
        QC_INSPECTION_LIFECYCLE.assert_transition(InspectionStatus(inspection.status), target)

        # Settled before the movements are posted, because it is also the ledger
        # rows' ``occurred_at``: the business time of a check carried out on the
        # floor last night is not the time it was keyed in this morning, and the
        # stock event has to carry the same instant as the verdict that caused it.
        decided_at = payload.inspected_at or utcnow()
        moved = await _post_decision_movements(
            session, tenant, inspection, delta, reason=payload.reason, occurred_at=decided_at
        )

        inspection.accepted_quantity = requested.accepted
        inspection.rejected_quantity = requested.rejected
        inspection.rework_quantity = requested.rework
        inspection.hold_quantity = requested.hold
        inspection.scrap_quantity = requested.scrap
        inspection.rejection_reason = payload.rejection_reason or inspection.rejection_reason
        if payload.remarks is not None:
            inspection.remarks = payload.remarks
        inspection.inspector_user_id = tenant.user_id
        inspection.inspected_at = decided_at

        # ASSUMPTION: a rejection against a named vendor sets a flag, not a
        # purchase return. Raising the return is procurement's decision, its
        # numbering series and its approval; quality states the requirement and
        # notifies the buyers.
        inspection.return_required = requested.rejected > ZERO and inspection.vendor_id is not None

        # Deliberately not flushed here: ``_transition`` writes the status and
        # the verdict together in one UPDATE, because the database refuses a row
        # that carries one without the other.
        await _transition(
            session,
            tenant,
            inspection,
            target,
            reason=payload.reason,
            repo=repo,
            action=AuditAction.QC_DECISION,
            result=result,
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.QC_DECISION,
            entity_type=AUDIT_QC_INSPECTION,
            entity_id=inspection.id,
            entity_code=inspection.code,
            summary=(
                f"{inspection.code} decided {result}: "
                f"{requested.accepted} accepted, {requested.rejected} rejected, "
                f"{requested.rework} for rework, {requested.hold} held, "
                f"{requested.scrap} scrapped of {inspection.inspected_quantity}"
            ),
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(inspection, _INSPECTION_AUDITED_FIELDS),
            context={
                "reason": payload.reason,
                "released_to_available": moved[StockState.AVAILABLE],
                "moved_to_damaged": moved[StockState.DAMAGED],
                "moved_to_scrap": moved[StockState.SCRAP],
                "critical_failures": list(evaluation.critical_failures),
                "failures": list(evaluation.failures),
                "sample_size": inspection.sample_size,
            },
        )

        if payload.close:
            await _close(session, tenant, inspection, repo=repo, reason=payload.reason)

        # THE ONE RELEASE PATH. The stock has moved; the document that put it on
        # hold is told in the same transaction and completes itself from this
        # verdict without moving anything again. An owner that refuses rolls
        # the decision back with it (``owners`` module docstring).
        await owners.notify_owner(session, tenant, inspection, reason=payload.reason)

        await _announce_failure(session, tenant, inspection, requested)

    return DecisionResponse(
        inspection=inspection_detail(inspection, parameters, results),
        evaluation=evaluation_read(evaluation),
        released_quantity=moved[StockState.AVAILABLE],
        damaged_quantity=moved[StockState.DAMAGED],
        scrapped_quantity=moved[StockState.SCRAP],
        still_on_hold_quantity=to_quantity(requested.still_held),
        return_required=inspection.return_required,
    )


async def _post_decision_movements(
    session: AsyncSession,
    tenant: TenantContext,
    inspection: QcInspection,
    delta: Disposition,
    *,
    reason: str,
    occurred_at: datetime,
) -> dict[StockState, Decimal]:
    """Move the newly dispositioned quantity out of QC hold.

    Posted one target state at a time because ``change_stock_state`` writes one
    ledger row per movement and the three destinations are genuinely three
    different events. They all touch the same balance position, which the
    inventory engine locks and re-reads for each one inside this transaction, so
    the three cannot race each other or anybody else (database.md section 4.4).
    """
    moved: dict[StockState, Decimal] = {
        StockState.AVAILABLE: ZERO,
        StockState.DAMAGED: ZERO,
        StockState.SCRAP: ZERO,
    }
    for verdict, quantity in (
        (InspectionResult.ACCEPTED, delta.accepted),
        (InspectionResult.REJECTED, delta.rejected),
        (InspectionResult.SCRAP, delta.scrap),
    ):
        target_state = STATE_FOR_DISPOSITION[verdict]
        if target_state is None or quantity <= ZERO:
            continue
        await change_stock_state(
            session,
            tenant,
            item_id=inspection.item_id,
            warehouse_id=inspection.warehouse_id,
            quantity=quantity,
            from_state=StockState.QC_HOLD,
            to_state=target_state,
            reason=f"{inspection.code} {verdict}: {reason}",
            batch_id=inspection.batch_id,
            reference_type=MOVEMENT_REFERENCE_QC,
            reference_id=inspection.id,
            reference_code=inspection.code,
            occurred_at=occurred_at,
        )
        moved[target_state] = to_quantity(quantity)
    return moved


async def _announce_failure(
    session: AsyncSession,
    tenant: TenantContext,
    inspection: QcInspection,
    disposition: Disposition,
) -> None:
    """Tell the people who have to act on a failure.

    Inside the deciding transaction, so an alert cannot survive a decision that
    rolled back. ``group_key`` collapses repeat alerts about the same inspection
    onto one unread row rather than adding a line every time a partial
    disposition is revised.
    """
    if disposition.rejected <= ZERO and disposition.scrap <= ZERO:
        return

    refused = to_quantity(disposition.rejected + disposition.scrap)
    if (
        inspection.vendor_id is not None
        and inspection.reference_type == GOODS_RECEIPT_REFERENCE
        and inspection.reference_id is not None
    ):
        # The supplier of an incoming lot is told too (WP C1a), through
        # procurement - which owns which vendor users may see the receipt's
        # order. Only goods receipts: the vendor portal shows receipts and their
        # QC, and a job-work lot's verdict would link to nothing the vendor has.
        from src.core.permissions import Permission
        from src.modules.procurement.service import notify_vendor_about_receipt

        await notify_vendor_about_receipt(
            session,
            tenant,
            inspection.reference_id,
            permission=Permission.VENDOR_PORTAL_QUALITY,
            notification_type=NotificationType.QC_FAILED,
            severity=NotificationSeverity.WARNING,
            title=f"Quality rejection on {inspection.reference_code or inspection.code}",
            body=(
                f"{_plain_quantity(refused)} refused at inspection {inspection.code}. "
                f"Reason: {inspection.rejection_reason or 'not stated'}."
            ),
            entity_type=AUDIT_QC_INSPECTION,
            entity_id=inspection.id,
            group_key=f"vendor-qc:{inspection.id}",
        )
    await notify_role(
        session,
        company_id=tenant.company_id,
        role_codes=QC_FAILURE_ROLE_CODES,
        notification_type=NotificationType.QC_FAILED,
        severity=NotificationSeverity.WARNING,
        title=f"{refused} refused at {inspection.qc_stage} inspection {inspection.code}",
        body=(
            f"{inspection.code} refused {refused} of {inspection.inspected_quantity}. "
            f"Reason: {inspection.rejection_reason or 'not stated'}."
        ),
        entity_type=AUDIT_QC_INSPECTION,
        entity_id=inspection.id,
        group_key=f"qc-failed:{inspection.id}",
        actor_id=tenant.user_id,
    )

    if inspection.return_required:
        await notify_role(
            session,
            company_id=tenant.company_id,
            role_codes=QC_RETURN_ROLE_CODES,
            notification_type=NotificationType.QC_FAILED,
            # CRITICAL: material the company has already taken custody of is
            # sitting in the damaged pool and the vendor has to be told before
            # the invoice is paid. That escalation is why this is not INFO.
            severity=NotificationSeverity.CRITICAL,
            title=f"Vendor return owed for {inspection.code}",
            body=(
                f"{inspection.rejected_quantity} rejected at incoming inspection "
                f"{inspection.code}"
                + (f" against {inspection.reference_code}" if inspection.reference_code else "")
                + ". Raise a purchase return or agree a replacement with the vendor."
            ),
            entity_type=AUDIT_QC_INSPECTION,
            entity_id=inspection.id,
            group_key=f"qc-return:{inspection.id}",
            actor_id=tenant.user_id,
        )


async def _close(
    session: AsyncSession,
    tenant: TenantContext,
    inspection: QcInspection,
    *,
    repo: QcInspectionRepository,
    reason: str,
) -> QcInspection:
    """Close a decided inspection, once nothing of it is still held."""
    disposition = Disposition.from_inspection(inspection)
    validate_disposition(Decimal(inspection.inspected_quantity), disposition)
    if disposition.still_held > ZERO:
        raise BusinessRuleError(
            f"{inspection.code} still has {disposition.still_held} on QC hold for rework or a "
            "further decision; it cannot be closed until that quantity is dispositioned.",
            details={"still_held_quantity": str(disposition.still_held)},
        )

    inspection.closed_at = utcnow()
    return await _transition(
        session,
        tenant,
        inspection,
        InspectionStatus.CLOSED,
        reason=reason,
        repo=repo,
        action=AuditAction.QC_DECISION,
    )


async def close_inspection(
    session: AsyncSession, tenant: TenantContext, inspection_id: uuid.UUID, *, reason: str
) -> QcInspection:
    """Close a decided inspection as a separate step.

    Closing settles nothing new - it is only legal once nothing is held - but
    the owner is told again anyway. Its handler is idempotent, so for an
    inspection decided after the callback existed this changes nothing; for one
    decided before it, closing is the moment its stranded owner is repaired.
    """
    repo = QcInspectionRepository(session, tenant)
    async with transaction(session):
        inspection = await repo.lock_for_update(inspection_id)
        if inspection is None:
            raise NotFoundError("QC inspection", inspection_id)
        await owners.lock_owner(session, tenant, inspection)
        await _close(session, tenant, inspection, repo=repo, reason=reason)
        await owners.notify_owner(session, tenant, inspection, reason=reason)
    return inspection


# =============================================================================
# Reads
# =============================================================================


async def list_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    filters: InspectionFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[QcInspection], OffsetPagination]:
    return await QcInspectionRepository(session, tenant).list_inspections(filters, params, sort)


async def list_pending(
    session: AsyncSession,
    tenant: TenantContext,
    params: PageParams,
    *,
    stage: QcStage | None = None,
    warehouse_id: uuid.UUID | None = None,
) -> tuple[list[QcInspection], OffsetPagination]:
    """The inspection queue - what is holding stock up right now."""
    return await QcInspectionRepository(session, tenant).list_pending(
        params, stage=stage, warehouse_id=warehouse_id
    )


async def get_inspection_detail(
    session: AsyncSession, tenant: TenantContext, inspection_id: uuid.UUID
) -> InspectionDetail:
    repo = QcInspectionRepository(session, tenant)
    inspection = await repo.get_or_404(inspection_id)
    parameters, results = await _load_sheet(session, tenant, inspection)
    return inspection_detail(inspection, parameters, results)


async def inspections_for_reference(
    session: AsyncSession,
    tenant: TenantContext,
    reference_type: str,
    reference_id: uuid.UUID | Sequence[uuid.UUID],
) -> list[QcInspection]:
    """Every inspection one document raised.

    The cross-module read a goods receipt's own screen uses, so procurement does
    not have to know this module's table.
    """
    return await QcInspectionRepository(session, tenant).list_for_reference(
        reference_type, reference_id
    )


async def inspections_for_references(
    session: AsyncSession,
    tenant: TenantContext,
    references: Sequence[tuple[str, uuid.UUID]],
) -> list[QcInspection]:
    """Every inspection a set of documents raised, in one statement.

    The batched cross-module read: job work asks it for every lot of an order
    under both reference keys a lot has had, instead of one read per lot.
    """
    return await QcInspectionRepository(session, tenant).list_for_references(references)


# =============================================================================
# Owner reconciliation (maintenance.reconcile_quality_owners)
# =============================================================================


async def companies_with_decided_inspections(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants holding at least one decided inspection - the sweep's work list.

    Deliberately cross-tenant: a beat sweep has no principal, so it discovers the
    companies first and then works each one under its own scope, exactly as
    ``maintenance.release_stale_reservations`` does.
    """
    return await decided_inspection_companies(session)


async def reconcile_quality_owners(
    session: AsyncSession, tenant: TenantContext
) -> owners.OwnerReconciliation:
    """Repair owner documents an earlier decision left waiting - see ``owners``."""
    return await owners.reconcile_owners(session, tenant)


# =============================================================================
# Reports
# =============================================================================


async def item_quality_history(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    *,
    window_days: int | None = None,
) -> ItemQualityHistory:
    """Rejection rate for one item over a window, month by month.

    Two aggregate statements, both grouped in the database: the totals for the
    window and one row per month. Reading the raw inspections and adding them up
    in Python would move a whole window of rows over the wire to produce a dozen
    numbers (section 25).

    Only decided inspections count. A pending one has no verdict, and counting it
    would drag the rate towards zero purely because nobody has looked yet.
    """
    since, until, days = _resolve_window(window_days)
    repo = QcInspectionRepository(session, tenant)

    totals = await repo.item_totals(item_id, since=since, until=until)
    periods = await repo.item_history_rows(item_id, since=since, until=until)

    return ItemQualityHistory(
        item_id=item_id,
        window_days=days,
        window_start=since,
        window_end=until,
        inspection_count=totals.inspection_count,
        inspected_quantity=totals.inspected_quantity,
        accepted_quantity=totals.accepted_quantity,
        rejected_quantity=totals.rejected_quantity,
        rework_quantity=totals.rework_quantity,
        scrap_quantity=totals.scrap_quantity,
        rejection_rate=rejection_rate(totals),
        periods=[
            QualityPeriodRow(
                period_start=row.period_start,
                inspection_count=row.totals.inspection_count,
                inspected_quantity=row.totals.inspected_quantity,
                accepted_quantity=row.totals.accepted_quantity,
                rejected_quantity=row.totals.rejected_quantity,
                rework_quantity=row.totals.rework_quantity,
                scrap_quantity=row.totals.scrap_quantity,
                rejection_rate=rejection_rate(row.totals),
            )
            for row in periods
        ],
    )


async def vendor_quality_summary(
    session: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    window_days: int | None = None,
    item_limit: int = VENDOR_ITEM_BREAKDOWN_LIMIT,
) -> VendorQualitySummary:
    """A vendor's rejection rate over a window, broken down by item.

    The read the vendor rating service consumes alongside its own delivery
    figures. ``has_evidence`` distinguishes a vendor who has never had anything
    rejected from one who has never delivered anything inspectable - the rating
    engine needs that difference, because scoring the second as a perfect record
    would reward a supplier for having no history.

    The breakdown is ordered by inspected quantity and capped, so the rows that
    dominate the rate survive the cut and the result set is bounded however large
    the vendor's history is (section 31).
    """
    since, until, days = _resolve_window(window_days)
    repo = QcInspectionRepository(session, tenant)

    totals = await repo.vendor_totals(vendor_id, since=since, until=until)
    rows = await repo.vendor_item_rows(vendor_id, since=since, until=until, limit=item_limit)
    truncated = len(rows) > item_limit
    rows = rows[:item_limit]

    return VendorQualitySummary(
        vendor_id=vendor_id,
        window_days=days,
        window_start=since,
        window_end=until,
        inspection_count=totals.inspection_count,
        inspected_quantity=totals.inspected_quantity,
        accepted_quantity=totals.accepted_quantity,
        rejected_quantity=totals.rejected_quantity,
        rework_quantity=totals.rework_quantity,
        scrap_quantity=totals.scrap_quantity,
        rejection_rate=rejection_rate(totals),
        has_evidence=totals.inspected_quantity > ZERO,
        items=[
            VendorQualityItemRow(
                item_id=row.item_id,
                inspection_count=row.totals.inspection_count,
                inspected_quantity=row.totals.inspected_quantity,
                rejected_quantity=row.totals.rejected_quantity,
                scrap_quantity=row.totals.scrap_quantity,
                rejection_rate=rejection_rate(row.totals),
            )
            for row in rows
        ],
        items_truncated=truncated,
    )


__all__ = [
    "AQL_LOT_BANDS",
    "AQL_MAX_SAMPLE",
    "AUDIT_QC_INSPECTION",
    "AUDIT_QC_TEMPLATE",
    "DEFAULT_AUTO_STAGE",
    "MOVEMENT_REFERENCE_QC",
    "QC_INSPECTION_LIFECYCLE",
    "SEVERITY_ORDER",
    "STAGE_BY_REFERENCE_TYPE",
    "STATE_FOR_DISPOSITION",
    "Disposition",
    "ParameterEvaluation",
    "ParameterSpec",
    "Reading",
    "aql_sample_size",
    "assert_critical_override",
    "close_inspection",
    "companies_with_decided_inspections",
    "compute_sample_size",
    "create_in_process_inspection",
    "create_incoming_inspection",
    "create_inspection",
    "create_template",
    "create_template_version",
    "decide_inspection",
    "derive_result",
    "disposition_delta",
    "evaluate_inspection",
    "evaluate_reading",
    "get_inspection_detail",
    "get_template_detail",
    "inspection_detail",
    "inspections_for_reference",
    "inspections_for_references",
    "item_quality_history",
    "list_inspections",
    "list_pending",
    "list_templates",
    "reconcile_quality_owners",
    "record_results",
    "rejection_rate",
    "retire_template",
    "status_for",
    "template_detail",
    "update_template",
    "validate_disposition",
    "vendor_quality_summary",
]
