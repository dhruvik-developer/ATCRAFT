"""Vendor portal contracts.

Two rules shape every class here:

* **Requests name nothing a vendor may not decide.** No tenant, no vendor id
  (it comes from the verified token), no internal cost centre - each request
  model forbids extra keys, so a smuggled ``vendor_id`` or ``cost_centre_id`` is
  a 422, not a silently ignored field.
* **Responses are allow-lists.** Each read model names its fields rather than
  extending the buyer's schema, so the buyer's internal notes, duplicate-override
  reasons, approval comments and source links never reach a vendor because a
  later change added them to the staff contract.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.core.responses import OffsetPagination
from src.modules.finance.models import MatchStatus, PaymentMethod, PaymentStatus
from src.modules.finance.models import VendorInvoiceStatus as InvoiceStatus
from src.modules.finance.schemas import VendorInvoiceCreate, VendorInvoiceLineIn
from src.modules.procurement.inbound_schemas import (
    InboundDocumentRead,
    InboundShipmentRead,
    InboundTrackingRead,
)
from src.modules.procurement.models import PurchaseOrderLineStatus, PurchaseOrderStatus

# -----------------------------------------------------------------------------
# Requests
# -----------------------------------------------------------------------------


class VendorPortalAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The vendor's own promise. Stored apart from the buyer's planning date,
    #: which only a buyer moves (docs/assumptions.md §C1-1).
    committed_delivery_date: date | None = None
    vendor_reference: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)


class VendorPortalDecline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=255)


class VendorPortalDateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    reason: str = Field(min_length=3, max_length=255)


class VendorPortalReady(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready_on: datetime | None = None
    packages: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=2000)


class VendorPortalInvoiceLineIn(BaseModel):
    """One bill line. No cost centre: a vendor cannot list them and must not name one."""

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID | None = None
    purchase_order_line_id: uuid.UUID | None = None
    goods_receipt_line_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    hsn_code: str | None = Field(default=None, max_length=16)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    uom_id: uuid.UUID | None = None
    unit_price: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=9, decimal_places=4)
    is_interstate: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class VendorPortalInvoiceCreate(BaseModel):
    """A bill against one of the vendor's own purchase orders, submitted for review."""

    model_config = ConfigDict(extra="forbid")

    purchase_order_id: uuid.UUID
    #: The id an ``INVOICE`` upload on the same order returned, if the bill has a file.
    invoice_document_id: uuid.UUID | None = None
    vendor_invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    due_date: date | None = None
    goods_receipt_id: uuid.UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    round_off_amount: Decimal = Field(default=Decimal("0"), max_digits=18, decimal_places=4)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[VendorPortalInvoiceLineIn] = Field(min_length=1, max_length=200)

    def staff_payload(self, vendor_id: uuid.UUID) -> VendorInvoiceCreate:
        """The finance module's create body, with the vendor forced from the token."""
        return VendorInvoiceCreate(
            **self.model_dump(exclude={"lines", "invoice_document_id"}),
            vendor_id=vendor_id,
            lines=[VendorInvoiceLineIn(**line.model_dump()) for line in self.lines],
        )


# -----------------------------------------------------------------------------
# Profile and dashboard
# -----------------------------------------------------------------------------


class VendorPortalCategory(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class VendorPortalMe(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    roles: list[str]
    #: The categories this account works in: its own scope, or every category
    #: the vendor is qualified for when the account is unrestricted.
    categories: list[VendorPortalCategory]
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    unread_count: int = 0


class VendorPortalDashboard(BaseModel):
    """The vendor app's tiles; each order count matches its list tab exactly."""

    to_accept: int = 0
    accepted: int = 0
    ready: int = 0
    in_transit: int = 0
    delivered: int = 0
    #: Own bills in an open status with money still owed.
    payment_pending: int = 0
    #: Own posted receipt lines with a rejected quantity, in the last
    #: ``VENDOR_QC_LOOKBACK_DAYS`` days.
    qc_rejected: int = 0
    qc_lookback_days: int


class VendorPortalPaymentSummary(BaseModel):
    """The vendor payment dashboard, one row per currency."""

    currency: str
    outstanding: Decimal
    overdue: Decimal
    due_next_seven_days: Decimal
    paid_this_month: Decimal
    last_payment_date: date | None
    last_payment_amount: Decimal | None


# -----------------------------------------------------------------------------
# Purchase orders
# -----------------------------------------------------------------------------


class VendorPortalOrderActionRead(BaseModel):
    """The vendor-visible result of accepting, declining or updating its promise."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    po_date: date
    status: PurchaseOrderStatus
    expected_delivery_date: date | None
    vendor_committed_date: date | None
    vendor_reference: str | None
    sent_at: datetime | None
    acknowledged_at: datetime | None
    ready_for_dispatch_at: datetime | None
    ready_packages: int | None
    vendor_decline_reason: str | None
    version: int


class VendorPortalOrderLine(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    item_category_id: uuid.UUID | None = None
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    uom_code: str | None = None
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


class VendorPortalTimelineEvent(BaseModel):
    """One status change the vendor took part in (from SENT onwards)."""

    id: uuid.UUID
    at: datetime
    from_status: str | None
    to_status: str


class VendorPortalInvoiceTotals(BaseModel):
    """The vendor's bills against one order (cancelled bills excluded)."""

    count: int = 0
    total: Decimal = Decimal("0")
    paid: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")


class VendorPortalReceiptTotals(BaseModel):
    count: int = 0
    qc_pending: bool = False


class VendorPortalReceiptLine(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    purchase_order_line_id: uuid.UUID | None
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rejection_reason: str | None
    qc_status: str


class VendorPortalInspection(BaseModel):
    """Quality's verdict on a received lot, as the supplier may read it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: str
    result: str | None = None
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rejection_reason: str | None = None


class VendorPortalReceiptRead(BaseModel):
    id: uuid.UUID
    code: str
    purchase_order_id: uuid.UUID
    purchase_order_code: str
    receipt_date: date
    status: str
    lines: list[VendorPortalReceiptLine]
    inspections: list[VendorPortalInspection]


class VendorPortalOrderDetail(BaseModel):
    """One order as its vendor sees it.

    The first page of shipments, documents and receipts is embedded with its
    paging metadata; the dedicated list routes page the rest
    (docs/assumptions.md §C1-8).
    """

    id: uuid.UUID
    code: str
    po_date: date
    status: PurchaseOrderStatus
    tracking: InboundTrackingRead
    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    item_category_id: uuid.UUID | None = None
    item_category_code: str | None = None
    item_category_name: str | None = None
    currency: str
    subtotal: Decimal
    discount: Decimal
    tax_amount: Decimal
    freight_amount: Decimal
    total_amount: Decimal
    payment_terms_days: int
    expected_delivery_date: date | None
    vendor_committed_date: date | None
    #: The order's own text, else the receiving warehouse's address.
    delivery_address: str | None
    terms: str | None
    vendor_reference: str | None
    sent_at: datetime | None
    acknowledged_at: datetime | None
    ready_for_dispatch_at: datetime | None
    ready_packages: int | None
    ready_note: str | None
    vendor_decline_reason: str | None
    assigned_vendor_user_id: uuid.UUID | None
    assigned_vendor_user_name: str | None = None
    version: int
    lines: list[VendorPortalOrderLine]
    timeline: list[VendorPortalTimelineEvent]
    invoice_summary: VendorPortalInvoiceTotals
    receipt_summary: VendorPortalReceiptTotals
    shipment_count: int = 0
    documents_count: int = 0
    shipments: list[InboundShipmentRead]
    shipments_pagination: OffsetPagination
    documents: list[InboundDocumentRead]
    documents_pagination: OffsetPagination
    receipts: list[VendorPortalReceiptRead]
    receipts_pagination: OffsetPagination


# -----------------------------------------------------------------------------
# Bills and payments
# -----------------------------------------------------------------------------


class VendorPortalInvoiceLine(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID | None
    purchase_order_line_id: uuid.UUID | None
    goods_receipt_line_id: uuid.UUID | None
    description: str | None
    hsn_code: str | None
    quantity: Decimal
    uom_id: uuid.UUID | None
    unit_price: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    tax_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal
    notes: str | None


class VendorPortalInvoiceSummary(BaseModel):
    """One of the vendor's own bills: status, money owed, when it is due."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    vendor_invoice_number: str
    invoice_date: date
    due_date: date
    status: InvoiceStatus
    match_status: MatchStatus
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    paid_amount: Decimal
    outstanding_amount: Decimal
    purchase_order_id: uuid.UUID | None
    goods_receipt_id: uuid.UUID | None
    created_at: datetime


class VendorPortalInvoiceDetail(VendorPortalInvoiceSummary):
    round_off_amount: Decimal
    #: Why the buyer disputed the bill - the vendor has to act on it.
    dispute_reason: str | None
    cancellation_reason: str | None
    notes: str | None
    lines: list[VendorPortalInvoiceLine] = Field(default_factory=list)


class VendorPortalPaymentAllocation(BaseModel):
    vendor_invoice_id: uuid.UUID
    invoice_code: str | None = None
    vendor_invoice_number: str | None = None
    amount: Decimal


class VendorPortalPayment(BaseModel):
    """A payment to the vendor and the bills it settled."""

    id: uuid.UUID
    code: str
    payment_date: date
    amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    currency: str
    payment_method: PaymentMethod
    #: The bank's reference (UTR / cheque number) - what the vendor reconciles by.
    reference_number: str | None
    bank_name: str | None
    instrument_date: date | None
    status: PaymentStatus
    allocations: list[VendorPortalPaymentAllocation] = Field(default_factory=list)


__all__ = [
    "VendorPortalAccept",
    "VendorPortalCategory",
    "VendorPortalDashboard",
    "VendorPortalDateChange",
    "VendorPortalDecline",
    "VendorPortalInspection",
    "VendorPortalInvoiceCreate",
    "VendorPortalInvoiceDetail",
    "VendorPortalInvoiceLine",
    "VendorPortalInvoiceLineIn",
    "VendorPortalInvoiceSummary",
    "VendorPortalInvoiceTotals",
    "VendorPortalMe",
    "VendorPortalOrderActionRead",
    "VendorPortalOrderDetail",
    "VendorPortalOrderLine",
    "VendorPortalPayment",
    "VendorPortalPaymentAllocation",
    "VendorPortalPaymentSummary",
    "VendorPortalReady",
    "VendorPortalReceiptLine",
    "VendorPortalReceiptRead",
    "VendorPortalReceiptTotals",
    "VendorPortalTimelineEvent",
]
