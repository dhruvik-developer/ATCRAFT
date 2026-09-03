"""Finance: the general ledger, vendor invoices, payments and their allocations.

Master rules section 24. Six modelling decisions carry the weight of this file.

**A ledger entry is one leg of a balanced set, and the set has an identity.**
``journal_id`` is on every row and is what makes a posting retrievable,
reversible and provable as a unit. Without it a "balanced posting" is a claim the
service makes at write time and nothing can re-check afterwards; with it,
``SELECT sum(debit_amount) - sum(credit_amount) ... GROUP BY journal_id`` is the
audit query, and it must return zero for every journal ever written.

**Postings are never edited, only reversed.** ``ledger_entries`` inherits
:class:`~src.core.models.ImmutableLedgerEntity` - no ``updated_at``, no
``deleted_at``, no version column - and a correction is a new journal carrying
``is_reversal`` and pointing at what it undoes. The database enforces the pair:
a row claiming to be a reversal must name the entry it reverses, and a row that
names one must admit to being a reversal.

**Exactly one of debit/credit is non-zero, as a CHECK.** A leg that is both, or
neither, is not a bookkeeping entry. Enforcing this in the service alone would
put the integrity of the ledger one forgotten code path away from a row nothing
can interpret.

**A vendor cannot bill the same number twice.** ``(company_id, vendor_id,
vendor_invoice_number)`` is unique. Duplicate-invoice fraud and honest duplicate
data entry produce exactly the same row, and both are caught here rather than by
whoever reconciles the vendor account three months later.

**Money invariants are constraints, not conventions.** ``total_amount =
subtotal + tax_amount + round_off_amount`` on the invoice header,
``tax_amount = cgst + sgst + igst`` and ``line_total = taxable_amount +
tax_amount`` on the line, ``paid_amount <= total_amount`` on the header and
``allocated_amount <= amount`` on the payment. Each of these is a sum somebody
will later reconcile against a bank statement or a GST return, and a row that
fails one of them is not a rounding problem - it is an invoice nobody can
defend.

**IGST and CGST/SGST are mutually exclusive.** One supply is either intra-state
or inter-state; a line carrying both splits is a place-of-supply bug that would
otherwise reach a statutory return.

The parties are deliberately polymorphic: ``party_id`` on a ledger entry and on
a payment is a bare UUID with no foreign key, because it addresses either a
``customers`` row or a ``vendors`` row, and those tables belong to two other
modules. This is the same decision, for the same reason, as the inventory
ledger's ``reference_id`` (database.md section 4.2).
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
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import (
    ImmutableLedgerEntity,
    OptimisticLockMixin,
    TenantEntity,
    TenantMasterEntity,
)
from src.core.types import GUID, Money, Percentage, Quantity, UnitPrice

# -----------------------------------------------------------------------------
# Module constants
# -----------------------------------------------------------------------------

#: ISO-4217 alphabetic currency codes.
CURRENCY_CODE_LENGTH = 3

#: Everything is INR today (docs/assumptions.md, open question 3).
DEFAULT_CURRENCY = "INR"

# ASSUMPTION: a vendor invoice may differ from the purchase-order price by up to
# 2%, and from the accepted goods-receipt quantity by 0%. Price rounding and
# freight allocation produce small, legitimate price variances; a quantity
# variance is not a rounding artefact - it means the invoice disagrees with what
# was physically accepted. Recorded in docs/assumptions.md section 5. Beyond the
# tolerance approval is blocked unless a holder of `vendor_invoice.approve`
# overrides with a recorded reason.
PRICE_VARIANCE_TOLERANCE = Decimal("0.02")
QUANTITY_VARIANCE_TOLERANCE = Decimal("0")

#: Upper bound on the legs of one journal. A posting with more legs than this is
#: a bulk import that belongs in a batch job, not a request - and an unbounded
#: list would let one call hold a transaction open indefinitely (section 31).
MAX_JOURNAL_LINES = 200

#: Upper bound on the lines of one vendor invoice, for the same reason.
MAX_INVOICE_LINES = 200

#: Upper bound on the invoices one payment may settle in a single call.
MAX_PAYMENT_ALLOCATIONS = 100

#: Ageing bucket upper bounds in days overdue. The fourth bucket is everything
#: beyond the last boundary. Repository, schema and report all derive their
#: buckets from this tuple so the numbers on the screen, in the API and in the
#: SQL cannot drift apart.
AGEING_BUCKET_BOUNDARIES: tuple[int, int, int] = (30, 60, 90)

AGEING_BUCKET_LABELS: tuple[str, str, str, str] = ("0-30", "31-60", "61-90", "90+")


def _sql_in_list(values: tuple[StrEnum, ...] | type[StrEnum]) -> str:
    """Render a status vocabulary as a SQL ``IN`` list.

    Generated from the Python enum so a database CHECK cannot drift away from
    the values the schemas validate against.
    """
    members = values if isinstance(values, tuple) else tuple(values)
    return ", ".join(f"'{member.value}'" for member in members)


# -----------------------------------------------------------------------------
# Vocabularies
# -----------------------------------------------------------------------------


class LedgerAccountType(StrEnum):
    """The five account classes, which fix the sign of a normal balance."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"


#: Account classes whose normal balance is a debit. Used to present a balance in
#: the direction an accountant expects rather than as a signed number.
DEBIT_NORMAL_TYPES: frozenset[LedgerAccountType] = frozenset(
    {LedgerAccountType.ASSET, LedgerAccountType.EXPENSE}
)


class PartyType(StrEnum):
    """Who a ledger entry or a payment is against."""

    CUSTOMER = "CUSTOMER"
    VENDOR = "VENDOR"
    #: A posting with no counterparty: bank, tax, expense and control legs.
    NONE = "NONE"


class SystemAccount(StrEnum):
    """Control accounts the automatic postings need by name.

    Automatic postings cannot ask a human which account to use, so the accounts
    this module posts to are identified by a stable ``system_code`` rather than
    by a chart-of-accounts code somebody may rename. See
    ``service.DEFAULT_CHART`` for the account each one is provisioned as.
    """

    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE"
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE"
    PURCHASE_EXPENSE = "PURCHASE_EXPENSE"
    INPUT_TAX = "INPUT_TAX"
    OUTPUT_TAX = "OUTPUT_TAX"
    BANK = "BANK"
    CASH = "CASH"
    ROUND_OFF = "ROUND_OFF"


class VendorInvoiceStatus(StrEnum):
    """Lifecycle of a purchase invoice.

    ``DISPUTED`` is a live state, not a terminal one: a disputed invoice is
    still owed until it is either resolved back to APPROVED or cancelled.
    """

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    DISPUTED = "DISPUTED"
    CANCELLED = "CANCELLED"


#: Invoices that still owe the vendor money. The predicate of the partial index
#: behind the outstanding and ageing reports - change both together.
OPEN_INVOICE_STATUSES: tuple[VendorInvoiceStatus, ...] = (
    VendorInvoiceStatus.PENDING_APPROVAL,
    VendorInvoiceStatus.APPROVED,
    VendorInvoiceStatus.PARTIALLY_PAID,
    VendorInvoiceStatus.DISPUTED,
)

#: Invoices a payment may be allocated against. A payment cannot settle an
#: invoice nobody has approved, and settling a disputed one would concede the
#: dispute silently.
PAYABLE_INVOICE_STATUSES: tuple[VendorInvoiceStatus, ...] = (
    VendorInvoiceStatus.APPROVED,
    VendorInvoiceStatus.PARTIALLY_PAID,
)

#: Invoices whose input tax has been accepted into the books.
INPUT_TAX_STATUSES: tuple[VendorInvoiceStatus, ...] = (
    VendorInvoiceStatus.APPROVED,
    VendorInvoiceStatus.PARTIALLY_PAID,
    VendorInvoiceStatus.PAID,
)

#: Statuses in which an invoice's own fields may still be edited.
EDITABLE_INVOICE_STATUSES: tuple[VendorInvoiceStatus, ...] = (
    VendorInvoiceStatus.DRAFT,
    VendorInvoiceStatus.PENDING_APPROVAL,
)


class MatchStatus(StrEnum):
    """Outcome of the last three-way match run against an invoice."""

    #: Never matched, or matched before the invoice was last edited.
    NOT_MATCHED = "NOT_MATCHED"
    MATCHED = "MATCHED"
    #: Matched, and at least one line is outside tolerance.
    VARIANCE = "VARIANCE"
    #: Outside tolerance, and approved anyway with a recorded reason.
    OVERRIDDEN = "OVERRIDDEN"


class PaymentType(StrEnum):
    """Which way the money moves."""

    #: Money leaving the company - settles a vendor invoice.
    PAYMENT = "PAYMENT"
    #: Money arriving - a customer receipt.
    RECEIPT = "RECEIPT"


class PaymentMethod(StrEnum):
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    UPI = "UPI"
    CARD = "CARD"
    #: A book entry with no cash movement: contra, write-off or set-off.
    ADJUSTMENT = "ADJUSTMENT"


#: Methods that move money through a bank account rather than the cash box.
BANK_METHODS: frozenset[PaymentMethod] = frozenset(
    {PaymentMethod.BANK_TRANSFER, PaymentMethod.CHEQUE, PaymentMethod.UPI, PaymentMethod.CARD}
)


class PaymentStatus(StrEnum):
    """Lifecycle of a payment or receipt.

    Money moves at APPROVED - that is where the journal is posted and where the
    party's outstanding balance changes. Allocation afterwards decides *which
    invoices* it settled, and cannot change the amount that left the bank.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    ALLOCATED = "ALLOCATED"
    CANCELLED = "CANCELLED"


#: Payments that have been posted and may still be allocated.
ALLOCATABLE_PAYMENT_STATUSES: tuple[PaymentStatus, ...] = (
    PaymentStatus.APPROVED,
    PaymentStatus.PARTIALLY_ALLOCATED,
    PaymentStatus.ALLOCATED,
)

#: Payments that have hit the ledger and the party balance.
POSTED_PAYMENT_STATUSES: tuple[PaymentStatus, ...] = (
    PaymentStatus.APPROVED,
    PaymentStatus.PARTIALLY_ALLOCATED,
    PaymentStatus.ALLOCATED,
)


# -----------------------------------------------------------------------------
# Chart of accounts
# -----------------------------------------------------------------------------


class LedgerAccount(TenantMasterEntity):
    """One account in the company's chart of accounts.

    Master data, so it carries ``deleted_at`` - but retiring an account that has
    postings is refused by the service, because a ledger whose accounts can
    disappear is not a ledger. ``is_system`` marks the rows the automatic
    postings depend on; those may be renamed but never retired or re-typed.
    """

    __tablename__ = "ledger_accounts"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    account_type: Mapped[LedgerAccountType] = mapped_column(String(16), nullable=False)

    #: Groups accounts for reporting (Current Assets -> Receivables -> Trade).
    #: RESTRICT: deleting a parent would orphan a subtree of a live chart.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Provisioned and depended upon by the automatic postings - see
    #: :class:`SystemAccount`.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The stable identifier an automatic posting resolves. NULL on the accounts
    #: a bookkeeper creates; unique per company where present.
    system_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_ledger_accounts_company_id_code"),
        # Partial: only the system accounts carry a system_code, and two rows
        # claiming to be "the" accounts-payable account would make every
        # automatic posting non-deterministic.
        Index(
            "uq_ledger_accounts_system_code",
            "company_id",
            "system_code",
            unique=True,
            postgresql_where=text("system_code IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index("ix_ledger_accounts_type", "company_id", "account_type", "is_active"),
        CheckConstraint(
            f"account_type IN ({_sql_in_list(LedgerAccountType)})", name="account_type_known"
        ),
        # An account cannot be its own parent. Deeper cycles are prevented by the
        # service, which walks the chain - a CHECK cannot see beyond one row.
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_is_not_self"),
        CheckConstraint(
            "NOT is_system OR system_code IS NOT NULL", name="system_accounts_carry_a_system_code"
        ),
    )


# -----------------------------------------------------------------------------
# The general ledger
# -----------------------------------------------------------------------------


class LedgerEntry(ImmutableLedgerEntity):
    """One leg of one balanced posting. Written once, never modified.

    ``entry_date`` is the business date the posting belongs to - which is what
    the financial year, the statement and the ageing report are computed from -
    and it is routinely earlier than ``created_at``, the moment somebody keyed
    it in.
    """

    __tablename__ = "ledger_entries"

    #: Shared by every leg of one posting. The identity of the balanced set:
    #: it is how a journal is fetched, reversed and proved to balance.
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    #: Position within the journal, so a posting always renders in the order it
    #: was written rather than in whatever order the rows come back.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    party_type: Mapped[PartyType] = mapped_column(
        String(16), nullable=False, default=PartyType.NONE, server_default=PartyType.NONE.value
    )
    #: A ``customers`` or a ``vendors`` row. Bare UUID: the target table depends
    #: on ``party_type`` and both belong to other modules - see the module
    #: docstring.
    party_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    entry_date: Mapped[date] = mapped_column(Date, nullable=False)

    debit_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    credit_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )

    #: What caused the posting: ``vendor_invoice``, ``payment``, ``goods_receipt``.
    #: A type and an id rather than a foreign key, for the same reason the
    #: inventory ledger uses one: the ledger outlives its source.
    reference_type: Mapped[str] = mapped_column(String(48), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    reference_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    narration: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: The period this posting belongs to. RESTRICT: a year with postings in it
    #: cannot be deleted out from under them. NULL when the company has not set
    #: its financial years up yet - the posting is still valid.
    financial_year_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("financial_years.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    is_reversal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The entry this one undoes. Self-referential and RESTRICT, because the
    #: reversal is meaningless without the row it points at.
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("ledger_entries.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("journal_id", "line_number", name="uq_ledger_entries_journal_line"),
        # THE statement index: one party's postings in date order, which is
        # exactly the running-balance query's scan.
        Index(
            "ix_ledger_entries_party",
            "company_id",
            "party_type",
            "party_id",
            "entry_date",
            "id",
        ),
        # The account ledger and the trial balance.
        Index("ix_ledger_entries_account_date", "company_id", "account_id", "entry_date"),
        # "Show me this posting" and "reverse this posting".
        Index("ix_ledger_entries_journal", "company_id", "journal_id"),
        # "What did this invoice/payment/receipt post?"
        Index("ix_ledger_entries_reference", "company_id", "reference_type", "reference_id"),
        # Keyset paging over the whole ledger, newest first.
        Index("ix_ledger_entries_company_date", "company_id", "entry_date", "id"),
        # The correctness floor. A leg that is both a debit and a credit, or
        # neither, is not a bookkeeping entry - see the module docstring.
        CheckConstraint("debit_amount >= 0", name="debit_non_negative"),
        CheckConstraint("credit_amount >= 0", name="credit_non_negative"),
        CheckConstraint(
            "(debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0)",
            name="exactly_one_side_is_non_zero",
        ),
        CheckConstraint(f"party_type IN ({_sql_in_list(PartyType)})", name="party_type_known"),
        # A party leg must name its party, and a leg with no party must not
        # claim one - otherwise a statement silently omits or invents rows.
        CheckConstraint(
            "(party_type = 'NONE') = (party_id IS NULL)", name="party_type_matches_party_id"
        ),
        # A reversal names what it reverses, and a row naming one admits to
        # being a reversal. Either half alone makes the pair unauditable.
        CheckConstraint(
            "is_reversal = (reverses_entry_id IS NOT NULL)", name="reversal_evidence_complete"
        ),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
    )


# -----------------------------------------------------------------------------
# Vendor invoices
# -----------------------------------------------------------------------------


class VendorInvoice(TenantEntity, OptimisticLockMixin):
    """A bill from a vendor, matched against the order and the receipt.

    Optimistically locked: accounts payable and the approver work the same
    invoice at the same time, and a silent last-writer-wins on an amount is the
    difference between paying the right figure and the wrong one.
    """

    __tablename__ = "vendor_invoices"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The number the vendor printed on their own document. The natural key
    #: their accounts team quotes, and the one duplicate billing is caught on.
    vendor_invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)

    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Invoice date plus the vendor's agreed credit days, resolved once at
    #: creation. Stored rather than derived: the ageing report must not move if
    #: the vendor's payment terms are renegotiated next year.
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    #: What the invoice is billing for. Both nullable: a vendor legitimately
    #: bills for something with no purchase order behind it (a service call, a
    #: freight debit note), and an invoice may arrive before the receipt is
    #: keyed. Without an order there is nothing to price-match against, and
    #: ``match_invoice`` says so rather than passing the invoice silently.
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    goods_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("goods_receipts.id", ondelete="RESTRICT"), nullable=True, index=True
    )

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
    #: The rupee or two an Indian invoice rounds to a whole number. Kept as its
    #: own figure so the header identity below can be a CHECK rather than an
    #: approximate comparison.
    round_off_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Advanced only by the allocation path, under a row lock on this header.
    paid_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    status: Mapped[VendorInvoiceStatus] = mapped_column(
        String(24),
        nullable=False,
        default=VendorInvoiceStatus.DRAFT,
        server_default=VendorInvoiceStatus.DRAFT.value,
    )

    # --- three-way match evidence -------------------------------------------
    match_status: Mapped[MatchStatus] = mapped_column(
        String(16),
        nullable=False,
        default=MatchStatus.NOT_MATCHED,
        server_default=MatchStatus.NOT_MATCHED.value,
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The worst absolute money variance the last match found. Stored so the
    #: payables list can sort by "how wrong is this?" without re-running the
    #: match for every row on the page.
    match_variance_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Why an invoice outside tolerance was approved anyway. The CHECK below
    #: makes an override without a reason impossible.
    variance_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- lifecycle evidence -------------------------------------------------
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_vendor_invoices_company_id_code"),
        # A vendor cannot bill the same invoice number twice - see the module
        # docstring. This is the module's single most valuable constraint.
        UniqueConstraint(
            "company_id",
            "vendor_id",
            "vendor_invoice_number",
            name="uq_vendor_invoices_vendor_invoice_number",
        ),
        # The payables list and the approval queue.
        Index("ix_vendor_invoices_status_due", "company_id", "status", "due_date"),
        Index("ix_vendor_invoices_vendor_date", "company_id", "vendor_id", "invoice_date"),
        Index("ix_vendor_invoices_purchase_order", "company_id", "purchase_order_id"),
        # THE index behind the outstanding and ageing reports. Partial, because a
        # mature company's table is mostly settled invoices and neither report
        # ever looks at them. Its predicate is OPEN_INVOICE_STATUSES - the two
        # must change together.
        Index(
            "ix_vendor_invoices_open",
            "company_id",
            "vendor_id",
            "due_date",
            postgresql_where=text(f"status IN ({_sql_in_list(OPEN_INVOICE_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({_sql_in_list(VendorInvoiceStatus)})", name="status_known"),
        CheckConstraint(
            f"match_status IN ({_sql_in_list(MatchStatus)})", name="match_status_known"
        ),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        CheckConstraint("paid_amount >= 0", name="paid_non_negative"),
        # The header identity. A total that is not its own parts is an invoice
        # nobody can reconcile against the vendor's document.
        CheckConstraint(
            "total_amount = subtotal + tax_amount + round_off_amount",
            name="total_is_subtotal_plus_tax_plus_round_off",
        ),
        # Paying more than was billed is not an overpayment, it is a mistake in
        # the allocation arithmetic.
        CheckConstraint("paid_amount <= total_amount", name="paid_within_total"),
        CheckConstraint("due_date >= invoice_date", name="due_not_before_invoice"),
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)", name="approval_evidence_complete"
        ),
        CheckConstraint(
            "status <> 'DISPUTED' OR dispute_reason IS NOT NULL",
            name="disputed_invoices_carry_a_reason",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_invoices_carry_a_reason",
        ),
        # An override is a decision somebody has to answer for later.
        CheckConstraint(
            "match_status <> 'OVERRIDDEN' OR variance_override_reason IS NOT NULL",
            name="overrides_carry_a_reason",
        ),
        CheckConstraint(
            "(matched_at IS NULL) = (match_status = 'NOT_MATCHED')",
            name="match_evidence_complete",
        ),
    )


class VendorInvoiceLine(TenantEntity):
    """One billed item or charge on a vendor invoice.

    ``item_id`` is nullable: freight, packing and inspection charges are billed
    on their own lines and are not items in the master. Those lines have no
    purchase-order line to price-match against either, and the matcher reports
    them as unmatched rather than pretending they agreed with something.
    """

    __tablename__ = "vendor_invoice_lines"

    vendor_invoice_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendor_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: What this line is billing against. RESTRICT on both: these links are the
    #: evidence the three-way match is built on.
    purchase_order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    goods_receipt_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("goods_receipt_lines.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Snapshot of what the vendor billed, not a pointer to the item name.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Copied from the item master at entry. Frozen here because the GST return
    #: has to reproduce what was on the document, and an item may be
    #: reclassified afterwards.
    hsn_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )
    unit_price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Value the tax is charged on: quantity x price, less discount.
    taxable_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    tax_rate: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Stored split, not a halved total: the return prints CGST and SGST
    #: separately, and halving a rounded figure does not reliably reproduce the
    #: two halves.
    cgst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    sgst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    igst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "vendor_invoice_id", "line_number", name="uq_vendor_invoice_lines_line_number"
        ),
        Index("ix_vendor_invoice_lines_invoice", "company_id", "vendor_invoice_id", "line_number"),
        Index("ix_vendor_invoice_lines_po_line", "company_id", "purchase_order_line_id"),
        # The GST return's HSN summary reads exactly this.
        Index("ix_vendor_invoice_lines_hsn", "company_id", "hsn_code", "tax_rate"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("taxable_amount >= 0", name="taxable_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_bounded"),
        CheckConstraint("cgst_amount >= 0", name="cgst_non_negative"),
        CheckConstraint("sgst_amount >= 0", name="sgst_non_negative"),
        CheckConstraint("igst_amount >= 0", name="igst_non_negative"),
        CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount", name="tax_is_the_sum_of_splits"
        ),
        CheckConstraint(
            "line_total = taxable_amount + tax_amount", name="line_total_is_taxable_plus_tax"
        ),
        # One supply is either intra-state or inter-state. A line carrying both
        # splits is a place-of-supply bug on its way into a statutory return.
        CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name="igst_excludes_cgst_and_sgst",
        ),
    )


# -----------------------------------------------------------------------------
# Payments
# -----------------------------------------------------------------------------


class Payment(TenantEntity, OptimisticLockMixin):
    """Money paid to a vendor or received from a customer.

    The amount is what left or entered the bank. Allocation decides which
    invoices it settled and can never change it - which is why
    ``allocated_amount`` is bounded by ``amount`` in the database rather than
    only in the allocation service.
    """

    __tablename__ = "payments"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    payment_type: Mapped[PaymentType] = mapped_column(String(16), nullable=False)
    party_type: Mapped[PartyType] = mapped_column(String(16), nullable=False)
    #: A ``customers`` or ``vendors`` row - see the module docstring.
    party_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)

    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Advanced only by the allocation path, under a row lock on this header.
    allocated_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )

    payment_method: Mapped[PaymentMethod] = mapped_column(String(24), nullable=False)
    #: Cheque number, UTR, UPI reference. The string the bank statement is
    #: reconciled on, so it is indexed for the "find this payment" search.
    reference_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Cheque or transfer date as the bank sees it, when it differs from the
    #: date the payment was recorded.
    instrument_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[PaymentStatus] = mapped_column(
        String(24),
        nullable=False,
        default=PaymentStatus.DRAFT,
        server_default=PaymentStatus.DRAFT.value,
    )

    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_payments_company_id_code"),
        Index("ix_payments_party", "company_id", "party_type", "party_id", "payment_date"),
        Index("ix_payments_status_date", "company_id", "status", "payment_date"),
        # Bank reconciliation searches on the instrument reference.
        Index("ix_payments_reference_number", "company_id", "reference_number"),
        CheckConstraint(f"status IN ({_sql_in_list(PaymentStatus)})", name="status_known"),
        CheckConstraint(
            f"payment_type IN ({_sql_in_list(PaymentType)})", name="payment_type_known"
        ),
        CheckConstraint(
            f"payment_method IN ({_sql_in_list(PaymentMethod)})", name="payment_method_known"
        ),
        # A payment is always against somebody: PartyType.NONE describes a
        # control leg in the ledger, never a party the money moved with.
        CheckConstraint("party_type IN ('CUSTOMER', 'VENDOR')", name="party_type_is_a_real_party"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("allocated_amount >= 0", name="allocated_non_negative"),
        # Allocating more than was paid would settle invoices with money that
        # never moved.
        CheckConstraint("allocated_amount <= amount", name="allocated_within_amount"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)", name="approval_evidence_complete"
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_payments_carry_a_reason",
        ),
    )


class PaymentAllocation(TenantEntity):
    """How much of one payment settles one vendor invoice.

    ASSUMPTION: allocations target vendor invoices only, because
    ``vendor_invoices`` is the only settleable document the schema defines
    (database.md section 2 gives finance no customer-invoice table). A customer
    receipt is recorded as a ``RECEIPT`` payment and reduces the customer's
    outstanding balance directly; it carries no allocation rows until a
    sales-invoice document exists to allocate against. Recorded in the returned
    notes.

    One row per (payment, invoice): settling the same invoice twice from one
    payment is an amendment of the existing allocation, not a second row that
    somebody has to add up.
    """

    __tablename__ = "payment_allocations"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: RESTRICT: the invoice records that it was paid, and deleting it would
    #: leave the payment claiming to have settled something that never existed.
    vendor_invoice_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendor_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "payment_id", "vendor_invoice_id", name="uq_payment_allocations_payment_invoice"
        ),
        # "What settled this invoice?" - the invoice's payment history.
        Index("ix_payment_allocations_invoice", "company_id", "vendor_invoice_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


__all__ = [
    "AGEING_BUCKET_BOUNDARIES",
    "AGEING_BUCKET_LABELS",
    "ALLOCATABLE_PAYMENT_STATUSES",
    "BANK_METHODS",
    "CURRENCY_CODE_LENGTH",
    "DEBIT_NORMAL_TYPES",
    "DEFAULT_CURRENCY",
    "EDITABLE_INVOICE_STATUSES",
    "INPUT_TAX_STATUSES",
    "MAX_INVOICE_LINES",
    "MAX_JOURNAL_LINES",
    "MAX_PAYMENT_ALLOCATIONS",
    "OPEN_INVOICE_STATUSES",
    "PAYABLE_INVOICE_STATUSES",
    "POSTED_PAYMENT_STATUSES",
    "PRICE_VARIANCE_TOLERANCE",
    "QUANTITY_VARIANCE_TOLERANCE",
    "LedgerAccount",
    "LedgerAccountType",
    "LedgerEntry",
    "MatchStatus",
    "PartyType",
    "Payment",
    "PaymentAllocation",
    "PaymentMethod",
    "PaymentStatus",
    "PaymentType",
    "SystemAccount",
    "VendorInvoice",
    "VendorInvoiceLine",
    "VendorInvoiceStatus",
]
