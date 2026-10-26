"""Request and response schemas for quality control.

Conventions applied throughout, each for a specific reason:

* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (section 40).
* **Measurements and quantities are ``Decimal`` with explicit precision**, so
  Pydantic rejects a float before it can reach a ``NUMERIC`` column (section 84).
* **A decision states a reason.** Deciding an inspection moves stock; section 83
  requires a reason for every stock event, and a rejection with no stated cause
  is useless for vendor feedback and for corrective action.
* **The disposition is stated in full, never inferred.** ``DecideRequest`` names
  all five buckets and they must add up to the inspected quantity. A payload that
  named only "rejected: 5" would leave the other 95 pieces sitting on QC hold
  with a closed document above them - the single most common way stock becomes
  permanently unusable without anyone deciding that it should be.
* **``is_within_spec`` is never accepted from the client.** It is computed on the
  server from the template parameter's specification. A client that could assert
  its own pass/fail would make the whole check sheet decorative.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.quality.models import (
    ALLOWED_VALUE_LENGTH,
    MAX_PARAMETERS_PER_TEMPLATE,
    PARAMETER_NAME_LENGTH,
    TEXT_MEASUREMENT_LENGTH,
    InspectionResult,
    InspectionStatus,
    ParameterType,
    QcStage,
    ReworkOutcome,
    ReworkStatus,
    SamplingPlan,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Measurement = Annotated[Decimal, Field(max_digits=18, decimal_places=6)]
SamplePercentage = Annotated[Decimal, Field(gt=0, le=100, max_digits=9, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
Code = Annotated[str, Field(min_length=2, max_length=64)]
Name = Annotated[str, Field(min_length=2, max_length=255)]
ParameterName = Annotated[str, Field(min_length=1, max_length=PARAMETER_NAME_LENGTH)]
AllowedValue = Annotated[str, Field(min_length=1, max_length=ALLOWED_VALUE_LENGTH)]
TextMeasurement = Annotated[str, Field(min_length=1, max_length=TEXT_MEASUREMENT_LENGTH)]
Remark = Annotated[str, Field(max_length=2000)]

#: Longest reporting window a single report request may span. Two years covers
#: any trend a quality engineer reads on screen; anything longer is an export,
#: and letting it through here would scan the whole inspection history on a
#: request path (section 25).
MAX_REPORT_WINDOW_DAYS = 730
DEFAULT_REPORT_WINDOW_DAYS = 90

#: How many item rows a vendor quality report breaks out. Ordered by inspected
#: quantity, so the cap drops the tail rather than an arbitrary slice - and no
#: result set is unbounded however it is reached (section 31).
VENDOR_ITEM_BREAKDOWN_LIMIT = 100

#: Upper bound on the parameter measurements one request may record. The same
#: bound the template itself carries: a check sheet cannot have more parameters
#: than the template defines.
MAX_RESULTS_PER_REQUEST = MAX_PARAMETERS_PER_TEMPLATE


# -----------------------------------------------------------------------------
# Templates
# -----------------------------------------------------------------------------


class TemplateParameterIn(BaseModel):
    """One row of a template's check sheet, as submitted.

    The type/specification agreement is validated here as well as by the database
    check constraints: a tolerance band typed against a BOOLEAN parameter would
    otherwise be accepted by the API and then silently ignored by the evaluator,
    which is worse than a rejection because the template looks configured.
    """

    model_config = ConfigDict(extra="forbid")

    sequence: Annotated[int, Field(ge=1, le=MAX_PARAMETERS_PER_TEMPLATE)]
    parameter_name: ParameterName
    parameter_type: ParameterType
    unit: Annotated[str, Field(max_length=24)] | None = None

    min_value: Measurement | None = None
    max_value: Measurement | None = None
    target_value: Measurement | None = None

    allowed_values: Annotated[list[AllowedValue], Field(max_length=64)] | None = None

    is_critical: bool = False
    is_mandatory: bool = True

    @model_validator(mode="after")
    def _specification_matches_type(self) -> Self:
        bounds = (self.min_value, self.max_value, self.target_value)
        if self.parameter_type is not ParameterType.NUMERIC and any(
            value is not None for value in bounds
        ):
            raise ValueError(
                f"'{self.parameter_name}' is a {self.parameter_type} parameter; a minimum, "
                "maximum or target value can only be judged on a NUMERIC one."
            )
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(f"'{self.parameter_name}': the minimum is above the maximum.")

        if self.parameter_type is ParameterType.SELECT and not self.allowed_values:
            raise ValueError(
                f"'{self.parameter_name}' is a SELECT parameter and needs the list of values "
                "it accepts."
            )
        if self.parameter_type in (ParameterType.NUMERIC, ParameterType.BOOLEAN) and (
            self.allowed_values
        ):
            raise ValueError(
                f"'{self.parameter_name}': allowed values apply to SELECT and TEXT parameters only."
            )
        if self.allowed_values is not None and len(set(self.allowed_values)) != len(
            self.allowed_values
        ):
            raise ValueError(f"'{self.parameter_name}': the allowed values repeat.")

        # A critical parameter nobody has to measure cannot do its job: the
        # inspection would be decidable without ever reading the one
        # characteristic that can reject the lot on its own.
        if self.is_critical and not self.is_mandatory:
            raise ValueError(
                f"'{self.parameter_name}' is critical, so it cannot be optional - a critical "
                "characteristic that may be skipped never rejects anything."
            )
        return self


class TemplateCreate(BaseModel):
    """Create version 1 of an inspection specification."""

    model_config = ConfigDict(extra="forbid")

    code: Code
    name: Name
    description: Remark | None = None

    #: Scope, narrowest first. Both omitted makes this the stage-wide default.
    item_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None

    qc_stage: QcStage
    sampling_plan: SamplingPlan = SamplingPlan.ALL
    sample_percentage: SamplePercentage | None = None
    sample_size: Annotated[int, Field(ge=1, le=1_000_000)] | None = None

    is_active: bool = True

    parameters: Annotated[
        list[TemplateParameterIn], Field(min_length=1, max_length=MAX_PARAMETERS_PER_TEMPLATE)
    ]

    @model_validator(mode="after")
    def _scope_and_plan_are_coherent(self) -> Self:
        if self.item_id is not None and self.item_category_id is not None:
            raise ValueError(
                "A template applies to one item or to one category, not both - two scopes "
                "would resolve differently depending on which is looked at first."
            )
        if self.sampling_plan is SamplingPlan.PERCENTAGE and self.sample_percentage is None:
            raise ValueError("A PERCENTAGE sampling plan needs sample_percentage.")
        if self.sampling_plan is SamplingPlan.FIXED_SAMPLE and self.sample_size is None:
            raise ValueError("A FIXED_SAMPLE sampling plan needs sample_size.")
        if self.sampling_plan is not SamplingPlan.PERCENTAGE and self.sample_percentage is not None:
            raise ValueError("sample_percentage applies to the PERCENTAGE plan only.")
        if self.sampling_plan is not SamplingPlan.FIXED_SAMPLE and self.sample_size is not None:
            raise ValueError("sample_size applies to the FIXED_SAMPLE plan only.")

        sequences = [parameter.sequence for parameter in self.parameters]
        if len(set(sequences)) != len(sequences):
            raise ValueError("Parameter sequence numbers repeat.")
        names = [parameter.parameter_name.strip().lower() for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("Two parameters share a name; a check sheet cannot ask twice.")
        return self


class TemplateUpdate(BaseModel):
    """Amend a template.

    ``name``, ``description`` and ``is_active`` are always editable - they change
    how the template is presented and whether it is selected, not what it means.
    Everything else, including the parameter set, is editable only while the
    template has never been applied to an inspection; afterwards the change forks
    a new version, so past pass/fail results stay reproducible against the
    specification they were judged by.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: Remark | None = None
    is_active: bool | None = None

    item_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None
    qc_stage: QcStage | None = None
    sampling_plan: SamplingPlan | None = None
    sample_percentage: SamplePercentage | None = None
    sample_size: Annotated[int, Field(ge=1, le=1_000_000)] | None = None

    parameters: (
        Annotated[list[TemplateParameterIn], Field(max_length=MAX_PARAMETERS_PER_TEMPLATE)] | None
    ) = None

    @model_validator(mode="after")
    def _at_least_one_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Nothing to update.")
        if self.item_id is not None and self.item_category_id is not None:
            raise ValueError("A template applies to one item or to one category, not both.")
        if self.parameters is not None and not self.parameters:
            raise ValueError("A template with no parameters cannot inspect anything.")
        return self


class TemplateVersionRequest(BaseModel):
    """Fork a used template into a new revision.

    The new version starts from the current one's parameter set unless a
    replacement is supplied, so a single tolerance change does not mean retyping
    a forty-line check sheet.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    name: Name | None = None
    description: Remark | None = None
    sampling_plan: SamplingPlan | None = None
    sample_percentage: SamplePercentage | None = None
    sample_size: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    parameters: (
        Annotated[
            list[TemplateParameterIn], Field(min_length=1, max_length=MAX_PARAMETERS_PER_TEMPLATE)
        ]
        | None
    ) = None
    #: Whether the superseded version stops being selected. Almost always true;
    #: false only while both revisions are deliberately in use during a changeover.
    retire_previous: bool = True


class TemplateParameterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    sequence: int
    parameter_name: str
    parameter_type: ParameterType
    unit: str | None
    min_value: Decimal | None
    max_value: Decimal | None
    target_value: Decimal | None
    allowed_values: list[str] | None
    is_critical: bool
    is_mandatory: bool


class TemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    item_id: uuid.UUID | None
    item_category_id: uuid.UUID | None
    qc_stage: QcStage
    sampling_plan: SamplingPlan
    sample_percentage: Decimal | None
    sample_size: int | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class TemplateDetail(TemplateSummary):
    description: str | None = None
    parameters: list[TemplateParameterRead] = Field(default_factory=list)


class TemplateFilters(BaseModel):
    """List filters. Built by the router from query parameters."""

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    item_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None
    qc_stage: QcStage | None = None
    sampling_plan: SamplingPlan | None = None
    is_active: bool | None = None


# -----------------------------------------------------------------------------
# Inspections
# -----------------------------------------------------------------------------


class InspectionCreate(BaseModel):
    """Raise an inspection by hand.

    Used by a supervisor pulling a lot off the shelf for a final check. The
    inspections raised automatically by a goods receipt, a job work return or an
    assembly completion come through ``service.create_incoming_inspection`` and
    ``service.create_in_process_inspection`` instead, and carry the reference of
    the document that triggered them.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    qc_stage: QcStage
    inspected_quantity: PositiveQuantity

    batch_id: uuid.UUID | None = None
    serial_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10_000)
    vendor_id: uuid.UUID | None = None
    #: Omit to resolve the active template for this item and stage. Naming one
    #: explicitly is how a lot is inspected against a customer's own specification.
    template_id: uuid.UUID | None = None

    reference_type: Annotated[str, Field(min_length=2, max_length=48)] = "manual"
    reference_id: uuid.UUID | None = None
    reference_code: Annotated[str, Field(max_length=64)] | None = None

    #: Move the quantity from AVAILABLE into QC_HOLD as part of raising this
    #: inspection. Off by default: an inspection raised by a receipt is inspecting
    #: stock the receipt already held, and holding it twice would take the
    #: quantity out of availability a second time.
    hold_stock: bool = False
    reason: Reason
    remarks: Remark | None = None

    @model_validator(mode="after")
    def _serials_match_quantity(self) -> Self:
        if len(self.serial_ids) != len(set(self.serial_ids)):
            raise ValueError("Each inspected serial unit can appear only once.")
        if self.serial_ids and Decimal(len(self.serial_ids)) != self.inspected_quantity:
            raise ValueError("Select one serial unit per inspected unit.")
        return self


class InspectionFilters(BaseModel):
    """List filters. Built by the router from query parameters."""

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    qc_stage: QcStage | None = None
    status: InspectionStatus | None = None
    result: InspectionResult | None = None
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    reference_type: str | None = None
    reference_id: uuid.UUID | None = None
    #: Only inspections nobody has decided yet. Served by the partial index on
    #: the open statuses rather than by a status equality filter, so the queue
    #: stays cheap as the table grows.
    pending_only: bool = False
    #: Only inspections carrying quantity flagged for rework.
    rework_only: bool = False
    inspected_from: datetime | None = None
    inspected_to: datetime | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if (
            self.inspected_from is not None
            and self.inspected_to is not None
            and self.inspected_to < self.inspected_from
        ):
            raise ValueError("inspected_to cannot be before inspected_from.")
        return self


class MeasurementIn(BaseModel):
    """One reading against one template parameter.

    Exactly one value is supplied, and which one is decided by the parameter's
    type - the service checks the pair, because a numeric reading typed into the
    text field would be compared as a string and "10" would come out below "9".
    """

    model_config = ConfigDict(extra="forbid")

    parameter_id: uuid.UUID
    measured_value_numeric: Measurement | None = None
    measured_value_text: TextMeasurement | None = None
    measured_value_bool: bool | None = None
    remark: Remark | None = None
    photo_document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_value(self) -> Self:
        supplied = [
            value
            for value in (
                self.measured_value_numeric,
                self.measured_value_text,
                self.measured_value_bool,
            )
            if value is not None
        ]
        if len(supplied) != 1:
            raise ValueError(
                "Record exactly one measured value per parameter: a reading with none is not a "
                "measurement, and one with two cannot be judged."
            )
        return self


class RecordResultsRequest(BaseModel):
    """Record or correct a set of measurements on an inspection."""

    model_config = ConfigDict(extra="forbid")

    measurements: Annotated[
        list[MeasurementIn], Field(min_length=1, max_length=MAX_RESULTS_PER_REQUEST)
    ]
    remarks: Remark | None = None

    @model_validator(mode="after")
    def _one_reading_per_parameter(self) -> Self:
        ids = [measurement.parameter_id for measurement in self.measurements]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "The same parameter is measured twice in one request; a correction replaces the "
                "earlier reading rather than adding to it."
            )
        return self


class DecideRequest(BaseModel):
    """The verdict, and the full disposition of the lot.

    The five quantities must add up to ``inspected_quantity``. Stating the
    disposition in full is what makes the stock consequence total: every piece
    either leaves QC hold for a named pool or is explicitly left on it.
    """

    model_config = ConfigDict(extra="forbid")

    accepted_quantity: NonNegativeQuantity = Decimal("0")
    rejected_quantity: NonNegativeQuantity = Decimal("0")
    rework_quantity: NonNegativeQuantity = Decimal("0")
    hold_quantity: NonNegativeQuantity = Decimal("0")
    scrap_quantity: NonNegativeQuantity = Decimal("0")
    serial_dispositions: dict[
        uuid.UUID, Literal["ACCEPTED", "REJECTED", "REWORK", "HOLD", "SCRAP"]
    ] = Field(default_factory=dict, max_length=10_000)

    reason: Reason
    #: Required whenever anything is rejected or scrapped. Checked here so the
    #: caller is told which field is missing, and again by the database so a
    #: write that did not come through this schema cannot skip it.
    rejection_reason: Annotated[str, Field(min_length=3, max_length=2000)] | None = None
    remarks: Remark | None = None

    #: Business time of the inspection. Defaults to now; supplied when a check
    #: carried out on the floor last night is keyed in this morning.
    inspected_at: datetime | None = None

    #: Close the document as part of the decision. Only legal when nothing is
    #: left on QC hold - a lot with rework or hold quantity outstanding still
    #: owes somebody a second decision.
    close: bool = False

    @model_validator(mode="after")
    def _disposition_is_stated(self) -> Self:
        total = (
            self.accepted_quantity
            + self.rejected_quantity
            + self.rework_quantity
            + self.hold_quantity
            + self.scrap_quantity
        )
        if total <= 0:
            raise ValueError("A decision has to disposition some quantity.")
        if (self.rejected_quantity + self.scrap_quantity) > 0 and not self.rejection_reason:
            raise ValueError(
                "State why the material was refused: a rejection with no cause cannot be fed "
                "back to the vendor or turned into a corrective action."
            )
        if self.close and (self.rework_quantity + self.hold_quantity) > 0:
            raise ValueError(
                "This lot still has quantity on QC hold for rework or a further decision; it "
                "cannot be closed until that is dispositioned."
            )
        return self


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class InspectionResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inspection_id: uuid.UUID
    parameter_id: uuid.UUID
    measured_value_numeric: Decimal | None
    measured_value_text: str | None
    measured_value_bool: bool | None
    is_within_spec: bool
    remark: str | None
    photo_document_id: uuid.UUID | None


class ReworkRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    inspection_id: uuid.UUID
    quantity: Decimal
    assigned_to: uuid.UUID | None
    work_center_id: uuid.UUID | None
    status: ReworkStatus
    outcome: ReworkOutcome | None
    cost: Decimal | None
    notes: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class DecisionHistoryRead(BaseModel):
    """One verdict in an inspection's decision history, oldest first."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inspection_id: uuid.UUID
    sequence: int
    result: InspectionResult
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    hold_quantity: Decimal
    scrap_quantity: Decimal
    serial_dispositions: dict[str, str] = Field(default_factory=dict)
    reason: str
    rejection_reason: str | None
    inspected_at: datetime
    created_at: datetime


class InspectionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    qc_stage: QcStage
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    batch_id: uuid.UUID | None
    serial_ids: list[uuid.UUID] = Field(default_factory=list)
    serial_dispositions: dict[str, str] = Field(default_factory=dict)
    vendor_id: uuid.UUID | None
    template_id: uuid.UUID | None
    template_version: int | None
    reference_type: str
    reference_id: uuid.UUID | None
    reference_code: str | None
    inspected_quantity: Decimal
    sample_size: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    hold_quantity: Decimal
    scrap_quantity: Decimal
    result: InspectionResult
    status: InspectionStatus
    return_required: bool
    inspector_user_id: uuid.UUID | None
    inspected_at: datetime | None
    created_at: datetime


class InspectionDetail(InspectionSummary):
    serial_labels: dict[uuid.UUID, str] = Field(default_factory=dict)
    rejection_reason: str | None = None
    remarks: str | None = None
    closed_at: datetime | None = None
    parameters: list[TemplateParameterRead] = Field(default_factory=list)
    results: list[InspectionResultRead] = Field(default_factory=list)
    #: Every verdict this inspection has carried, oldest first. Re-deciding a
    #: reworked lot overwrites the five quantities above; the cycles are kept
    #: here instead of being lost.
    decisions: list[DecisionHistoryRead] = Field(default_factory=list)
    #: The rework cycles raised against the held quantity, oldest first.
    rework_records: list[ReworkRecordRead] = Field(default_factory=list)


class ParameterEvaluationRead(BaseModel):
    """What the recorded measurements imply, before anybody decides anything.

    Returned by the record-results endpoint so the inspector sees the
    consequence of what they just typed - in particular that a critical failure
    has already settled the verdict.
    """

    model_config = ConfigDict(from_attributes=True)

    recommended_result: InspectionResult
    is_conclusive: bool
    has_critical_failure: bool
    critical_failures: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    missing_mandatory: list[str] = Field(default_factory=list)
    measured_count: int = 0
    in_spec_count: int = 0


class RecordResultsResponse(BaseModel):
    inspection: InspectionDetail
    evaluation: ParameterEvaluationRead


class DecisionResponse(BaseModel):
    """What the decision did, including the stock it moved."""

    inspection: InspectionDetail
    evaluation: ParameterEvaluationRead
    released_quantity: Decimal
    damaged_quantity: Decimal
    scrapped_quantity: Decimal
    still_on_hold_quantity: Decimal
    return_required: bool


# -----------------------------------------------------------------------------
# Rework cycles
# -----------------------------------------------------------------------------


class ReworkCreate(BaseModel):
    """Open a rework cycle against an inspection holding rework quantity.

    The cycle covers part of the inspection's ``rework_quantity`` - the stock
    stays in the QC hold pool for the whole cycle, so no movement is posted
    here. Who does the work is named one of two ways: ``assigned_to`` (a user)
    or ``work_center_id`` (a bench). One of the two is required; both set means
    the assignee works at that centre.
    """

    model_config = ConfigDict(extra="forbid")

    inspection_id: uuid.UUID
    quantity: PositiveQuantity
    assigned_to: uuid.UUID | None = None
    work_center_id: uuid.UUID | None = None
    notes: Remark | None = None
    reason: Reason

    @model_validator(mode="after")
    def _somebody_owns_the_bench(self) -> Self:
        if self.assigned_to is None and self.work_center_id is None:
            raise ValueError(
                "Name who does the rework: an assignee, a work centre, or both. A cycle "
                "with nobody behind it is a quantity parked on a bench nobody owns."
            )
        return self


class ReworkStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ReworkCompleteRequest(BaseModel):
    """Finish a rework cycle and hand the quantity back for re-decision.

    The outcome is the bench's recommendation, not a stock movement: quality
    owns the hold and the decision is the one release path, so even a SCRAPPED
    outcome leaves the quantity in QC hold for the inspector to scrap on
    re-decision. A RETURNED outcome against a named vendor flags the inspection
    for a vendor return, the way a rejection does.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: ReworkOutcome
    cost: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)] | None = None
    notes: Remark | None = None
    reason: Reason


class ReworkFilters(BaseModel):
    """List filters for the rework bench's work list."""

    model_config = ConfigDict(extra="forbid")

    inspection_id: uuid.UUID | None = None
    status: ReworkStatus | None = None
    assigned_to: uuid.UUID | None = None
    work_center_id: uuid.UUID | None = None


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


class QualityPeriodRow(BaseModel):
    """One month of an item's quality history."""

    period_start: datetime
    inspection_count: int
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    scrap_quantity: Decimal
    #: Rejected plus scrapped, over inspected. Expressed 0-100 so it reads as the
    #: percentage every quality report is quoted in.
    rejection_rate: Decimal


class ItemQualityHistory(BaseModel):
    """Rejection rate for one item over a window, and how it moved."""

    item_id: uuid.UUID
    window_days: int
    window_start: datetime
    window_end: datetime
    inspection_count: int
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    scrap_quantity: Decimal
    rejection_rate: Decimal
    #: Empty when the item has never been inspected in the window - a legitimate
    #: answer, and one the caller can distinguish from a rate of zero.
    periods: list[QualityPeriodRow] = Field(default_factory=list)


class VendorQualityItemRow(BaseModel):
    """One item's contribution to a vendor's rejection rate."""

    item_id: uuid.UUID
    inspection_count: int
    inspected_quantity: Decimal
    rejected_quantity: Decimal
    scrap_quantity: Decimal
    rejection_rate: Decimal


class VendorQualitySummary(BaseModel):
    """Rejection rate by vendor, broken down by item.

    Consumed by the vendor rating service alongside its own delivery figures.
    ``has_evidence`` is false when the vendor has no inspected quantity in the
    window at all - a distinction the rating engine needs, because "never
    rejected anything" and "never delivered anything" are not the same vendor.
    """

    vendor_id: uuid.UUID
    window_days: int
    window_start: datetime
    window_end: datetime
    inspection_count: int
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    scrap_quantity: Decimal
    rejection_rate: Decimal
    has_evidence: bool
    items: list[VendorQualityItemRow] = Field(default_factory=list)
    #: True when the item breakdown was cut at ``VENDOR_ITEM_BREAKDOWN_LIMIT``,
    #: so a reader knows the rows below do not add up to the totals above.
    items_truncated: bool = False


__all__ = [
    "DEFAULT_REPORT_WINDOW_DAYS",
    "MAX_REPORT_WINDOW_DAYS",
    "MAX_RESULTS_PER_REQUEST",
    "VENDOR_ITEM_BREAKDOWN_LIMIT",
    "CloseRequest",
    "DecideRequest",
    "DecisionResponse",
    "InspectionCreate",
    "InspectionDetail",
    "InspectionFilters",
    "InspectionResultRead",
    "InspectionSummary",
    "ItemQualityHistory",
    "MeasurementIn",
    "ParameterEvaluationRead",
    "QualityPeriodRow",
    "RecordResultsRequest",
    "RecordResultsResponse",
    "TemplateCreate",
    "TemplateDetail",
    "TemplateFilters",
    "TemplateParameterIn",
    "TemplateParameterRead",
    "TemplateSummary",
    "TemplateUpdate",
    "TemplateVersionRequest",
    "VendorQualityItemRow",
    "VendorQualitySummary",
]
