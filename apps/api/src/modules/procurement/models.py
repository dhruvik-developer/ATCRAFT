"""Procurement documents: requisition -> RFQ -> quotation -> purchase order ->
goods receipt -> purchase return.

Seven modelling decisions in this file carry the weight of the module.

**Received quantity is bounded in the database, not only in the service.** A
purchase order line may be over-received by :data:`OVER_RECEIPT_TOLERANCE`, and
that bound is a CHECK constraint generated from the same Python constant the
service validates against. A tolerance enforced only in application code is one
forgotten code path away from a vendor invoicing for whatever they shipped
(master rules section 17).

**``accepted + rejected = received`` on every receipt line, in the database.**
Splitting a delivery into an accepted and a rejected part is the whole job of a
goods receipt. A line where the two parts do not add up to what arrived is not a
rounding problem; it is a receipt nobody can reconcile against the vendor's
challan.

**Quantities on the purchase order line are the running totals; the goods
receipt lines are the history.** ``received_quantity`` on the order line exists
so "what is still due?" is one indexed read rather than an aggregate over every
receipt ever posted, and it is written only by the posting path.

**The duplicate-prevention query drives two partial indexes.** Master rules
section 16 requires every new purchase order to be checked against open orders
for the same vendor, item, warehouse and demand window. That check runs on the
hottest write path in the module, so ``ix_purchase_orders_open_vendor`` and
``ix_purchase_order_lines_open_item`` are built for exactly its predicate - the
tuple of open statuses and the index must therefore change together.

**``source_type``/``source_id`` are a bare pair, not a foreign key.** A purchase
order can descend from an MRP suggestion, a requisition, a vendor quotation, a
sales order or a reorder sweep, and three of those tables belong to modules
deployed independently of this one. The same decision, for the same reason, as
the inventory ledger's ``reference_id`` (database.md section 4.2).

**Optimistic locking is on the purchase order header only.** Buyer and approver
edit a live order at the same time and the last writer must not silently win
(database.md section 1). Lines are always rewritten through the header, so the
header's version covers them. Goods receipts are single-shot documents posted
once, and correctness there rests on row locks, not on a version counter.

**A vendor quotation has no company-generated code.** ``src.common.codes``
defines no numbering series for one, and inventing a second numbering mechanism
for a document the vendor already numbered would produce two identifiers for the
same piece of paper. The vendor's own reference plus ``(rfq, vendor)`` is the
natural key, and it is unique per company.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.core.models import OptimisticLockMixin, TenantEntity, sql_in_list
from src.core.types import GUID, Money, Percentage, Quantity, UnitPrice

# -----------------------------------------------------------------------------
# Module constants
# -----------------------------------------------------------------------------

# ASSUMPTION: a goods receipt may exceed the ordered quantity by up to 5%.
# Hardware is supplied in batch quantities and an exact match is unrealistic,
# but unlimited over-receipt would let a vendor decide the order value after the
# fact. Beyond the tolerance the receipt is refused and the purchase order has
# to be amended. Recorded in docs/assumptions.md section 3; a candidate for a
# per-vendor or per-item setting once Atcraft's vendor agreements are known.
OVER_RECEIPT_TOLERANCE = Decimal("0.05")

#: The over-receipt ceiling as a multiplier, used by both the service and the
#: CHECK constraint below so the two cannot drift apart.
OVER_RECEIPT_MULTIPLIER = Decimal("1") + OVER_RECEIPT_TOLERANCE

# ASSUMPTION: two purchase orders count as competing for the same demand when
# their required dates fall within seven days of each other. A same-day-only
# rule would miss the ordinary case - the same shortage ordered twice, three
# days apart - which is precisely what section 16 exists to catch. A month-wide
# window would flag every routine repeat order for a consumable.
DUPLICATE_REQUIRED_DATE_WINDOW_DAYS = 7

#: Planning priority, shared by requisitions and purchase orders. Lower is more
#: urgent, so the natural ascending sort is also the order to work in.
#:
#: ASSUMPTION: the scale is 1-9 with 5 as the default, matching the sales
#: module. An unbounded integer would let two sites invent incompatible
#: conventions.
MIN_PRIORITY = 1
MAX_PRIORITY = 9
DEFAULT_PRIORITY = 5

#: ISO-4217 alphabetic currency codes.


# -----------------------------------------------------------------------------
# Status vocabularies
# -----------------------------------------------------------------------------


class RequisitionStatus(StrEnum):
    """Lifecycle of a purchase requisition (master rules section 15)."""

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    #: An RFQ or a purchase order has been raised from it.
    CONVERTED = "CONVERTED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class RequisitionLineStatus(StrEnum):
    PENDING = "PENDING"
    PARTIALLY_ORDERED = "PARTIALLY_ORDERED"
    ORDERED = "ORDERED"
    CANCELLED = "CANCELLED"


class RfqStatus(StrEnum):
    """Lifecycle of a request for quotation.

    Quotations arriving do not move the RFQ: an RFQ with two of five responses
    in is still an open RFQ, and a status that flickered on every vendor reply
    would say nothing a buyer could act on.
    """

    DRAFT = "DRAFT"
    SENT = "SENT"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class QuotationStatus(StrEnum):
    """Lifecycle of one vendor's response to an RFQ."""

    RECEIVED = "RECEIVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PurchaseOrderStatus(StrEnum):
    """Lifecycle of a purchase order (database.md section 7).

    ``CANCELLED`` is reachable only before anything has been received. Once
    stock has physically moved against the order, cancelling it would leave the
    inventory ledger describing a receipt against a document claiming never to
    have existed; a partially received order is short-closed instead.
    """

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    VENDOR_DECLINED = "VENDOR_DECLINED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    QC_PENDING = "QC_PENDING"
    QC_COMPLETED = "QC_COMPLETED"
    GRN_COMPLETED = "GRN_COMPLETED"
    INVOICED = "INVOICED"
    PAID = "PAID"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class PurchaseOrderLineStatus(StrEnum):
    """Fulfilment state of one order line.

    Derived from the quantities by ``service.po_line_status_for`` rather than
    set independently, so a line can never claim RECEIVED while its received
    quantity is short.
    """

    PENDING = "PENDING"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    #: Short-closed: the balance will never arrive and is no longer expected.
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class GoodsReceiptStatus(StrEnum):
    """Lifecycle of a goods receipt.

    ``POSTED`` is terminal on purpose. Posting writes the inventory ledger, and
    an append-only ledger is corrected by an offsetting entry - a purchase
    return - never by retracting the document that created it (database.md
    section 4.2).
    """

    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


class ReceiptLineQcStatus(StrEnum):
    """Incoming-inspection state of one received line.

    ``PENDING`` is the flag the quality module picks up when it is not present
    at posting time: the quantity is already sitting in ``QC_HOLD`` and needs an
    inspection raised against it.
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class PurchaseReturnStatus(StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


#: The stock pools a purchase return may take goods from, as the inventory
#: module names them. Plain strings rather than ``inventory.StockState`` so this
#: module's table definition does not import another module's enum; the first
#: entry is the default. Rendered into the CHECK constraint below.
RETURNABLE_STOCK_STATES: tuple[str, ...] = ("AVAILABLE", "DAMAGED")


class PurchaseSourceType(StrEnum):
    """Where a procurement document's demand came from.

    Read by the duplicate matcher: two orders that name the same source demand
    are covering the same shortage twice, which is the strongest duplicate
    signal there is (master rules section 16).
    """

    MANUAL = "MANUAL"
    PURCHASE_REQUISITION = "PURCHASE_REQUISITION"
    VENDOR_QUOTATION = "VENDOR_QUOTATION"
    MRP_SUGGESTION = "MRP_SUGGESTION"
    MRP_RUN = "MRP_RUN"
    SALES_ORDER = "SALES_ORDER"
    BOM_DEMAND = "BOM_DEMAND"
    REORDER = "REORDER"


# -----------------------------------------------------------------------------
# Status groupings the queries and the partial indexes share
# -----------------------------------------------------------------------------

#: Purchase-order statuses in which the order still represents an expected
#: inbound quantity. MRP nets against exactly this set, the duplicate matcher
#: searches exactly this set, and ``ix_purchase_orders_open_vendor`` is built
#: for it - so the tuple and the index change together.
OPEN_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
)

#: Statuses in which a purchase order may still be edited by a buyer. Past
#: APPROVED the vendor has a copy of the document and a quantity change is an
#: amendment, not an edit.
EDITABLE_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
)

#: Statuses a goods receipt may be posted against: the vendor has the order and
#: the goods can legitimately turn up.
RECEIVABLE_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
)

#: Requisition statuses that still represent unmet demand.
OPEN_REQUISITION_STATUSES: tuple[RequisitionStatus, ...] = (
    RequisitionStatus.APPROVED,
    RequisitionStatus.CONVERTED,
)

# ASSUMPTION: a vendor-portal principal sees a purchase order only once it has
# been sent: SENT and every status that can only follow it. CANCELLED is
# visible too, but only when the order had been sent first (``sent_at`` set) -
# see ``PurchaseOrderRepository._scoped``. DRAFT, PENDING_APPROVAL, APPROVED and
# REJECTED are the buyer's working state: an order the vendor has not been
# given, with prices nobody has approved yet (docs/assumptions.md §56).
VENDOR_VISIBLE_PO_STATUSES: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.VENDOR_DECLINED,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
    PurchaseOrderStatus.RECEIVED,
    PurchaseOrderStatus.QC_PENDING,
    PurchaseOrderStatus.QC_COMPLETED,
    PurchaseOrderStatus.GRN_COMPLETED,
    PurchaseOrderStatus.INVOICED,
    PurchaseOrderStatus.PAID,
    PurchaseOrderStatus.CLOSED,
)

#: Purchase-order-line statuses that still expect a delivery. The partial index
#: on ``purchase_order_lines`` below is built for exactly this predicate.
OPEN_PO_LINE_STATUSES: tuple[PurchaseOrderLineStatus, ...] = (
    PurchaseOrderLineStatus.PENDING,
    PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
)


# -----------------------------------------------------------------------------
# Purchase requisition
# -----------------------------------------------------------------------------


class PurchaseRequisition(TenantEntity):
    """An internal request to buy something, before any vendor is involved."""

    __tablename__ = "purchase_requisitions"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    requisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    required_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: Where the goods are wanted. NOT NULL because the requisition is the
    #: document a purchase order is raised from, and an order with no
    #: destination cannot be received anywhere.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    #: Who wants the material. Distinct from ``created_by_id``: a storekeeper
    #: routinely raises a requisition on behalf of a production supervisor, and
    #: the approver needs to see whose need it is.
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[RequisitionStatus] = mapped_column(
        String(24),
        nullable=False,
        default=RequisitionStatus.DRAFT,
        server_default=RequisitionStatus.DRAFT.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PRIORITY, server_default=str(DEFAULT_PRIORITY)
    )

    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Provenance when the requisition was generated rather than typed - an MRP
    #: suggestion, most often. Bare pair, no foreign key: see module docstring.
    source_type: Mapped[PurchaseSourceType] = mapped_column(
        String(32),
        nullable=False,
        default=PurchaseSourceType.MANUAL,
        server_default=PurchaseSourceType.MANUAL.value,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_purchase_requisitions_company_id_code"),
        # The buyer's queue and the approver's queue are the same query with a
        # different status.
        Index(
            "ix_purchase_requisitions_status_required",
            "company_id",
            "status",
            "required_date",
        ),
        Index("ix_purchase_requisitions_warehouse", "company_id", "warehouse_id", "status"),
        # "What did this MRP run raise?" - traced back through the source pair.
        Index("ix_purchase_requisitions_source", "company_id", "source_type", "source_id"),
        CheckConstraint(f"status IN ({sql_in_list(RequisitionStatus)})", name="status_known"),
        CheckConstraint(
            f"source_type IN ({sql_in_list(PurchaseSourceType)})", name="source_type_known"
        ),
        CheckConstraint(
            f"priority BETWEEN {MIN_PRIORITY} AND {MAX_PRIORITY}", name="priority_in_range"
        ),
        CheckConstraint(
            "required_date >= requisition_date", name="required_date_not_before_requisition"
        ),
        # Approval evidence is all-or-nothing: an approver with no timestamp
        # cannot be audited, a timestamp with no approver names nobody.
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)", name="approval_evidence_complete"
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name="rejected_requisitions_carry_a_reason",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_requisitions_carry_a_reason",
        ),
    )


class PurchaseRequisitionLine(TenantEntity):
    """One item wanted on a requisition."""

    __tablename__ = "purchase_requisition_lines"

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Snapshot of what was asked for. The item master may be renamed; last
    #: quarter's requisition must still read as what the requester wrote.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Indicative only - the requester's expectation, not a committed price.
    estimated_unit_price: Mapped[Decimal | None] = mapped_column(UnitPrice, nullable=True)
    suggested_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True
    )

    #: How much has been placed on purchase orders. May legitimately exceed
    #: ``quantity``: a vendor's minimum order quantity rounds the buy upwards.
    ordered_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    status: Mapped[RequisitionLineStatus] = mapped_column(
        String(24),
        nullable=False,
        default=RequisitionLineStatus.PENDING,
        server_default=RequisitionLineStatus.PENDING.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "requisition_id", "line_number", name="uq_purchase_requisition_lines_line_number"
        ),
        Index(
            "ix_purchase_requisition_lines_requisition",
            "company_id",
            "requisition_id",
            "line_number",
        ),
        Index("ix_purchase_requisition_lines_item", "company_id", "item_id", "status"),
        CheckConstraint(f"status IN ({sql_in_list(RequisitionLineStatus)})", name="status_known"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("ordered_quantity >= 0", name="ordered_quantity_non_negative"),
        CheckConstraint(
            "estimated_unit_price IS NULL OR estimated_unit_price >= 0",
            name="estimated_unit_price_non_negative",
        ),
    )


# -----------------------------------------------------------------------------
# Request for quotation
# -----------------------------------------------------------------------------


class Rfq(TenantEntity):
    """A request sent to several vendors for a price on the same basket."""

    __tablename__ = "rfqs"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    rfq_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The date after which a quotation is late. Drives the buyer's chase list.
    response_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: When the goods are wanted; copied onto the purchase order raised from the
    #: winning quotation so the vendor sees the same date they quoted against.
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    requisition_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("purchase_requisitions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: The vendors invited to quote. An array rather than a join table because
    #: the contract (database.md section 2) fixes this module's tables and does
    #: not include an ``rfq_vendors`` table; the set is small, always read whole,
    #: and never queried from the vendor side - a vendor's own quotations are
    #: found through ``vendor_quotations.vendor_id``, which is indexed.
    vendor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, server_default=text("'{}'::uuid[]")
    )

    status: Mapped[RfqStatus] = mapped_column(
        String(24), nullable=False, default=RfqStatus.DRAFT, server_default=RfqStatus.DRAFT.value
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_rfqs_company_id_code"),
        Index("ix_rfqs_status_due", "company_id", "status", "response_due_date"),
        Index("ix_rfqs_requisition", "company_id", "requisition_id"),
        CheckConstraint(f"status IN ({sql_in_list(RfqStatus)})", name="status_known"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint(
            "response_due_date IS NULL OR response_due_date >= rfq_date",
            name="response_due_not_before_rfq_date",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_rfqs_carry_a_reason",
        ),
        # An RFQ that names no vendor has been sent to nobody. Checked at SENT
        # rather than always, so a draft can be assembled one vendor at a time.
        CheckConstraint(
            "status <> 'SENT' OR array_length(vendor_ids, 1) >= 1",
            name="sent_rfqs_have_vendors",
        ),
    )


class RfqLine(TenantEntity):
    """One item every invited vendor is asked to price."""

    __tablename__ = "rfq_lines"

    rfq_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The requisition line this came from, when the RFQ was raised from one.
    #: Kept so a quotation can be traced back to the person who asked for it.
    requisition_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_requisition_lines.id", ondelete="RESTRICT"), nullable=True
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Free-text drawing revision, finish, tolerance - whatever the vendor needs
    #: to quote against. Not a structured field: every commodity wants a
    #: different one.
    specification: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("rfq_id", "line_number", name="uq_rfq_lines_line_number"),
        Index("ix_rfq_lines_rfq", "company_id", "rfq_id", "line_number"),
        Index("ix_rfq_lines_item", "company_id", "item_id"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )


# -----------------------------------------------------------------------------
# Vendor quotation
# -----------------------------------------------------------------------------


class VendorQuotation(TenantEntity):
    """One vendor's priced response to an RFQ."""

    __tablename__ = "vendor_quotations"

    rfq_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: The vendor's own quotation number. Required: it is the reference their
    #: sales desk will answer a query on, and this document has no
    #: company-generated code of its own (see the module docstring).
    vendor_reference: Mapped[str] = mapped_column(String(64), nullable=False)

    quotation_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: After this date the price is no longer offered. Read by the comparison so
    #: a lapsed quotation is not recommended.
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    #: Commercial terms quoted, which is what makes two prices comparable: a
    #: cheaper part with a four-week lead time and payment on dispatch is not
    #: obviously the better offer.
    payment_terms_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    subtotal: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    discount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    freight_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    status: Mapped[QuotationStatus] = mapped_column(
        String(24),
        nullable=False,
        default=QuotationStatus.RECEIVED,
        server_default=QuotationStatus.RECEIVED.value,
    )
    selected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why this quotation was chosen or passed over. Recorded because the
    #: comparison's recommendation is advice, and a buyer who overrides it owes
    #: the auditor a sentence (master rules section 15).
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    converted_purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True
    )

    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One live quotation per vendor per RFQ. A revised offer replaces the
        # numbers on this row rather than creating a second one, because the
        # comparison has to show one column per vendor.
        UniqueConstraint(
            "company_id", "rfq_id", "vendor_id", name="uq_vendor_quotations_rfq_vendor"
        ),
        Index("ix_vendor_quotations_vendor", "company_id", "vendor_id", "quotation_date"),
        Index("ix_vendor_quotations_status", "company_id", "status", "valid_until"),
        CheckConstraint(f"status IN ({sql_in_list(QuotationStatus)})", name="status_known"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint("payment_terms_days >= 0", name="payment_terms_non_negative"),
        CheckConstraint("lead_time_days >= 0", name="lead_time_non_negative"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("freight_amount >= 0", name="freight_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until >= quotation_date",
            name="valid_until_not_before_quotation_date",
        ),
        CheckConstraint(
            "(selected_by_id IS NULL) = (selected_at IS NULL)", name="selection_evidence_complete"
        ),
    )


class VendorQuotationLine(TenantEntity):
    """One item priced by one vendor."""

    __tablename__ = "vendor_quotation_lines"

    quotation_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendor_quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The RFQ line being answered. NULL when a vendor volunteered an item that
    #: was not asked for, which happens and must not be silently dropped.
    rfq_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("rfq_lines.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    unit_price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)
    discount_percentage: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    discount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_rate: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Per-line commercial terms. A vendor quotes one lead time for a stocked
    #: part and another for a machined one on the same sheet of paper.
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_order_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "quotation_id", "line_number", name="uq_vendor_quotation_lines_line_number"
        ),
        Index("ix_vendor_quotation_lines_quotation", "company_id", "quotation_id", "line_number"),
        # The comparison reads every offer for one RFQ line at once.
        Index("ix_vendor_quotation_lines_rfq_line", "company_id", "rfq_line_id"),
        Index("ix_vendor_quotation_lines_item", "company_id", "item_id"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name="discount_percentage_bounded",
        ),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_bounded"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        CheckConstraint("min_order_quantity >= 0", name="min_order_quantity_non_negative"),
        CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0", name="lead_time_non_negative"
        ),
    )


# -----------------------------------------------------------------------------
# Purchase order
# -----------------------------------------------------------------------------


class PurchaseOrder(TenantEntity, OptimisticLockMixin):
    """A committed order on a vendor."""

    __tablename__ = "purchase_orders"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    po_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: What the vendor committed to. Drives the delay flag on the tracking view
    #: (master rules section 23) and MRP's netting of incoming supply.
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    #: Where the goods will be received. NOT NULL: a goods receipt has to post
    #: into a warehouse, and an order that cannot say which one is unreceivable.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    status: Mapped[PurchaseOrderStatus] = mapped_column(
        String(24),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
        server_default=PurchaseOrderStatus.DRAFT.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PRIORITY, server_default=str(DEFAULT_PRIORITY)
    )

    # --- money --------------------------------------------------------------
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The money value of every line discount, rolled up. An amount, not a rate:
    #: percentage entry is resolved to money at line level and frozen, so a
    #: later change to the rounding rule cannot restate an accepted order.
    discount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Charged by the vendor at header level. Deliberately not apportioned into
    #: the item cost at receipt - see ``service.receipt_unit_cost``.
    freight_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    payment_terms_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Printed on the order. Free text rather than an address foreign key: goods
    #: are routinely directed to a site, a job-work vendor or a customer, none of
    #: which is a row in this company's warehouse table.
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- lifecycle evidence -------------------------------------------------
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the order was raised despite duplicate candidates. The reason is
    #: the audit trail master rules section 16 asks for, and an acknowledgement
    #: with no reason is a click, not a decision - hence the CHECK below.
    duplicate_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    duplicate_acknowledgement_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- provenance ---------------------------------------------------------
    source_type: Mapped[PurchaseSourceType] = mapped_column(
        String(32),
        nullable=False,
        default=PurchaseSourceType.MANUAL,
        server_default=PurchaseSourceType.MANUAL.value,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: The vendor-side account responsible for this order (master rules section
    #: 16). A vendor principal sees the orders assigned to them, and the buyer
    #: has a name to chase rather than a company switchboard.
    assigned_vendor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The procurement category this order was raised for: the one item category
    #: its lines share at the granularity the vendor is engaged at (nearest
    #: ancestor-or-self in the vendor's ``vendor_categories``), or NULL when the
    #: lines span several. Derived by the service on create, update and
    #: requisition conversion - never accepted from the client - and read by the
    #: vendor-portal scope, which shows a category-restricted vendor manager only
    #: the orders in their categories (docs/assumptions.md §50).
    item_category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=True
    )

    # --- inbound shipment (master rules section 23) -------------------------
    # ASSUMPTION: the inbound leg of a purchase order carries its courier
    # details here. The ``dispatch`` module owns ``dispatches`` and
    # ``dispatch_tracking_events``, but those describe consignments this company
    # sends; nothing in the contract gives the vendor's shipment *to* us a home,
    # and the tracking board in section 23 cannot be built without one. Recorded
    # by the vendor's dispatch manager through the portal.
    courier_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tracking_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    vendor_committed_date: Mapped[date | None] = mapped_column(Date)
    vendor_decline_reason: Mapped[str | None] = mapped_column(String(255))
    ready_for_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_packages: Mapped[int | None] = mapped_column(Integer)
    ready_note: Mapped[str | None] = mapped_column(Text)

    vendor_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_purchase_orders_company_id_code"),
        # The buyer's default list: open orders by promised date.
        Index(
            "ix_purchase_orders_status_expected",
            "company_id",
            "status",
            "expected_delivery_date",
        ),
        Index("ix_purchase_orders_vendor", "company_id", "vendor_id", "po_date"),
        Index("ix_purchase_orders_warehouse_status", "company_id", "warehouse_id", "status"),
        # THE index behind duplicate detection and MRP's incoming-supply netting
        # (master rules section 16). Partial, because a mature company's table is
        # mostly closed orders and neither query ever looks at them. Its
        # predicate is OPEN_PO_STATUSES - change both together.
        Index(
            "ix_purchase_orders_open_vendor",
            "company_id",
            "vendor_id",
            "warehouse_id",
            "expected_delivery_date",
            postgresql_where=text(f"status IN ({sql_in_list(OPEN_PO_STATUSES)})"),
        ),
        # "Which order came from this MRP suggestion?" - and the second half of
        # the duplicate check, which matches on the source demand reference.
        Index("ix_purchase_orders_source", "company_id", "source_type", "source_id"),
        # The vendor portal's own list.
        Index("ix_purchase_orders_assigned_vendor_user", "company_id", "assigned_vendor_user_id"),
        # "This vendor's orders in this category" - the category-wise split's
        # own question, and the vendor-portal scope for a category manager.
        Index("ix_purchase_orders_vendor_category", "company_id", "vendor_id", "item_category_id"),
        CheckConstraint(f"status IN ({sql_in_list(PurchaseOrderStatus)})", name="status_known"),
        CheckConstraint(
            f"source_type IN ({sql_in_list(PurchaseSourceType)})", name="source_type_known"
        ),
        CheckConstraint(
            f"priority BETWEEN {MIN_PRIORITY} AND {MAX_PRIORITY}", name="priority_in_range"
        ),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint("payment_terms_days >= 0", name="payment_terms_non_negative"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("freight_amount >= 0", name="freight_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        CheckConstraint(
            "expected_delivery_date IS NULL OR expected_delivery_date >= po_date",
            name="expected_delivery_not_before_po_date",
        ),
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)", name="approval_evidence_complete"
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name="rejected_orders_carry_a_reason",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_orders_carry_a_reason",
        ),
        CheckConstraint(
            "NOT duplicate_acknowledged OR duplicate_acknowledgement_reason IS NOT NULL",
            name="duplicate_acknowledgement_carries_a_reason",
        ),
    )


class PurchaseOrderLine(TenantEntity):
    """One item on a purchase order, with its own receipt progress."""

    __tablename__ = "purchase_order_lines"

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Snapshot of what was ordered, not a pointer to the item name. The item
    #: master may be renamed; last year's order must still print what the vendor
    #: agreed to supply.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    unit_price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)
    discount_percentage: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The money the percentage worked out to, or the flat discount the buyer
    #: typed. Stored rather than recomputed: an accepted order's arithmetic must
    #: not move under a later rounding change.
    discount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_rate: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Per-line delivery date for an order called off in tranches. NULL means
    #: the header's ``expected_delivery_date`` applies; the duplicate matcher
    #: and the tracking view both coalesce the two.
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- receipt progress, written only by the GRN posting path -------------
    received_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    accepted_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Sent back to the vendor after acceptance. Does not reduce
    #: ``accepted_quantity``: what was accepted is a historical fact, and the
    #: return is a second, offsetting event (database.md section 4.2).
    returned_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Whether arriving stock is held for incoming inspection. Lives on the line
    #: rather than on the item because ``items`` carries no incoming-QC flag and
    #: is owned by another module - and because the requirement is genuinely
    #: per-order: a first delivery from a new vendor is inspected where a repeat
    #: order of the same part is not.
    requires_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    status: Mapped[PurchaseOrderLineStatus] = mapped_column(
        String(24),
        nullable=False,
        default=PurchaseOrderLineStatus.PENDING,
        server_default=PurchaseOrderLineStatus.PENDING.value,
    )

    #: The requisition line this satisfies, when the order was raised from one.
    requisition_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_requisition_lines.id", ondelete="RESTRICT"), nullable=True
    )
    quotation_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("vendor_quotation_lines.id", ondelete="RESTRICT"), nullable=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "purchase_order_id", "line_number", name="uq_purchase_order_lines_line_number"
        ),
        Index("ix_purchase_order_lines_order", "company_id", "purchase_order_id", "line_number"),
        # The second half of the duplicate check and of MRP's incoming netting:
        # open lines for one item. Partial for the same reason as the header
        # index above; its predicate is OPEN_PO_LINE_STATUSES.
        Index(
            "ix_purchase_order_lines_open_item",
            "company_id",
            "item_id",
            "required_date",
            postgresql_where=text(f"status IN ({sql_in_list(OPEN_PO_LINE_STATUSES)})"),
        ),
        Index("ix_purchase_order_lines_requisition_line", "company_id", "requisition_line_id"),
        CheckConstraint(f"status IN ({sql_in_list(PurchaseOrderLineStatus)})", name="status_known"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name="discount_percentage_bounded",
        ),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_bounded"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        CheckConstraint("received_quantity >= 0", name="received_non_negative"),
        CheckConstraint("accepted_quantity >= 0", name="accepted_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("returned_quantity >= 0", name="returned_non_negative"),
        # The correctness floor for receiving. Over-receipt beyond the tolerance
        # is the defect this prevents, and application code is not trusted with
        # it - the multiplier is generated from OVER_RECEIPT_TOLERANCE so the
        # constraint and the service cannot disagree.
        CheckConstraint(
            f"received_quantity <= quantity * {OVER_RECEIPT_MULTIPLIER}",
            name="received_within_over_receipt_tolerance",
        ),
        # Everything that arrived was either taken in or refused. A line where
        # the two parts do not add up is a receipt nobody can reconcile against
        # the vendor's challan.
        CheckConstraint(
            "accepted_quantity + rejected_quantity = received_quantity",
            name="accepted_plus_rejected_equals_received",
        ),
        CheckConstraint("returned_quantity <= accepted_quantity", name="returned_within_accepted"),
    )


# -----------------------------------------------------------------------------
# Goods receipt
# -----------------------------------------------------------------------------


class GoodsReceipt(TenantEntity):
    """What physically arrived against a purchase order."""

    __tablename__ = "goods_receipts"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Denormalised from the order. The store's list, the vendor's ledger and
    #: the quality queue all filter by vendor, and a join to the order for a
    #: column that can never change is a join paid on every read.
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[GoodsReceiptStatus] = mapped_column(
        String(24),
        nullable=False,
        default=GoodsReceiptStatus.DRAFT,
        server_default=GoodsReceiptStatus.DRAFT.value,
    )

    # --- the vendor's paperwork, as keyed at the gate -----------------------
    vendor_delivery_note: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gate_entry_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Bare UUID: ``transporters`` belongs to the vendors module, and a hard
    #: reference is not worth the coupling for an informational field.
    transporter_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    # --- posting evidence ---------------------------------------------------
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Rolled up from the lines at posting time so the store's list does not
    #: aggregate the line table on every page load.
    total_received_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_accepted_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_goods_receipts_company_id_code"),
        Index("ix_goods_receipts_order", "company_id", "purchase_order_id", "receipt_date"),
        Index("ix_goods_receipts_vendor_date", "company_id", "vendor_id", "receipt_date"),
        Index("ix_goods_receipts_status", "company_id", "status", "receipt_date"),
        CheckConstraint(f"status IN ({sql_in_list(GoodsReceiptStatus)})", name="status_known"),
        CheckConstraint("total_received_quantity >= 0", name="received_non_negative"),
        CheckConstraint("total_accepted_quantity >= 0", name="accepted_non_negative"),
        CheckConstraint("total_rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint(
            "total_accepted_quantity + total_rejected_quantity = total_received_quantity",
            name="accepted_plus_rejected_equals_received",
        ),
        # Posting evidence is all-or-nothing; a posted receipt without it cannot
        # be attributed to anyone.
        CheckConstraint(
            "status <> 'POSTED' OR (posted_at IS NOT NULL AND posted_by_id IS NOT NULL)",
            name="posted_receipts_carry_evidence",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_receipts_carry_a_reason",
        ),
    )


class GoodsReceiptLine(TenantEntity):
    """One item on a delivery: what arrived, what was taken in, what was refused."""

    __tablename__ = "goods_receipt_lines"

    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    received_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    accepted_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Sent back after acceptance, by a purchase return that names this line.
    returned_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    #: Valuation for the inventory movement this line posts. Frozen here so the
    #: ledger entry and the receipt agree even if the order is later amended.
    unit_cost: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- traceability -------------------------------------------------------
    #: The lot as marked by the vendor. Resolved to a ``stock_batches`` row at
    #: posting time - inventory owns that table and creates the row.
    batch_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    manufactured_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True
    )

    # --- incoming quality ---------------------------------------------------
    #: Copied from the order line at receipt, so the decision that applied on
    #: the day is preserved even if the order line is later changed.
    requires_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    qc_status: Mapped[ReceiptLineQcStatus] = mapped_column(
        String(24),
        nullable=False,
        default=ReceiptLineQcStatus.NOT_REQUIRED,
        server_default=ReceiptLineQcStatus.NOT_REQUIRED.value,
    )
    #: The ``qc_inspections`` row raised for this line, when the quality module
    #: was present at posting time. Bare UUID: that table belongs to a module
    #: deployed independently of this one, and a PENDING row with no inspection
    #: id is exactly the work queue quality picks up.
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Which stock pool the accepted quantity was posted into: AVAILABLE, or
    #: QC_HOLD when the line requires inspection. Recorded because a later
    #: release has to know what it is releasing from.
    posted_stock_state: Mapped[str | None] = mapped_column(String(24), nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "goods_receipt_id", "line_number", name="uq_goods_receipt_lines_line_number"
        ),
        Index("ix_goods_receipt_lines_receipt", "company_id", "goods_receipt_id", "line_number"),
        Index("ix_goods_receipt_lines_po_line", "company_id", "purchase_order_line_id"),
        Index("ix_goods_receipt_lines_item", "company_id", "item_id"),
        # The quality module's inbound work queue: lines held for inspection
        # that nobody has raised an inspection for yet.
        Index(
            "ix_goods_receipt_lines_qc_pending",
            "company_id",
            "item_id",
            postgresql_where=text("qc_status = 'PENDING'"),
        ),
        CheckConstraint(
            f"qc_status IN ({sql_in_list(ReceiptLineQcStatus)})", name="qc_status_known"
        ),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("received_quantity > 0", name="received_positive"),
        CheckConstraint("accepted_quantity >= 0", name="accepted_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("returned_quantity >= 0", name="returned_non_negative"),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        CheckConstraint(
            "accepted_quantity + rejected_quantity = received_quantity",
            name="accepted_plus_rejected_equals_received",
        ),
        CheckConstraint("returned_quantity <= accepted_quantity", name="returned_within_accepted"),
        # A refusal without a reason cannot be argued with the vendor.
        CheckConstraint(
            "rejected_quantity = 0 OR rejection_reason IS NOT NULL",
            name="rejections_carry_a_reason",
        ),
        CheckConstraint(
            "expires_on IS NULL OR manufactured_on IS NULL OR expires_on >= manufactured_on",
            name="expiry_not_before_manufacture",
        ),
        # A line that needs inspection cannot be marked as not needing one.
        CheckConstraint(
            "NOT requires_qc OR qc_status <> 'NOT_REQUIRED'",
            name="qc_required_lines_have_a_qc_status",
        ),
    )


# -----------------------------------------------------------------------------
# Purchase return
# -----------------------------------------------------------------------------


class PurchaseReturn(TenantEntity):
    """Goods sent back to the vendor after they were accepted into stock."""

    __tablename__ = "purchase_returns"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The receipt being reversed. NOT NULL: a return exists to undo a specific
    #: delivery, and one that cannot name it cannot be valued, traced to a batch
    #: or matched against the vendor's invoice.
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("goods_receipts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    return_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[PurchaseReturnStatus] = mapped_column(
        String(24),
        nullable=False,
        default=PurchaseReturnStatus.DRAFT,
        server_default=PurchaseReturnStatus.DRAFT.value,
    )

    #: Why the goods are going back. NOT NULL - master rules section 83: no
    #: stock movement without a reason.
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The debit note raised on the vendor, when finance has issued one.
    debit_note_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_purchase_returns_company_id_code"),
        Index("ix_purchase_returns_receipt", "company_id", "goods_receipt_id"),
        Index("ix_purchase_returns_vendor_date", "company_id", "vendor_id", "return_date"),
        Index("ix_purchase_returns_status", "company_id", "status", "return_date"),
        CheckConstraint(f"status IN ({sql_in_list(PurchaseReturnStatus)})", name="status_known"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        CheckConstraint(
            "status <> 'POSTED' OR (posted_at IS NOT NULL AND posted_by_id IS NOT NULL)",
            name="posted_returns_carry_evidence",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_returns_carry_a_reason",
        ),
    )


class PurchaseReturnLine(TenantEntity):
    """One item going back, against the receipt line it reverses."""

    __tablename__ = "purchase_return_lines"

    purchase_return_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("purchase_returns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The receipt line being reversed. NOT NULL and the whole point of the
    #: table: it is what bounds the return by what was actually accepted, and
    #: what carries the batch and the cost the stock came in at.
    goods_receipt_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("goods_receipt_lines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    unit_price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Carried from the receipt line so the outgoing movement leaves the same
    #: lot the incoming one created. Bare UUID - ``stock_batches`` is inventory's.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True
    )
    #: Which pool the returned quantity leaves. ``AVAILABLE`` for stock that
    #: was released, ``DAMAGED`` for stock an incoming inspection rejected -
    #: quality moves a rejected lot from QC hold into DAMAGED, and the return
    #: has to take it from there, not from the good stock beside it. Resolved
    #: from the inspection when the line is raised and bounded per pool again
    #: when it is posted.
    stock_state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=RETURNABLE_STOCK_STATES[0],
        server_default=RETURNABLE_STOCK_STATES[0],
    )

    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "purchase_return_id", "line_number", name="uq_purchase_return_lines_line_number"
        ),
        Index("ix_purchase_return_lines_return", "company_id", "purchase_return_id", "line_number"),
        Index("ix_purchase_return_lines_receipt_line", "company_id", "goods_receipt_line_id"),
        CheckConstraint(
            "stock_state IN ('" + "', '".join(RETURNABLE_STOCK_STATES) + "')",
            name="stock_state_returnable",
        ),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_bounded"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
    )


__all__ = [
    "CURRENCY_CODE_LENGTH",
    "DEFAULT_PRIORITY",
    "DUPLICATE_REQUIRED_DATE_WINDOW_DAYS",
    "EDITABLE_PO_STATUSES",
    "MAX_PRIORITY",
    "MIN_PRIORITY",
    "OPEN_PO_LINE_STATUSES",
    "OPEN_PO_STATUSES",
    "OPEN_REQUISITION_STATUSES",
    "OVER_RECEIPT_MULTIPLIER",
    "OVER_RECEIPT_TOLERANCE",
    "RECEIVABLE_PO_STATUSES",
    "RETURNABLE_STOCK_STATES",
    "GoodsReceipt",
    "GoodsReceiptLine",
    "GoodsReceiptStatus",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "PurchaseOrderLineStatus",
    "PurchaseOrderStatus",
    "PurchaseRequisition",
    "PurchaseRequisitionLine",
    "PurchaseReturn",
    "PurchaseReturnLine",
    "PurchaseReturnStatus",
    "PurchaseSourceType",
    "QuotationStatus",
    "ReceiptLineQcStatus",
    "RequisitionLineStatus",
    "RequisitionStatus",
    "Rfq",
    "RfqLine",
    "RfqStatus",
    "VendorQuotation",
    "VendorQuotationLine",
]

# Registry discovery imports this model module; consignments live in a focused file.
from src.modules.procurement.inbound_models import (  # noqa: E402, F401
    PurchaseOrderDocument,
    PurchaseOrderShipment,
)
