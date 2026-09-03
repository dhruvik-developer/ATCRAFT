"""Request and response schemas for the planning engine.

Conventions, each of them load-bearing:

* **Quantities and dates are ``Decimal`` and ``date``**, never float and never a
  formatted string (section 84). A plan that drifted by 1e-16 would propose
  ordering 99.9999999 screws.
* **No schema carries ``company_id``.** It comes from the authenticated
  principal's tenant context (section 40).
* **No schema lets a client state a stock, incoming or shortage figure.** Those
  are read by the engine; anything a request claimed about them would be ignored,
  so the API does not offer the field.
* **A run states its scope, not its answer.** The request carries the horizon,
  the warehouses and any manual demand. Everything else on ``MrpRunRead`` is
  output.
* **Ignoring a suggestion needs a reason.** Required rather than optional,
  because "the planner clicked ignore" teaches the next run's reader nothing and
  the column is NOT NULL for ignored rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from src.core.types import to_quantity
from src.modules.mrp.models import (
    DEFAULT_HORIZON_DAYS,
    MAX_HORIZON_DAYS,
    MIN_HORIZON_DAYS,
    MrpDemandSource,
    MrpRunMode,
    MrpRunStatus,
    MrpSuggestionStatus,
    MrpSuggestionType,
)

# ``decimal_places`` is not decoration: every quantity written by this module is
# ``Numeric(18,4)`` and is quantised with ``to_quantity`` on the way in, so a
# request for 0.00004 of something is silently rounded to zero and then rejected
# by the ``quantity > 0`` check constraint as an unexplained 500. Bounding the
# scale here turns that into the 422 it always was.
PositiveQuantity = Annotated[
    Decimal, Field(gt=0, le=Decimal("99999999999999"), max_digits=18, decimal_places=4)
]
Reason = Annotated[str, Field(min_length=3, max_length=255)]

#: Bound on a single manual-demand payload. A planner types these by hand; a
#: request carrying thousands is a bulk import, which is a different endpoint
#: with a different (background) execution model.
MAX_MANUAL_DEMAND_LINES = 200

#: Bound on one bulk-ignore call. Keeps the transaction, its row locks and its
#: audit fan-out to a size that finishes inside a request.
MAX_BULK_SUGGESTIONS = 500

#: Warehouses one run may be scoped to. Beyond this the scope is "everything",
#: which is expressed by sending no list at all.
MAX_RUN_WAREHOUSES = 50


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


class ConversionTarget(StrEnum):
    """The document class a suggestion is turned into.

    One target per suggestion type, with the planner free to override: deciding
    to buy a part the item master says is made here is a legitimate commercial
    call, and ``docs/assumptions.md`` section 6 already accepts that every
    suggestion is reviewed before it becomes a transaction. The override is
    recorded in the audit entry.
    """

    PURCHASE_REQUISITION = "PURCHASE_REQUISITION"
    PRODUCTION_ORDER = "PRODUCTION_ORDER"
    JOB_WORK_ORDER = "JOB_WORK_ORDER"


#: Default target for each suggestion type. ``TRANSFER`` is absent because this
#: engine does not raise transfer suggestions - see ``models.MrpSuggestionType``.
DEFAULT_CONVERSION_TARGET: dict[MrpSuggestionType, ConversionTarget] = {
    MrpSuggestionType.PURCHASE: ConversionTarget.PURCHASE_REQUISITION,
    MrpSuggestionType.MANUFACTURE: ConversionTarget.PRODUCTION_ORDER,
    MrpSuggestionType.JOB_WORK: ConversionTarget.JOB_WORK_ORDER,
}


# -----------------------------------------------------------------------------
# Starting a run
# -----------------------------------------------------------------------------


class ManualDemandIn(BaseModel):
    """Demand a planner adds on top of the order book.

    A forecast line, a spares top-up, an internal build. It carries no source
    document, which is why ``mrp_demands`` allows a null ``source_id`` only for
    ``MANUAL``.
    """

    item_id: uuid.UUID
    quantity: PositiveQuantity
    required_date: date
    #: Optional: a manual line need not name a warehouse, in which case it is
    #: planned against whatever the run itself is scoped to.
    warehouse_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=255)

    @field_validator("notes")
    @classmethod
    def _tidy_notes(cls, value: str | None) -> str | None:
        return _clean_text(value)


class MrpRunRequest(BaseModel):
    """Start a planning run.

    Nothing here says how the run should execute. That is the service's decision,
    taken from the demand-line count against
    ``settings.mrp_sync_threshold_demand_lines`` (database.md section 6): a
    client cannot ask for a four-minute calculation to be held open on an HTTP
    connection.
    """

    horizon_days: Annotated[int, Field(ge=MIN_HORIZON_DAYS, le=MAX_HORIZON_DAYS)] = (
        DEFAULT_HORIZON_DAYS
    )
    #: Empty means every warehouse. When set, demand, stock and inbound supply are
    #: all restricted to these warehouses - a run scoped to one plant must not
    #: net against another plant's stock.
    warehouse_ids: list[uuid.UUID] = Field(default_factory=list)
    manual_demand: list[ManualDemandIn] = Field(default_factory=list)
    #: Hold safety stock back from the netting rather than planning it away.
    #: Defaults to true - see ``engine.planning_available``.
    respect_safety_stock: bool = True
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("warehouse_ids")
    @classmethod
    def _bound_warehouses(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        unique = list(dict.fromkeys(value))
        if len(unique) > MAX_RUN_WAREHOUSES:
            raise ValueError(
                f"A run may be scoped to at most {MAX_RUN_WAREHOUSES} warehouses; "
                "omit the list to plan across all of them."
            )
        return unique

    @field_validator("manual_demand")
    @classmethod
    def _bound_manual_demand(cls, value: list[ManualDemandIn]) -> list[ManualDemandIn]:
        if len(value) > MAX_MANUAL_DEMAND_LINES:
            raise ValueError(
                f"At most {MAX_MANUAL_DEMAND_LINES} manual demand lines may be sent with a run."
            )
        return value

    @field_validator("notes")
    @classmethod
    def _tidy_notes(cls, value: str | None) -> str | None:
        return _clean_text(value)


# -----------------------------------------------------------------------------
# Reading runs
# -----------------------------------------------------------------------------


class MrpRunFilters(BaseModel):
    status: MrpRunStatus | None = None
    created_from: date | None = None
    created_to: date | None = None
    search: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _ordered_window(self) -> MrpRunFilters:
        if self.created_from and self.created_to and self.created_to < self.created_from:
            raise ValueError("created_to cannot be before created_from")
        return self


class MrpRunSummary(BaseModel):
    """One row of the run list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: MrpRunStatus
    mode: MrpRunMode
    horizon_days: int
    warehouse_ids: list[uuid.UUID] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    demand_line_count: int
    suggestion_count: int
    created_at: datetime


class MrpRunRead(MrpRunSummary):
    """One run with everything it was given and everything it produced.

    ``warnings`` is not decoration. It carries the netting stages the run could
    not perform - a missing procurement module, a make item with no bill of
    materials - and a planner reading suggestions without reading these would be
    trusting numbers the engine already knows are an upper bound.

    It also carries the supply the run deliberately did *not* count: an order or
    a job inside the horizon that arrives after the demand needs it. That is a
    shortage a buyer will otherwise query - "we have a purchase order for these"
    - so the plan names each feed's quantity with the date that feed lands,
    rather than leaving the two facts unconnected or pairing one date with a
    quantity from the other feed. ``result_summary["late_supply_item_count"]``
    is the same statement as a number, and :class:`MrpRequirementRead` carries
    the quantities and a date per feed, which is where a client should read them
    from: this text is prose, written in one language, for a human.

    ``result_summary["demand_dates_settled"]`` reports the property those
    figures rest on: that every item's needed-by date was final before any
    supply was phased against it. It is ``True`` on any plan a planner will see;
    ``False`` means a structure so deep the dates were still moving, which is
    also named in a warning, and the component demand underneath those items may
    be understated.
    """

    celery_task_id: str | None = None
    error_message: str | None = None
    parameters: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    #: Lifted out of ``result_summary`` so a client does not have to know the
    #: shape of a JSONB blob to show the caveats.
    warnings: list[str] = Field(default_factory=list)
    suggestion_counts_by_status: dict[str, int] = Field(default_factory=dict)
    #: Polling URL for a run handed to Celery. Present on every run so a client
    #: has one way to refresh, whichever mode it ended up in.
    status_url: str | None = None


class MrpDemandRead(BaseModel):
    """One demand line as the run captured it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    demand_source: MrpDemandSource
    source_id: uuid.UUID | None = None
    source_line_id: uuid.UUID | None = None
    source_code: str | None = None
    item_id: uuid.UUID
    warehouse_id: uuid.UUID | None = None
    quantity: Decimal
    required_date: date
    priority: int
    notes: str | None = None


class MrpRequirementRead(BaseModel):
    """One merged requirement, with every figure that went into it.

    The netting inputs are all present on purpose: "we are 40 short" cannot be
    reviewed, and "gross 200 = 100 direct + 100 from bills of materials, 60 free
    of 160 on hand, 100 already on order, so 40 short" can.

    That includes the supply the run deliberately did *not* net. The two
    quantities and the two dates beside them are the per-row form of the run's
    ``late_supply_item_count`` and its "arrives AFTER it is needed" warning: an
    order exists, it lands on this date, and the shortage beside it stands
    anyway. A client that had only the warning would have to parse an English
    sentence to render a row, which is fragile and untranslatable.

    Each quantity carries **its own INTERVAL**, not a single date. A late
    quantity is a SUM over that feed's late tranches, so one date beside it is a
    whole fact only when the feed had one tranche; with several, the earliest
    date understates how late the bulk is, which reads as "nearly here". So
    ``late_incoming_quantity`` opens at ``first_late_incoming_date`` and is
    complete by ``last_late_incoming_date``, and ``late_in_production_quantity``
    likewise. Render the interval - "the first of 5010 on 2026-07-30, all of it
    by 2026-12-15" - never the quantity against one end of it. The two feeds are
    independent schedules and pairing either quantity with the other's dates is
    a false statement. The
    single ``first_late_supply_date`` is the earlier of the two and answers only
    "how soon does anything land at all" - it used to be the only date on the
    row, which made "5000 on order" beside a 0.0001 job's date read as five
    thousand arriving the next day.

    ``late_supply_quantity`` is the sum of the two, computed here so a client
    never adds two decimal strings in JavaScript and gets a float back. There is
    deliberately no summed-quantity-plus-summary-date pair: that combination is
    the misreading above, written down.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    level: int

    direct_demand_quantity: Decimal
    dependent_demand_quantity: Decimal
    gross_requirement: Decimal

    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    incoming_quantity: Decimal
    in_production_quantity: Decimal

    #: Supply inside the horizon that arrives after this requirement needs it
    #: and was therefore not netted off it. Defaulted rather than required so a
    #: consumer constructing one by hand keeps working.
    late_incoming_quantity: Decimal = Decimal("0")
    late_in_production_quantity: Decimal = Decimal("0")
    #: When each of those two starts landing - one date per feed, beside the
    #: quantity it belongs to. ``None`` when that feed has nothing late. Render
    #: the pairs: ``late_incoming_quantity`` with ``first_late_incoming_date``,
    #: ``late_in_production_quantity`` with ``first_late_in_production_date``.
    first_late_incoming_date: date | None = None
    first_late_in_production_date: date | None = None
    #: When each feed's late supply is COMPLETE. Pair each quantity with BOTH of
    #: its dates: the quantity is a sum, so the opening date alone attributes all
    #: of it to the first tranche. ``None`` when that feed has nothing late.
    last_late_incoming_date: date | None = None
    last_late_in_production_date: date | None = None
    #: The earlier of the two, or ``None`` when neither feed is late. The
    #: soonest anything at all lands - a row-level summary, and NOT the date for
    #: either quantity above. Reading it beside ``late_incoming_quantity``
    #: reported a purchase order for 5 000 due in four months as arriving on the
    #: day a 0.0001 job did, which is why the per-feed pair exists.
    first_late_supply_date: date | None = None

    net_requirement: Decimal
    shortage_quantity: Decimal
    earliest_required_date: date

    source_demand_count: int
    source_demands: list[dict[str, Any]] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def late_supply_quantity(self) -> Decimal:
        """Everything the phasing excluded for this row, from either source."""
        return to_quantity(self.late_incoming_quantity + self.late_in_production_quantity)


class MrpSuggestionRead(BaseModel):
    """One suggestion and what has happened to it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    suggestion_type: MrpSuggestionType

    required_quantity: Decimal
    suggested_quantity: Decimal
    shortage_quantity: Decimal
    available_quantity: Decimal
    incoming_quantity: Decimal

    required_date: date
    suggested_order_date: date
    is_late: bool
    lead_time_days: int

    status: MrpSuggestionStatus
    converted_to_type: str | None = None
    converted_to_id: uuid.UUID | None = None
    converted_at: datetime | None = None
    ignored_reason: str | None = None

    source_demands: list[dict[str, Any]] | None = None


class SuggestionFilters(BaseModel):
    run_id: uuid.UUID | None = None
    suggestion_type: MrpSuggestionType | None = None
    status: MrpSuggestionStatus | None = None
    item_id: uuid.UUID | None = None
    late_only: bool = False
    search: str | None = Field(default=None, max_length=120)


class ShortageReportFilters(BaseModel):
    """Filters for the shortage report.

    ``run_id`` is optional: omitting it reports against the most recent completed
    run, which is what a planner opening the screen means. ``shortage_only``
    defaults to true because the report is the short list, not the whole plan.
    """

    run_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    shortage_only: bool = True
    search: str | None = Field(default=None, max_length=120)


# -----------------------------------------------------------------------------
# Acting on suggestions
# -----------------------------------------------------------------------------


class SuggestionConvertRequest(BaseModel):
    """Turn one suggestion into a document.

    ``target`` may be omitted, in which case it is derived from the suggestion
    type. ``quantity`` may be omitted, in which case the suggested quantity is
    used; a planner raising a *smaller* order than suggested is knowingly leaving
    a shortage, so the value is accepted but the suggestion still records what
    was planned.
    """

    target: ConversionTarget | None = None
    quantity: PositiveQuantity | None = None
    required_date: date | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("notes")
    @classmethod
    def _tidy_notes(cls, value: str | None) -> str | None:
        return _clean_text(value)


class ConversionRequest(BaseModel):
    """A typed instruction to create one document from one suggestion.

    This is the seam between planning and execution. When the owning module is
    deployed the service hands it this payload and receives back the document it
    created; when it is not, the same payload is returned to the caller so the
    request can be replayed later or actioned by hand. Either way the *shape* of
    what MRP asks for is fixed here rather than being re-derived by each consumer.
    """

    suggestion_id: uuid.UUID
    run_id: uuid.UUID
    target: ConversionTarget
    item_id: uuid.UUID
    item_code: str
    quantity: Decimal
    required_date: date
    suggested_order_date: date
    warehouse_ids: list[uuid.UUID] = Field(default_factory=list)
    source_demands: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class SuggestionConversionResult(BaseModel):
    """What conversion actually achieved.

    ``handled`` is the field that matters and is never inferred from the
    suggestion's status: false means no document exists yet and the caller is
    holding the only copy of the instruction. Reporting that plainly is the
    difference between a seam and a silent gap.
    """

    suggestion: MrpSuggestionRead
    request: ConversionRequest
    handled: bool
    created_document_type: str | None = None
    created_document_id: uuid.UUID | None = None
    created_document_code: str | None = None
    message: str


class BulkIgnoreRequest(BaseModel):
    """Dismiss several suggestions at once, with one shared reason."""

    suggestion_ids: Annotated[list[uuid.UUID], Field(min_length=1)]
    reason: Reason

    @field_validator("suggestion_ids")
    @classmethod
    def _bound_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        unique = list(dict.fromkeys(value))
        if len(unique) > MAX_BULK_SUGGESTIONS:
            raise ValueError(
                f"At most {MAX_BULK_SUGGESTIONS} suggestions may be ignored in one call."
            )
        return unique


class BulkIgnoreResult(BaseModel):
    """Outcome of a bulk dismissal.

    ``skipped`` is reported rather than raising: a planner selecting a page of
    suggestions where one has already been converted should not have the whole
    action refused, but must be told which ones were left alone.
    """

    ignored_count: int
    ignored_ids: list[uuid.UUID] = Field(default_factory=list)
    skipped_ids: list[uuid.UUID] = Field(default_factory=list)
    message: str


__all__ = [
    "DEFAULT_CONVERSION_TARGET",
    "MAX_BULK_SUGGESTIONS",
    "MAX_MANUAL_DEMAND_LINES",
    "MAX_RUN_WAREHOUSES",
    "BulkIgnoreRequest",
    "BulkIgnoreResult",
    "ConversionRequest",
    "ConversionTarget",
    "ManualDemandIn",
    "MrpDemandRead",
    "MrpRequirementRead",
    "MrpRunFilters",
    "MrpRunRead",
    "MrpRunRequest",
    "MrpRunSummary",
    "MrpSuggestionRead",
    "ShortageReportFilters",
    "SuggestionConversionResult",
    "SuggestionConvertRequest",
    "SuggestionFilters",
]
