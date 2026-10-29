"""Request and response schemas for the procurement lifecycle.

Conventions applied throughout, each for a specific reason:

* **Nothing that decides money or authority is accepted from the client.**
  ``tax_rate`` comes from the item master, ``company_id`` from the tenant
  context, and ``status`` is only ever moved by a named transition endpoint. A
  request cannot understate GST on a statutory document or write itself into
  another tenant.
* **A percentage discount and a flat discount are mutually exclusive.** Sending
  both is ambiguous, and quietly preferring one produces an order total the
  buyer did not intend.
* **Accepted and rejected quantities are both explicit on a receipt line.** The
  service refuses a line where they do not add up to what arrived rather than
  inferring the missing one: a storekeeper who types 100 received and 5 rejected
  and means 95 accepted is making a claim the document should carry, not one the
  server should guess.
* **Money and quantities are ``Decimal`` with explicit precision**, so Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (master rules
  section 84).
* **Every destructive or lifecycle action carries a reason.** Cancellations,
  rejections, short closes and returns all end up in the audit trail, and an
  entry that cannot say why is not an audit trail (section 83).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.constants import DEFAULT_CURRENCY
from src.core.demand_provenance import DemandReference
from src.modules.procurement.models import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MAX_SOURCE_DEMAND_REFS,
    MIN_PRIORITY,
    GoodsReceiptStatus,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    ReceiptLineQcStatus,
    RequisitionLineStatus,
    RequisitionStatus,
    RfqStatus,
)
from src.modules.procurement.service_rules import (
    RequisitionPriceSource,
    RequisitionVendorSource,
    VendorAssigneeReason,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NonNegativeUnitPrice = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
DiscountPercentage = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
TaxRate = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
CurrencyCode = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
Priority = Annotated[int, Field(ge=MIN_PRIORITY, le=MAX_PRIORITY)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
LeadTimeDays = Annotated[int, Field(ge=0, le=3650)]
PaymentTermsDays = Annotated[int, Field(ge=0, le=365)]
#: The pools a purchase return may draw from - ``models.RETURNABLE_STOCK_STATES``
#: spelled as a literal so the contract enumerates it.
ReturnStockState = Literal["AVAILABLE", "DAMAGED"]

#: A document with more lines than this is a data-entry accident or a bulk
#: import that belongs in a job, not an HTTP request. The bound matters because
#: create, update and posting all handle the whole line set in one transaction.
MAX_LINES_PER_DOCUMENT = 200


def _validate_line_numbers(numbers: list[int], document: str) -> None:
    """Reject a line set the database would reject anyway, with a usable message.

    Duplicate line numbers hit a unique constraint halfway through the insert,
    which surfaces as an integrity error naming a constraint rather than naming
    the line the user typed twice.
    """
    if not numbers:
        raise ValueError(f"A {document} needs at least one line.")
    if len(numbers) > MAX_LINES_PER_DOCUMENT:
        raise ValueError(f"A {document} cannot have more than {MAX_LINES_PER_DOCUMENT} lines.")
    if len(set(numbers)) != len(numbers):
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        raise ValueError(f"Duplicate line numbers on the {document}: {duplicates}")


# =============================================================================
# Purchase requisition
# =============================================================================


class SourceDemandRef(DemandReference):
    """One demand a requisition line was raised for (WP D1 section 1).

    Stored on the line as a JSONB array and carried onto purchase orders at
    conversion. ``demand_type`` is an open vocabulary because the sources are
    deliberately not foreign keys - a sales order line today, a BOM demand or a
    packing order tomorrow - and the duplicate matcher only compares triples
    for equality, never resolves them.
    """


class RequisitionLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    item_id: uuid.UUID
    description: str | None = Field(default=None, max_length=500)
    quantity: PositiveQuantity
    #: Defaults to the item's purchase unit, falling back to its stock unit.
    uom_id: uuid.UUID | None = None
    required_date: date | None = None
    #: The requester's expectation, never a committed price.
    estimated_unit_price: NonNegativeUnitPrice | None = None
    suggested_vendor_id: uuid.UUID | None = None
    #: Demand provenance (WP D1). The planner sets it; a typed line leaves it
    #: empty. Capped rather than unbounded - see ``MAX_SOURCE_DEMAND_REFS``.
    source_demand_refs: list[SourceDemandRef] = Field(
        default_factory=list, max_length=MAX_SOURCE_DEMAND_REFS
    )
    notes: str | None = Field(default=None, max_length=2000)


class RequisitionLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requisition_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    required_date: date | None
    estimated_unit_price: Decimal | None
    suggested_vendor_id: uuid.UUID | None
    ordered_quantity: Decimal
    status: RequisitionLineStatus
    source_demand_refs: list[SourceDemandRef] | None = None
    notes: str | None


class RequisitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_date: date
    warehouse_id: uuid.UUID
    requisition_date: date | None = None
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    requested_by_id: uuid.UUID | None = None
    priority: Priority = DEFAULT_PRIORITY
    purpose: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL
    source_id: uuid.UUID | None = None
    lines: list[RequisitionLineIn]

    @model_validator(mode="after")
    def _check_lines(self) -> RequisitionCreate:
        _validate_line_numbers([line.line_number for line in self.lines], "requisition")
        return self


class RequisitionUpdate(BaseModel):
    """Partial update of a draft requisition.

    ``lines`` replaces the whole set when present: a patch language for
    individual lines would need its own add/change/remove vocabulary, and the
    document is small enough that resending it is honest about what the result
    will be.
    """

    model_config = ConfigDict(extra="forbid")

    required_date: date | None = None
    warehouse_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    requested_by_id: uuid.UUID | None = None
    priority: Priority | None = None
    purpose: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[RequisitionLineIn] | None = None

    @model_validator(mode="after")
    def _check_lines(self) -> RequisitionUpdate:
        if self.lines is not None:
            _validate_line_numbers([line.line_number for line in self.lines], "requisition")
        return self


class RequisitionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    requisition_date: date
    required_date: date
    warehouse_id: uuid.UUID
    department_id: uuid.UUID | None
    status: RequisitionStatus
    priority: int
    requested_by_id: uuid.UUID | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    purpose: str | None
    created_at: datetime


class RequisitionDetail(RequisitionSummary):
    branch_id: uuid.UUID | None
    rejection_reason: str | None
    cancellation_reason: str | None
    notes: str | None
    lines: list[RequisitionLineRead] = Field(default_factory=list)


class RequisitionListRow(RequisitionSummary):
    """A requisition as the list shows it.

    ``open_line_count`` is how many of its lines still have quantity to order -
    live lines whose ordered quantity is short of the requested one - so the
    list can offer "Raise purchase orders" only where there is something left
    to raise. Counted for the whole page in one grouped statement.
    """

    open_line_count: int


class RequisitionFilters(BaseModel):
    search: str | None = None
    status: RequisitionStatus | None = None
    warehouse_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    open_only: bool = False
    #: APPROVED or CONVERTED with at least one line still to order - what the
    #: batch conversion picker lists.
    convertible_only: bool = False
    required_from: date | None = None
    required_to: date | None = None


# =============================================================================
# Request for quotation
# =============================================================================


class RfqLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    item_id: uuid.UUID
    description: str | None = Field(default=None, max_length=500)
    quantity: PositiveQuantity
    uom_id: uuid.UUID | None = None
    required_date: date | None = None
    requisition_line_id: uuid.UUID | None = None
    specification: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=2000)


class RfqLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rfq_id: uuid.UUID
    line_number: int
    requisition_line_id: uuid.UUID | None
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    required_date: date | None
    specification: str | None
    notes: str | None


class RfqCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_id: uuid.UUID
    rfq_date: date | None = None
    response_due_date: date | None = None
    required_date: date | None = None
    #: Raise the RFQ from an approved requisition. When given without ``lines``,
    #: every open requisition line becomes an RFQ line.
    requisition_id: uuid.UUID | None = None
    vendor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    currency: CurrencyCode = DEFAULT_CURRENCY
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[RfqLineIn] | None = None

    @model_validator(mode="after")
    def _check_lines(self) -> RfqCreate:
        if self.lines is not None:
            _validate_line_numbers([line.line_number for line in self.lines], "RFQ")
        elif self.requisition_id is None:
            raise ValueError("Give either a requisition to copy or an explicit set of lines.")
        return self


class RfqUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_due_date: date | None = None
    required_date: date | None = None
    warehouse_id: uuid.UUID | None = None
    vendor_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    currency: CurrencyCode | None = None
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[RfqLineIn] | None = None

    @model_validator(mode="after")
    def _check_lines(self) -> RfqUpdate:
        if self.lines is not None:
            _validate_line_numbers([line.line_number for line in self.lines], "RFQ")
        return self


class RfqSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    rfq_date: date
    response_due_date: date | None
    required_date: date | None
    warehouse_id: uuid.UUID
    requisition_id: uuid.UUID | None
    vendor_ids: list[uuid.UUID]
    status: RfqStatus
    currency: str
    sent_at: datetime | None
    closed_at: datetime | None
    created_at: datetime


class RfqDetail(RfqSummary):
    terms: str | None
    notes: str | None
    cancellation_reason: str | None
    lines: list[RfqLineRead] = Field(default_factory=list)


class RfqFilters(BaseModel):
    search: str | None = None
    status: RfqStatus | None = None
    warehouse_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    due_before: date | None = None


# =============================================================================
# Vendor quotation
# =============================================================================


class QuotationLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    item_id: uuid.UUID
    description: str | None = Field(default=None, max_length=500)
    quantity: PositiveQuantity
    uom_id: uuid.UUID | None = None
    unit_price: NonNegativeUnitPrice
    discount_percentage: DiscountPercentage | None = None
    #: Flat discount for the whole line. Mutually exclusive with the percentage.
    discount: NonNegativeMoney | None = None
    #: The RFQ line being answered. Omit when the vendor offered something that
    #: was not asked for.
    rfq_line_id: uuid.UUID | None = None
    lead_time_days: LeadTimeDays | None = None
    min_order_quantity: NonNegativeQuantity | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_discount_form(self) -> QuotationLineIn:
        if self.discount_percentage is not None and self.discount is not None:
            raise ValueError(
                "Give either a discount percentage or a flat discount amount, not both."
            )
        return self


class QuotationLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    quotation_id: uuid.UUID
    line_number: int
    rfq_line_id: uuid.UUID | None
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    unit_price: Decimal
    discount_percentage: Decimal
    discount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    lead_time_days: int | None
    min_order_quantity: Decimal
    notes: str | None


class QuotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rfq_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_reference: Annotated[str, Field(min_length=1, max_length=64)]
    quotation_date: date | None = None
    valid_until: date | None = None
    currency: CurrencyCode = DEFAULT_CURRENCY
    #: Defaults to the vendor's agreed terms when omitted. ``0`` is a legal,
    #: meaningful value - an ex-stock quote, or cash on delivery - and is stored
    #: as sent, never replaced by the vendor master's figure.
    payment_terms_days: PaymentTermsDays | None = None
    lead_time_days: LeadTimeDays | None = None
    freight_amount: NonNegativeMoney = Decimal("0")
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[QuotationLineIn]

    @model_validator(mode="after")
    def _check_lines(self) -> QuotationCreate:
        _validate_line_numbers([line.line_number for line in self.lines], "quotation")
        return self


class QuotationUpdate(BaseModel):
    """A revised offer from the same vendor against the same RFQ."""

    model_config = ConfigDict(extra="forbid")

    vendor_reference: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    quotation_date: date | None = None
    valid_until: date | None = None
    currency: CurrencyCode | None = None
    payment_terms_days: PaymentTermsDays | None = None
    lead_time_days: LeadTimeDays | None = None
    freight_amount: NonNegativeMoney | None = None
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[QuotationLineIn] | None = None
    #: Why the offer is being revised. Recorded on the status change when a
    #: rejected or lapsed quotation is reopened by the revised offer.
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _check_lines(self) -> QuotationUpdate:
        if self.lines is not None:
            _validate_line_numbers([line.line_number for line in self.lines], "quotation")
        return self


class QuotationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rfq_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_reference: str
    quotation_date: date
    valid_until: date | None
    currency: str
    payment_terms_days: int
    lead_time_days: int
    subtotal: Decimal
    discount: Decimal
    tax_amount: Decimal
    freight_amount: Decimal
    total_amount: Decimal
    status: QuotationStatus
    selected_at: datetime | None
    converted_purchase_order_id: uuid.UUID | None
    created_at: datetime


class QuotationDetail(QuotationSummary):
    selected_by_id: uuid.UUID | None
    decision_reason: str | None
    terms: str | None
    notes: str | None
    lines: list[QuotationLineRead] = Field(default_factory=list)


class QuotationFilters(BaseModel):
    rfq_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    status: QuotationStatus | None = None
    search: str | None = None
    valid_on: date | None = None


class QuotationDecisionRequest(BaseModel):
    """Select or reject one quotation.

    The reason is required even when following the recommendation: the buyer's
    judgement is the record, and the comparison's score is only its input.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason


# --- comparison ---------------------------------------------------------------


class ComparisonOffer(BaseModel):
    """One vendor's offer for one item, priced for comparison."""

    vendor_id: uuid.UUID
    vendor_code: str
    vendor_name: str
    quotation_id: uuid.UUID
    quotation_line_id: uuid.UUID
    unit_price: Decimal
    discount_percentage: Decimal
    #: Unit price after the line discount - the number that is actually
    #: comparable between two vendors quoting different discount structures.
    effective_unit_price: Decimal
    quantity: Decimal
    tax_rate: Decimal
    line_total: Decimal
    lead_time_days: int
    vendor_rating: Decimal | None
    payment_terms_days: int
    is_expired: bool
    is_lowest_price: bool
    is_fastest: bool
    #: 0-1, higher is better. Weighted blend of price, lead time and rating.
    score: Decimal
    rank: int


class ComparisonItemRow(BaseModel):
    """One row of the side-by-side: one item, every vendor's offer for it."""

    item_id: uuid.UUID
    item_code: str
    item_name: str
    quantity: Decimal
    uom_id: uuid.UUID | None
    rfq_line_id: uuid.UUID | None
    offers: list[ComparisonOffer]
    recommended_vendor_id: uuid.UUID | None
    recommendation_reason: str


class ComparisonVendorColumn(BaseModel):
    """One vendor's column header: the totals a buyer compares at a glance."""

    vendor_id: uuid.UUID
    vendor_code: str
    vendor_name: str
    quotation_id: uuid.UUID
    quotation_status: QuotationStatus
    vendor_rating: Decimal | None
    rating_sample_size: int
    lead_time_days: int
    payment_terms_days: int
    currency: str
    subtotal: Decimal
    freight_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    valid_until: date | None
    is_expired: bool
    quoted_item_count: int


class QuotationComparison(BaseModel):
    """Side-by-side comparison of every quotation received for one RFQ."""

    rfq_id: uuid.UUID
    rfq_code: str
    rfq_status: RfqStatus
    currency: str
    vendors: list[ComparisonVendorColumn]
    items: list[ComparisonItemRow]
    recommended_vendor_id: uuid.UUID | None
    recommendation_reason: str
    #: True when the vendors quoted in different currencies. Their totals are
    #: then not directly comparable and the recommendation is withheld rather
    #: than computed from numbers that do not share a unit.
    mixed_currency: bool


# =============================================================================
# Purchase order
# =============================================================================


class PurchaseOrderSourceAllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requisition_line_id: uuid.UUID
    quantity: PositiveQuantity


class PurchaseOrderSourceAllocationRead(PurchaseOrderSourceAllocationIn):
    model_config = ConfigDict(from_attributes=True)

    requisition_code: str | None = None
    requisition_line_number: int | None = None
    is_active: bool = True


def _validate_source_allocations(
    allocations: list[PurchaseOrderSourceAllocationIn] | None,
    quantity: Decimal,
    scalar_source: uuid.UUID | None = None,
) -> None:
    if allocations is None:
        return
    if scalar_source is not None:
        raise ValueError("Use source_allocations or requisition_line_id, not both.")
    identities = [allocation.requisition_line_id for allocation in allocations]
    if len(set(identities)) != len(identities):
        raise ValueError("A requisition line can be allocated only once per PO line.")
    if sum((allocation.quantity for allocation in allocations), Decimal("0")) > quantity:
        raise ValueError("Source allocations cannot exceed the physical PO line quantity.")


class PurchaseOrderLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    item_id: uuid.UUID
    description: str | None = Field(default=None, max_length=500)
    quantity: PositiveQuantity
    uom_id: uuid.UUID | None = None
    unit_price: NonNegativeUnitPrice
    discount_percentage: DiscountPercentage | None = None
    #: Flat discount for the whole line. Mutually exclusive with the percentage.
    discount: NonNegativeMoney | None = None
    required_date: date | None = None
    #: Hold the arriving quantity for incoming inspection instead of releasing
    #: it to available stock.
    requires_qc: bool = False
    requisition_line_id: uuid.UUID | None = None
    source_allocations: list[PurchaseOrderSourceAllocationIn] | None = Field(
        default=None, max_length=MAX_LINES_PER_DOCUMENT
    )
    quotation_line_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_discount_form(self) -> PurchaseOrderLineIn:
        _validate_source_allocations(
            self.source_allocations, self.quantity, self.requisition_line_id
        )
        if self.discount_percentage is not None and self.discount is not None:
            raise ValueError(
                "Give either a discount percentage or a flat discount amount, not both."
            )
        return self


class PurchaseOrderLineRead(BaseModel):
    item_code: str | None = None
    item_name: str | None = None
    item_category_id: uuid.UUID | None = None
    uom_code: str | None = None
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purchase_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    unit_price: Decimal
    discount_percentage: Decimal
    discount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    required_date: date | None
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    returned_quantity: Decimal
    requires_qc: bool
    status: PurchaseOrderLineStatus
    requisition_line_id: uuid.UUID | None
    source_allocations: list[PurchaseOrderSourceAllocationRead] = Field(default_factory=list)
    quotation_line_id: uuid.UUID | None
    notes: str | None


class PurchaseOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    po_date: date | None = None
    expected_delivery_date: date | None = None
    branch_id: uuid.UUID | None = None
    priority: Priority = DEFAULT_PRIORITY
    currency: CurrencyCode = DEFAULT_CURRENCY
    freight_amount: NonNegativeMoney = Decimal("0")
    #: Defaults to the vendor's agreed terms when omitted.
    payment_terms_days: PaymentTermsDays | None = None
    delivery_address: str | None = Field(default=None, max_length=4000)
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    vendor_reference: str | None = Field(default=None, max_length=64)
    assigned_vendor_user_id: uuid.UUID | None = None
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL
    source_id: uuid.UUID | None = None

    #: Proceed even though the duplicate check found candidates. Refused unless
    #: the caller holds ``purchase_order.approve`` (master rules section 16).
    acknowledge_duplicates: bool = False
    #: Why the duplicate was accepted. Required whenever the flag is set - an
    #: acknowledgement with no reason is a click, not a decision.
    duplicate_acknowledgement_reason: str | None = Field(default=None, max_length=1000)

    lines: list[PurchaseOrderLineIn]

    @model_validator(mode="after")
    def _check_payload(self) -> PurchaseOrderCreate:
        _validate_line_numbers([line.line_number for line in self.lines], "purchase order")
        acknowledgement = (self.duplicate_acknowledgement_reason or "").strip()
        if self.acknowledge_duplicates and not acknowledgement:
            raise ValueError(
                "Acknowledging duplicate purchase orders requires a reason to be recorded."
            )
        return self


class PurchaseOrderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_delivery_date: date | None = None
    warehouse_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    priority: Priority | None = None
    currency: CurrencyCode | None = None
    freight_amount: NonNegativeMoney | None = None
    payment_terms_days: PaymentTermsDays | None = None
    delivery_address: str | None = Field(default=None, max_length=4000)
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    vendor_reference: str | None = Field(default=None, max_length=64)
    assigned_vendor_user_id: uuid.UUID | None = None
    lines: list[PurchaseOrderLineIn] | None = None
    acknowledge_duplicates: bool = False
    duplicate_acknowledgement_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_lines(self) -> PurchaseOrderUpdate:
        if self.lines is not None:
            _validate_line_numbers([line.line_number for line in self.lines], "purchase order")
        if (
            self.acknowledge_duplicates
            and not (self.duplicate_acknowledgement_reason or "").strip()
        ):
            raise ValueError(
                "Acknowledging duplicate purchase orders requires a reason to be recorded."
            )
        return self


class PurchaseOrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    po_date: date
    expected_delivery_date: date | None
    warehouse_id: uuid.UUID
    status: PurchaseOrderStatus
    priority: int
    currency: str
    subtotal: Decimal
    discount: Decimal
    tax_amount: Decimal
    freight_amount: Decimal
    total_amount: Decimal
    payment_terms_days: int
    approved_at: datetime | None
    sent_at: datetime | None
    acknowledged_at: datetime | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    assigned_vendor_user_id: uuid.UUID | None
    #: The procurement category the order was raised for, or ``None`` when its
    #: lines span several. Derived by the service, never accepted from a client.
    item_category_id: uuid.UUID | None
    #: The label of ``item_category_id``, filled by the service from one batched
    #: lookup - for the whole page on the list, for the one order on the detail -
    #: because the buyers who read the order book hold ``purchase_order.view``
    #: but not ``category.view``, and a category column they cannot read is a
    #: column of dashes. ``None`` when the order has no category.
    item_category_code: str | None = None
    item_category_name: str | None = None
    vendor_committed_date: date | None = None
    ready_for_dispatch_at: datetime | None = None
    ready_packages: int | None = None
    vendor_decline_reason: str | None = None
    version: int
    amendment_number: int = 0
    last_amended_at: datetime | None = None
    last_amended_by_id: uuid.UUID | None = None
    created_at: datetime


class PurchaseOrderDetail(PurchaseOrderSummary):
    branch_id: uuid.UUID | None
    approved_by_id: uuid.UUID | None
    delivery_address: str | None
    terms: str | None
    notes: str | None
    vendor_reference: str | None
    rejection_reason: str | None
    cancellation_reason: str | None
    duplicate_acknowledged: bool
    duplicate_acknowledgement_reason: str | None
    duplicate_confirmed_by_id: uuid.UUID | None = None
    duplicate_confirmed_at: datetime | None = None
    duplicate_confirmation_reason: str | None = None
    has_receipt_history: bool = True
    courier_name: str | None
    tracking_reference: str | None
    shipped_at: datetime | None
    #: The label of ``assigned_vendor_user_id``, filled by the service so a
    #: buyer without ``user.view`` can still read who the order is with (the
    #: category's label is on the summary).
    assigned_vendor_user_name: str | None = None
    lines: list[PurchaseOrderLineRead] = Field(default_factory=list)


class PurchaseOrderFilters(BaseModel):
    search: str | None = None
    status: PurchaseOrderStatus | None = None
    vendor_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    #: Orders raised for this category or any category beneath it.
    item_category_id: uuid.UUID | None = None
    open_only: bool = False
    overdue_only: bool = False
    source_type: PurchaseSourceType | None = None
    source_id: uuid.UUID | None = None
    expected_from: date | None = None
    expected_to: date | None = None


# --- lifecycle requests -------------------------------------------------------


class ReasonRequest(BaseModel):
    """A transition whose only payload is why it happened."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=255)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The vendor-side account that will own this order in the portal.
    assigned_vendor_user_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=255)


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The date the vendor commits to, which may differ from what was asked.
    expected_delivery_date: date | None = None
    vendor_reference: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=255)


class ShipmentUpdate(BaseModel):
    """Courier details for the inbound leg (master rules section 23)."""

    model_config = ConfigDict(extra="forbid")

    courier_name: str | None = Field(default=None, max_length=120)
    tracking_reference: str | None = Field(default=None, max_length=120)
    shipped_at: datetime | None = None
    expected_delivery_date: date | None = None

    @model_validator(mode="after")
    def _something_to_record(self) -> ShipmentUpdate:
        if not any(
            (
                self.courier_name,
                self.tracking_reference,
                self.shipped_at,
                self.expected_delivery_date,
            )
        ):
            raise ValueError("Give at least one shipment detail to record.")
        return self


# --- duplicate detection ------------------------------------------------------


class DuplicateCheckLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    required_date: date | None = None
    #: The pending requisition line the order line would satisfy. An open order
    #: already claiming the same line is an EXACT duplicate.
    requisition_line_id: uuid.UUID | None = None


class DuplicateCheckRequest(BaseModel):
    """Ask what an order would collide with, before raising it."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    expected_delivery_date: date | None = None
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL
    source_id: uuid.UUID | None = None
    lines: list[DuplicateCheckLine] = Field(min_length=1, max_length=MAX_LINES_PER_DOCUMENT)


class DuplicateCandidateRead(BaseModel):
    """An existing open order that the proposed one may be duplicating."""

    purchase_order_id: uuid.UUID
    purchase_order_code: str
    purchase_order_line_id: uuid.UUID
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    ordered_quantity: Decimal
    received_quantity: Decimal
    open_quantity: Decimal
    required_date: date | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    #: EXACT when the two orders name the same source demand, STRONG when the
    #: existing order has not been sent to the vendor yet, POSSIBLE otherwise.
    confidence: str
    match_reasons: list[str]
    recommended_action: str
    available_actions: list[str]
    item_category_id: uuid.UUID | None = None
    has_receipt_history: bool = True
    purchase_order_version: int = 1


class DuplicateCheckResult(BaseModel):
    candidates: list[DuplicateCandidateRead]
    #: True when creating the order would be refused without an acknowledgement.
    requires_acknowledgement: bool


# --- requisition to purchase orders: the category-wise split -------------------

#: A batch conversion names at most this many requisitions. MRP raises one
#: single-line requisition per suggestion, so converting a planning run's
#: purchases is the batch form's whole purpose; the bound keeps the one
#: transaction that locks them all short.
MAX_REQUISITIONS_PER_CONVERSION = 50


class RequisitionConversionLineOverride(BaseModel):
    """The buyer's correction to how one requisition line is converted."""

    model_config = ConfigDict(extra="forbid")

    requisition_line_id: uuid.UUID
    #: Buy this line from this vendor instead of the resolved one.
    vendor_id: uuid.UUID | None = None
    unit_price: NonNegativeUnitPrice | None = None
    #: Defaults to what is still to order: quantity less ordered quantity. May
    #: exceed it - a vendor's minimum order rounds the buy upwards.
    quantity: PositiveQuantity | None = None
    #: Leave this line out of the conversion; it stays open on the requisition.
    exclude: bool = False


class RequisitionConversionRequest(BaseModel):
    """How to turn approved requisitions into purchase orders.

    Every header field applies to every order the conversion raises. Nothing
    names a vendor, a category or an assignee per order: those are resolved,
    and the per-line overrides are how a buyer changes the resolution.
    """

    model_config = ConfigDict(extra="forbid")

    lines: list[RequisitionConversionLineOverride] = Field(
        default_factory=list, max_length=MAX_LINES_PER_DOCUMENT
    )
    po_date: date | None = None
    #: Defaults, per order, to the earliest delivery date among its lines.
    expected_delivery_date: date | None = None
    #: Defaults, per order, to the most urgent priority among its requisitions.
    priority: Priority | None = None
    delivery_address: str | None = Field(default=None, max_length=4000)
    terms: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)

    #: As on :class:`PurchaseOrderCreate`: proceed over duplicate candidates,
    #: which needs ``purchase_order.approve`` and a reason.
    acknowledge_duplicates: bool = False
    duplicate_acknowledgement_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_payload(self) -> RequisitionConversionRequest:
        line_ids = [override.requisition_line_id for override in self.lines]
        if len(set(line_ids)) != len(line_ids):
            duplicates = sorted({str(i) for i in line_ids if line_ids.count(i) > 1})
            raise ValueError(f"Each requisition line can be overridden once: {duplicates}")
        acknowledgement = (self.duplicate_acknowledgement_reason or "").strip()
        if self.acknowledge_duplicates and not acknowledgement:
            raise ValueError(
                "Acknowledging duplicate purchase orders requires a reason to be recorded."
            )
        return self


class RequisitionBatchConversionRequest(RequisitionConversionRequest):
    """The batch form: several approved requisitions converted together."""

    requisition_ids: list[uuid.UUID] = Field(
        min_length=1, max_length=MAX_REQUISITIONS_PER_CONVERSION
    )

    @model_validator(mode="after")
    def _distinct_requisitions(self) -> RequisitionBatchConversionRequest:
        if len(set(self.requisition_ids)) != len(self.requisition_ids):
            raise ValueError("Each requisition can be named once.")
        return self


class RequisitionConversionLine(BaseModel):
    """One requisition line as it will appear on the proposed order."""

    requisition_id: uuid.UUID
    requisition_code: str
    requisition_line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str
    item_name: str
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    required_date: date | None
    unit_price: Decimal
    price_source: RequisitionPriceSource
    vendor_source: RequisitionVendorSource
    #: Quantity times price after tax at the item's GST rate - what the line
    #: adds to the order's total.
    line_total: Decimal


class RequisitionConversionGroup(BaseModel):
    """One proposed purchase order: one vendor, one category, one warehouse."""

    vendor_id: uuid.UUID
    vendor_code: str
    vendor_name: str
    item_category_id: uuid.UUID | None
    item_category_code: str | None
    item_category_name: str | None
    warehouse_id: uuid.UUID
    #: The requisition the order comes from when all its lines share one;
    #: ``None`` when a batch conversion pools several.
    source_requisition_id: uuid.UUID | None
    assigned_vendor_user_id: uuid.UUID | None
    assigned_vendor_user_name: str | None
    assignee_reason: VendorAssigneeReason
    currency: str
    expected_delivery_date: date | None
    priority: int
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    lines: list[RequisitionConversionLine]
    #: Open orders this one would collide with (master rules section 16).
    duplicate_candidates: list[DuplicateCandidateRead]
    #: Filled once the order is raised; ``None`` on a preview.
    purchase_order_id: uuid.UUID | None = None
    purchase_order_code: str | None = None


class RequisitionConversionUnresolvedLine(BaseModel):
    """A line that cannot be converted, and every reason why."""

    requisition_id: uuid.UUID
    requisition_code: str
    requisition_line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str
    #: The vendor the line resolved to, when one did and something else failed.
    vendor_id: uuid.UUID | None
    reasons: list[str]


class RequisitionConversionResult(BaseModel):
    """A conversion, proposed (preview) or done (convert)."""

    requisitions: list[RequisitionSummary]
    groups: list[RequisitionConversionGroup]
    unresolved_lines: list[RequisitionConversionUnresolvedLine]
    #: Lines left out because nothing is left to order on them, they are
    #: cancelled, or the buyer excluded them.
    skipped_line_count: int
    #: The most purchase orders one conversion may raise.
    max_orders: int
    #: Creating the orders would be refused without an acknowledgement.
    requires_acknowledgement: bool
    #: ``False`` on a preview, ``True`` once the orders exist.
    converted: bool


class PurchaseOrderApprovalRequest(BaseModel):
    """Only PO approvals carry duplicate confirmation; other approvals stay unchanged."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)
    confirm_duplicate: bool = False

    @model_validator(mode="after")
    def _confirmation_reason(self) -> PurchaseOrderApprovalRequest:
        if self.reason is not None:
            self.reason = self.reason.strip()
        if self.confirm_duplicate and len(self.reason or "") < 3:
            raise ValueError("Confirming duplicate demand requires a meaningful approval reason.")
        return self


class PurchaseOrderAmendLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: uuid.UUID | None = None
    line_number: Annotated[int, Field(ge=1, le=100_000)] | None = None
    item_id: uuid.UUID | None = None
    quantity: PositiveQuantity
    unit_price: NonNegativeUnitPrice | None = None
    uom_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    discount_percentage: DiscountPercentage | None = None
    discount: NonNegativeMoney | None = None
    required_date: date | None = None
    requires_qc: bool | None = None
    quotation_line_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    source_allocations: list[PurchaseOrderSourceAllocationIn] | None = Field(
        default=None, max_length=MAX_LINES_PER_DOCUMENT
    )

    @field_validator("unit_price", "uom_id", "requires_qc", "item_id", mode="before")
    @classmethod
    def _omit_instead_of_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Omit this field to retain its value; it cannot be null.")
        return value

    @model_validator(mode="after")
    def _valid_line(self) -> PurchaseOrderAmendLineIn:
        if self.line_id is None and (self.item_id is None or self.unit_price is None):
            raise ValueError("A new amendment line requires item_id and unit_price.")
        if self.discount is not None and self.discount_percentage is not None:
            raise ValueError("Give either a discount percentage or a flat discount, not both.")
        _validate_source_allocations(self.source_allocations, self.quantity)
        return self


class PurchaseOrderAmendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    reason: Reason
    lines: list[PurchaseOrderAmendLineIn] = Field(min_length=1, max_length=MAX_LINES_PER_DOCUMENT)
    expected_delivery_date: date | None = None
    freight_amount: NonNegativeMoney | None = None
    terms: str | None = Field(default=None, max_length=4000)
    delivery_address: str | None = Field(default=None, max_length=4000)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _unique_lines(self) -> PurchaseOrderAmendRequest:
        identities = [line.line_id for line in self.lines if line.line_id is not None]
        if len(set(identities)) != len(identities):
            raise ValueError("An existing PO line can be amended only once.")
        numbers = [line.line_number for line in self.lines if line.line_number is not None]
        if len(set(numbers)) != len(numbers):
            raise ValueError("Each amendment line number must be unique.")
        return self


class PurchaseOrderManualDuplicateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["MANUAL"]
    purchase_order: PurchaseOrderCreate


class PurchaseOrderRequisitionDuplicateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["REQUISITION"]
    requisition_ids: list[uuid.UUID] = Field(
        min_length=1, max_length=MAX_REQUISITIONS_PER_CONVERSION
    )
    requisition_line_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_LINES_PER_DOCUMENT)
    conversion: RequisitionConversionRequest

    @model_validator(mode="after")
    def _distinct_ids(self) -> PurchaseOrderRequisitionDuplicateDraft:
        if len(set(self.requisition_ids)) != len(self.requisition_ids):
            raise ValueError("A requisition can be named only once.")
        if len(set(self.requisition_line_ids)) != len(self.requisition_line_ids):
            raise ValueError("A selected requisition line can be named only once.")
        return self


class PurchaseOrderDuplicateResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["MERGE", "INCREASE", "CREATE_WITH_APPROVAL", "CANCEL_REQUEST"]
    candidate_purchase_order_id: uuid.UUID | None = None
    candidate_line_id: uuid.UUID | None = None
    expected_candidate_version: Annotated[int, Field(ge=1)] | None = None
    draft: Annotated[
        PurchaseOrderManualDuplicateDraft | PurchaseOrderRequisitionDuplicateDraft,
        Field(discriminator="kind"),
    ]
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _action_context(self) -> PurchaseOrderDuplicateResolutionRequest:
        if self.action != "CREATE_WITH_APPROVAL" and len(self.reason) > 255:
            raise ValueError("This action's reason cannot exceed 255 characters.")
        if self.action in {"MERGE", "INCREASE"} and (
            self.candidate_purchase_order_id is None or self.expected_candidate_version is None
        ):
            raise ValueError("Changing a candidate requires its ID and current version.")
        if self.action == "INCREASE" and self.candidate_line_id is None:
            raise ValueError("Increasing quantity requires the matched candidate line ID.")
        return self


class PurchaseOrderDuplicateResolutionResult(BaseModel):
    action: Literal["MERGE", "INCREASE", "CREATE_WITH_APPROVAL", "CANCEL_REQUEST"]
    purchase_orders: list[PurchaseOrderSummary] = Field(default_factory=list)
    cancelled_requisition_line_ids: list[uuid.UUID] = Field(default_factory=list)
    discarded: bool = False


class PurchaseOrderAmendmentLineRead(PurchaseOrderLineRead):
    """Frozen line terms at the revision boundary, including source quantities."""


class PurchaseOrderAmendmentValues(BaseModel):
    status: PurchaseOrderStatus
    expected_delivery_date: date | None
    freight_amount: Decimal
    terms: str | None
    delivery_address: str | None
    amendment_number: int
    lines: list[PurchaseOrderAmendmentLineRead]


class PurchaseOrderAmendmentRead(BaseModel):
    id: uuid.UUID
    amendment_number: int
    amended_at: datetime
    amended_by_id: uuid.UUID | None
    amended_by_name: str | None = None
    reason: str
    from_status: PurchaseOrderStatus
    old_values: PurchaseOrderAmendmentValues
    new_values: PurchaseOrderAmendmentValues


class PurchaseOrderVendorAssignee(BaseModel):
    """A vendor user an order may be sent to - the Send dialog's picker row.

    Served by procurement because the buyer choosing the assignee holds
    ``purchase_order.send``, not ``user.view``.
    """

    id: uuid.UUID
    full_name: str
    email: str
    #: The user's category scope; empty means every category of the vendor.
    allowed_category_ids: list[uuid.UUID]
    allowed_category_codes: list[str]
    #: The user the assignee rule would choose if the buyer names nobody.
    is_suggested: bool


# --- tracking (master rules section 23) ---------------------------------------


class TrackedReceipt(BaseModel):
    id: uuid.UUID
    code: str
    receipt_date: date
    status: GoodsReceiptStatus
    total_received_quantity: Decimal
    total_accepted_quantity: Decimal
    total_rejected_quantity: Decimal


class PurchaseOrderTracking(BaseModel):
    """The dispatch-tracking view of one inbound order."""

    purchase_order_id: uuid.UUID
    code: str
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    vendor_name: str
    warehouse_id: uuid.UUID
    po_date: date
    expected_delivery_date: date | None

    sent_at: datetime | None
    acknowledged_at: datetime | None
    shipped_at: datetime | None
    courier_name: str | None
    tracking_reference: str | None

    ordered_quantity: Decimal
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    pending_quantity: Decimal

    #: Past the promised date with quantity still outstanding.
    is_delayed: bool
    days_late: int
    #: Still expecting a delivery against this order.
    grn_pending: bool
    #: Quantity is sitting in QC_HOLD waiting for an inspection decision.
    qc_pending: bool
    qc_pending_line_count: int
    receipts: list[TrackedReceipt]


# =============================================================================
# Goods receipt
# =============================================================================


class GoodsReceiptLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    purchase_order_line_id: uuid.UUID
    received_quantity: PositiveQuantity
    #: What was taken into stock. Accepted plus rejected must equal received -
    #: the service refuses the line rather than inferring the difference.
    accepted_quantity: NonNegativeQuantity
    rejected_quantity: NonNegativeQuantity = Decimal("0")
    rejection_reason: str | None = Field(default=None, max_length=2000)

    batch_number: str | None = Field(default=None, max_length=64)
    #: Typed or scanned by goods-in, one entry per accepted unit. Minted as
    #: ``stock_serials`` rows when the receipt posts.
    serial_numbers: list[str] = Field(default_factory=list, max_length=500)
    manufactured_on: date | None = None
    expires_on: date | None = None
    location_id: uuid.UUID | None = None
    #: Overrides the order line's setting for this delivery - a first shipment
    #: from a new vendor is inspected even when the order line did not say so.
    requires_qc: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _quantities_add_up(self) -> GoodsReceiptLineIn:
        if self.accepted_quantity + self.rejected_quantity != self.received_quantity:
            raise ValueError(
                "Accepted plus rejected quantity must equal the received quantity "
                f"(line {self.line_number})."
            )
        if self.rejected_quantity > 0 and not (self.rejection_reason or "").strip():
            raise ValueError(f"A rejected quantity needs a reason (line {self.line_number}).")
        if self.expires_on and self.manufactured_on and self.expires_on < self.manufactured_on:
            raise ValueError(f"Expiry cannot precede manufacture (line {self.line_number}).")
        cleaned = ["".join(value.split()).upper() for value in self.serial_numbers]
        if any(not value or len(value) > 64 for value in cleaned):
            raise ValueError(f"Every serial must be 1-64 characters (line {self.line_number}).")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"A serial is repeated (line {self.line_number}).")
        if cleaned and Decimal(len(cleaned)) != self.accepted_quantity:
            raise ValueError(
                "A serialised receipt line must name one unit per accepted quantity "
                f"(line {self.line_number})."
            )
        self.serial_numbers = cleaned
        return self


class GoodsReceiptLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goods_receipt_id: uuid.UUID
    line_number: int
    purchase_order_line_id: uuid.UUID
    item_id: uuid.UUID
    description: str | None
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    returned_quantity: Decimal
    uom_id: uuid.UUID
    unit_cost: Decimal
    batch_number: str | None
    batch_id: uuid.UUID | None
    serial_numbers: list[str] | None
    serial_ids: list[uuid.UUID] | None
    manufactured_on: date | None
    expires_on: date | None
    location_id: uuid.UUID | None
    requires_qc: bool
    qc_status: ReceiptLineQcStatus
    qc_inspection_id: uuid.UUID | None
    posted_stock_state: str | None
    rejection_reason: str | None
    notes: str | None


class GoodsReceiptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchase_order_id: uuid.UUID
    receipt_date: date | None = None
    vendor_delivery_note: str | None = Field(default=None, max_length=64)
    vendor_invoice_number: str | None = Field(default=None, max_length=64)
    gate_entry_number: str | None = Field(default=None, max_length=64)
    vehicle_number: str | None = Field(default=None, max_length=32)
    transporter_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[GoodsReceiptLineIn]

    @model_validator(mode="after")
    def _check_lines(self) -> GoodsReceiptCreate:
        _validate_line_numbers([line.line_number for line in self.lines], "goods receipt")
        order_line_ids = [line.purchase_order_line_id for line in self.lines]
        if len(set(order_line_ids)) != len(order_line_ids):
            raise ValueError(
                "A purchase order line may appear only once on a goods receipt; "
                "combine the quantities onto one line."
            )
        serials = [serial for line in self.lines for serial in line.serial_numbers]
        if len(set(serials)) != len(serials):
            raise ValueError("A serial number may only be received once on a document.")
        return self


class GoodsReceiptSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    purchase_order_id: uuid.UUID
    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    receipt_date: date
    status: GoodsReceiptStatus
    vendor_delivery_note: str | None
    vendor_invoice_number: str | None
    total_received_quantity: Decimal
    total_accepted_quantity: Decimal
    total_rejected_quantity: Decimal
    posted_at: datetime | None
    created_at: datetime


class GoodsReceiptDetail(GoodsReceiptSummary):
    gate_entry_number: str | None
    vehicle_number: str | None
    transporter_id: uuid.UUID | None
    posted_by_id: uuid.UUID | None
    cancellation_reason: str | None
    notes: str | None
    lines: list[GoodsReceiptLineRead] = Field(default_factory=list)


class GoodsReceiptFilters(BaseModel):
    search: str | None = None
    status: GoodsReceiptStatus | None = None
    purchase_order_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    qc_pending_only: bool = False
    received_from: date | None = None
    received_to: date | None = None


class PostedMovement(BaseModel):
    """One inventory movement written by a posting, echoed back to the caller."""

    transaction_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal
    stock_state: str
    batch_id: uuid.UUID | None


class GoodsReceiptPostResult(BaseModel):
    """What posting a receipt did: stock moved, order advanced, QC raised."""

    receipt: GoodsReceiptDetail
    purchase_order_status: PurchaseOrderStatus
    movements: list[PostedMovement]
    #: Receipt lines whose quantity went to QC_HOLD instead of AVAILABLE.
    qc_hold_line_count: int
    #: True when the quality module raised inspections; False when the receipt
    #: lines were left flagged PENDING for it to pick up later.
    qc_inspections_created: bool


# =============================================================================
# Purchase return
# =============================================================================


class PurchaseReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    #: The receipt line being reversed. Required: it bounds the return by what
    #: was actually accepted and carries the batch the stock came in on.
    goods_receipt_line_id: uuid.UUID
    quantity: PositiveQuantity
    #: Which pool the quantity leaves: ``AVAILABLE`` (released stock) or
    #: ``DAMAGED`` (stock an incoming inspection rejected). Omit to let the
    #: service resolve it from the inspection; give it when a receipt line has
    #: quantity in both pools and the return must say which one.
    stock_state: ReturnStockState | None = None
    reason: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)


class PurchaseReturnLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purchase_return_id: uuid.UUID
    line_number: int
    goods_receipt_line_id: uuid.UUID
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    unit_price: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    batch_id: uuid.UUID | None
    location_id: uuid.UUID | None
    stock_state: ReturnStockState
    reason: str | None
    notes: str | None


class PurchaseReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goods_receipt_id: uuid.UUID
    reason: Reason
    return_date: date | None = None
    debit_note_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[PurchaseReturnLineIn]

    @model_validator(mode="after")
    def _check_lines(self) -> PurchaseReturnCreate:
        _validate_line_numbers([line.line_number for line in self.lines], "purchase return")
        receipt_line_ids = [line.goods_receipt_line_id for line in self.lines]
        if len(set(receipt_line_ids)) != len(receipt_line_ids):
            raise ValueError(
                "A goods receipt line may appear only once on a return; "
                "combine the quantities onto one line."
            )
        return self


class PurchaseReturnSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    goods_receipt_id: uuid.UUID
    purchase_order_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    return_date: date
    status: PurchaseReturnStatus
    reason: str
    debit_note_number: str | None
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    posted_at: datetime | None
    created_at: datetime


class PurchaseReturnDetail(PurchaseReturnSummary):
    posted_by_id: uuid.UUID | None
    cancellation_reason: str | None
    notes: str | None
    lines: list[PurchaseReturnLineRead] = Field(default_factory=list)


class PurchaseReturnFilters(BaseModel):
    search: str | None = None
    status: PurchaseReturnStatus | None = None
    vendor_id: uuid.UUID | None = None
    goods_receipt_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    return_from: date | None = None
    return_to: date | None = None


class PurchaseReturnPostResult(BaseModel):
    purchase_return: PurchaseReturnDetail
    debit_note_journal_id: uuid.UUID | None
    movements: list[PostedMovement]


# =============================================================================
# Approvals queue
# =============================================================================


class PendingApproval(BaseModel):
    """One document waiting on the caller's decision."""

    document_type: str
    document_id: uuid.UUID
    code: str
    status: str
    #: Money value of the document, so a queue can be worked highest-value first.
    total_amount: Decimal | None
    currency: str | None
    counterparty_id: uuid.UUID | None
    required_date: date | None
    priority: int
    requested_by_id: uuid.UUID | None
    created_at: datetime


class PendingApprovalFilters(BaseModel):
    document_type: str | None = None
    warehouse_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None


__all__ = [
    "MAX_LINES_PER_DOCUMENT",
    "MAX_REQUISITIONS_PER_CONVERSION",
    "AcknowledgeRequest",
    "ApproveRequest",
    "ComparisonItemRow",
    "ComparisonOffer",
    "ComparisonVendorColumn",
    "DuplicateCandidateRead",
    "DuplicateCheckLine",
    "DuplicateCheckRequest",
    "DuplicateCheckResult",
    "GoodsReceiptCreate",
    "GoodsReceiptDetail",
    "GoodsReceiptFilters",
    "GoodsReceiptLineIn",
    "GoodsReceiptLineRead",
    "GoodsReceiptPostResult",
    "GoodsReceiptSummary",
    "PendingApproval",
    "PendingApprovalFilters",
    "PostedMovement",
    "PurchaseOrderCreate",
    "PurchaseOrderDetail",
    "PurchaseOrderFilters",
    "PurchaseOrderLineIn",
    "PurchaseOrderLineRead",
    "PurchaseOrderSummary",
    "PurchaseOrderTracking",
    "PurchaseOrderUpdate",
    "PurchaseOrderVendorAssignee",
    "PurchaseReturnCreate",
    "PurchaseReturnDetail",
    "PurchaseReturnFilters",
    "PurchaseReturnLineIn",
    "PurchaseReturnLineRead",
    "PurchaseReturnPostResult",
    "PurchaseReturnSummary",
    "QuotationComparison",
    "QuotationCreate",
    "QuotationDecisionRequest",
    "QuotationDetail",
    "QuotationFilters",
    "QuotationLineIn",
    "QuotationLineRead",
    "QuotationSummary",
    "QuotationUpdate",
    "ReasonRequest",
    "RequisitionBatchConversionRequest",
    "RequisitionConversionGroup",
    "RequisitionConversionLine",
    "RequisitionConversionLineOverride",
    "RequisitionConversionRequest",
    "RequisitionConversionResult",
    "RequisitionConversionUnresolvedLine",
    "RequisitionCreate",
    "RequisitionDetail",
    "RequisitionFilters",
    "RequisitionLineIn",
    "RequisitionLineRead",
    "RequisitionListRow",
    "RequisitionSummary",
    "RequisitionUpdate",
    "RfqCreate",
    "RfqDetail",
    "RfqFilters",
    "RfqLineIn",
    "RfqLineRead",
    "RfqSummary",
    "RfqUpdate",
    "SendRequest",
    "ShipmentUpdate",
    "TrackedReceipt",
]
