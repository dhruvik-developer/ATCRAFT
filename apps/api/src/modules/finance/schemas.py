"""Request and response schemas for the finance module.

Conventions applied throughout, each for a reason:

* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (section 40).
* **``status`` is never accepted from a client.** Every status change goes
  through a named action endpoint and the state machine behind it, so a payload
  that could set it would be a way around both (section 73).
* **Every amount is ``Decimal`` with explicit precision**, so Pydantic rejects a
  float before it can reach a ``NUMERIC`` column (section 84). There is no
  ``float`` anywhere in this module, and a value that arrives as one is a
  validation error rather than a silently wrong invoice.
* **Totals are computed on the server, never accepted.** A vendor invoice's
  subtotal, tax and total are derived from its lines - a client that could post
  its own totals could post an invoice whose parts do not add up, and the
  database CHECK would then reject it with a message nobody can act on.
* **Report rows are flat projections.** The outstanding, ageing, valuation and
  GST rows are answered by one aggregate query each; a nested vendor or item
  object would invite a second query per row to fill it (section 32).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from src.core.messages import date_before
from src.core.responses import OffsetPagination
from src.modules.finance.models import (
    JOB_WORK_ALLOW_FREE_LINES,
    MAX_INVOICE_LINES,
    MAX_PAYMENT_ALLOCATIONS,
    CustomerAdjustmentType,
    LedgerAccountType,
    MatchStatus,
    PartyType,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    VendorInvoiceStatus,
    VendorInvoiceType,
)

# --- reusable constrained scalars --------------------------------------------

Money = Annotated[Decimal, Field(max_digits=18, decimal_places=4)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
UnitPriceIn = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
TaxRate = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=500)]
Notes = Annotated[str, Field(max_length=2000)]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]

#: Upper bound on a ledger statement page. A statement is read a page at a time
#: and an unbounded one would be both a slow query and an unusable document.
MAX_STATEMENT_ROWS = 200

#: Upper bound on the rows one inventory-valuation page returns. The full
#: company run goes to Celery instead - see ``reports.inventory_valuation``.
MAX_VALUATION_ROWS = 200

NONCASH_ADJUSTMENT_UNSUPPORTED = (
    "Noncash adjustments cannot be recorded as cash or bank payments. "
    "Choose an actual payment method or record an appropriate journal adjustment."
)


def _money_movement_method(value: PaymentMethod | None) -> PaymentMethod | None:
    if value is PaymentMethod.ADJUSTMENT:
        raise ValueError(NONCASH_ADJUSTMENT_UNSUPPORTED)
    return value


# -----------------------------------------------------------------------------
# Chart of accounts
# -----------------------------------------------------------------------------


class LedgerAccountCreate(BaseModel):
    """Open an account in the chart.

    ``is_system`` and ``system_code`` are absent on purpose: the accounts the
    automatic postings depend on are provisioned by the module itself, and a
    client that could claim ``system_code = 'ACCOUNTS_PAYABLE'`` could redirect
    every payables posting in the company.
    """

    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=160)]
    account_type: LedgerAccountType
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    description: Notes | None = None


class LedgerAccountUpdate(BaseModel):
    """Amend an account. ``account_type`` is not amendable.

    Re-typing an account with postings in it would silently restate every report
    that has ever been run: the same rows would move from expenses to assets. A
    mis-typed account is retired and replaced.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None
    description: Notes | None = None


class LedgerAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    account_type: LedgerAccountType
    parent_id: uuid.UUID | None
    is_active: bool
    is_system: bool
    system_code: str | None
    description: str | None
    created_at: datetime
    updated_at: datetime


class LedgerAccountFilters(BaseModel):
    """Server-side filters for the chart listing."""

    model_config = ConfigDict(extra="forbid")

    search: Annotated[str, Field(max_length=120)] | None = None
    account_type: LedgerAccountType | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None
    is_system: bool | None = None


# -----------------------------------------------------------------------------
# Ledger statements
# -----------------------------------------------------------------------------


class LedgerStatementRow(BaseModel):
    """One posting on a party statement, with the balance after it.

    ``running_balance`` is computed by the database with a window function over
    the whole filtered set and is therefore correct on every page - a Python
    accumulator would only be correct on page one (see the repository).
    """

    model_config = ConfigDict(from_attributes=True)

    entry_id: uuid.UUID
    journal_id: uuid.UUID
    entry_date: date
    account_code: str
    account_name: str
    reference_type: str
    reference_code: str | None
    narration: str | None
    debit_amount: Decimal
    credit_amount: Decimal
    #: Signed so that a positive number always means "owed": receivable for a
    #: customer, payable for a vendor. See ``repository.statement_stmt``.
    running_balance: Decimal
    is_reversal: bool


class LedgerStatement(BaseModel):
    """A party statement: opening balance, the movements, closing balance."""

    party_type: PartyType
    party_id: uuid.UUID
    party_code: str
    party_name: str
    date_from: date | None
    date_to: date
    #: Balance carried in from before ``date_from``. Included in every row's
    #: ``running_balance``, so the last row of the last page is the closing
    #: balance rather than only the movement in the window.
    opening_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal
    rows: list[LedgerStatementRow] = Field(default_factory=list)


class StatementFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None

    @field_validator("date_to")
    @classmethod
    def _range_is_ordered(cls, value: date | None, info: ValidationInfo) -> date | None:
        # A field validator rather than a model one so the failure is reported
        # against ``date_to`` - ``query.date_to`` in the error envelope - and
        # the frontend can attach it to the control that produced it.
        lower = info.data.get("date_from")
        if value is not None and lower is not None and value < lower:
            raise ValueError(date_before("period end", "period start"))
        return value


# -----------------------------------------------------------------------------
# Vendor invoices
# -----------------------------------------------------------------------------


class VendorInvoiceLineIn(BaseModel):
    """One billed line.

    ``taxable_amount``, the tax split and ``line_total`` are computed by the
    server from quantity, price, discount and rate - see
    ``service.price_invoice_line``. Accepting them would let a client post a
    line whose parts do not add up, which the CHECK constraints would then
    reject with a message nobody can act on.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID | None = None
    purchase_order_line_id: uuid.UUID | None = None
    goods_receipt_line_id: uuid.UUID | None = None
    #: The job-work receipt line this charge bills. Set only on a JOB_WORK
    #: invoice; mutually exclusive with ``purchase_order_line_id`` - a line is
    #: one supply or the other, which the row's ``one_source_document`` CHECK
    #: also enforces. docs/assumptions.md §B3-1.
    job_work_receipt_line_id: uuid.UUID | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    hsn_code: Annotated[str, Field(max_length=16)] | None = None
    quantity: PositiveQuantity
    uom_id: uuid.UUID | None = None
    unit_price: UnitPriceIn
    discount_amount: NonNegativeMoney = Decimal("0")
    tax_rate: TaxRate = Decimal("0")
    #: True when the supply is inter-state and the whole tax is IGST. Resolved
    #: by the caller from the vendor's place of supply; the split itself is
    #: computed on the server.
    is_interstate: bool = False
    #: This line's own cost centre. Unset means "the invoice's".
    cost_centre_id: uuid.UUID | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def _line_identifies_what_it_bills(self) -> VendorInvoiceLineIn:
        """A line naming a receipt line must also name the order line.

        The matcher compares price against the order and quantity against the
        receipt. A line that names only the receipt could be quantity-matched
        and never price-matched, which is a two-way match wearing a three-way
        match's name.

        A job-work line names the job-work receipt line instead: its price is
        matched against the job-work order's rate and its quantity against the
        receipt line's good output, so a purchase-order link on the same line
        would price it against the wrong document.
        """
        if self.goods_receipt_line_id is not None and self.purchase_order_line_id is None:
            raise ValueError(
                "A line billing against a goods receipt must also name the purchase order line "
                "it was received against."
            )
        if self.job_work_receipt_line_id is not None and (
            self.purchase_order_line_id is not None or self.goods_receipt_line_id is not None
        ):
            raise ValueError(
                "A line billing a job-work receipt line cannot also name a purchase order line "
                "or a goods receipt line."
            )
        if self.item_id is None and not (self.description or "").strip():
            raise ValueError("A line with no item must carry a description of what is billed.")
        return self


class VendorInvoiceCreate(BaseModel):
    """Record a bill in DRAFT. Nothing posts to the ledger until approval."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    vendor_invoice_number: Annotated[str, Field(min_length=1, max_length=64)]
    invoice_date: date
    #: NULL means "use the vendor's agreed credit days", resolved once at
    #: creation and then frozen - see the model.
    due_date: date | None = None
    purchase_order_id: uuid.UUID | None = None
    goods_receipt_id: uuid.UUID | None = None
    #: What the bill charges for. ``JOB_WORK`` matches a job-work order and its
    #: receipt lines instead of a purchase order and a goods receipt, and shares
    #: everything else - payments, allocations, approval, disputes, the payables
    #: ledger and ageing - with a purchase bill. docs/assumptions.md §B3-1.
    invoice_type: VendorInvoiceType = VendorInvoiceType.PURCHASE
    #: The job-work order being billed. Required exactly when ``invoice_type``
    #: is ``JOB_WORK``; forbidden otherwise.
    job_work_order_id: uuid.UUID | None = None
    #: The cost centre the bill is charged to, and the default for every line
    #: naming none of its own. Optional; NULL is "unallocated".
    cost_centre_id: uuid.UUID | None = None
    #: NULL means "the currency this company keeps its books in" -
    #: ``companies.currency``, resolved once at creation and then frozen on the
    #: row. A module-level default here would let a company keeping books in one
    #: denomination silently accumulate invoices in another, which is precisely
    #: the disagreement that made the ageing report unreadable.
    currency: Currency | None = None
    round_off_amount: Money = Decimal("0")
    notes: Notes | None = None
    lines: Annotated[list[VendorInvoiceLineIn], Field(min_length=1, max_length=MAX_INVOICE_LINES)]

    @model_validator(mode="after")
    def _due_date_not_before_invoice(self) -> VendorInvoiceCreate:
        if self.due_date is not None and self.due_date < self.invoice_date:
            raise ValueError(date_before("due date", "invoice date"))
        return self._typed_links_cohere()

    def _typed_links_cohere(self) -> VendorInvoiceCreate:
        """The header links must agree with the invoice type.

        A job-work bill is matched against its order and receipt lines, so a
        purchase-order or goods-receipt link on it would price the bill against
        one document while claiming another - and a purchase bill naming a
        job-work order would be matched against the wrong documents entirely.
        Every goods line of a job-work bill must name a receipt line; a free
        service or freight line is refused unless the named constant permits it
        (docs/assumptions.md §B3-1).
        """
        is_job_work = self.invoice_type is VendorInvoiceType.JOB_WORK
        if is_job_work and self.job_work_order_id is None:
            raise ValueError("A job-work invoice must name the job-work order it bills.")
        if not is_job_work and self.job_work_order_id is not None:
            raise ValueError("Only a job-work invoice may name a job-work order.")
        if is_job_work and (
            self.purchase_order_id is not None or self.goods_receipt_id is not None
        ):
            raise ValueError(
                "A job-work invoice is matched against its job-work order, not against a "
                "purchase order or a goods receipt."
            )
        if is_job_work and not JOB_WORK_ALLOW_FREE_LINES:
            for number, line in enumerate(self.lines, start=1):
                if line.job_work_receipt_line_id is None:
                    raise ValueError(
                        f"Line {number} of a job-work invoice must name the job-work receipt "
                        "line it bills."
                    )
        if not is_job_work:
            for number, line in enumerate(self.lines, start=1):
                if line.job_work_receipt_line_id is not None:
                    raise ValueError(
                        f"Line {number} bills a job-work receipt line on a purchase invoice."
                    )
        return self


class VendorInvoiceUpdate(BaseModel):
    """Amend an invoice that has not been approved.

    Replacing ``lines`` replaces the whole set: a bill is a document, and
    patching individual lines of a document somebody is about to approve invites
    an approval of something other than what was reviewed.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_invoice_number: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    purchase_order_id: uuid.UUID | None = None
    goods_receipt_id: uuid.UUID | None = None
    #: Repoint a job-work bill at another order of the same vendor. The type
    #: itself is frozen at creation: turning a purchase bill into a job-work
    #: bill would reprice every line against different documents, which is a
    #: new document, not an amendment.
    job_work_order_id: uuid.UUID | None = None
    #: An explicit null clears the header cost centre.
    cost_centre_id: uuid.UUID | None = None
    round_off_amount: Money | None = None
    notes: Notes | None = None
    lines: Annotated[list[VendorInvoiceLineIn], Field(max_length=MAX_INVOICE_LINES)] | None = None


class VendorInvoiceLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID | None
    purchase_order_line_id: uuid.UUID | None
    goods_receipt_line_id: uuid.UUID | None
    #: The job-work receipt line billed, on a JOB_WORK invoice.
    job_work_receipt_line_id: uuid.UUID | None
    #: The line's own cost centre; null means it takes the invoice's.
    cost_centre_id: uuid.UUID | None
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
    #: This line's share of the excess-loss recovery posted with the invoice.
    #: Zero on purchase bills and on lines that recovered nothing.
    recovery_amount: Decimal
    notes: str | None


class VendorInvoiceSummary(BaseModel):
    """List row. Carries the derived outstanding so the payables screen does not
    subtract two columns in the browser and disagree with the ageing report."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    vendor_invoice_number: str
    invoice_date: date
    due_date: date
    status: VendorInvoiceStatus
    match_status: MatchStatus
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    paid_amount: Decimal
    #: What is still owed: ``total_amount - recovered_amount - paid_amount``.
    #: Net of the excess-loss recovery posted beside a job-work invoice, which
    #: already reduced the payable in the ledger. docs/assumptions.md §B3-2.
    outstanding_amount: Decimal
    purchase_order_id: uuid.UUID | None
    goods_receipt_id: uuid.UUID | None
    #: What the bill charges for; a JOB_WORK row is matched against its order.
    invoice_type: VendorInvoiceType
    #: The job-work order billed, on a JOB_WORK invoice.
    job_work_order_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    created_at: datetime


class VendorInvoiceDetail(VendorInvoiceSummary):
    round_off_amount: Decimal
    match_variance_amount: Decimal
    matched_at: datetime | None
    variance_override_reason: str | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    dispute_reason: str | None
    cancellation_reason: str | None
    #: The excess-loss recovery posted beside this invoice (Dr AP / Cr
    #: job-work expense). The vendor's net payable is
    #: ``total_amount - recovered_amount``. docs/assumptions.md §B3-2.
    recovered_amount: Decimal
    notes: str | None
    version: int
    lines: list[VendorInvoiceLineRead] = Field(default_factory=list)


class VendorInvoiceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: Annotated[str, Field(max_length=120)] | None = None
    status: VendorInvoiceStatus | None = None
    match_status: MatchStatus | None = None
    #: Only bills of this type - the job-work "Invoices" tab lists JOB_WORK.
    invoice_type: VendorInvoiceType | None = None
    vendor_id: uuid.UUID | None = None
    purchase_order_id: uuid.UUID | None = None
    job_work_order_id: uuid.UUID | None = None
    invoice_from: date | None = None
    invoice_to: date | None = None
    due_before: date | None = None
    #: Only invoices that still owe money. The payables working set.
    open_only: bool = False
    overdue_only: bool = False


# -----------------------------------------------------------------------------
# Three-way match
# -----------------------------------------------------------------------------


class MatchLineVariance(BaseModel):
    """What one invoice line agreed and disagreed with.

    Both variances are reported as a signed money amount *and* as a proportion,
    because the tolerance is proportional but the conversation with the vendor
    is about rupees.
    """

    invoice_line_id: uuid.UUID | None = None
    line_number: int
    item_id: uuid.UUID | None = None
    item_code: str | None = None
    description: str | None = None

    #: NULL when the line names no purchase-order line - a freight or service
    #: charge with nothing to match against.
    purchase_order_line_id: uuid.UUID | None = None

    #: The job-work receipt line billed, on a JOB_WORK invoice. Its price is
    #: matched against the order's rate and its quantity against the good
    #: output the receipt line delivered.
    job_work_receipt_line_id: uuid.UUID | None = None

    invoiced_quantity: Decimal
    #: What was accepted at the gate: the goods receipt's accepted quantity when
    #: the invoice names one, otherwise the order line's cumulative accepted
    #: total. See ``service.match_invoice``.
    accepted_quantity: Decimal | None = None
    ordered_quantity: Decimal | None = None
    quantity_variance: Decimal | None = None
    quantity_variance_ratio: Decimal | None = None

    invoiced_unit_price: Decimal
    ordered_unit_price: Decimal | None = None
    price_variance: Decimal | None = None
    price_variance_ratio: Decimal | None = None

    invoiced_amount: Decimal
    #: Accepted quantity at the ordered price - what the invoice should say.
    expected_amount: Decimal | None = None
    amount_variance: Decimal | None = None

    within_tolerance: bool
    #: Plain-language reasons this line is outside tolerance. Empty when it is
    #: inside, so a client can render the line without interpreting numbers.
    findings: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    """The outcome of matching one vendor invoice against its order and receipt.

    ``matched`` is the single question approval asks. Everything else exists so
    a human can see *why* the answer is no.
    """

    invoice_id: uuid.UUID
    invoice_code: str
    vendor_id: uuid.UUID
    purchase_order_id: uuid.UUID | None
    goods_receipt_id: uuid.UUID | None
    #: The job-work order matched against, on a JOB_WORK invoice.
    job_work_order_id: uuid.UUID | None = None

    matched: bool
    match_status: MatchStatus
    price_tolerance: Decimal
    quantity_tolerance: Decimal

    total_invoiced: Decimal
    total_expected: Decimal
    total_variance: Decimal
    #: Largest single-line absolute money variance - what the payables list
    #: sorts on.
    worst_variance: Decimal

    lines: list[MatchLineVariance] = Field(default_factory=list)
    #: Why approval is blocked. Empty exactly when ``matched`` is true.
    blocking_reasons: list[str] = Field(default_factory=list)
    #: True when a job-work line bills beyond its receipt line's unbilled
    #: charges. A hard block: no override reason can approve it, because
    #: approving it would bill the same charges twice.
    has_overbilling: bool = False


class InvoiceApproval(BaseModel):
    """Approve an invoice, optionally overriding a match variance.

    ``override_reason`` is what makes an out-of-tolerance approval auditable.
    Approval requires ``vendor_invoice.approve``, so supplying it is itself the
    exercise of that authority (master rules section 42).
    """

    model_config = ConfigDict(extra="forbid")

    override_reason: Reason | None = None


class DisputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


# -----------------------------------------------------------------------------
# Payments
# -----------------------------------------------------------------------------


class PaymentAllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_invoice_id: uuid.UUID | None = None
    sales_invoice_id: uuid.UUID | None = None
    amount: PositiveMoney
    notes: Notes | None = None

    @model_validator(mode="after")
    def _one_invoice(self) -> PaymentAllocationIn:
        if (self.vendor_invoice_id is None) == (self.sales_invoice_id is None):
            raise ValueError("Exactly one sales or vendor invoice must be named.")
        return self


class PaymentCreate(BaseModel):
    """Record a payment or receipt in DRAFT. Nothing posts until approval.

    Allocations are deliberately absent: money can only settle an invoice once
    it has actually moved, so a draft has nothing to allocate. Settlement is
    ``POST /payments/{id}/allocate`` on an approved payment.
    """

    model_config = ConfigDict(extra="forbid")

    payment_type: PaymentType
    party_type: PartyType
    party_id: uuid.UUID
    payment_date: date
    amount: PositiveMoney
    payment_method: PaymentMethod
    #: NULL means the company's own currency - see ``VendorInvoiceCreate``.
    #: Allocation refuses to settle an invoice with a payment in a different
    #: denomination, so the two defaults have to come from the same place.
    currency: Currency | None = None
    reference_number: Annotated[str, Field(max_length=64)] | None = None
    bank_name: Annotated[str, Field(max_length=120)] | None = None
    instrument_date: date | None = None
    notes: Notes | None = None

    _method = field_validator("payment_method")(_money_movement_method)

    @model_validator(mode="after")
    def _party_matches_direction(self) -> PaymentCreate:
        """A payment goes to a vendor; a receipt comes from a customer.

        The inverse combinations describe a refund, which is a different
        document with different accounting - refusing them here is the
        conservative reading rather than posting a refund as an ordinary
        payment.
        """
        if self.party_type is PartyType.NONE:
            raise ValueError("A payment must name a customer or a vendor.")
        expected = (
            PartyType.VENDOR if self.payment_type is PaymentType.PAYMENT else (PartyType.CUSTOMER)
        )
        if self.party_type is not expected:
            raise ValueError(
                f"A {self.payment_type.value} is recorded against a "
                f"{expected.value.lower()}, not a {self.party_type.value.lower()}."
            )
        return self


class PaymentUpdate(BaseModel):
    """Amend a payment that has not been approved."""

    model_config = ConfigDict(extra="forbid")

    payment_date: date | None = None
    amount: PositiveMoney | None = None
    payment_method: PaymentMethod | None = None
    reference_number: Annotated[str, Field(max_length=64)] | None = None
    bank_name: Annotated[str, Field(max_length=120)] | None = None
    instrument_date: date | None = None
    notes: Notes | None = None

    _method = field_validator("payment_method")(_money_movement_method)


class AllocationRequest(BaseModel):
    """Settle invoices from an approved payment.

    ``replace`` decides whether the given allocations are added to what the
    payment already carries or replace it entirely. Defaulting to False makes a
    retried request additive rather than destructive only if the caller says so.
    """

    model_config = ConfigDict(extra="forbid")

    allocations: Annotated[
        list[PaymentAllocationIn], Field(min_length=0, max_length=MAX_PAYMENT_ALLOCATIONS)
    ]
    replace: bool = False

    @model_validator(mode="after")
    def _no_duplicate_invoices(self) -> AllocationRequest:
        if not self.allocations and not self.replace:
            raise ValueError("An empty allocation set requires replace=true.")
        seen = {
            (allocation.vendor_invoice_id, allocation.sales_invoice_id)
            for allocation in self.allocations
        }
        if len(seen) != len(self.allocations):
            raise ValueError("The same invoice is allocated twice in one request.")
        return self


class PaymentAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_invoice_id: uuid.UUID | None
    sales_invoice_id: uuid.UUID | None
    invoice_code: str | None = None
    vendor_invoice_number: str | None = None
    amount: Decimal
    notes: str | None


class PaymentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    payment_type: PaymentType
    party_type: PartyType
    party_id: uuid.UUID
    payment_date: date
    amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    currency: str
    payment_method: PaymentMethod
    reference_number: str | None
    status: PaymentStatus
    created_at: datetime


class PaymentDetail(PaymentSummary):
    bank_name: str | None
    instrument_date: date | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    cancellation_reason: str | None
    notes: str | None
    version: int
    allocations: list[PaymentAllocationRead] = Field(default_factory=list)


class PaymentFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: Annotated[str, Field(max_length=120)] | None = None
    status: PaymentStatus | None = None
    payment_type: PaymentType | None = None
    party_type: PartyType | None = None
    party_id: uuid.UUID | None = None
    payment_method: PaymentMethod | None = None
    date_from: date | None = None
    date_to: date | None = None
    #: Approved money that has not yet been told which invoices it settled.
    unallocated_only: bool = False


# -----------------------------------------------------------------------------
# Customer adjustments
# -----------------------------------------------------------------------------

#: Bounded to what the customers module accepts on its own ``OutstandingAdjustment``
#: payload (3-255 characters): the reason is forwarded verbatim, so a reason
#: that passes here must not fail validation there and become a 500.
AdjustmentReason = Annotated[str, Field(min_length=3, max_length=255)]


class CustomerAdjustmentCreate(BaseModel):
    """A debit or credit note against a customer's account, posted by hand.

    Nothing here names the receivable account: the receivable leg is always the
    system control account, so a caller cannot post a "receivable" adjustment to
    an account the ageing report does not read. ``account_id`` is the other leg -
    the revenue, expense or write-off account the adjustment belongs to.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    adjustment_type: CustomerAdjustmentType
    amount: PositiveMoney
    account_id: uuid.UUID
    entry_date: date | None = Field(default=None, description="Defaults to today")
    reason: AdjustmentReason


class CustomerAdjustmentRead(BaseModel):
    """What one adjustment did: the journal it posted and the balance it left."""

    journal_id: uuid.UUID
    reference_code: str
    customer_id: uuid.UUID
    customer_code: str
    adjustment_type: CustomerAdjustmentType
    amount: Decimal
    #: The signed change applied to the customer's cached outstanding balance.
    delta: Decimal
    outstanding_amount: Decimal
    entry_date: date
    currency: str
    account_id: uuid.UUID
    reason: str
    entry_ids: list[uuid.UUID] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Outstanding and ageing
# -----------------------------------------------------------------------------


class OutstandingRow(BaseModel):
    """One open vendor invoice, aged against the report date."""

    model_config = ConfigDict(from_attributes=True)

    invoice_id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    vendor_code: str
    vendor_name: str
    vendor_invoice_number: str
    invoice_date: date
    due_date: date
    status: VendorInvoiceStatus
    #: The denomination of the three amounts below. The list is per invoice, so
    #: nothing here is summed across currencies - but an amount without its
    #: currency cannot be read, let alone compared.
    currency: str
    total_amount: Decimal
    paid_amount: Decimal
    outstanding_amount: Decimal
    #: Negative until the due date passes, so "not due yet" is visible rather
    #: than clamped to zero.
    days_overdue: int
    ageing_bucket: str


class AgeingRow(BaseModel):
    """One party's outstanding split across the ageing buckets.

    Every figure is in :attr:`currency`, which is the currency of the block the
    row belongs to: a row is a set of sums, and this schema has no exchange rate
    with which to add one denomination to another. The same party appears once
    per denomination it owes in, never once with the two added together.
    """

    party_type: PartyType
    party_id: uuid.UUID
    party_code: str
    party_name: str
    currency: str
    bucket_0_30: Decimal
    bucket_31_60: Decimal
    bucket_61_90: Decimal
    bucket_90_plus: Decimal
    not_due: Decimal
    total_outstanding: Decimal
    oldest_due_date: date | None
    open_document_count: int


class AgeingCurrencyBlock(BaseModel):
    """One denomination's ageing: its parties, and what they add up to.

    A block is the unit in which this report is arithmetically meaningful. Every
    figure in it is in :attr:`currency`, and nothing in it was added to a figure
    from another block - there is no exchange rate in this schema with which
    that could have been done honestly.
    """

    currency: str
    #: Parties with an open item in this denomination across the whole
    #: statement. :attr:`rows` is capped, so ``party_count > len(rows)`` means
    #: the block is showing the worst offenders rather than all of them - and
    #: the totals below are still the whole block's.
    party_count: int
    #: Open items behind those parties, on the same whole-statement basis. This
    #: is what orders the blocks and what chooses the primary one when the
    #: company's own currency has nothing open: a count means the same thing in
    #: every denomination, and an amount does not.
    document_count: int = 0
    rows: list[AgeingRow] = Field(default_factory=list)
    total_bucket_0_30: Decimal
    total_bucket_31_60: Decimal
    total_bucket_61_90: Decimal
    total_bucket_90_plus: Decimal
    total_not_due: Decimal
    total_outstanding: Decimal


class AgeingReport(BaseModel):
    """Ageing per denomination, with one of them restated at the top level.

    Multi-currency is an open question for this business (``docs/assumptions.md``,
    open question 3) and there is no exchange-rate table to convert with. Two
    failures follow from that, and this shape is built to avoid both:

    * A total that adds rupees to dollars means nothing, and ranking parties by
      it is worse than meaningless. So every total lives inside an
      :class:`AgeingCurrencyBlock` and no figure anywhere spans two.
    * A report narrowed to one denomination tells a company whose debts are all
      in another that it owes nothing. So :attr:`currencies` carries *every*
      denomination with an open item - narrowing is a choice about what to
      restate at the top, never about what the report covers.

    :attr:`currency` and the ``rows``/``total_*`` fields beside it are the
    primary block - ``currencies[0]`` - repeated at the top level so the
    single-currency company this system is built for today reads the answer
    without walking a list. They are a restatement, not a second figure.
    """

    party_type: PartyType
    as_of: date
    #: The denomination the top-level figures are in, and always
    #: ``currencies[0].currency``. The requested currency if one was asked for;
    #: otherwise the company's own if anything is open in it, and failing that
    #: the denomination with the most open documents. Never chosen by comparing
    #: two denominations' amounts - without an exchange rate that comparison
    #: means nothing. Never a mixture, and never an empty answer while some
    #: denomination has an open item.
    currency: str
    #: Every denomination with an open item, most open documents first, except
    #: that the primary block is moved to the front. Always non-empty: with
    #: nothing outstanding it holds a single zeroed block for :attr:`currency`.
    currencies: list[AgeingCurrencyBlock] = Field(default_factory=list)
    #: The denominations in :attr:`currencies` other than the primary one, in
    #: the same order. Empty for a single-currency company - which is what makes
    #: a bare reading of the top-level totals safe there.
    other_currencies: list[str] = Field(default_factory=list)
    bucket_labels: list[str]
    #: ``currencies[0].rows``.
    rows: list[AgeingRow] = Field(default_factory=list)
    total_bucket_0_30: Decimal
    total_bucket_31_60: Decimal
    total_bucket_61_90: Decimal
    total_bucket_90_plus: Decimal
    total_not_due: Decimal
    total_outstanding: Decimal


# -----------------------------------------------------------------------------
# Inventory valuation
# -----------------------------------------------------------------------------


class InventoryValuationRow(BaseModel):
    """On-hand stock at one item and warehouse, valued FIFO.

    ``value`` is ``sum(stock_value)`` across the position's balance rows: each
    position's open FIFO cost layers, added up and rounded once at the four
    places that column stores. ``recorded_value`` is the same total recomputed
    straight from the layer rows, so a balance that has drifted from the layers
    behind it shows as a difference instead of being reported as fact.
    """

    item_id: uuid.UUID
    item_code: str
    item_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    warehouse_name: str
    on_hand_quantity: Decimal
    #: Value-weighted, so a position split across batches reports the cost per
    #: unit an accountant would compute rather than the average of the averages.
    average_cost: Decimal
    #: The FIFO value as the inventory engine recorded it: the sum of each
    #: position's ``stock_value``, which is that position's open cost layers
    #: rounded once at four places. Close to the layers, not identical to them.
    value: Decimal
    #: The reconciliation: the same total recomputed from ``inventory_cost_layers``
    #: itself, ``sum(remaining_quantity x unit_cost)``, with nothing the balance
    #: row says about itself in the arithmetic. It agrees with ``value`` to
    #: within the four-place rounding ``stock_value`` carries per position; a
    #: material difference means a balance and its layers have come apart, and
    #: is a reconciliation item.
    recorded_value: Decimal


class InventoryValuationSummary(BaseModel):
    as_of: datetime
    row_count: int
    total_quantity: Decimal
    total_value: Decimal


class InventoryValuationPage(BaseModel):
    """A page of valuation rows with the totals for the whole filtered set.

    The totals sit beside the rows rather than in a second endpoint because a
    valuation is meaningless without them - a page showing ten positions out of
    four thousand needs to say what the four thousand are worth. The page itself
    is still server-bounded and carries its own pagination block.
    """

    summary: InventoryValuationSummary
    pagination: OffsetPagination
    rows: list[InventoryValuationRow] = Field(default_factory=list)


class ValuationFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    #: Positions worth less than this are noise on a valuation report; the
    #: default keeps zero-quantity positions out.
    min_value: NonNegativeMoney | None = None


# -----------------------------------------------------------------------------
# GST summary
# -----------------------------------------------------------------------------


class GstRateRow(BaseModel):
    """One HSN and rate slab within the period."""

    hsn_code: str | None
    tax_rate: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax: Decimal
    document_count: int


class GstSection(BaseModel):
    """One side of the return: output tax, or input tax."""

    rows: list[GstRateRow] = Field(default_factory=list)
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax: Decimal


class GstSummary(BaseModel):
    """Output tax from sales, input tax from purchases, and the net position."""

    date_from: date
    date_to: date
    output: GstSection
    dispatched_not_invoiced: GstSection
    input: GstSection
    #: Output minus input. Positive means tax is payable; negative is credit
    #: carried forward.
    net_cgst: Decimal
    net_sgst: Decimal
    net_igst: Decimal
    net_payable: Decimal


class PeriodFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date

    @field_validator("date_to")
    @classmethod
    def _range_is_ordered(cls, value: date, info: ValidationInfo) -> date:
        # Reported against ``date_to`` for the same reason as ``StatementFilters``.
        # ``date_from`` is absent from ``info.data`` when it failed its own
        # validation, in which case that error is the one to report.
        lower = info.data.get("date_from")
        if lower is not None and value < lower:
            raise ValueError(date_before("period end", "period start"))
        return value


__all__ = [
    "MAX_STATEMENT_ROWS",
    "MAX_VALUATION_ROWS",
    "AgeingCurrencyBlock",
    "AgeingReport",
    "AgeingRow",
    "AllocationRequest",
    "CustomerAdjustmentCreate",
    "CustomerAdjustmentRead",
    "DisputeRequest",
    "GstRateRow",
    "GstSection",
    "GstSummary",
    "InventoryValuationPage",
    "InventoryValuationRow",
    "InventoryValuationSummary",
    "InvoiceApproval",
    "LedgerAccountCreate",
    "LedgerAccountFilters",
    "LedgerAccountRead",
    "LedgerAccountUpdate",
    "LedgerStatement",
    "LedgerStatementRow",
    "MatchLineVariance",
    "MatchResult",
    "OutstandingRow",
    "PaymentAllocationIn",
    "PaymentAllocationRead",
    "PaymentCreate",
    "PaymentDetail",
    "PaymentFilters",
    "PaymentSummary",
    "PaymentUpdate",
    "PeriodFilters",
    "ReasonRequest",
    "StatementFilters",
    "ValuationFilters",
    "VendorInvoiceCreate",
    "VendorInvoiceDetail",
    "VendorInvoiceFilters",
    "VendorInvoiceLineIn",
    "VendorInvoiceLineRead",
    "VendorInvoiceSummary",
    "VendorInvoiceUpdate",
]
