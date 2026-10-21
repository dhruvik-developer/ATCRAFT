"""Request and response schemas for sales orders.

Conventions applied throughout, each for a specific reason:

* **Prices are resolved by the server unless a price override is supplied.**
  ``unit_price`` is optional on input; leaving it out means "use the price list
  for this customer's channel". Sending it is an *override* and is refused
  unless the caller holds ``price.manage`` (master rules section 38: authority
  is checked on the server, never inferred from the fact that a field arrived).
* **A percentage discount and a flat discount are mutually exclusive.** Sending
  both is ambiguous, and quietly preferring one would produce an order total the
  salesperson did not intend.
* **Tax rate is never accepted from the client.** It comes from the item's
  ``gst_rate`` and the treatment comes from the two state codes, so a request
  cannot understate GST on a statutory document.
* **Money and quantities are ``Decimal`` with explicit precision**, so Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (section 84).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.sales.models import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MIN_PRIORITY,
    SalesOrderLineStatus,
    SalesOrderStatus,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeUnitPrice = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
DiscountPercentage = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
TaxRate = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
StateCode = Annotated[str, Field(min_length=2, max_length=4, pattern=r"^[0-9]{2}$")]
CurrencyCode = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
Priority = Annotated[int, Field(ge=MIN_PRIORITY, le=MAX_PRIORITY)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]

#: An order with more lines than this is a data-entry accident or a bulk import
#: that belongs in a job, not an HTTP request. The bound matters because create
#: and update price and persist the whole line set in one transaction.
MAX_LINES_PER_ORDER = 200


# -----------------------------------------------------------------------------
# Lines
# -----------------------------------------------------------------------------


class SalesOrderLineIn(BaseModel):
    """One line on a create or update payload."""

    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    item_id: uuid.UUID
    item_variant_id: uuid.UUID | None = None
    #: Defaults to the item's name when omitted. Snapshotted onto the line so a
    #: later rename of the item does not rewrite an accepted order.
    description: str | None = Field(default=None, max_length=500)

    quantity: PositiveQuantity
    #: Defaults to the item's sale unit, falling back to its stock unit.
    uom_id: uuid.UUID | None = None

    #: Price override. Omit to let the server resolve the customer's price list.
    #: Supplying it requires ``price.manage``; below the item's cost it also
    #: requires an approval before the order can be confirmed.
    unit_price: NonNegativeUnitPrice | None = None

    discount_percentage: DiscountPercentage | None = None
    #: Flat discount for the whole line. Mutually exclusive with the percentage.
    discount_amount: NonNegativeMoney | None = None

    required_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_discount_form(self) -> SalesOrderLineIn:
        if self.discount_percentage is not None and self.discount_amount is not None:
            raise ValueError(
                "Give either a discount percentage or a flat discount amount, not both."
            )
        return self


class SalesOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sales_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_variant_id: uuid.UUID | None
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    unit_price: Decimal
    discount_percentage: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal
    required_date: date | None
    allocated_quantity: Decimal
    dispatched_quantity: Decimal
    invoiced_quantity: Decimal
    status: SalesOrderLineStatus
    reservation_id: uuid.UUID | None
    offer_id: uuid.UUID | None
    notes: str | None


# -----------------------------------------------------------------------------
# Order header
# -----------------------------------------------------------------------------


def _validate_line_set(lines: list[SalesOrderLineIn]) -> list[SalesOrderLineIn]:
    """Reject a line set that the database would reject anyway, with a message.

    Duplicate line numbers hit a unique constraint halfway through the insert,
    which surfaces as an integrity error naming a constraint rather than the
    line the user typed twice.
    """
    if not lines:
        raise ValueError("A sales order needs at least one line.")
    numbers = [line.line_number for line in lines]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise ValueError(f"Duplicate line numbers: {duplicates}")
    return lines


class SalesOrderCreate(BaseModel):
    """Create a DRAFT sales order with its lines."""

    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    customer_po_number: str | None = Field(default=None, max_length=64)

    #: Defaults to today (UTC) in the service - not here, so a frozen clock in a
    #: test is respected by both the default and the audit stamp.
    order_date: date | None = None
    required_date: date
    promised_date: date | None = None

    branch_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID

    priority: Priority = DEFAULT_PRIORITY
    is_urgent: bool = False
    #: Unspecified currency follows the company's credit-balance denomination.
    currency: CurrencyCode | None = None

    billing_address_id: uuid.UUID | None = None
    shipping_address_id: uuid.UUID | None = None
    transporter_id: uuid.UUID | None = None

    notes: str | None = Field(default=None, max_length=4000)
    lines: Annotated[list[SalesOrderLineIn], Field(min_length=1, max_length=MAX_LINES_PER_ORDER)]

    @model_validator(mode="after")
    def _check(self) -> SalesOrderCreate:
        _validate_line_set(self.lines)
        if self.order_date is not None and self.required_date < self.order_date:
            raise ValueError("required_date cannot be before order_date.")
        return self


class SalesOrderUpdate(BaseModel):
    """Partial update.

    Omitting ``lines`` leaves the line set alone. Sending it replaces the set on
    a draft, and adjusts quantities on a confirmed order - the service decides,
    because the rule depends on the order's status.
    """

    model_config = ConfigDict(extra="forbid")

    customer_po_number: str | None = Field(default=None, max_length=64)
    required_date: date | None = None
    promised_date: date | None = None
    branch_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    priority: Priority | None = None
    is_urgent: bool | None = None
    billing_address_id: uuid.UUID | None = None
    shipping_address_id: uuid.UUID | None = None
    transporter_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)
    lines: Annotated[
        list[SalesOrderLineIn] | None, Field(default=None, max_length=MAX_LINES_PER_ORDER)
    ] = None

    @model_validator(mode="after")
    def _check(self) -> SalesOrderUpdate:
        if self.lines is not None:
            _validate_line_set(self.lines)
        return self


class SalesOrderSummary(BaseModel):
    """List row. Deliberately without the lines - a page of 50 orders must not
    drag several hundred line rows with it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    customer_id: uuid.UUID
    customer_po_number: str | None
    source: str
    order_date: date
    required_date: date
    promised_date: date | None
    branch_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    status: SalesOrderStatus
    priority: int
    is_urgent: bool
    currency: str
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    is_interstate: bool
    place_of_supply_state_code: str | None
    requires_approval: bool
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class SalesOrderDetail(SalesOrderSummary):
    billing_address_id: uuid.UUID | None
    shipping_address_id: uuid.UUID | None
    transporter_id: uuid.UUID | None
    cancellation_reason: str | None
    notes: str | None
    lines: list[SalesOrderLineRead]


class SalesOrderFilters(BaseModel):
    """Server-side filters for the order list."""

    search: str | None = None
    customer_id: uuid.UUID | None = None
    source: str | None = None
    warehouse_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    status: SalesOrderStatus | None = None
    #: True narrows to the open-demand statuses in one filter, which is what
    #: every operational screen actually wants.
    open_only: bool = False
    is_urgent: bool | None = None
    required_from: date | None = None
    required_to: date | None = None


# -----------------------------------------------------------------------------
# Lifecycle payloads
# -----------------------------------------------------------------------------


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason = "Order confirmed by sales"


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Mandatory: the header carries a NOT NULL-when-cancelled constraint, and
    #: "why was this cancelled?" is the question the account manager is asked.
    reason: Reason


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason = "Below-cost pricing approved"


class AllocateOrderRequest(BaseModel):
    """``POST /sales/{id}/allocate``: top a short order up from free stock."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason = "Newly available stock allocated to the order"


# -----------------------------------------------------------------------------
# Allocation reporting - master rules section 72, "finished goods shortage"
# -----------------------------------------------------------------------------


class OtherWarehouseStock(BaseModel):
    """Free stock for one item in a warehouse other than the order's own."""

    warehouse_id: uuid.UUID
    warehouse_code: str
    warehouse_name: str
    quantity: Decimal


class LineAllocationRead(BaseModel):
    """What one line got, and what it is still short of.

    ``shortfall`` is the number MRP has to cover. It is reported per line on the
    confirm response so the salesperson sees it immediately rather than
    discovering it when the picking list comes up short.

    ``reserved_quantity`` is what the line's reservation still holds *live* - a
    reservation released, expired or consumed elsewhere counts as nothing, which
    is why it can be below ``allocated_quantity`` until the order is topped up.
    ``dispatchable_quantity`` is what a new consignment may carry for the line
    right now (``sales.service.dispatchable_quantity``).
    """

    model_config = ConfigDict(from_attributes=True)

    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    ordered_quantity: Decimal
    allocated_quantity: Decimal
    reserved_quantity: Decimal
    dispatchable_quantity: Decimal
    dispatched_quantity: Decimal
    shortfall_quantity: Decimal
    status: SalesOrderLineStatus
    reservation_id: uuid.UUID | None = None
    #: Free stock at the order's warehouse right now. Present on the
    #: allocation-status read, absent on the confirm response (where the number
    #: has just been consumed and would be misleading).
    available_quantity: Decimal | None = None
    required_date: date | None = None
    #: Free stock for this item in *other* warehouses (WP E1b section 4), one
    #: grouped read for the whole order, largest first. Empty when nothing is
    #: available elsewhere; always empty on the confirm response for the same
    #: reason as ``available_quantity``.
    available_elsewhere: list[OtherWarehouseStock] = Field(default_factory=list)


class OrderAllocationRead(BaseModel):
    """Per-line reserved versus shortfall for one order."""

    order_id: uuid.UUID
    code: str
    status: SalesOrderStatus
    warehouse_id: uuid.UUID
    fully_allocated: bool
    total_shortfall_lines: int
    lines: list[LineAllocationRead]


# -----------------------------------------------------------------------------
# Shortfall planning (WP E1b section 1)
# -----------------------------------------------------------------------------


class PlannedDocumentRead(BaseModel):
    """One draft document the shortfall plan raised or adjusted."""

    kind: str
    id: uuid.UUID
    code: str
    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity: Decimal
    status: str


class NeedsJobWorkRead(BaseModel):
    """A short line the plan deliberately left alone, because it needs job work."""

    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    quantity: Decimal


class ShortfallPlanRead(BaseModel):
    """The outcome of planning one order's shortfall.

    ``documents`` are the drafts that now cover the shortfall. ``needs_job_work``
    is not a failure: a JOB_WORK item is obtained through the job-work flow, and
    raising a production order for it would contradict the item master.
    ``warnings`` names the lines the plan could not touch, with the reason -
    typically a requisition already submitted, which is a human decision to
    change rather than something an automation should rewrite.
    """

    order_id: uuid.UUID
    documents: list[PlannedDocumentRead] = Field(default_factory=list)
    needs_job_work: list[NeedsJobWorkRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ConfirmResult(BaseModel):
    """The confirm response: the order, plus what could actually be reserved."""

    order: SalesOrderDetail
    allocation: OrderAllocationRead
    #: Credit headroom left after this order, or None when the customer has no
    #: agreed limit. Returned so the desk sees the consequence of the order it
    #: just placed.
    credit_headroom: Decimal | None = None
    #: What the auto-plan raised for the shortfall, when it ran. None when the
    #: order was fully covered or the setting is off; the "Plan shortfall"
    #: action returns the same shape.
    planned: ShortfallPlanRead | None = None


# -----------------------------------------------------------------------------
# Sales settings (WP E1b section 1)
# -----------------------------------------------------------------------------


class SalesSettingsRead(BaseModel):
    """Effective per-company sales settings.

    ``auto_plan_on_confirm`` is the value the planner acts on, default applied.
    ``is_default`` says whether it came from the code default or a stored row,
    so the screen can show "using the default" without guessing.
    """

    auto_plan_on_confirm: bool
    is_default: bool


class SalesSettingsUpdate(BaseModel):
    """The settings a company may change. Every field optional."""

    model_config = ConfigDict(extra="forbid")

    auto_plan_on_confirm: bool | None = None


# -----------------------------------------------------------------------------
# Fulfilment trail (WP E1b section 3)
# -----------------------------------------------------------------------------


class FulfilmentLineRead(BaseModel):
    """One sales order line's reservation and progress summary."""

    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    ordered_quantity: Decimal
    allocated_quantity: Decimal
    reserved_quantity: Decimal
    dispatched_quantity: Decimal
    shortfall_quantity: Decimal
    status: SalesOrderLineStatus
    reservation_id: uuid.UUID | None = None


class FulfilmentDocumentRead(BaseModel):
    """One document in the order's fulfilment trail.

    Deliberately one flexible shape rather than a schema per family: the trail
    is a timeline, every family shows the same six facts (what, which document,
    what state, how much, when, and a link), and a per-family schema would
    multiply the response contract without adding a field the screen renders
    differently. ``line_id`` attributes a document to a sales order line when
    the owning module records one; order-level documents leave it None.
    """

    kind: str
    id: uuid.UUID
    code: str
    status: str
    item_id: uuid.UUID | None = None
    item_code: str | None = None
    quantity: Decimal | None = None
    secondary_quantity: Decimal | None = None
    document_date: date | None = None
    expected_date: date | None = None
    line_id: uuid.UUID | None = None
    detail: str | None = None
    url: str


class FulfilmentRead(BaseModel):
    """Everything that has happened, or is planned, for one sales order."""

    order_id: uuid.UUID
    code: str
    status: SalesOrderStatus
    lines: list[FulfilmentLineRead] = Field(default_factory=list)
    mrp_suggestions: list[FulfilmentDocumentRead] = Field(default_factory=list)
    production_orders: list[FulfilmentDocumentRead] = Field(default_factory=list)
    assembly_orders: list[FulfilmentDocumentRead] = Field(default_factory=list)
    job_work_orders: list[FulfilmentDocumentRead] = Field(default_factory=list)
    requisitions: list[FulfilmentDocumentRead] = Field(default_factory=list)
    purchase_orders: list[FulfilmentDocumentRead] = Field(default_factory=list)
    goods_receipts: list[FulfilmentDocumentRead] = Field(default_factory=list)
    inspections: list[FulfilmentDocumentRead] = Field(default_factory=list)
    packing_orders: list[FulfilmentDocumentRead] = Field(default_factory=list)
    dispatches: list[FulfilmentDocumentRead] = Field(default_factory=list)
    transfers: list[FulfilmentDocumentRead] = Field(default_factory=list)
    invoices: list[FulfilmentDocumentRead] = Field(default_factory=list)
    credit_notes: list[FulfilmentDocumentRead] = Field(default_factory=list)
    returns: list[FulfilmentDocumentRead] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Demand summary - the MRP and dashboard feed
# -----------------------------------------------------------------------------


class DemandSummaryFilters(BaseModel):
    warehouse_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    #: Only demand needed on or before this date. MRP passes its horizon here so
    #: the aggregate does not carry orders outside the planning window.
    required_before: date | None = None
    #: Only items that are actually short - the shortage worklist.
    shortfall_only: bool = False


class DemandSummaryRow(BaseModel):
    """Open demand for one item, aggregated across every open order."""

    item_id: uuid.UUID
    open_quantity: Decimal
    allocated_quantity: Decimal
    shortfall_quantity: Decimal
    order_count: int
    line_count: int
    earliest_required_date: date | None
    #: Lowest (most urgent) priority number across the contributing orders.
    highest_priority: int
    has_urgent_order: bool


__all__ = [
    "MAX_LINES_PER_ORDER",
    "AllocateOrderRequest",
    "ApproveRequest",
    "CancelRequest",
    "ConfirmRequest",
    "ConfirmResult",
    "DemandSummaryFilters",
    "DemandSummaryRow",
    "FulfilmentDocumentRead",
    "FulfilmentLineRead",
    "FulfilmentRead",
    "LineAllocationRead",
    "NeedsJobWorkRead",
    "OrderAllocationRead",
    "OtherWarehouseStock",
    "PlannedDocumentRead",
    "SalesOrderCreate",
    "SalesOrderDetail",
    "SalesOrderFilters",
    "SalesOrderLineIn",
    "SalesOrderLineRead",
    "SalesOrderSummary",
    "SalesOrderUpdate",
    "SalesSettingsRead",
    "SalesSettingsUpdate",
    "ShortfallPlanRead",
]
