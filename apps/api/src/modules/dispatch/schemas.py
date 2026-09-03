"""Request and response schemas for dispatches and their tracking.

Conventions applied throughout, each for a reason:

* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (section 40).
* **``status`` is never accepted from a client.** Every status change goes
  through a named action endpoint and the state machine behind it, so a payload
  that could set it would be a way around both (section 73).
* **Quantities and money are ``Decimal`` with explicit precision**, so Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (section 84).
* **Every irreversible action carries a mandatory reason.** Dispatching consumes
  a reservation and moves stock, and section 83 requires a reason for every
  stock event.
* **The tracking board rows are flat projections, not nested entities.** The
  board is answered by one joined query (section 32); a nested customer object
  would invite a second query per row to fill it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.dispatch.models import (
    EWAY_BILL_LENGTH,
    MAX_CARTONS_PER_LINE,
    MAX_LINES_PER_DISPATCH,
    MAX_SERIALS_PER_LINE,
    DispatchStatus,
    DispatchType,
    FreightPaidBy,
    TrackingEventSource,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeWeight = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
Notes = Annotated[str, Field(max_length=2000)]

#: Upper bound on a single delay sweep. The Celery beat task runs hourly against
#: every tenant, so an unbounded result set is forbidden however it is reached
#: (section 31) - a backlog larger than this is worked through over several runs.
MAX_DELAY_SCAN = 500

#: Upper bound on the tracking history returned for one consignment in a single
#: read. A courier feed can produce dozens of scans; a pathological one must not
#: be able to return an unbounded page.
MAX_TRACKING_EVENTS_PAGE = 200


# -----------------------------------------------------------------------------
# Header and lines
# -----------------------------------------------------------------------------


class DispatchLineIn(BaseModel):
    """One item to load onto the consignment.

    ``sales_order_line_id`` is optional even on a dispatch raised against a sales
    order: a shipment often carries something the customer did not order on that
    line - a replacement part, a sample - and forcing a link would either block
    it or invent a demand row for it.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    quantity: PositiveQuantity
    sales_order_line_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    serial_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SERIALS_PER_LINE)
    carton_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CARTONS_PER_LINE)
    notes: Notes | None = None

    @model_validator(mode="after")
    def _serials_match_quantity(self) -> DispatchLineIn:
        """A serialised line names exactly the units it is shipping.

        Checked here as well as by the CHECK constraint so the caller is told
        which line is wrong, rather than receiving an integrity error naming a
        constraint they have never heard of.
        """
        if self.serial_ids:
            if len(set(self.serial_ids)) != len(self.serial_ids):
                raise ValueError("The same serial number is listed twice on one line.")
            if Decimal(len(self.serial_ids)) != self.quantity:
                raise ValueError(
                    f"{len(self.serial_ids)} serial numbers were given for a quantity of "
                    f"{self.quantity}; a serialised line needs exactly one per unit."
                )
        if self.carton_ids and len(set(self.carton_ids)) != len(self.carton_ids):
            raise ValueError("The same carton is listed twice on one line.")
        return self


class TransportDetails(BaseModel):
    """The section 23 capture list, shared by create and update.

    Every field is optional at creation: a consignment is often planned before
    the transporter has been booked, and refusing to record the plan until the
    lorry is known would push the whole thing into a spreadsheet.
    """

    model_config = ConfigDict(extra="forbid")

    transporter_id: uuid.UUID | None = None
    courier_name: Annotated[str, Field(max_length=120)] | None = None
    transport_name: Annotated[str, Field(max_length=160)] | None = None
    tracking_id: Annotated[str, Field(max_length=120)] | None = None
    lr_number: Annotated[str, Field(max_length=64)] | None = None
    vehicle_number: Annotated[str, Field(max_length=32)] | None = None
    driver_name: Annotated[str, Field(max_length=120)] | None = None
    driver_mobile: Annotated[str, Field(max_length=20)] | None = None
    total_boxes: Annotated[int, Field(ge=0, le=100_000)] | None = None
    total_weight_kg: NonNegativeWeight | None = None
    freight_amount: NonNegativeMoney | None = None
    freight_paid_by: FreightPaidBy | None = None
    eway_bill_number: Annotated[str, Field(max_length=EWAY_BILL_LENGTH)] | None = None
    invoice_document_id: uuid.UUID | None = None


class DispatchCreate(TransportDetails):
    """Raise a consignment in ``NOT_DISPATCHED``.

    Nothing moves: this is the loading plan. Stock leaves only when
    ``POST /{id}/dispatch`` runs.
    """

    dispatch_type: DispatchType = DispatchType.SALES
    warehouse_id: uuid.UUID
    dispatch_date: date
    expected_delivery_date: date | None = None

    customer_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    sales_order_id: uuid.UUID | None = None
    packing_order_id: uuid.UUID | None = None

    notes: Notes | None = None
    lines: list[DispatchLineIn] = Field(min_length=1, max_length=MAX_LINES_PER_DISPATCH)

    @model_validator(mode="after")
    def _counterparty_matches_type(self) -> DispatchCreate:
        """Mirror of the table's CHECK constraints, phrased for a human.

        The database is still the enforcement point - this only means the caller
        gets a 422 naming the missing field instead of a 500 naming a constraint.
        """
        if self.dispatch_type is DispatchType.SALES and self.customer_id is None:
            raise ValueError("A SALES dispatch must name the customer it is going to.")
        if (
            self.dispatch_type in (DispatchType.PURCHASE_RETURN, DispatchType.JOB_WORK)
            and self.vendor_id is None
        ):
            raise ValueError(f"A {self.dispatch_type} dispatch must name the vendor.")
        if self.sales_order_id is not None and self.dispatch_type is not DispatchType.SALES:
            raise ValueError("A sales order can only be dispatched by a SALES dispatch.")
        if (
            self.expected_delivery_date is not None
            and self.expected_delivery_date < self.dispatch_date
        ):
            raise ValueError("The expected delivery date cannot precede the dispatch date.")
        return self


class DispatchUpdate(TransportDetails):
    """Amend a consignment that has not left yet.

    ``dispatch_type``, ``customer_id``, ``vendor_id`` and ``sales_order_id`` are
    absent on purpose: sending different goods to a different party is a
    different consignment, not an edit of this one, and changing them after the
    lines were validated against them would leave the document self-inconsistent.
    """

    warehouse_id: uuid.UUID | None = None
    dispatch_date: date | None = None
    expected_delivery_date: date | None = None
    packing_order_id: uuid.UUID | None = None
    notes: Notes | None = None
    #: Supplying this replaces the whole line set. Patching lines individually
    #: would need a line identity in the payload, and a half-applied edit would
    #: leave the consignment loading a quantity nobody decided on.
    lines: list[DispatchLineIn] | None = Field(
        default=None, min_length=1, max_length=MAX_LINES_PER_DISPATCH
    )


# -----------------------------------------------------------------------------
# Actions
# -----------------------------------------------------------------------------


class ReasonRequest(BaseModel):
    """A status change that needs no data beyond why it happened."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class DispatchRequest(BaseModel):
    """Send the consignment: consume the reservations and post the movements."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    #: Overrides the planned ``dispatch_date`` when the lorry actually leaves on
    #: a different day. Omitted, the planned date stands - it is what the
    #: paperwork already carries.
    dispatch_date: date | None = None
    expected_delivery_date: date | None = None
    #: Recorded on the consignment before it leaves. The lorry receipt number is
    #: usually only known at the gate, which is why it can be supplied here as
    #: well as on the header.
    lr_number: Annotated[str, Field(max_length=64)] | None = None
    vehicle_number: Annotated[str, Field(max_length=32)] | None = None
    tracking_id: Annotated[str, Field(max_length=120)] | None = None
    location: Annotated[str, Field(max_length=255)] | None = None


class InTransitRequest(BaseModel):
    """Report that the consignment is moving, with where it was last seen."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    location: Annotated[str, Field(max_length=255)] | None = None
    event_time: datetime | None = None
    source: TrackingEventSource = TrackingEventSource.MANUAL


class DeliveryConfirmation(BaseModel):
    """Confirm the receiver has the goods."""

    model_config = ConfigDict(extra="forbid")

    #: Defaults to today in the service when omitted. Supplied explicitly when
    #: the confirmation reaches us days after the lorry actually arrived, which
    #: is the ordinary case for a signed challan coming back by post.
    actual_delivery_date: date | None = None
    delivery_note: Notes | None = None
    #: Mandatory when the consignment arrived after its expected date: an
    #: on-time-delivery report with unexplained late rows tells nobody anything.
    #: Enforced in the service, which is the only place that knows the expected
    #: date.
    delay_reason: Notes | None = None
    location: Annotated[str, Field(max_length=255)] | None = None
    event_time: datetime | None = None
    source: TrackingEventSource = TrackingEventSource.MANUAL


class TrackingEventIn(BaseModel):
    """Record one reported position or status.

    An event whose ``event_status`` differs from the consignment's current status
    also moves it, through the state machine - which is why the vocabulary is the
    same one. An event repeating the current status is a position update.
    """

    model_config = ConfigDict(extra="forbid")

    event_status: DispatchStatus
    #: Defaults to now in the service. Supplied when back-filling a courier scan
    #: that happened before we heard about it.
    event_time: datetime | None = None
    location: Annotated[str, Field(max_length=255)] | None = None
    remark: Annotated[str, Field(max_length=500)] | None = None
    source: TrackingEventSource = TrackingEventSource.MANUAL


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


class DispatchLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    sales_order_line_id: uuid.UUID | None
    item_id: uuid.UUID
    quantity: Decimal
    batch_id: uuid.UUID | None
    serial_ids: list[uuid.UUID]
    carton_ids: list[uuid.UUID]
    reservation_id: uuid.UUID | None
    inventory_transaction_id: uuid.UUID | None
    notes: str | None


class DispatchSummary(BaseModel):
    """List row. Deliberately narrower than the detail - a list of fifty
    consignments does not need every transport field."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    dispatch_type: DispatchType
    status: DispatchStatus
    customer_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    sales_order_id: uuid.UUID | None
    dispatch_date: date
    expected_delivery_date: date | None
    actual_delivery_date: date | None
    transporter_id: uuid.UUID | None
    courier_name: str | None
    tracking_id: str | None
    total_boxes: int
    total_weight_kg: Decimal
    created_at: datetime


class DispatchDetail(DispatchSummary):
    """One consignment with everything captured about it."""

    model_config = ConfigDict(from_attributes=True)

    packing_order_id: uuid.UUID | None
    dispatched_at: datetime | None
    transport_name: str | None
    lr_number: str | None
    vehicle_number: str | None
    driver_name: str | None
    driver_mobile: str | None
    freight_amount: Decimal
    freight_paid_by: FreightPaidBy | None
    eway_bill_number: str | None
    invoice_document_id: uuid.UUID | None
    delivery_confirmed_by: uuid.UUID | None
    delivery_note: str | None
    delay_reason: str | None
    delay_notified_at: datetime | None
    notes: str | None
    version: int
    lines: list[DispatchLineRead] = Field(default_factory=list)


class TrackingEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dispatch_id: uuid.UUID
    event_status: DispatchStatus
    event_time: datetime
    location: str | None
    remark: str | None
    recorded_by_id: uuid.UUID | None
    source: TrackingEventSource
    created_at: datetime


class DispatchResult(BaseModel):
    """What sending a consignment actually did.

    Returned rather than a bare 200 because the caller's next question is always
    "did the sales order close, and what did it post?" - and answering it here
    saves the UI three follow-up reads.
    """

    model_config = ConfigDict(from_attributes=True)

    dispatch: DispatchDetail
    #: Ledger rows written, one per line.
    inventory_transaction_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Reservations drawn down, in the order they were consumed.
    consumed_reservation_ids: list[uuid.UUID] = Field(default_factory=list)
    #: The sales order's status after the dispatch, when there was one. This is
    #: where PARTIALLY_DISPATCHED becomes visible to the caller.
    sales_order_id: uuid.UUID | None = None
    sales_order_status: str | None = None
    #: Order lines still owing goods after this consignment. Zero means the order
    #: is fully dispatched.
    open_sales_order_lines: int = 0


class TrackingBoardRow(BaseModel):
    """One row of the admin tracking board (master rules section 23).

    Every column here comes out of one joined query. ``is_delayed``,
    ``qc_pending`` and ``grn_pending`` are computed in SQL rather than in Python
    so the board can be filtered and sorted on them without reading the whole
    table first.
    """

    model_config = ConfigDict(from_attributes=True)

    dispatch_id: uuid.UUID
    code: str
    dispatch_type: DispatchType
    status: DispatchStatus
    #: Customer name for an outbound sale, vendor name for a return or a job-work
    #: consignment, empty for an internal transfer.
    party_name: str | None
    party_type: str
    #: The sales order this fulfils. The inbound half of the board - purchase
    #: orders on their way to us - is served by the procurement module's own
    #: tracking endpoint; see docs/assumptions.md section 19.
    reference_code: str | None
    courier_name: str | None
    transporter_name: str | None
    tracking_id: str | None
    dispatch_date: date
    expected_delivery_date: date | None
    actual_delivery_date: date | None
    is_delayed: bool
    delay_days: int
    delay_reason: str | None
    qc_pending: bool
    grn_pending: bool


class DelayedDispatchRow(BaseModel):
    """A consignment past its promised date with no delivery recorded."""

    model_config = ConfigDict(from_attributes=True)

    dispatch_id: uuid.UUID
    company_id: uuid.UUID
    code: str
    status: DispatchStatus
    customer_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    party_name: str | None
    tracking_id: str | None
    dispatch_date: date
    expected_delivery_date: date
    delay_days: int
    delay_reason: str | None
    already_notified: bool


class DelayScanResult(BaseModel):
    """What one company's delay sweep found and did."""

    model_config = ConfigDict(from_attributes=True)

    company_id: uuid.UUID
    delayed_count: int
    notified_count: int


class DispatchFilters(BaseModel):
    """Server-side filters for the consignment list. Never trusted for tenancy."""

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    status: DispatchStatus | None = None
    dispatch_type: DispatchType | None = None
    customer_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    sales_order_id: uuid.UUID | None = None
    transporter_id: uuid.UUID | None = None
    tracking_id: str | None = None
    #: Only consignments that have left and not arrived - the "where is it?" view.
    in_flight_only: bool = False
    #: Only consignments past their promised delivery date with no arrival.
    delayed_only: bool = False
    dispatch_from: date | None = None
    dispatch_to: date | None = None


__all__ = [
    "MAX_DELAY_SCAN",
    "MAX_TRACKING_EVENTS_PAGE",
    "DelayScanResult",
    "DelayedDispatchRow",
    "DeliveryConfirmation",
    "DispatchCreate",
    "DispatchDetail",
    "DispatchFilters",
    "DispatchLineIn",
    "DispatchLineRead",
    "DispatchRequest",
    "DispatchResult",
    "DispatchSummary",
    "DispatchUpdate",
    "InTransitRequest",
    "ReasonRequest",
    "TrackingBoardRow",
    "TrackingEventIn",
    "TrackingEventRead",
    "TransportDetails",
]
