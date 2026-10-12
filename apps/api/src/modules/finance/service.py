"""Finance: double-entry postings, the three-way match, payments and reports.

Master rules section 24. Seven decisions govern everything below.

**A posting is a balanced set or it does not happen.** :func:`post_journal` is
the only way a row reaches ``ledger_entries``, and it refuses unless the debits
equal the credits to the money quantum. That check is a pure function,
:func:`assert_balanced`, so it is unit-tested without a database and cannot be
skipped by any caller: there is no other write path.

**The three-way match is the reason this module is worth having.**
:func:`match_invoice` compares what the vendor billed against what was *ordered*
(price) and what was *accepted* (quantity), line by line, and reports the
variance in both money and proportion. Beyond tolerance the invoice cannot be
approved unless somebody holding ``vendor_invoice.approve`` records a reason -
and that reason is stored on the invoice, not only in the audit log, so the
payables list can show which bills were passed over an objection.

**Price is matched against the order, quantity against the receipt.** Those are
the two documents that can contradict a bill, and they contradict it in
different ways: a price the buyer never agreed, or a quantity the store never
took in. Matching quantity against the order instead would pass an invoice for
goods that were rejected at the gate.

**Money moves at approval, and the ledger moves with it.** Approving an invoice
posts the payable; approving a payment posts the settlement; cancelling an
approved payment posts a reversal rather than deleting anything. Every one of
those happens inside one transaction with the status change and the audit row.

**Allocation cannot exceed either side.** A payment may not allocate more than
it is worth, and an invoice may not be allocated more than it still owes. Both
bounds are checked against values read *under a row lock*, because both are
read-modify-write sequences and two allocations racing on one invoice would
otherwise each add their amount to the value they read first.

**Reports are single queries.** The statement's running balance is a window
function; ageing is one aggregate; valuation is one grouped scan; the GST
summary is two aggregates. None of them loops in Python over a result set, and
the full-company valuation - which is genuinely large - goes to Celery instead
of holding an HTTP worker open (sections 22, 25, 35).

**The ageing report is per denomination, and covers all of them.** Invoices,
payments and ledger entries each carry their own ``currency`` and this schema
has no exchange-rate table - multi-currency is an open question, not a settled
requirement (``docs/assumptions.md``, open question 3). Two failures follow, and
:func:`ageing_report` is shaped to avoid both: a total that adds rupees to
dollars means nothing and ranking vendors by it is worse, so every figure lives
in a single-currency block; and a report *narrowed* to one denomination tells a
company whose debts are all in another that it owes nothing, so the report
covers every denomination with an open item and the ``currency`` argument only
chooses which block is restated at the top level. Where a document does not name
a currency, the denomination comes from :func:`company_currency` - the same
column the report defaults to, so a write and a report cannot disagree about
what "unspecified" meant.

This claim is about the ageing report specifically. The party statement's
running balance and the GST summary still sum across whatever denominations
their rows carry; the repository docstring names them as a known gap rather than
implying the module as a whole is safe.

**Every amount here is a Decimal.** There is no ``float`` in this module; a
value that arrives as one is rejected by Pydantic before it reaches the service.
Rounding is ``to_money`` - HALF_UP at two places - applied where a figure
becomes a stored amount, never repeatedly on the way there.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.ids import new_id
from src.core.logging import get_logger
from src.core.messages import date_before, inactive_record
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity, to_unit_price
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.customers.schemas import OutstandingAdjustment
from src.modules.customers.service import apply_outstanding_delta, get_customer_for_order
from src.modules.finance.models import (
    AGEING_BUCKET_BOUNDARIES,
    AGEING_BUCKET_LABELS,
    ALLOCATABLE_PAYMENT_STATUSES,
    COST_CENTRE_ACCOUNT_TYPES,
    CURRENCY_CODE_LENGTH,
    EDITABLE_INVOICE_STATUSES,
    MAX_JOURNAL_LINES,
    MAX_PAYMENT_ALLOCATIONS,
    PAYABLE_INVOICE_STATUSES,
    POSTED_PAYMENT_STATUSES,
    PRICE_VARIANCE_TOLERANCE,
    QUANTITY_VARIANCE_TOLERANCE,
    CustomerAdjustmentType,
    LedgerAccount,
    LedgerAccountType,
    LedgerEntry,
    MatchStatus,
    PartyType,
    Payment,
    PaymentAllocation,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceLine,
    VendorInvoiceStatus,
)
from src.modules.finance.repository import (
    AGEING_TOTAL_COLUMNS,
    AgeingCurrencyTotal,
    CustomerLookupRepository,
    FinancialYearRepository,
    GoodsReceiptLineLookupRepository,
    GoodsReceiptLookupRepository,
    GstRepository,
    InventoryValuationRepository,
    ItemLookupRepository,
    LedgerAccountRepository,
    LedgerEntryRepository,
    PartyFacts,
    PaymentAllocationRepository,
    PaymentRepository,
    PostingAccount,
    PurchaseOrderLineFacts,
    PurchaseOrderLineLookupRepository,
    PurchaseOrderLookupRepository,
    ReportingCurrencyRepository,
    UomLookupRepository,
    VendorInvoiceLineRepository,
    VendorInvoiceRepository,
    VendorLookupRepository,
    ageing_totals_by_currency,
    paginate_projection,
    ranked_ageing_rows_stmt,
)
from src.modules.finance.schemas import (
    AgeingCurrencyBlock,
    AgeingReport,
    AgeingRow,
    AllocationRequest,
    CustomerAdjustmentCreate,
    CustomerAdjustmentRead,
    GstRateRow,
    GstSection,
    GstSummary,
    InventoryValuationRow,
    InventoryValuationSummary,
    InvoiceApproval,
    LedgerAccountCreate,
    LedgerAccountFilters,
    LedgerAccountRead,
    LedgerAccountUpdate,
    LedgerStatement,
    LedgerStatementRow,
    MatchLineVariance,
    MatchResult,
    OutstandingRow,
    PaymentAllocationRead,
    PaymentCreate,
    PaymentDetail,
    PaymentFilters,
    PaymentSummary,
    PaymentUpdate,
    StatementFilters,
    ValuationFilters,
    VendorInvoiceCreate,
    VendorInvoiceDetail,
    VendorInvoiceFilters,
    VendorInvoiceLineIn,
    VendorInvoiceLineRead,
    VendorInvoiceSummary,
    VendorInvoiceUpdate,
)
from src.modules.organization.service import (
    COST_CENTRE_ENTITY,
    cost_centre_facts,
    require_cost_centres,
)
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository

log = get_logger(__name__)

AUDIT_LEDGER_ACCOUNT = "ledger_account"
AUDIT_JOURNAL = "ledger_journal"
AUDIT_VENDOR_INVOICE = "vendor_invoice"
AUDIT_PAYMENT = "payment"

#: What the ledger rows this module posts point back at. Bare strings matching
#: ``ledger_entries.reference_type``: the ledger outlives its source, so it
#: holds a type and an id rather than a foreign key.
REFERENCE_VENDOR_INVOICE = "vendor_invoice"
REFERENCE_PAYMENT = "payment"
#: A debit or credit note raised against a customer by hand - the journal *is*
#: the document, so ``reference_id`` is the journal id.
REFERENCE_CUSTOMER_ADJUSTMENT = "customer_adjustment"
AUDIT_CUSTOMER_ADJUSTMENT = "customer_adjustment"

#: How a customer adjustment is described in narrations and audit summaries.
CUSTOMER_ADJUSTMENT_LABELS: dict[CustomerAdjustmentType, str] = {
    CustomerAdjustmentType.CHARGE: "Debit note",
    CustomerAdjustmentType.CREDIT: "Credit note",
}

#: Celery task and queue for the full-company valuation. Reached by *name* so
#: the API process never imports the worker package.
VALUATION_TASK_NAME = "reports.inventory_valuation"
VALUATION_QUEUE = "reports"

#: Label used on an open item that is not yet due. Kept out of
#: :data:`AGEING_BUCKET_LABELS` because "not due" is not an age.
NOT_DUE_LABEL = "NOT_DUE"

#: Upper bound on the parties one ageing report returns *per denomination*. The
#: totals are aggregated over the whole statement regardless, so this caps the
#: response rather than the arithmetic.
MAX_AGEING_PARTIES = 500

#: ASSUMPTION: an ageing report returns detailed party rows for at most this
#: many denominations - the one it is drawn in, then the busiest by open
#: document count. Not "the largest by outstanding": that ranks a dollar total
#: against a rupee one, which is the comparison this module refuses to make.
#: Every denomination with an open item still reports its totals and its party
#: count, so nothing is hidden; what is bounded is the size of the response. A
#: book with a dozen live currencies is not a case this business has, and
#: ``limit`` x "however many currencies happen to be in the data" is not a bound
#: at all. Raise it if a real company ever needs more.
MAX_AGEING_ROW_CURRENCIES = 5

#: Upper bound on the HSN-and-rate slabs one GST summary may carry (section 31).
#: The section totals are summed from these rows, so a silent LIMIT would
#: understate a statutory return - a period spanning more slabs than this is
#: refused and has to be narrowed, which is wrong-shaped rather than wrong.
MAX_GST_ROWS = 1000

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
INVOICE_AUDITED_FIELDS = [
    "code",
    "vendor_id",
    "vendor_invoice_number",
    "invoice_date",
    "due_date",
    "purchase_order_id",
    "goods_receipt_id",
    "cost_centre_id",
    "currency",
    "subtotal",
    "tax_amount",
    "round_off_amount",
    "total_amount",
    "paid_amount",
    "status",
    "match_status",
    "notes",
]

PAYMENT_AUDITED_FIELDS = [
    "code",
    "payment_type",
    "party_type",
    "party_id",
    "payment_date",
    "amount",
    "allocated_amount",
    "currency",
    "payment_method",
    "reference_number",
    "bank_name",
    "instrument_date",
    "status",
    "notes",
]


# -----------------------------------------------------------------------------
# Module errors
# -----------------------------------------------------------------------------


class UnbalancedJournalError(BusinessRuleError):
    """A posting whose debits and credits do not agree.

    Its own class rather than a bare :class:`BusinessRuleError` because it is the
    one failure a ledger must never absorb quietly, and because the details it
    carries - both totals and the difference - are what an operator needs.
    """

    def __init__(self, debit: Decimal, credit: Decimal, line_count: int) -> None:
        super().__init__(
            f"The journal does not balance: debits {debit} against credits {credit} "
            f"(difference {debit - credit}).",
            details={
                "total_debit": str(debit),
                "total_credit": str(credit),
                "difference": str(debit - credit),
                "line_count": line_count,
            },
        )


class MatchVarianceError(BusinessRuleError):
    """Approval blocked because the invoice disagrees with the order or receipt."""

    def __init__(self, result: MatchResult) -> None:
        super().__init__(
            f"{result.invoice_code} does not match its purchase order and goods receipt: "
            + "; ".join(result.blocking_reasons),
            code=ErrorCode.APPROVAL_REQUIRED,
            details={
                "invoice_id": str(result.invoice_id),
                "blocking_reasons": result.blocking_reasons,
                "total_variance": str(result.total_variance),
                "worst_variance": str(result.worst_variance),
            },
        )


# -----------------------------------------------------------------------------
# State machines - the only thing permitted to change a status column
# -----------------------------------------------------------------------------

VENDOR_INVOICE_LIFECYCLE: StateMachine[VendorInvoiceStatus] = StateMachine(
    entity="Vendor invoice",
    initial=VendorInvoiceStatus.DRAFT,
    transitions=(
        Transition(
            VendorInvoiceStatus.DRAFT,
            VendorInvoiceStatus.PENDING_APPROVAL,
            permission=Permission.VENDOR_INVOICE_CREATE,
            label="Submit for approval",
        ),
        Transition(
            VendorInvoiceStatus.PENDING_APPROVAL,
            VendorInvoiceStatus.APPROVED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Approve",
            is_irreversible=True,
        ),
        Transition(
            VendorInvoiceStatus.PENDING_APPROVAL,
            VendorInvoiceStatus.DRAFT,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Return to draft",
        ),
        # Payment progress. Both directions, because correcting a mis-keyed
        # allocation must not leave an invoice permanently marked as paid.
        Transition(VendorInvoiceStatus.APPROVED, VendorInvoiceStatus.PARTIALLY_PAID),
        Transition(VendorInvoiceStatus.APPROVED, VendorInvoiceStatus.PAID),
        Transition(VendorInvoiceStatus.PARTIALLY_PAID, VendorInvoiceStatus.PAID),
        Transition(VendorInvoiceStatus.PARTIALLY_PAID, VendorInvoiceStatus.APPROVED),
        Transition(VendorInvoiceStatus.PAID, VendorInvoiceStatus.PARTIALLY_PAID),
        Transition(VendorInvoiceStatus.PAID, VendorInvoiceStatus.APPROVED),
        # A dispute is a live state: the money is still owed until the dispute
        # is settled one way or the other.
        Transition(
            VendorInvoiceStatus.PENDING_APPROVAL,
            VendorInvoiceStatus.DISPUTED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Dispute",
        ),
        Transition(
            VendorInvoiceStatus.APPROVED,
            VendorInvoiceStatus.DISPUTED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Dispute",
        ),
        Transition(
            VendorInvoiceStatus.PARTIALLY_PAID,
            VendorInvoiceStatus.DISPUTED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Dispute",
        ),
        Transition(
            VendorInvoiceStatus.DISPUTED,
            VendorInvoiceStatus.APPROVED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Resolve dispute",
        ),
        Transition(
            VendorInvoiceStatus.DRAFT,
            VendorInvoiceStatus.CANCELLED,
            permission=Permission.VENDOR_INVOICE_CREATE,
            label="Cancel",
        ),
        Transition(
            VendorInvoiceStatus.PENDING_APPROVAL,
            VendorInvoiceStatus.CANCELLED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Cancel",
        ),
        Transition(
            VendorInvoiceStatus.DISPUTED,
            VendorInvoiceStatus.CANCELLED,
            permission=Permission.VENDOR_INVOICE_APPROVE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({VendorInvoiceStatus.CANCELLED}),
)

PAYMENT_LIFECYCLE: StateMachine[PaymentStatus] = StateMachine(
    entity="Payment",
    initial=PaymentStatus.DRAFT,
    transitions=(
        Transition(
            PaymentStatus.DRAFT,
            PaymentStatus.APPROVED,
            permission=Permission.PAYMENT_APPROVE,
            label="Approve",
            is_irreversible=True,
        ),
        Transition(PaymentStatus.APPROVED, PaymentStatus.PARTIALLY_ALLOCATED),
        Transition(PaymentStatus.APPROVED, PaymentStatus.ALLOCATED),
        Transition(PaymentStatus.PARTIALLY_ALLOCATED, PaymentStatus.ALLOCATED),
        Transition(PaymentStatus.PARTIALLY_ALLOCATED, PaymentStatus.APPROVED),
        Transition(PaymentStatus.ALLOCATED, PaymentStatus.PARTIALLY_ALLOCATED),
        Transition(PaymentStatus.ALLOCATED, PaymentStatus.APPROVED),
        Transition(
            PaymentStatus.DRAFT,
            PaymentStatus.CANCELLED,
            permission=Permission.PAYMENT_RECORD,
            label="Cancel",
        ),
        Transition(
            PaymentStatus.APPROVED,
            PaymentStatus.CANCELLED,
            permission=Permission.PAYMENT_APPROVE,
            label="Cancel and reverse",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({PaymentStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# The default chart of accounts
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChartSeed:
    """One control account the automatic postings need."""

    system_code: SystemAccount
    code: str
    name: str
    account_type: LedgerAccountType


# ASSUMPTION: the control accounts the automatic postings need are provisioned
# on first use rather than requiring a chart of accounts to be configured before
# the first invoice can be approved. They are created as `is_system` rows with a
# fixed `system_code`, are audited like any other creation, and may be renamed
# afterwards - the postings resolve the system code, not the name or the code.
# The alternative, refusing to post until somebody sets up a chart, would make
# the module unusable on day one for a company that does not yet run a general
# ledger. Recorded in the returned notes.
DEFAULT_CHART: tuple[ChartSeed, ...] = (
    ChartSeed(
        SystemAccount.ACCOUNTS_RECEIVABLE, "1100", "Accounts Receivable", LedgerAccountType.ASSET
    ),
    ChartSeed(SystemAccount.BANK, "1200", "Bank", LedgerAccountType.ASSET),
    ChartSeed(SystemAccount.CASH, "1210", "Cash", LedgerAccountType.ASSET),
    ChartSeed(SystemAccount.INPUT_TAX, "1300", "Input GST Credit", LedgerAccountType.ASSET),
    ChartSeed(
        SystemAccount.ACCOUNTS_PAYABLE, "2100", "Accounts Payable", LedgerAccountType.LIABILITY
    ),
    ChartSeed(SystemAccount.OUTPUT_TAX, "2200", "Output GST Payable", LedgerAccountType.LIABILITY),
    ChartSeed(
        SystemAccount.PURCHASE_EXPENSE, "5100", "Purchases and Services", LedgerAccountType.EXPENSE
    ),
    ChartSeed(SystemAccount.ROUND_OFF, "5900", "Rounding Differences", LedgerAccountType.EXPENSE),
)


# -----------------------------------------------------------------------------
# Pure rule helpers - unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalLine:
    """One leg of a posting, as the caller describes it.

    Exactly one of ``debit``/``credit`` is non-zero - the same rule the database
    enforces on the row, checked here first so the caller is told which leg is
    wrong rather than receiving an integrity error.
    """

    account_id: uuid.UUID
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    party_type: PartyType = PartyType.NONE
    party_id: uuid.UUID | None = None
    narration: str | None = None
    #: Where the money was spent or earned. Only an INCOME or EXPENSE leg may
    #: carry one, and a new posting needs it active - see :func:`post_journal`.
    cost_centre_id: uuid.UUID | None = None
    #: Set only by :func:`reverse_journal`; makes the row a reversal.
    reverses_entry_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class JournalTotals:
    """What a balanced set adds up to."""

    debit: Decimal
    credit: Decimal
    line_count: int

    @property
    def difference(self) -> Decimal:
        return self.debit - self.credit

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO


def assert_balanced(lines: Sequence[JournalLine]) -> JournalTotals:
    """Validate a posting and return its totals, or raise.

    Pure: no session, no ORM, no clock. This is the rule that makes the ledger a
    ledger, so it is exercised directly by the unit tests rather than only
    through a service that needs a database.

    Each leg is rounded to the money quantum *before* being summed. Summing raw
    values and rounding the totals afterwards can turn two legs that each round
    to the same figure into a difference of a paisa, which would reject a
    perfectly good posting - or, worse, accept one that is genuinely out by
    less than half a paisa.
    """
    if not lines:
        raise ValidationError("A journal needs at least two legs; none were given.")
    if len(lines) < 2:
        raise ValidationError(
            "A journal needs at least two legs: one debit and one credit.",
            details={"line_count": len(lines)},
        )
    if len(lines) > MAX_JOURNAL_LINES:
        raise ValidationError(
            f"A journal may carry at most {MAX_JOURNAL_LINES} legs.",
            details={"line_count": len(lines), "maximum": MAX_JOURNAL_LINES},
        )

    total_debit = ZERO
    total_credit = ZERO
    for index, line in enumerate(lines, start=1):
        debit = to_money(line.debit or ZERO)
        credit = to_money(line.credit or ZERO)
        if debit < ZERO or credit < ZERO:
            raise ValidationError(
                f"Leg {index} carries a negative amount. A reduction is posted on the "
                "opposite side, not as a negative.",
                details={"line_number": index, "debit": str(debit), "credit": str(credit)},
            )
        if (debit > ZERO) == (credit > ZERO):
            raise ValidationError(
                f"Leg {index} must be either a debit or a credit, and exactly one of them.",
                details={"line_number": index, "debit": str(debit), "credit": str(credit)},
            )
        if (line.party_type is PartyType.NONE) != (line.party_id is None):
            raise ValidationError(
                f"Leg {index} must name a party when it has a party type, and none when it "
                "does not.",
                details={"line_number": index, "party_type": str(line.party_type)},
            )
        total_debit += debit
        total_credit += credit

    totals = JournalTotals(debit=total_debit, credit=total_credit, line_count=len(lines))
    if not totals.is_balanced:
        raise UnbalancedJournalError(total_debit, total_credit, len(lines))
    return totals


def assert_cost_centre_legs(
    lines: Sequence[JournalLine], accounts: Mapping[uuid.UUID, PostingAccount]
) -> None:
    """Refuse a cost centre on any leg whose account is not income or expense.

    Pure, like :func:`assert_balanced`. ``accounts`` is the posting's own
    account lookup, so the rule costs no query. Applied to reversal legs too:
    an account's class cannot change (``LedgerAccountUpdate`` does not carry
    it), so the mirror of a leg that passed always passes - and a leg that
    names an account the lookup did not return is the caller's not-found
    error to raise, not this rule's.
    """
    for index, line in enumerate(lines, start=1):
        if line.cost_centre_id is None:
            continue
        account = accounts.get(line.account_id)
        if account is None or account.account_type in COST_CENTRE_ACCOUNT_TYPES:
            continue
        raise ValidationError(
            f"Leg {index} posts to account {account.code}, a {account.account_type.lower()} "
            "account; only income and expense legs may carry a cost centre.",
            details={
                "line_number": index,
                "account_code": account.code,
                "account_type": str(account.account_type),
                "cost_centre_id": str(line.cost_centre_id),
            },
        )


def split_by_cost_centre(
    amounts: Sequence[tuple[uuid.UUID | None, Decimal]], *, total: Decimal
) -> list[tuple[uuid.UUID | None, Decimal]]:
    """Group amounts by cost centre so the groups sum to ``total`` exactly.

    Pure. ``amounts`` is one ``(cost centre, amount)`` per source line, in line
    order; a group appears where its first line does, so the journal reads in
    the order the document does. Each group is the money-rounded sum of its
    lines. When the lines were the source of ``total`` - an invoice's subtotal
    is the sum of its taxable amounts - that is already exact; if a header ever
    disagrees with its own lines, the difference lands on the largest group
    (the first, on a tie) so the posting still balances to the paisa rather
    than failing approval over a figure nobody can see. A group that comes to
    zero is dropped: a ledger leg must carry money.
    """
    grouped: dict[uuid.UUID | None, Decimal] = {}
    for cost_centre_id, amount in amounts:
        grouped[cost_centre_id] = grouped.get(cost_centre_id, ZERO) + amount

    split = [(cost_centre_id, to_money(amount)) for cost_centre_id, amount in grouped.items()]
    target = to_money(total)
    residual = target - sum((amount for _, amount in split), start=ZERO)
    if residual != ZERO:
        if not split:
            split = [(None, target)]
        else:
            largest = max(range(len(split)), key=lambda index: (split[index][1], -index))
            cost_centre_id, amount = split[largest]
            split[largest] = (cost_centre_id, amount + residual)
    return [(cost_centre_id, amount) for cost_centre_id, amount in split if amount != ZERO]


@dataclass(frozen=True, slots=True)
class LinePricing:
    """The derived money on one invoice line."""

    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


def price_invoice_line(
    *,
    quantity: Decimal,
    unit_price: Decimal,
    discount_amount: Decimal,
    tax_rate: Decimal,
    is_interstate: bool,
) -> LinePricing:
    """Derive a line's taxable value, tax split and total.

    The CGST half is computed and the SGST half is then the *remainder* rather
    than a second independent halving: on an odd number of paise the two halves
    must still add up to the tax, which is what the ``tax_amount = cgst + sgst +
    igst`` CHECK on the row requires.
    """
    gross = to_money(Decimal(quantity) * Decimal(unit_price))
    taxable = to_money(gross - to_money(discount_amount))
    if taxable < ZERO:
        raise ValidationError(
            "The discount on a line cannot exceed the line's value.",
            details={"gross": str(gross), "discount_amount": str(to_money(discount_amount))},
        )

    tax = to_money(taxable * Decimal(tax_rate) / Decimal(100))
    if is_interstate:
        cgst = sgst = ZERO
        igst = tax
    else:
        cgst = to_money(tax / Decimal(2))
        sgst = tax - cgst
        igst = ZERO

    return LinePricing(
        taxable_amount=taxable,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
        tax_amount=tax,
        line_total=taxable + tax,
    )


def ageing_bucket(days_overdue: int) -> str:
    """Which ageing bucket an open item falls in.

    Derived from :data:`AGEING_BUCKET_BOUNDARIES`, the same constant the SQL
    ``CASE`` in the repository is built from, so a change to the boundaries
    moves both at once. An item due in the future is not aged at all.
    """
    if days_overdue < 0:
        return NOT_DUE_LABEL
    first, second, third = AGEING_BUCKET_BOUNDARIES
    if days_overdue <= first:
        return AGEING_BUCKET_LABELS[0]
    if days_overdue <= second:
        return AGEING_BUCKET_LABELS[1]
    if days_overdue <= third:
        return AGEING_BUCKET_LABELS[2]
    return AGEING_BUCKET_LABELS[3]


def variance_ratio(actual: Decimal, expected: Decimal) -> Decimal | None:
    """Proportional variance, or ``None`` when there is no basis to compare to.

    A zero expected value is not a zero variance: billing for something that was
    ordered at no charge is an infinite proportional variance, and reporting it
    as 0% would let it through the tolerance untouched.
    """
    if expected == ZERO:
        return None
    return (actual - expected) / expected


@dataclass(frozen=True, slots=True)
class MatchLineInput:
    """One invoice line and the two documents it is matched against.

    Prices here are *net* unit prices - discount already applied on both sides -
    because a vendor who bills the list price and omits the agreed discount has
    overbilled, and comparing gross prices would not notice.
    """

    line_number: int
    invoiced_quantity: Decimal
    invoiced_unit_price: Decimal
    invoiced_amount: Decimal
    invoice_line_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    item_code: str | None = None
    description: str | None = None
    purchase_order_line_id: uuid.UUID | None = None
    ordered_quantity: Decimal | None = None
    ordered_unit_price: Decimal | None = None
    #: What the store actually took in. ``None`` means nothing has been received
    #: against this line yet, which is itself a finding.
    accepted_quantity: Decimal | None = None


def evaluate_match(
    lines: Sequence[MatchLineInput],
    *,
    invoice_id: uuid.UUID,
    invoice_code: str,
    vendor_id: uuid.UUID,
    purchase_order_id: uuid.UUID | None,
    goods_receipt_id: uuid.UUID | None,
    price_tolerance: Decimal = PRICE_VARIANCE_TOLERANCE,
    quantity_tolerance: Decimal = QUANTITY_VARIANCE_TOLERANCE,
    header_findings: Sequence[str] = (),
) -> MatchResult:
    """The three-way match itself: pure, and therefore thoroughly testable.

    Takes the facts the repository loaded and decides, line by line, whether the
    bill agrees with the order and the receipt. No session, no clock, no
    randomness - the same inputs always produce the same verdict, which is what
    lets the tolerance behaviour be pinned down by unit tests rather than
    inferred from a passing integration run.

    ASSUMPTION: a line with no purchase-order line behind it blocks approval.
    Freight, packing and handling charges a vendor adds after the fact are
    exactly the overbilling master rules section 24 asks this match to catch, so
    they are surfaced rather than waved through - and an authorised override
    with a recorded reason takes one action. For the same reason an invoice with
    no purchase order at all is reported as unmatched: non-order spend is
    approved deliberately, not by default.
    """
    results: list[MatchLineVariance] = []
    blocking: list[str] = list(header_findings)
    total_invoiced = ZERO
    total_expected = ZERO
    worst = ZERO

    if purchase_order_id is None:
        blocking.append(
            "The invoice is not linked to a purchase order, so there is no agreed price to "
            "match against."
        )

    for line in lines:
        findings: list[str] = []
        invoiced_amount = to_money(line.invoiced_amount)
        total_invoiced += invoiced_amount

        expected_amount: Decimal | None = None
        quantity_variance: Decimal | None = None
        quantity_ratio: Decimal | None = None
        price_variance: Decimal | None = None
        price_ratio: Decimal | None = None

        if line.purchase_order_line_id is None:
            findings.append(
                f"Line {line.line_number} is not billed against any purchase order line."
            )
        else:
            # --- price, against what was ordered ----------------------------
            if line.ordered_unit_price is None:
                findings.append(
                    f"Line {line.line_number} names a purchase order line that could not be read."
                )
            else:
                ordered_price = to_unit_price(line.ordered_unit_price)
                invoiced_price = to_unit_price(line.invoiced_unit_price)
                price_variance = invoiced_price - ordered_price
                price_ratio = variance_ratio(invoiced_price, ordered_price)
                if price_ratio is None:
                    if price_variance != ZERO:
                        findings.append(
                            f"Line {line.line_number} is billed at {invoiced_price} against an "
                            "ordered price of zero."
                        )
                elif abs(price_ratio) > price_tolerance:
                    findings.append(
                        f"Line {line.line_number} price {invoiced_price} differs from the "
                        f"ordered {ordered_price} by {_as_percent(price_ratio)}, beyond the "
                        f"{_as_percent(price_tolerance)} tolerance."
                    )

            # --- quantity, against what was accepted ------------------------
            if line.accepted_quantity is None:
                findings.append(
                    f"Line {line.line_number} has no accepted goods-receipt quantity to match "
                    "against."
                )
            else:
                accepted = to_quantity(line.accepted_quantity)
                invoiced_quantity = to_quantity(line.invoiced_quantity)
                quantity_variance = invoiced_quantity - accepted
                quantity_ratio = variance_ratio(invoiced_quantity, accepted)
                allowed = to_quantity(abs(accepted) * quantity_tolerance)
                if abs(quantity_variance) > allowed:
                    findings.append(
                        f"Line {line.line_number} bills {invoiced_quantity} against "
                        f"{accepted} accepted at the gate."
                    )
                if line.ordered_unit_price is not None:
                    expected_amount = to_money(accepted * to_unit_price(line.ordered_unit_price))
                    total_expected += expected_amount

        amount_variance = (
            to_money(invoiced_amount - expected_amount) if expected_amount is not None else None
        )
        if amount_variance is not None and abs(amount_variance) > abs(worst):
            worst = amount_variance

        results.append(
            MatchLineVariance(
                invoice_line_id=line.invoice_line_id,
                line_number=line.line_number,
                item_id=line.item_id,
                item_code=line.item_code,
                description=line.description,
                purchase_order_line_id=line.purchase_order_line_id,
                invoiced_quantity=to_quantity(line.invoiced_quantity),
                accepted_quantity=(
                    to_quantity(line.accepted_quantity)
                    if line.accepted_quantity is not None
                    else None
                ),
                ordered_quantity=(
                    to_quantity(line.ordered_quantity)
                    if line.ordered_quantity is not None
                    else None
                ),
                quantity_variance=quantity_variance,
                quantity_variance_ratio=quantity_ratio,
                invoiced_unit_price=to_unit_price(line.invoiced_unit_price),
                ordered_unit_price=(
                    to_unit_price(line.ordered_unit_price)
                    if line.ordered_unit_price is not None
                    else None
                ),
                price_variance=price_variance,
                price_variance_ratio=price_ratio,
                invoiced_amount=invoiced_amount,
                expected_amount=expected_amount,
                amount_variance=amount_variance,
                within_tolerance=not findings,
                findings=findings,
            )
        )
        blocking.extend(findings)

    matched = not blocking
    return MatchResult(
        invoice_id=invoice_id,
        invoice_code=invoice_code,
        vendor_id=vendor_id,
        purchase_order_id=purchase_order_id,
        goods_receipt_id=goods_receipt_id,
        matched=matched,
        match_status=MatchStatus.MATCHED if matched else MatchStatus.VARIANCE,
        price_tolerance=price_tolerance,
        quantity_tolerance=quantity_tolerance,
        total_invoiced=to_money(total_invoiced),
        total_expected=to_money(total_expected),
        total_variance=to_money(total_invoiced - total_expected),
        worst_variance=to_money(worst),
        lines=results,
        blocking_reasons=blocking,
    )


def _as_percent(ratio: Decimal) -> str:
    return f"{to_money(ratio * Decimal(100))}%"


def invoice_status_after_payment(
    current: VendorInvoiceStatus, *, total_amount: Decimal, paid_amount: Decimal
) -> VendorInvoiceStatus:
    """Where an invoice sits once its paid total changes.

    Derived from the money rather than set independently, so an invoice can
    never claim to be PAID while it still owes something. A disputed invoice
    keeps its status: settling part of a disputed bill does not end the dispute.
    """
    if current in (VendorInvoiceStatus.DISPUTED, VendorInvoiceStatus.CANCELLED):
        return current
    paid = to_money(paid_amount)
    total = to_money(total_amount)
    if paid >= total and total > ZERO:
        return VendorInvoiceStatus.PAID
    if paid > ZERO:
        return VendorInvoiceStatus.PARTIALLY_PAID
    return VendorInvoiceStatus.APPROVED


def payment_status_after_allocation(*, amount: Decimal, allocated_amount: Decimal) -> PaymentStatus:
    """Where a payment sits once its allocated total changes."""
    allocated = to_money(allocated_amount)
    total = to_money(amount)
    if allocated >= total:
        return PaymentStatus.ALLOCATED
    if allocated > ZERO:
        return PaymentStatus.PARTIALLY_ALLOCATED
    return PaymentStatus.APPROVED


def statement_direction(party_type: PartyType) -> int:
    """Sign that makes a positive statement balance mean "owed".

    A customer is a receivable and a vendor is a payable, so the two statements
    are the same query with opposite signs.
    """
    if party_type is PartyType.CUSTOMER:
        return 1
    if party_type is PartyType.VENDOR:
        return -1
    raise ValidationError("A statement is drawn for a customer or a vendor.")


# -----------------------------------------------------------------------------
# Chart of accounts
# -----------------------------------------------------------------------------


async def ensure_system_accounts(
    session: AsyncSession, tenant: TenantContext
) -> dict[str, LedgerAccount]:
    """Provision the control accounts the automatic postings need.

    Idempotent, and serialised on an advisory lock keyed to the company: two
    first postings arriving together would otherwise both find the chart empty
    and both try to insert, and the second would fail on the unique index with
    an integrity error rather than a business message. The lock is transaction
    scoped, so there is no unlock path to forget.
    """
    repo = LedgerAccountRepository(session, tenant)
    existing = await repo.by_system_codes([seed.system_code for seed in DEFAULT_CHART])
    missing = [seed for seed in DEFAULT_CHART if str(seed.system_code) not in existing]
    if not missing:
        return existing

    async with transaction(session):
        await advisory_xact_lock(session, "finance_chart", tenant.company_id)
        # Re-read under the lock: the holder before us may have created them.
        existing = await repo.by_system_codes([seed.system_code for seed in DEFAULT_CHART])
        missing = [seed for seed in DEFAULT_CHART if str(seed.system_code) not in existing]

        for seed in missing:
            account = repo.add(
                LedgerAccount(
                    code=seed.code,
                    name=seed.name,
                    account_type=seed.account_type,
                    is_active=True,
                    is_system=True,
                    system_code=str(seed.system_code),
                    description="Provisioned automatically for posted finance documents.",
                )
            )
            await session.flush()
            existing[str(seed.system_code)] = account
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_LEDGER_ACCOUNT,
                entity_id=account.id,
                entity_code=account.code,
                summary=f"Provisioned the {seed.name} control account",
                user_id=tenant.user_id,
                new_values={
                    "code": account.code,
                    "name": account.name,
                    "account_type": account.account_type,
                    "system_code": account.system_code,
                },
            )

    if missing:
        log.info(
            "finance.chart_provisioned",
            company_id=str(tenant.company_id),
            accounts=[seed.code for seed in missing],
        )
    return existing


async def list_accounts(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: LedgerAccountFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[LedgerAccountRead], OffsetPagination]:
    repo = LedgerAccountRepository(session, tenant)
    rows, pagination = await repo.list_page(params, stmt=repo.filtered(filters), sort=sort)
    return [LedgerAccountRead.model_validate(row) for row in rows], pagination


async def get_account(
    session: AsyncSession, tenant: TenantContext, account_id: uuid.UUID
) -> LedgerAccountRead:
    account = await LedgerAccountRepository(session, tenant).get_or_404(account_id)
    return LedgerAccountRead.model_validate(account)


async def create_account(
    session: AsyncSession, tenant: TenantContext, payload: LedgerAccountCreate
) -> LedgerAccountRead:
    repo = LedgerAccountRepository(session, tenant)

    async with transaction(session):
        if await repo.get_by_code(payload.code) is not None:
            raise BusinessRuleError(
                f"An account with code {payload.code} already exists.",
                code=ErrorCode.DUPLICATE_DOCUMENT,
                status_code=409,
                details={"code": payload.code},
            )
        if payload.parent_id is not None:
            await repo.get_or_404(payload.parent_id)

        account = repo.add(
            LedgerAccount(
                code=payload.code,
                name=payload.name,
                account_type=payload.account_type,
                parent_id=payload.parent_id,
                is_active=payload.is_active,
                is_system=False,
                description=payload.description,
            )
        )
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_LEDGER_ACCOUNT,
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Opened ledger account {account.code} - {account.name}",
            user_id=tenant.user_id,
            new_values={
                "code": account.code,
                "name": account.name,
                "account_type": account.account_type,
                "parent_id": account.parent_id,
            },
        )

    return LedgerAccountRead.model_validate(account)


async def update_account(
    session: AsyncSession,
    tenant: TenantContext,
    account_id: uuid.UUID,
    payload: LedgerAccountUpdate,
) -> LedgerAccountRead:
    repo = LedgerAccountRepository(session, tenant)

    async with transaction(session):
        account = await repo.get_or_404(account_id)
        before = snapshot(account, ["name", "parent_id", "is_active", "description"])

        changes = payload.model_dump(exclude_unset=True)
        if "parent_id" in changes and changes["parent_id"] is not None:
            new_parent = changes["parent_id"]
            if new_parent == account.id:
                raise ValidationError("An account cannot be its own parent.")
            await repo.get_or_404(new_parent)
            # A chart is a tree. Re-parenting under a descendant would produce a
            # cycle that no report could walk out of.
            if account.id in await repo.ancestor_ids(new_parent):
                raise BusinessRuleError(
                    f"Moving {account.code} under that account would create a cycle in the "
                    "chart of accounts.",
                    details={"account_id": str(account.id), "parent_id": str(new_parent)},
                )
        if changes.get("is_active") is False and account.is_system:
            raise BusinessRuleError(
                f"{account.code} is a control account the automatic postings depend on and "
                "cannot be deactivated.",
                details={"system_code": account.system_code},
            )

        for attribute, value in changes.items():
            setattr(account, attribute, value)
        repo.touch(account)
        await session.flush()

        after = snapshot(account, ["name", "parent_id", "is_active", "description"])
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_LEDGER_ACCOUNT,
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Updated ledger account {account.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=after,
        )

    return LedgerAccountRead.model_validate(account)


async def delete_account(
    session: AsyncSession, tenant: TenantContext, account_id: uuid.UUID, *, reason: str
) -> None:
    """Retire an account that has never been posted to.

    An account with history is never removed, not even softly: a ledger whose
    accounts can disappear is not a ledger, and section 28 forbids hiding a
    financially traceable record behind a flag.
    """
    repo = LedgerAccountRepository(session, tenant)

    async with transaction(session):
        account = await repo.get_or_404(account_id)
        if account.is_system:
            raise BusinessRuleError(
                f"{account.code} is a control account the automatic postings depend on.",
                details={"system_code": account.system_code},
            )
        if await repo.has_postings(account.id):
            raise BusinessRuleError(
                f"{account.code} has postings against it and cannot be removed. Deactivate it "
                "instead so it stops appearing on new documents.",
                code=ErrorCode.REFERENCED_RECORD_IN_USE,
                details={"account_id": str(account.id)},
            )
        if await repo.has_children(account.id):
            raise BusinessRuleError(
                f"{account.code} has accounts beneath it. Move or remove them first.",
                code=ErrorCode.REFERENCED_RECORD_IN_USE,
                details={"account_id": str(account.id)},
            )

        await repo.soft_delete(account)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_LEDGER_ACCOUNT,
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Retired ledger account {account.code}",
            user_id=tenant.user_id,
            context={"reason": reason},
        )


# -----------------------------------------------------------------------------
# Posting
# -----------------------------------------------------------------------------


async def post_journal(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[JournalLine],
    *,
    entry_date: date,
    reference_type: str,
    reference_id: uuid.UUID | None = None,
    reference_code: str | None = None,
    narration: str | None = None,
    currency: str | None = None,
    journal_id: uuid.UUID | None = None,
) -> list[LedgerEntry]:
    """Write one balanced set of postings. The only way into ``ledger_entries``.

    Refuses unless the debits equal the credits to the money quantum, every leg
    names an account that exists and is active, and the period the entry date
    falls in is still open. All of it happens inside the caller's transaction:
    if the document that caused the posting rolls back, the posting rolls back
    with it.

    Returns the entries in the order they were given, so a caller that needs to
    reverse a specific leg later can identify it.

    A leg may carry a cost centre only on an INCOME or EXPENSE account, and a
    new leg only one that is live and active in this tenant; every centre in
    the journal is checked in one statement, and a journal naming none costs no
    query at all. A reversal leg is exempt from the "active" half - see
    :func:`_assert_cost_centres_postable`.

    ASSUMPTION: ``currency`` left unset means the company's own - the same
    column an invoice and a payment default to. Every posting this module makes
    itself passes the source document's denomination explicitly; the default is
    for a direct journal, and a module constant there would put rows in the
    ledger denominated in a currency the company does not keep books in.
    """
    totals = assert_balanced(lines)
    denomination = currency or await company_currency(session, tenant)

    account_repo = LedgerAccountRepository(session, tenant)
    year_repo = FinancialYearRepository(session, tenant)

    async with transaction(session):
        known = await account_repo.posting_accounts([line.account_id for line in lines])
        unknown = {line.account_id for line in lines} - set(known)
        if unknown:
            raise NotFoundError(
                "Ledger account", ", ".join(sorted(str(account) for account in unknown))
            )
        assert_cost_centre_legs(lines, known)
        await _assert_cost_centres_postable(session, tenant, lines)

        financial_year = await year_repo.containing(entry_date)
        if financial_year is not None and financial_year.is_closed:
            raise BusinessRuleError(
                f"The financial year {financial_year.code} is closed; nothing may be posted "
                "into it.",
                details={
                    "entry_date": entry_date.isoformat(),
                    "financial_year": financial_year.code,
                },
            )

        identity = journal_id or new_id()
        entries: list[LedgerEntry] = []
        for index, line in enumerate(lines, start=1):
            entry = LedgerEntry(
                company_id=tenant.company_id,
                journal_id=identity,
                line_number=index,
                account_id=line.account_id,
                party_type=line.party_type,
                party_id=line.party_id,
                entry_date=entry_date,
                debit_amount=to_money(line.debit or ZERO),
                credit_amount=to_money(line.credit or ZERO),
                currency=denomination,
                reference_type=reference_type,
                reference_id=reference_id,
                reference_code=reference_code,
                narration=line.narration or narration,
                financial_year_id=financial_year.id if financial_year is not None else None,
                is_reversal=line.reverses_entry_id is not None,
                reverses_entry_id=line.reverses_entry_id,
                cost_centre_id=line.cost_centre_id,
                created_by_id=tenant.user_id,
            )
            session.add(entry)
            entries.append(entry)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_JOURNAL,
            entity_id=identity,
            entity_code=reference_code,
            summary=(
                f"Posted journal {identity} for {reference_code or reference_type}: "
                f"{totals.debit} debit / {totals.credit} credit across {totals.line_count} legs"
            ),
            user_id=tenant.user_id,
            new_values={
                "journal_id": identity,
                "entry_date": entry_date,
                "total_debit": totals.debit,
                "total_credit": totals.credit,
                "line_count": totals.line_count,
                "reference_type": reference_type,
                "reference_id": reference_id,
            },
        )

    log.info(
        "finance.journal_posted",
        journal_id=str(identity),
        reference_type=reference_type,
        reference_code=reference_code,
        debit=str(totals.debit),
        legs=totals.line_count,
    )
    return entries


#: The verb phrase a retired or inactive centre's refusal on a posting ends with.
_NO_NEW_POSTINGS = "cannot take new postings"


async def _assert_cost_centres_postable(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[JournalLine]
) -> None:
    """Every cost centre a journal names must be this tenant's, in one statement.

    A *new* leg also needs the centre live and active: allocating fresh spend
    to a centre somebody switched off is exactly what switching it off is
    meant to stop. A *reversal* leg does not - it copies the centre of the leg
    it undoes, and refusing it would make every journal posted to a centre
    irreversible the moment that centre was retired (docs/assumptions.md §43).
    Retired rows are therefore read too, and only tenant ownership is checked
    for a reversal.
    """
    wanted = {line.cost_centre_id for line in lines if line.cost_centre_id is not None}
    if not wanted:
        return
    found = await cost_centre_facts(session, tenant, wanted, include_archived=True)
    for index, line in enumerate(lines, start=1):
        if line.cost_centre_id is None:
            continue
        facts = found.get(line.cost_centre_id)
        if facts is None:
            # Another company's id answers exactly like one that never existed.
            raise NotFoundError(COST_CENTRE_ENTITY, line.cost_centre_id)
        if line.reverses_entry_id is None and not facts.is_assignable:
            raise ValidationError(
                inactive_record(COST_CENTRE_ENTITY, facts.code, _NO_NEW_POSTINGS),
                details={
                    "line_number": index,
                    "cost_centre_id": str(facts.id),
                    "cost_centre_code": facts.code,
                },
            )


async def reverse_journal(
    session: AsyncSession,
    tenant: TenantContext,
    journal_id: uuid.UUID,
    *,
    reason: str,
    entry_date: date | None = None,
) -> list[LedgerEntry]:
    """Undo a posting by writing its mirror image.

    Nothing is edited or deleted - that is what ``ImmutableLedgerEntity`` means.
    Each new leg names the leg it reverses, so the pair is visible from either
    side, and the mirror of a balanced set is balanced by construction.

    Each mirror leg keeps the cost centre of the leg it undoes, so a reversal
    nets to zero on the cost-centre statement as well as on the account - and
    it does so even after that centre was deactivated or retired.
    """
    entry_repo = LedgerEntryRepository(session, tenant)

    async with transaction(session):
        original = await entry_repo.journal_lines(journal_id)
        if not original:
            raise NotFoundError("Journal", journal_id)
        if any(entry.is_reversal for entry in original):
            raise BusinessRuleError(
                "That journal is itself a reversal; reversing a reversal would restate the "
                "original posting rather than correct it.",
                details={"journal_id": str(journal_id)},
            )

        mirrored = [
            JournalLine(
                account_id=entry.account_id,
                debit=entry.credit_amount,
                credit=entry.debit_amount,
                party_type=PartyType(entry.party_type),
                party_id=entry.party_id,
                narration=f"Reversal: {reason}",
                cost_centre_id=entry.cost_centre_id,
                reverses_entry_id=entry.id,
            )
            for entry in original
        ]
        head = original[0]
        return await post_journal(
            session,
            tenant,
            mirrored,
            entry_date=entry_date or today(),
            reference_type=head.reference_type,
            reference_id=head.reference_id,
            reference_code=head.reference_code,
            narration=f"Reversal of journal {journal_id}: {reason}",
            currency=head.currency,
        )


# -----------------------------------------------------------------------------
# Ledger statements
# -----------------------------------------------------------------------------


async def party_statement(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    party_type: PartyType,
    party_id: uuid.UUID,
    filters: StatementFilters,
    params: PageParams,
) -> tuple[LedgerStatement, OffsetPagination]:
    """One party's ledger with a running balance.

    Three queries, none of which loops in Python: the party's name, the header
    totals, and the page of rows whose running balance PostgreSQL computes with
    a window function. Paging never changes the header, and the balance on page
    three is the real balance rather than the movement since the page began.
    """
    party = await _party_facts(session, tenant, party_type, party_id)
    direction = statement_direction(party_type)
    date_to = filters.date_to or today()

    entry_repo = LedgerEntryRepository(session, tenant)
    totals = await entry_repo.statement_totals(
        party_type=party_type,
        party_id=party_id,
        direction=direction,
        date_from=filters.date_from,
        date_to=date_to,
    )
    stmt = entry_repo.statement_stmt(
        party_type=party_type,
        party_id=party_id,
        direction=direction,
        date_from=filters.date_from,
        date_to=date_to,
    )
    rows, pagination = await paginate_projection(session, stmt, params)

    statement = LedgerStatement(
        party_type=party_type,
        party_id=party_id,
        party_code=party.code,
        party_name=party.name,
        date_from=filters.date_from,
        date_to=date_to,
        opening_balance=to_money(totals.opening_balance),
        total_debit=to_money(totals.total_debit),
        total_credit=to_money(totals.total_credit),
        closing_balance=to_money(totals.closing_balance),
        rows=[
            LedgerStatementRow(
                entry_id=row.entry_id,
                journal_id=row.journal_id,
                entry_date=row.entry_date,
                account_code=row.account_code,
                account_name=row.account_name,
                reference_type=row.reference_type,
                reference_code=row.reference_code,
                narration=row.narration,
                debit_amount=to_money(row.debit_amount),
                credit_amount=to_money(row.credit_amount),
                running_balance=to_money(row.running_balance),
                is_reversal=row.is_reversal,
            )
            for row in rows
        ],
    )
    return statement, pagination


async def _party_facts(
    session: AsyncSession, tenant: TenantContext, party_type: PartyType, party_id: uuid.UUID
) -> PartyFacts:
    if party_type is PartyType.CUSTOMER:
        facts = await CustomerLookupRepository(session, tenant).get_facts(party_id)
        if facts is None:
            raise NotFoundError("Customer", party_id)
        return facts
    if party_type is PartyType.VENDOR:
        facts = await VendorLookupRepository(session, tenant).get_facts(party_id)
        if facts is None:
            raise NotFoundError("Vendor", party_id)
        return facts
    raise ValidationError("A statement is drawn for a customer or a vendor.")


# -----------------------------------------------------------------------------
# Vendor invoices
# -----------------------------------------------------------------------------


def invoice_summary(invoice: VendorInvoice) -> VendorInvoiceSummary:
    """Project an invoice, with the outstanding figure derived once.

    Derived here rather than in the browser so the payables list, the ageing
    report and the allocation path cannot disagree about what is still owed.
    """
    return VendorInvoiceSummary(
        id=invoice.id,
        code=invoice.code,
        vendor_id=invoice.vendor_id,
        vendor_invoice_number=invoice.vendor_invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        status=VendorInvoiceStatus(invoice.status),
        match_status=MatchStatus(invoice.match_status),
        currency=invoice.currency,
        subtotal=invoice.subtotal,
        tax_amount=invoice.tax_amount,
        total_amount=invoice.total_amount,
        paid_amount=invoice.paid_amount,
        outstanding_amount=to_money(invoice.total_amount - invoice.paid_amount),
        purchase_order_id=invoice.purchase_order_id,
        goods_receipt_id=invoice.goods_receipt_id,
        cost_centre_id=invoice.cost_centre_id,
        created_at=invoice.created_at,
    )


def invoice_detail(
    invoice: VendorInvoice, lines: Sequence[VendorInvoiceLine]
) -> VendorInvoiceDetail:
    return VendorInvoiceDetail(
        **invoice_summary(invoice).model_dump(),
        round_off_amount=invoice.round_off_amount,
        match_variance_amount=invoice.match_variance_amount,
        matched_at=invoice.matched_at,
        variance_override_reason=invoice.variance_override_reason,
        approved_by_id=invoice.approved_by_id,
        approved_at=invoice.approved_at,
        dispute_reason=invoice.dispute_reason,
        cancellation_reason=invoice.cancellation_reason,
        notes=invoice.notes,
        version=invoice.version,
        lines=[VendorInvoiceLineRead.model_validate(line) for line in lines],
    )


async def list_invoices(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: VendorInvoiceFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[VendorInvoiceSummary], OffsetPagination]:
    repo = VendorInvoiceRepository(session, tenant)
    rows, pagination = await repo.list_page(
        params, stmt=repo.filtered(filters, as_of=today()), sort=sort
    )
    return [invoice_summary(row) for row in rows], pagination


async def get_invoice_detail(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> VendorInvoiceDetail:
    invoice = await VendorInvoiceRepository(session, tenant).get_or_404(invoice_id)
    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


@dataclass(frozen=True, slots=True)
class _ResolvedLine:
    """One validated invoice line, priced and ready to persist."""

    payload: VendorInvoiceLineIn
    line_number: int
    hsn_code: str | None
    description: str | None
    pricing: LinePricing


async def _resolve_invoice_lines(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[VendorInvoiceLineIn],
    *,
    purchase_order_id: uuid.UUID | None,
) -> list[_ResolvedLine]:
    """Validate and price a whole line set in a handful of batched queries.

    Everything a line refers to - item, unit, order line, receipt line - is
    looked up for the entire set at once. A twenty-line invoice validated with a
    lookup per line would be eighty round trips on the write path (section 32).
    """
    item_ids = [line.item_id for line in lines if line.item_id is not None]
    uom_ids = [line.uom_id for line in lines if line.uom_id is not None]
    order_line_ids = [
        line.purchase_order_line_id for line in lines if line.purchase_order_line_id is not None
    ]
    receipt_line_ids = [
        line.goods_receipt_line_id for line in lines if line.goods_receipt_line_id is not None
    ]

    items = await ItemLookupRepository(session, tenant).load(item_ids)
    known_uoms = await UomLookupRepository(session, tenant).existing(uom_ids)
    order_lines = await PurchaseOrderLineLookupRepository(session, tenant).load(order_line_ids)
    receipt_lines = await GoodsReceiptLineLookupRepository(session, tenant).load(receipt_line_ids)

    resolved: list[_ResolvedLine] = []
    for number, payload in enumerate(lines, start=1):
        item = items.get(payload.item_id) if payload.item_id is not None else None
        if payload.item_id is not None and item is None:
            raise NotFoundError("Item", payload.item_id)
        if payload.uom_id is not None and payload.uom_id not in known_uoms:
            raise NotFoundError("Unit of measure", payload.uom_id)

        if payload.purchase_order_line_id is not None:
            order_line = order_lines.get(payload.purchase_order_line_id)
            if order_line is None:
                raise NotFoundError("Purchase order line", payload.purchase_order_line_id)
            if purchase_order_id is not None and order_line.purchase_order_id != purchase_order_id:
                raise BusinessRuleError(
                    f"Line {number} bills against a line of a different purchase order.",
                    details={
                        "line_number": number,
                        "purchase_order_line_id": str(payload.purchase_order_line_id),
                    },
                )
            if item is not None and order_line.item_id != item.id:
                raise BusinessRuleError(
                    f"Line {number} bills {item.code} against an order line for a different item.",
                    details={"line_number": number, "item_code": item.code},
                )

        if payload.goods_receipt_line_id is not None:
            receipt_line = receipt_lines.get(payload.goods_receipt_line_id)
            if receipt_line is None:
                raise NotFoundError("Goods receipt line", payload.goods_receipt_line_id)
            if receipt_line.purchase_order_line_id != payload.purchase_order_line_id:
                raise BusinessRuleError(
                    f"Line {number} names a goods receipt line that was received against a "
                    "different order line.",
                    details={"line_number": number},
                )

        pricing = price_invoice_line(
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            discount_amount=payload.discount_amount,
            tax_rate=payload.tax_rate,
            is_interstate=payload.is_interstate,
        )
        resolved.append(
            _ResolvedLine(
                payload=payload,
                line_number=number,
                hsn_code=payload.hsn_code or (item.hsn_code if item else None),
                description=payload.description or (item.name if item else None),
                pricing=pricing,
            )
        )
    return resolved


def _invoice_totals(lines: Sequence[_ResolvedLine], round_off: Decimal) -> tuple[Decimal, ...]:
    """Header totals from the priced lines. The CHECK on the row is the proof."""
    subtotal = to_money(sum((line.pricing.taxable_amount for line in lines), start=ZERO))
    tax = to_money(sum((line.pricing.tax_amount for line in lines), start=ZERO))
    return subtotal, tax, to_money(subtotal + tax + to_money(round_off))


async def _assert_invoice_cost_centres(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    header: uuid.UUID | None,
    lines: Sequence[VendorInvoiceLineIn] | None,
    already_on_document: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Validate every cost centre an invoice write names, in one statement.

    ``already_on_document`` is what the invoice carried before this write. A
    centre it already held is not a new allocation and is not re-checked: a
    document keeps its reference after the centre is deactivated, and saving an
    unrelated edit must not be refused for it. Approval is where an inactive
    centre is finally refused, because that is where money is allocated to it.
    """
    references: list[tuple[str, uuid.UUID]] = []
    if header is not None and header not in already_on_document:
        references.append(("cost_centre_id", header))
    for index, line in enumerate(lines or ()):
        if line.cost_centre_id is not None and line.cost_centre_id not in already_on_document:
            references.append((f"lines.{index}.cost_centre_id", line.cost_centre_id))
    if references:
        await require_cost_centres(session, tenant, references)


async def _assert_invoice_references(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor: PartyFacts,
    purchase_order_id: uuid.UUID | None,
    goods_receipt_id: uuid.UUID | None,
) -> None:
    """The order and the receipt must belong to the vendor being billed.

    Checked at entry rather than only at match time: an invoice pointing at
    another vendor's order is a data-entry error worth catching immediately, and
    leaving it until approval would let it sit in the payables queue looking
    legitimate.
    """
    if purchase_order_id is not None:
        order = await PurchaseOrderLookupRepository(session, tenant).get_facts(purchase_order_id)
        if order is None:
            raise NotFoundError("Purchase order", purchase_order_id)
        if order.vendor_id != vendor.id:
            raise BusinessRuleError(
                f"Purchase order {order.code} belongs to another vendor.",
                details={"purchase_order_id": str(purchase_order_id), "vendor": vendor.code},
            )

    if goods_receipt_id is not None:
        receipt = await GoodsReceiptLookupRepository(session, tenant).get_facts(goods_receipt_id)
        if receipt is None:
            raise NotFoundError("Goods receipt", goods_receipt_id)
        if receipt.vendor_id != vendor.id:
            raise BusinessRuleError(
                f"Goods receipt {receipt.code} belongs to another vendor.",
                details={"goods_receipt_id": str(goods_receipt_id), "vendor": vendor.code},
            )
        if purchase_order_id is not None and receipt.purchase_order_id != purchase_order_id:
            raise BusinessRuleError(
                f"Goods receipt {receipt.code} was received against a different purchase order.",
                details={"goods_receipt_id": str(goods_receipt_id)},
            )


async def _assert_lines_belong_to_order(
    session: AsyncSession, tenant: TenantContext, invoice: VendorInvoice
) -> None:
    """Every line's order-line link must sit on the invoice's own order.

    :func:`_resolve_invoice_lines` guarantees this whenever a line set is
    supplied. This is the same guarantee for the one path that does not go
    through it - repointing the header at a different purchase order and leaving
    the existing lines in place.
    """
    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    linked: list[tuple[int, uuid.UUID]] = [
        (line.line_number, line.purchase_order_line_id)
        for line in lines
        if line.purchase_order_line_id is not None
    ]
    if not linked:
        return
    if invoice.purchase_order_id is None:
        raise BusinessRuleError(
            f"{invoice.code} has lines billed against purchase order lines, so it cannot be "
            "unlinked from its purchase order. Resend the lines without those links instead.",
            details={"invoice_id": str(invoice.id), "line_count": len(linked)},
        )

    order_lines = await PurchaseOrderLineLookupRepository(session, tenant).load(
        [order_line_id for _, order_line_id in linked]
    )
    for line_number, order_line_id in linked:
        facts = order_lines.get(order_line_id)
        if facts is None or facts.purchase_order_id != invoice.purchase_order_id:
            raise BusinessRuleError(
                f"Line {line_number} is billed against a line of a different purchase order. "
                "Resend the invoice lines when moving it to another order.",
                details={
                    "line_number": line_number,
                    "purchase_order_id": str(invoice.purchase_order_id),
                    "purchase_order_line_id": str(order_line_id),
                },
            )


async def create_invoice(
    session: AsyncSession, tenant: TenantContext, payload: VendorInvoiceCreate
) -> VendorInvoiceDetail:
    """Record a bill in DRAFT. Nothing posts to the ledger until approval."""
    repo = VendorInvoiceRepository(session, tenant)
    line_repo = VendorInvoiceLineRepository(session, tenant)
    if tenant.vendor_id is not None and payload.vendor_id != tenant.vendor_id:
        raise NotFoundError("Vendor", payload.vendor_id)

    async with transaction(session):
        vendor = await VendorLookupRepository(session, tenant).get_facts(payload.vendor_id)
        if vendor is None:
            raise NotFoundError("Vendor", payload.vendor_id)

        duplicate = await repo.find_duplicate(
            payload.vendor_id, payload.vendor_invoice_number, exclude_id=None
        )
        if duplicate is not None:
            raise BusinessRuleError(
                f"{vendor.code} has already billed invoice number "
                f"{payload.vendor_invoice_number} - it was recorded as {duplicate.code}.",
                code=ErrorCode.DUPLICATE_DOCUMENT,
                status_code=409,
                details={
                    "existing_invoice_id": str(duplicate.id),
                    "existing_invoice_code": duplicate.code,
                },
            )

        await _assert_invoice_references(
            session,
            tenant,
            vendor=vendor,
            purchase_order_id=payload.purchase_order_id,
            goods_receipt_id=payload.goods_receipt_id,
        )
        resolved = await _resolve_invoice_lines(
            session, tenant, payload.lines, purchase_order_id=payload.purchase_order_id
        )
        await _assert_invoice_cost_centres(
            session, tenant, header=payload.cost_centre_id, lines=payload.lines
        )
        subtotal, tax, total = _invoice_totals(resolved, payload.round_off_amount)

        # ASSUMPTION: a bill that does not name a currency is denominated in the
        # company's own - see `company_currency`. Frozen onto the row here, so a
        # company that later changes its books' denomination does not restate
        # invoices it has already recorded.
        denomination = payload.currency or await company_currency(session, tenant)

        code = await next_document_number(session, tenant.company_id, DocTypes.VENDOR_INVOICE)
        invoice = repo.add(
            VendorInvoice(
                code=code,
                vendor_id=payload.vendor_id,
                vendor_invoice_number=payload.vendor_invoice_number,
                invoice_date=payload.invoice_date,
                due_date=_resolve_due_date(payload.due_date, payload.invoice_date, vendor),
                purchase_order_id=payload.purchase_order_id,
                goods_receipt_id=payload.goods_receipt_id,
                cost_centre_id=payload.cost_centre_id,
                currency=denomination,
                subtotal=subtotal,
                tax_amount=tax,
                round_off_amount=to_money(payload.round_off_amount),
                total_amount=total,
                status=VendorInvoiceStatus.DRAFT,
                notes=payload.notes,
            )
        )
        await session.flush()
        _persist_lines(line_repo, invoice.id, resolved)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_VENDOR_INVOICE,
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=(
                f"Recorded vendor invoice {invoice.code} ({vendor.code} "
                f"{invoice.vendor_invoice_number}) for {invoice.total_amount}"
            ),
            user_id=tenant.user_id,
            new_values=snapshot(invoice, INVOICE_AUDITED_FIELDS),
        )

    lines = await line_repo.list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


def _resolve_due_date(due_date: date | None, invoice_date: date, vendor: PartyFacts) -> date:
    """The vendor's agreed credit days, resolved once and then frozen."""
    if due_date is not None:
        return due_date
    return invoice_date + timedelta(days=max(0, vendor.payment_terms_days))


def _persist_lines(
    line_repo: VendorInvoiceLineRepository,
    invoice_id: uuid.UUID,
    resolved: Sequence[_ResolvedLine],
) -> None:
    for line in resolved:
        line_repo.add(
            VendorInvoiceLine(
                vendor_invoice_id=invoice_id,
                line_number=line.line_number,
                purchase_order_line_id=line.payload.purchase_order_line_id,
                goods_receipt_line_id=line.payload.goods_receipt_line_id,
                item_id=line.payload.item_id,
                cost_centre_id=line.payload.cost_centre_id,
                description=line.description,
                hsn_code=line.hsn_code,
                quantity=line.payload.quantity,
                uom_id=line.payload.uom_id,
                unit_price=line.payload.unit_price,
                discount_amount=to_money(line.payload.discount_amount),
                taxable_amount=line.pricing.taxable_amount,
                tax_rate=line.payload.tax_rate,
                cgst_amount=line.pricing.cgst_amount,
                sgst_amount=line.pricing.sgst_amount,
                igst_amount=line.pricing.igst_amount,
                tax_amount=line.pricing.tax_amount,
                line_total=line.pricing.line_total,
                notes=line.payload.notes,
            )
        )


async def update_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    invoice_id: uuid.UUID,
    payload: VendorInvoiceUpdate,
) -> VendorInvoiceDetail:
    """Amend an invoice that nobody has approved.

    Any edit invalidates a previous match: the figures it agreed with are no
    longer the figures on the document, so ``match_status`` returns to
    NOT_MATCHED and the invoice has to be matched again before approval.
    """
    repo = VendorInvoiceRepository(session, tenant)
    line_repo = VendorInvoiceLineRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.get_or_404(invoice_id)
        status = VendorInvoiceStatus(invoice.status)
        if status not in EDITABLE_INVOICE_STATUSES:
            raise BusinessRuleError(
                f"{invoice.code} is {status} and can no longer be edited. Dispute or cancel it "
                "instead.",
                details={"invoice_id": str(invoice.id), "status": str(status)},
            )

        before = snapshot(invoice, INVOICE_AUDITED_FIELDS)
        changes = payload.model_dump(exclude_unset=True)
        vendor = await VendorLookupRepository(session, tenant).get_facts(invoice.vendor_id)
        if vendor is None:
            raise NotFoundError("Vendor", invoice.vendor_id)

        if "vendor_invoice_number" in changes:
            duplicate = await repo.find_duplicate(
                invoice.vendor_id, changes["vendor_invoice_number"], exclude_id=invoice.id
            )
            if duplicate is not None:
                raise BusinessRuleError(
                    f"{vendor.code} has already billed invoice number "
                    f"{changes['vendor_invoice_number']} - recorded as {duplicate.code}.",
                    code=ErrorCode.DUPLICATE_DOCUMENT,
                    status_code=409,
                    details={"existing_invoice_code": duplicate.code},
                )
            invoice.vendor_invoice_number = changes["vendor_invoice_number"]

        for attribute in ("invoice_date", "due_date", "notes"):
            if attribute in changes and changes[attribute] is not None:
                setattr(invoice, attribute, changes[attribute])

        order_before = invoice.purchase_order_id
        if "purchase_order_id" in changes or "goods_receipt_id" in changes:
            invoice.purchase_order_id = changes.get("purchase_order_id", invoice.purchase_order_id)
            invoice.goods_receipt_id = changes.get("goods_receipt_id", invoice.goods_receipt_id)
            await _assert_invoice_references(
                session,
                tenant,
                vendor=vendor,
                purchase_order_id=invoice.purchase_order_id,
                goods_receipt_id=invoice.goods_receipt_id,
            )

        if invoice.due_date < invoice.invoice_date:
            raise ValidationError(date_before("due date", "invoice date"))

        header_centre_changing = (
            "cost_centre_id" in changes and changes["cost_centre_id"] != invoice.cost_centre_id
        )
        if header_centre_changing or payload.lines is not None:
            # What the invoice already carries is not a new allocation - see
            # _assert_invoice_cost_centres. Read before the lines are replaced.
            held = {invoice.cost_centre_id}
            if payload.lines is not None:
                held |= {
                    line.cost_centre_id for line in await line_repo.list_for_invoice(invoice.id)
                }
            await _assert_invoice_cost_centres(
                session,
                tenant,
                header=changes["cost_centre_id"] if header_centre_changing else None,
                lines=payload.lines,
                already_on_document=frozenset(
                    cost_centre_id for cost_centre_id in held if cost_centre_id is not None
                ),
            )
            if header_centre_changing:
                invoice.cost_centre_id = changes["cost_centre_id"]

        if payload.lines is not None:
            if not payload.lines:
                raise ValidationError("An invoice needs at least one line.")
            resolved = await _resolve_invoice_lines(
                session, tenant, payload.lines, purchase_order_id=invoice.purchase_order_id
            )
            await line_repo.delete_for_invoice(invoice.id)
            _persist_lines(line_repo, invoice.id, resolved)
            round_off = to_money(changes.get("round_off_amount", invoice.round_off_amount))
            invoice.subtotal, invoice.tax_amount, invoice.total_amount = _invoice_totals(
                resolved, round_off
            )
            invoice.round_off_amount = round_off
        elif "round_off_amount" in changes and changes["round_off_amount"] is not None:
            invoice.round_off_amount = to_money(changes["round_off_amount"])
            invoice.total_amount = to_money(
                invoice.subtotal + invoice.tax_amount + invoice.round_off_amount
            )

        if payload.lines is None and invoice.purchase_order_id != order_before:
            # Moving the header to another order without resending the lines
            # would leave every line still pointing at the previous order's
            # lines. The matcher reads the price from those links, so the
            # invoice would be priced against one order while claiming to belong
            # to another - and _resolve_invoice_lines, which is what normally
            # refuses that, only runs when the lines are supplied.
            await _assert_lines_belong_to_order(session, tenant, invoice)

        # The document has moved, so any previous verdict is stale.
        invoice.match_status = MatchStatus.NOT_MATCHED
        invoice.matched_at = None
        invoice.match_variance_amount = ZERO
        repo.touch(invoice)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_VENDOR_INVOICE,
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=f"Amended vendor invoice {invoice.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(invoice, INVOICE_AUDITED_FIELDS),
        )

    lines = await line_repo.list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


async def delete_draft_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a draft that was never used. Anything further along is cancelled."""
    repo = VendorInvoiceRepository(session, tenant)
    line_repo = VendorInvoiceLineRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.get_or_404(invoice_id)
        if VendorInvoiceStatus(invoice.status) is not VendorInvoiceStatus.DRAFT:
            raise BusinessRuleError(
                f"{invoice.code} is {invoice.status}; only a draft may be deleted. Cancel it "
                "instead so the trail survives.",
                details={"status": str(invoice.status)},
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_VENDOR_INVOICE,
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=f"Deleted draft vendor invoice {invoice.code}",
            user_id=tenant.user_id,
            old_values=snapshot(invoice, INVOICE_AUDITED_FIELDS),
            context={"reason": reason},
        )
        await line_repo.delete_for_invoice(invoice.id)
        await repo.hard_delete(invoice)


#: The bill outcomes a vendor's portal users are told about (WP C1a).
VENDOR_TOLD_INVOICE_STATUSES: frozenset[VendorInvoiceStatus] = frozenset(
    {VendorInvoiceStatus.APPROVED, VendorInvoiceStatus.DISPUTED}
)

#: The vendor app's bills page (WP C2a) - it has no per-bill route.
VENDOR_INVOICES_URL = "/vendor/invoices"
VENDOR_PAYMENTS_URL = "/vendor/payments"


async def _tell_vendor_about_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    invoice: VendorInvoice,
    target: VendorInvoiceStatus,
    reason: str | None,
) -> None:
    """Tell the vendor's invoice contacts their bill was approved or disputed.

    A dispute carries its reason - the vendor has to act on it. An approval does
    not: whatever the approver noted (an override of a price variance, say) is
    the buyer's own record, not a message to the supplier.
    """
    from src.core.permissions import Permission
    from src.modules.notifications.models import NotificationSeverity, NotificationType
    from src.modules.notifications.service import notify_vendor

    disputed = target is VendorInvoiceStatus.DISPUTED
    await notify_vendor(
        session,
        company_id=tenant.company_id,
        vendor_id=invoice.vendor_id,
        permission=Permission.VENDOR_PORTAL_INVOICE,
        notification_type=NotificationType.VENDOR_INVOICE_STATUS,
        severity=NotificationSeverity.WARNING if disputed else NotificationSeverity.INFO,
        title=(
            f"Invoice {invoice.vendor_invoice_number} "
            f"{'is disputed' if disputed else 'was approved'}"
        ),
        body=(
            f"Reason: {reason or 'not stated'}."
            if disputed
            else f"Recorded as {invoice.code}, due {invoice.due_date.isoformat()}."
        ),
        entity_type=AUDIT_VENDOR_INVOICE,
        entity_id=invoice.id,
        action_url=VENDOR_INVOICES_URL,
        group_key=f"vendor-invoice:{invoice.id}",
        actor_id=tenant.user_id,
    )


async def _tell_vendor_about_payment(
    session: AsyncSession, tenant: TenantContext, payment: Payment, *, title: str, body: str
) -> None:
    """Tell the paid vendor's invoice contacts, when the money is a payment *to* a vendor.

    A customer receipt shares this code path and its ``party_id`` is a customer
    id - which no vendor user is bound to, but which must not be looked up as a
    vendor either. One collapsing row per payment: approval, then allocation.
    """
    if payment.party_type != PartyType.VENDOR or payment.payment_type != PaymentType.PAYMENT:
        return
    from src.core.permissions import Permission
    from src.modules.notifications.models import NotificationType
    from src.modules.notifications.service import notify_vendor

    await notify_vendor(
        session,
        company_id=tenant.company_id,
        vendor_id=payment.party_id,
        permission=Permission.VENDOR_PORTAL_INVOICE,
        notification_type=NotificationType.VENDOR_PAYMENT_RECORDED,
        title=title,
        body=body,
        entity_type=AUDIT_PAYMENT,
        entity_id=payment.id,
        action_url=VENDOR_PAYMENTS_URL,
        group_key=f"vendor-payment:{payment.id}",
        actor_id=tenant.user_id,
    )


async def _transition_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    invoice: VendorInvoice,
    target: VendorInvoiceStatus,
    *,
    reason: str | None = None,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> None:
    """Move an invoice through its state machine and record the move.

    The only place ``vendor_invoices.status`` is written. An invalid edge raises
    :class:`~src.core.errors.InvalidStateTransitionError` before anything else
    happens, so the rejection rolls back with the rest of the operation.
    """
    current = VendorInvoiceStatus(invoice.status)
    if current is target:
        return
    VENDOR_INVOICE_LIFECYCLE.assert_transition(current, target)
    invoice.status = target
    await session.flush()
    if target in VENDOR_TOLD_INVOICE_STATUSES:
        await _tell_vendor_about_invoice(session, tenant, invoice, target, reason)
    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_VENDOR_INVOICE,
        entity_id=invoice.id,
        entity_code=invoice.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )


async def submit_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str | None
) -> VendorInvoiceDetail:
    """Send a draft to the approval queue, running the match on the way in.

    Matching here rather than only at approval means the variance is visible to
    the approver before they open the document, which is the difference between
    an approval queue and a rubber stamp.
    """
    repo = VendorInvoiceRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.get_or_404(invoice_id)
        await _transition_invoice(
            session, tenant, invoice, VendorInvoiceStatus.PENDING_APPROVAL, reason=reason
        )
        await _run_and_store_match(session, tenant, invoice)

    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


async def match_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> MatchResult:
    """Compare a vendor invoice against its purchase order and goods receipt.

    Quantity is matched against what was *accepted*, not what arrived: goods
    refused at the gate never entered the company's custody and must not be
    paid for (docs/assumptions.md section 18).

    ASSUMPTION: when the invoice names a goods receipt, that receipt's accepted
    quantity per order line is the basis. When it does not, the order line's
    *cumulative* accepted quantity is used instead - which is the right basis
    for a consolidated monthly bill covering several deliveries, and the only
    one available when the receipt is not identified.
    """
    repo = VendorInvoiceRepository(session, tenant)
    invoice = await repo.get_or_404(invoice_id)
    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return await _match(session, tenant, invoice, lines)


async def _match(
    session: AsyncSession,
    tenant: TenantContext,
    invoice: VendorInvoice,
    lines: Sequence[VendorInvoiceLine],
) -> MatchResult:
    order_line_ids = [
        line.purchase_order_line_id for line in lines if line.purchase_order_line_id is not None
    ]
    order_lines = await PurchaseOrderLineLookupRepository(session, tenant).load(order_line_ids)

    accepted_by_order_line: dict[uuid.UUID, Decimal] = {}
    if invoice.goods_receipt_id is not None:
        receipt_lines = await GoodsReceiptLineLookupRepository(session, tenant).load_for_receipt(
            invoice.goods_receipt_id
        )
        accepted_by_order_line = {
            order_line_id: facts.accepted_quantity for order_line_id, facts in receipt_lines.items()
        }
    elif invoice.purchase_order_id is not None:
        # Which basis was used is worth knowing when a variance is later
        # queried, and it is not a finding in itself - see the docstring.
        log.info(
            "finance.match_basis",
            invoice_code=invoice.code,
            basis="purchase_order_cumulative_accepted",
        )

    item_ids = [line.item_id for line in lines if line.item_id is not None]
    items = await ItemLookupRepository(session, tenant).load(item_ids)

    inputs = [
        MatchLineInput(
            line_number=line.line_number,
            invoice_line_id=line.id,
            item_id=line.item_id,
            item_code=items[line.item_id].code if line.item_id in items else None,
            description=line.description,
            invoiced_quantity=Decimal(line.quantity),
            invoiced_unit_price=_net_unit_price(
                Decimal(line.taxable_amount), Decimal(line.quantity)
            ),
            invoiced_amount=Decimal(line.taxable_amount),
            purchase_order_line_id=line.purchase_order_line_id,
            ordered_quantity=_ordered_quantity(order_lines, line.purchase_order_line_id),
            ordered_unit_price=_ordered_net_price(order_lines, line.purchase_order_line_id),
            accepted_quantity=_accepted_quantity(
                order_lines, accepted_by_order_line, line.purchase_order_line_id
            ),
        )
        for line in lines
    ]

    return evaluate_match(
        inputs,
        invoice_id=invoice.id,
        invoice_code=invoice.code,
        vendor_id=invoice.vendor_id,
        purchase_order_id=invoice.purchase_order_id,
        goods_receipt_id=invoice.goods_receipt_id,
    )


def _net_unit_price(taxable_amount: Decimal, quantity: Decimal) -> Decimal:
    """Price actually charged per unit, after discount and before tax."""
    if quantity == ZERO:
        return ZERO
    return to_unit_price(taxable_amount / quantity)


def _ordered_quantity(
    order_lines: dict[uuid.UUID, PurchaseOrderLineFacts], line_id: uuid.UUID | None
) -> Decimal | None:
    facts = order_lines.get(line_id) if line_id is not None else None
    return facts.quantity if facts is not None else None


def _ordered_net_price(
    order_lines: dict[uuid.UUID, PurchaseOrderLineFacts], line_id: uuid.UUID | None
) -> Decimal | None:
    """The agreed price per unit, with the order's line discount applied.

    Comparing gross prices would let a vendor bill the list price and omit the
    discount they agreed to, which is an overbill the match exists to catch.
    """
    facts = order_lines.get(line_id) if line_id is not None else None
    if facts is None or facts.quantity == ZERO:
        return None
    net = (facts.quantity * facts.unit_price) - facts.discount
    return to_unit_price(net / facts.quantity)


def _accepted_quantity(
    order_lines: dict[uuid.UUID, PurchaseOrderLineFacts],
    accepted_by_order_line: dict[uuid.UUID, Decimal],
    line_id: uuid.UUID | None,
) -> Decimal | None:
    if line_id is None:
        return None
    if accepted_by_order_line:
        return accepted_by_order_line.get(line_id)
    facts = order_lines.get(line_id)
    return facts.accepted_quantity if facts is not None else None


async def _run_and_store_match(
    session: AsyncSession, tenant: TenantContext, invoice: VendorInvoice
) -> MatchResult:
    """Match and record the verdict on the invoice, inside the caller's transaction."""
    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    result = await _match(session, tenant, invoice, lines)
    invoice.match_status = result.match_status
    invoice.matched_at = utcnow()
    invoice.match_variance_amount = result.worst_variance
    await session.flush()
    return result


def assert_may_override_variance(principal: Principal, *, invoice_code: str) -> None:
    """Overriding a three-way-match variance is the approver's right alone.

    The route already requires ``vendor_invoice.approve``; this is the same check
    at the service boundary, so a caller that reaches :func:`approve_invoice` by
    any other path cannot wave a variance through. Raised as the denial every
    other permission check produces - 403, ``PERMISSION_DENIED``, and the
    required right in ``details`` spelled from the enum - so a client can say
    which permission to ask for, and so the demo seeder's ``except
    BusinessRuleError`` around a walked-through approval does not swallow it.
    """
    if principal.has(Permission.VENDOR_INVOICE_APPROVE):
        return
    raise AuthorizationError(
        f"Overriding a match variance on {invoice_code} requires "
        f"{Permission.VENDOR_INVOICE_APPROVE}.",
        code=ErrorCode.PERMISSION_DENIED,
        details={"required_permission": str(Permission.VENDOR_INVOICE_APPROVE)},
    )


async def approve_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    invoice_id: uuid.UUID,
    payload: InvoiceApproval,
) -> VendorInvoiceDetail:
    """Approve a bill for payment, and post the payable.

    The match is re-run here rather than trusting whatever was stored: the order
    or the receipt may have moved since the invoice was submitted, and approving
    against a stale verdict is exactly the failure the match exists to prevent.

    An invoice outside tolerance is refused unless ``override_reason`` is
    supplied. The endpoint already requires ``vendor_invoice.approve``, so
    supplying it *is* the exercise of that authority; the reason is stored on
    the invoice and written to the audit log so the decision has a name and a
    justification against it (master rules section 42).
    """
    repo = VendorInvoiceRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.lock_for_update(invoice_id)
        if invoice is None:
            raise NotFoundError("Vendor invoice", invoice_id)

        current = VendorInvoiceStatus(invoice.status)
        if current is not VendorInvoiceStatus.PENDING_APPROVAL:
            raise BusinessRuleError(
                f"{invoice.code} is {current}; only an invoice awaiting approval can be approved.",
                details={"status": str(current)},
            )

        result = await _run_and_store_match(session, tenant, invoice)
        if not result.matched:
            if payload.override_reason is None:
                raise MatchVarianceError(result)
            # Belt and braces alongside the route's dependency: a service
            # called from anywhere else must not be able to override.
            assert_may_override_variance(principal, invoice_code=invoice.code)
            invoice.match_status = MatchStatus.OVERRIDDEN
            invoice.variance_override_reason = payload.override_reason

        invoice.approved_by_id = principal.user_id
        invoice.approved_at = utcnow()
        repo.touch(invoice)
        await _transition_invoice(
            session,
            tenant,
            invoice,
            VendorInvoiceStatus.APPROVED,
            reason=payload.override_reason,
            action=AuditAction.APPROVE,
        )

        await _post_invoice_journal(session, tenant, invoice)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.APPROVE,
            entity_type=AUDIT_VENDOR_INVOICE,
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=(
                f"Approved vendor invoice {invoice.code} for {invoice.total_amount}"
                + (" over a match variance" if not result.matched else "")
            ),
            user_id=principal.user_id,
            new_values={
                "status": invoice.status,
                "match_status": invoice.match_status,
                "total_amount": invoice.total_amount,
                "worst_variance": result.worst_variance,
            },
            context={
                "override_reason": payload.override_reason,
                "blocking_reasons": result.blocking_reasons or None,
            },
        )

    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


async def _post_invoice_journal(
    session: AsyncSession, tenant: TenantContext, invoice: VendorInvoice
) -> list[LedgerEntry]:
    """Dr purchases (one leg per cost centre), Dr input tax, Cr the vendor's payable.

    The round-off goes to its own account on whichever side it falls, so the
    journal balances against a total that the invoice's own CHECK already
    guarantees is ``subtotal + tax + round_off``.

    The purchase expense is split by each line's *effective* cost centre: the
    line's own, else the invoice's, else none. One leg per centre, in the order
    the lines first name them, summing to the subtotal exactly
    (:func:`split_by_cost_centre`). An invoice that names no centre anywhere
    posts the single unallocated leg it always did. The tax and payable legs
    never carry one - they are balance-sheet accounts (§44).
    """
    accounts = await ensure_system_accounts(session, tenant)
    invoice_lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    # ASSUMPTION: a line's centre is its own, else the invoice's, else none -
    # resolved here, at approval, so a header-only edit moves every line that
    # names no centre of its own; and none means unallocated, never a default
    # centre. docs/assumptions.md §41 and §42.
    expense = split_by_cost_centre(
        [
            (line.cost_centre_id or invoice.cost_centre_id, Decimal(line.taxable_amount))
            for line in invoice_lines
        ],
        total=invoice.subtotal,
    )
    lines = [
        JournalLine(
            account_id=accounts[str(SystemAccount.PURCHASE_EXPENSE)].id,
            debit=amount,
            narration=f"Purchases on {invoice.code}",
            cost_centre_id=cost_centre_id,
        )
        for cost_centre_id, amount in expense
    ]
    if to_money(invoice.tax_amount) > ZERO:
        lines.append(
            JournalLine(
                account_id=accounts[str(SystemAccount.INPUT_TAX)].id,
                debit=invoice.tax_amount,
                narration=f"Input GST on {invoice.code}",
            )
        )
    round_off = to_money(invoice.round_off_amount)
    if round_off != ZERO:
        lines.append(
            JournalLine(
                account_id=accounts[str(SystemAccount.ROUND_OFF)].id,
                debit=round_off if round_off > ZERO else ZERO,
                credit=-round_off if round_off < ZERO else ZERO,
                narration=f"Rounding on {invoice.code}",
            )
        )
    lines.append(
        JournalLine(
            account_id=accounts[str(SystemAccount.ACCOUNTS_PAYABLE)].id,
            credit=invoice.total_amount,
            party_type=PartyType.VENDOR,
            party_id=invoice.vendor_id,
            narration=f"Payable for {invoice.code}",
        )
    )

    return await post_journal(
        session,
        tenant,
        lines,
        entry_date=invoice.invoice_date,
        reference_type=REFERENCE_VENDOR_INVOICE,
        reference_id=invoice.id,
        reference_code=invoice.code,
        narration=f"Vendor invoice {invoice.code}",
        currency=invoice.currency,
    )


async def dispute_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str
) -> VendorInvoiceDetail:
    """Hold a bill while it is argued with the vendor.

    The money stays owed - a dispute is not a write-off - so the invoice keeps
    its outstanding amount and stays on the ageing report.
    """
    repo = VendorInvoiceRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.get_or_404(invoice_id)
        invoice.dispute_reason = reason
        repo.touch(invoice)
        await _transition_invoice(
            session, tenant, invoice, VendorInvoiceStatus.DISPUTED, reason=reason
        )

    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


async def resolve_dispute(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str
) -> VendorInvoiceDetail:
    """Settle a dispute and return the invoice to the payment run."""
    repo = VendorInvoiceRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.get_or_404(invoice_id)
        await _transition_invoice(
            session, tenant, invoice, VendorInvoiceStatus.APPROVED, reason=reason
        )
        # The status the money implies wins: a partly-paid invoice returning
        # from dispute is PARTIALLY_PAID, not APPROVED.
        settled = invoice_status_after_payment(
            VendorInvoiceStatus.APPROVED,
            total_amount=invoice.total_amount,
            paid_amount=invoice.paid_amount,
        )
        await _transition_invoice(session, tenant, invoice, settled, reason=reason)
        invoice.dispute_reason = None
        repo.touch(invoice)
        await session.flush()

    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


async def cancel_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str
) -> VendorInvoiceDetail:
    """Call off a bill that will never be paid.

    Refused once anything has been allocated against it: money that has moved
    cannot be un-billed, and the correction is a reversal, not a cancellation.
    """
    repo = VendorInvoiceRepository(session, tenant)

    async with transaction(session):
        invoice = await repo.lock_for_update(invoice_id)
        if invoice is None:
            raise NotFoundError("Vendor invoice", invoice_id)
        if to_money(invoice.paid_amount) > ZERO:
            raise BusinessRuleError(
                f"{invoice.code} has {invoice.paid_amount} allocated against it and cannot be "
                "cancelled. Reverse the allocation first.",
                details={"paid_amount": str(invoice.paid_amount)},
            )
        invoice.cancellation_reason = reason
        repo.touch(invoice)
        await _transition_invoice(
            session,
            tenant,
            invoice,
            VendorInvoiceStatus.CANCELLED,
            reason=reason,
            action=AuditAction.CANCEL,
        )

    lines = await VendorInvoiceLineRepository(session, tenant).list_for_invoice(invoice.id)
    return invoice_detail(invoice, lines)


# -----------------------------------------------------------------------------
# Payments
# -----------------------------------------------------------------------------


def payment_summary(payment: Payment) -> PaymentSummary:
    return PaymentSummary(
        id=payment.id,
        code=payment.code,
        payment_type=PaymentType(payment.payment_type),
        party_type=PartyType(payment.party_type),
        party_id=payment.party_id,
        payment_date=payment.payment_date,
        amount=payment.amount,
        allocated_amount=payment.allocated_amount,
        unallocated_amount=to_money(payment.amount - payment.allocated_amount),
        currency=payment.currency,
        payment_method=PaymentMethod(payment.payment_method),
        reference_number=payment.reference_number,
        status=PaymentStatus(payment.status),
        created_at=payment.created_at,
    )


def payment_detail(
    payment: Payment,
    allocations: Sequence[PaymentAllocation],
    invoice_codes: dict[uuid.UUID, tuple[str, str]],
) -> PaymentDetail:
    return PaymentDetail(
        **payment_summary(payment).model_dump(),
        bank_name=payment.bank_name,
        instrument_date=payment.instrument_date,
        approved_by_id=payment.approved_by_id,
        approved_at=payment.approved_at,
        cancellation_reason=payment.cancellation_reason,
        notes=payment.notes,
        version=payment.version,
        allocations=[
            PaymentAllocationRead(
                id=allocation.id,
                vendor_invoice_id=allocation.vendor_invoice_id,
                invoice_code=invoice_codes.get(allocation.vendor_invoice_id, (None, None))[0],
                vendor_invoice_number=invoice_codes.get(allocation.vendor_invoice_id, (None, None))[
                    1
                ],
                amount=allocation.amount,
                notes=allocation.notes,
            )
            for allocation in allocations
        ],
    )


async def list_payments(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: PaymentFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[PaymentSummary], OffsetPagination]:
    repo = PaymentRepository(session, tenant)
    rows, pagination = await repo.list_page(params, stmt=repo.filtered(filters), sort=sort)
    return [payment_summary(row) for row in rows], pagination


async def get_payment_detail(
    session: AsyncSession, tenant: TenantContext, payment_id: uuid.UUID
) -> PaymentDetail:
    payment = await PaymentRepository(session, tenant).get_or_404(payment_id)
    return await _payment_detail(session, tenant, payment)


async def _payment_detail(
    session: AsyncSession, tenant: TenantContext, payment: Payment
) -> PaymentDetail:
    allocation_repo = PaymentAllocationRepository(session, tenant)
    allocations = await allocation_repo.list_for_payment(payment.id)
    codes = await allocation_repo.invoice_codes(
        [allocation.vendor_invoice_id for allocation in allocations]
    )
    return payment_detail(payment, allocations, codes)


async def create_payment(
    session: AsyncSession, tenant: TenantContext, payload: PaymentCreate
) -> PaymentDetail:
    """Record a payment or receipt in DRAFT. Nothing posts until approval."""
    repo = PaymentRepository(session, tenant)

    async with transaction(session):
        await _party_facts(session, tenant, payload.party_type, payload.party_id)

        # ASSUMPTION: as for an invoice, money that does not name a currency is
        # in the company's own. The two must default the same way or an
        # allocation would refuse the pair on a denomination nobody chose.
        denomination = payload.currency or await company_currency(session, tenant)

        code = await next_document_number(session, tenant.company_id, DocTypes.PAYMENT)
        payment = repo.add(
            Payment(
                code=code,
                payment_type=payload.payment_type,
                party_type=payload.party_type,
                party_id=payload.party_id,
                payment_date=payload.payment_date,
                amount=to_money(payload.amount),
                allocated_amount=ZERO,
                currency=denomination,
                payment_method=payload.payment_method,
                reference_number=payload.reference_number,
                bank_name=payload.bank_name,
                instrument_date=payload.instrument_date,
                status=PaymentStatus.DRAFT,
                notes=payload.notes,
            )
        )
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_PAYMENT,
            entity_id=payment.id,
            entity_code=payment.code,
            summary=(
                f"Recorded {payment.payment_type} {payment.code} of {payment.amount} "
                f"by {payment.payment_method}"
            ),
            user_id=tenant.user_id,
            new_values=snapshot(payment, PAYMENT_AUDITED_FIELDS),
        )

    return await _payment_detail(session, tenant, payment)


async def update_payment(
    session: AsyncSession,
    tenant: TenantContext,
    payment_id: uuid.UUID,
    payload: PaymentUpdate,
) -> PaymentDetail:
    """Amend a payment nobody has approved. After approval the money has moved."""
    repo = PaymentRepository(session, tenant)

    async with transaction(session):
        payment = await repo.get_or_404(payment_id)
        if PaymentStatus(payment.status) is not PaymentStatus.DRAFT:
            raise BusinessRuleError(
                f"{payment.code} is {payment.status}; an approved payment has already moved "
                "money and can only be cancelled and reversed.",
                details={"status": str(payment.status)},
            )

        before = snapshot(payment, PAYMENT_AUDITED_FIELDS)
        for attribute, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(payment, attribute, to_money(value) if attribute == "amount" else value)
        repo.touch(payment)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PAYMENT,
            entity_id=payment.id,
            entity_code=payment.code,
            summary=f"Amended payment {payment.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(payment, PAYMENT_AUDITED_FIELDS),
        )

    return await _payment_detail(session, tenant, payment)


async def delete_draft_payment(
    session: AsyncSession, tenant: TenantContext, payment_id: uuid.UUID, *, reason: str
) -> None:
    repo = PaymentRepository(session, tenant)

    async with transaction(session):
        payment = await repo.get_or_404(payment_id)
        if PaymentStatus(payment.status) is not PaymentStatus.DRAFT:
            raise BusinessRuleError(
                f"{payment.code} is {payment.status}; only a draft may be deleted.",
                details={"status": str(payment.status)},
            )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_PAYMENT,
            entity_id=payment.id,
            entity_code=payment.code,
            summary=f"Deleted draft payment {payment.code}",
            user_id=tenant.user_id,
            old_values=snapshot(payment, PAYMENT_AUDITED_FIELDS),
            context={"reason": reason},
        )
        await repo.hard_delete(payment)


async def approve_payment(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payment_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> PaymentDetail:
    """Approve a payment: the money moves, and the ledger moves with it.

    A vendor payment debits the payable and credits the bank. A customer receipt
    debits the bank and credits the receivable, and additionally reduces the
    customer's cached outstanding balance through the customers module's own
    :func:`~src.modules.customers.service.apply_outstanding_delta` - inside this
    same transaction, so the cache and the ledger can never disagree.

    Only a DRAFT payment can be approved, and that is checked against the status
    read *under the row lock*. The state machine alone is not enough: it carries
    APPROVED -> PARTIALLY_ALLOCATED -> APPROVED edges so allocation can walk the
    status back, and :func:`_transition_payment` treats a move to the status the
    document already holds as a no-op. Without this guard a second approval - a
    double-clicked button, a retried request, or two requests that serialised on
    the lock - would fall straight through to the posting below and credit the
    bank twice for money that moved once.
    """
    repo = PaymentRepository(session, tenant)

    async with transaction(session):
        payment = await repo.lock_for_update(payment_id)
        if payment is None:
            raise NotFoundError("Payment", payment_id)

        current = PaymentStatus(payment.status)
        if current is not PaymentStatus.DRAFT:
            raise BusinessRuleError(
                f"{payment.code} is {current}; only a draft payment can be approved. "
                "Approving it again would post the settlement a second time.",
                details={"payment_id": str(payment.id), "status": str(current)},
            )

        payment.approved_by_id = principal.user_id
        payment.approved_at = utcnow()
        repo.touch(payment)
        await _transition_payment(
            session,
            tenant,
            payment,
            PaymentStatus.APPROVED,
            reason=reason,
            action=AuditAction.PAYMENT,
        )

        await _post_payment_journal(session, tenant, payment)
        await _tell_vendor_about_payment(
            session,
            tenant,
            payment,
            title=f"Payment {payment.code} approved",
            body=(
                f"{payment.currency} {to_money(payment.amount)} approved for payment"
                + (f" (ref {payment.reference_number})." if payment.reference_number else ".")
            ),
        )

        if PaymentType(payment.payment_type) is PaymentType.RECEIPT:
            await apply_outstanding_delta(
                session,
                tenant,
                payment.party_id,
                OutstandingAdjustment(
                    delta=-to_money(payment.amount),
                    reason=f"Receipt {payment.code}",
                    reference_type=REFERENCE_PAYMENT,
                    reference_id=payment.id,
                    reference_code=payment.code,
                ),
            )

    return await _payment_detail(session, tenant, payment)


async def _post_payment_journal(
    session: AsyncSession, tenant: TenantContext, payment: Payment
) -> list[LedgerEntry]:
    accounts = await ensure_system_accounts(session, tenant)
    method = PaymentMethod(payment.payment_method)
    # ASSUMPTION: a cash payment moves the cash account, everything else moves
    # the bank account, and an ADJUSTMENT - a set-off or write-off with no cash
    # movement - is also posted through the bank account because the schema
    # models no suspense account. Recorded in the returned notes.
    money_account = (
        accounts[str(SystemAccount.CASH)]
        if method is PaymentMethod.CASH
        else accounts[str(SystemAccount.BANK)]
    )
    amount = to_money(payment.amount)

    if PaymentType(payment.payment_type) is PaymentType.PAYMENT:
        lines = [
            JournalLine(
                account_id=accounts[str(SystemAccount.ACCOUNTS_PAYABLE)].id,
                debit=amount,
                party_type=PartyType.VENDOR,
                party_id=payment.party_id,
                narration=f"Payment {payment.code}",
            ),
            JournalLine(
                account_id=money_account.id,
                credit=amount,
                narration=f"Payment {payment.code} by {payment.payment_method}",
            ),
        ]
    else:
        lines = [
            JournalLine(
                account_id=money_account.id,
                debit=amount,
                narration=f"Receipt {payment.code} by {payment.payment_method}",
            ),
            JournalLine(
                account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                credit=amount,
                party_type=PartyType.CUSTOMER,
                party_id=payment.party_id,
                narration=f"Receipt {payment.code}",
            ),
        ]

    return await post_journal(
        session,
        tenant,
        lines,
        entry_date=payment.payment_date,
        reference_type=REFERENCE_PAYMENT,
        reference_id=payment.id,
        reference_code=payment.code,
        narration=f"{payment.payment_type} {payment.code}",
        currency=payment.currency,
    )


async def cancel_payment(
    session: AsyncSession, tenant: TenantContext, payment_id: uuid.UUID, *, reason: str
) -> PaymentDetail:
    """Call off a payment, reversing its posting if it was already approved.

    Refused while allocations stand: un-settling the invoices first is a
    deliberate act, not a side effect of cancelling the payment.

    Cancelling an already-cancelled payment is refused rather than treated as a
    no-op. :func:`_transition_payment` would accept it silently - the document
    already holds the target status - and the reversal work below would then run
    a second time against a document that has nothing left to undo, pushing the
    receipt's amount back onto the customer's outstanding balance again.
    """
    repo = PaymentRepository(session, tenant)
    entry_repo = LedgerEntryRepository(session, tenant)

    async with transaction(session):
        payment = await repo.lock_for_update(payment_id)
        if payment is None:
            raise NotFoundError("Payment", payment_id)

        current = PaymentStatus(payment.status)
        if current is PaymentStatus.CANCELLED:
            raise BusinessRuleError(
                f"{payment.code} is already cancelled.",
                details={"payment_id": str(payment.id), "status": str(current)},
            )
        if to_money(payment.allocated_amount) > ZERO:
            raise BusinessRuleError(
                f"{payment.code} still has {payment.allocated_amount} allocated to invoices. "
                "Release the allocation before cancelling it.",
                details={"allocated_amount": str(payment.allocated_amount)},
            )

        # Only a payment that actually reached the ledger has anything to undo.
        # "Not a draft" is not the same question, and answers it wrongly for a
        # document that is already cancelled.
        was_posted = current in POSTED_PAYMENT_STATUSES
        payment.cancellation_reason = reason
        repo.touch(payment)
        await _transition_payment(
            session,
            tenant,
            payment,
            PaymentStatus.CANCELLED,
            reason=reason,
            action=AuditAction.CANCEL,
        )

        if was_posted:
            posted = await entry_repo.entries_for_reference(REFERENCE_PAYMENT, payment.id)
            already_reversed = {
                entry.reverses_entry_id for entry in posted if entry.reverses_entry_id
            }
            originals: dict[uuid.UUID, list[LedgerEntry]] = {}
            for entry in posted:
                if not entry.is_reversal:
                    originals.setdefault(entry.journal_id, []).append(entry)

            for journal_id in sorted(originals, key=str):
                # Skip a journal somebody has already reversed: reversing it
                # twice would restate the payment rather than undo it.
                if all(entry.id in already_reversed for entry in originals[journal_id]):
                    continue
                await reverse_journal(
                    session, tenant, journal_id, reason=f"{payment.code} cancelled: {reason}"
                )

            if PaymentType(payment.payment_type) is PaymentType.RECEIPT:
                await apply_outstanding_delta(
                    session,
                    tenant,
                    payment.party_id,
                    OutstandingAdjustment(
                        delta=to_money(payment.amount),
                        reason=f"Receipt {payment.code} cancelled",
                        reference_type=REFERENCE_PAYMENT,
                        reference_id=payment.id,
                        reference_code=payment.code,
                    ),
                )

    return await _payment_detail(session, tenant, payment)


async def _transition_payment(
    session: AsyncSession,
    tenant: TenantContext,
    payment: Payment,
    target: PaymentStatus,
    *,
    reason: str | None = None,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> None:
    """The only place ``payments.status`` is written."""
    current = PaymentStatus(payment.status)
    if current is target:
        return
    PAYMENT_LIFECYCLE.assert_transition(current, target)
    payment.status = target
    await session.flush()
    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_PAYMENT,
        entity_id=payment.id,
        entity_code=payment.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """What one allocation request works out to, before anything is written.

    ``final`` is the complete allocation set the payment ends up with, not a
    delta. Reasoning about "what will this payment have settled when I am
    finished" is what makes the two bounds - the payment's worth and each
    invoice's outstanding - checkable in one place before a single row moves.
    """

    final: dict[uuid.UUID, Decimal]
    added: dict[uuid.UUID, Decimal]
    removed: dict[uuid.UUID, Decimal]

    @property
    def total(self) -> Decimal:
        return to_money(sum(self.final.values(), start=ZERO))


def plan_allocation(
    *,
    existing: dict[uuid.UUID, Decimal],
    requested: dict[uuid.UUID, Decimal],
    replace: bool,
) -> AllocationPlan:
    """Work out the resulting allocation set. Pure, and therefore unit tested.

    ``replace`` discards everything the payment previously settled. Otherwise a
    request naming an invoice this payment already settled *amends* that
    allocation rather than adding a second one - which is the same thing the
    unique ``(payment, invoice)`` constraint on the table says.

    The bound is on the *resulting* set, not on the request. One request is
    capped at :data:`MAX_PAYMENT_ALLOCATIONS` by the schema, but an additive
    request adds to what the payment already carries, so repeated calls would
    otherwise grow one payment's allocation set - and the query that reads it
    back - without any limit at all (section 31).
    """
    final: dict[uuid.UUID, Decimal] = {} if replace else dict(existing)
    removed = dict(existing) if replace else {}
    final.update(requested)
    if len(final) > MAX_PAYMENT_ALLOCATIONS:
        raise ValidationError(
            f"A payment may settle at most {MAX_PAYMENT_ALLOCATIONS} invoices; this request "
            f"would leave it settling {len(final)}.",
            details={"allocation_count": len(final), "maximum": MAX_PAYMENT_ALLOCATIONS},
        )
    return AllocationPlan(final=final, added=dict(requested), removed=removed)


async def allocate_payment(
    session: AsyncSession,
    tenant: TenantContext,
    payment_id: uuid.UUID,
    payload: AllocationRequest,
) -> PaymentDetail:
    """Settle vendor invoices from an approved payment.

    Two bounds, both checked against values read *under a row lock*:

    * an allocation may not exceed what the payment is worth, and
    * an allocation may not exceed what the invoice still owes.

    The payment header and every affected invoice are locked, the invoices in
    primary-key order so two allocations touching the same pair cannot deadlock
    (database.md section 4.4). Both totals are read after the lock is held,
    because a value read before it is worthless.
    """
    payment_repo = PaymentRepository(session, tenant)
    invoice_repo = VendorInvoiceRepository(session, tenant)
    allocation_repo = PaymentAllocationRepository(session, tenant)

    async with transaction(session):
        payment = await payment_repo.lock_for_update(payment_id)
        if payment is None:
            raise NotFoundError("Payment", payment_id)

        status = PaymentStatus(payment.status)
        if status not in ALLOCATABLE_PAYMENT_STATUSES:
            raise BusinessRuleError(
                f"{payment.code} is {status}; only an approved payment can settle invoices.",
                details={"status": str(status)},
            )
        if PaymentType(payment.payment_type) is not PaymentType.PAYMENT:
            raise BusinessRuleError(
                "A customer receipt has no invoice to allocate against: it reduces the "
                "customer's outstanding balance directly.",
                details={"payment_type": str(payment.payment_type)},
            )

        existing_rows = await allocation_repo.list_for_payment(payment.id)
        existing_by_invoice = {row.vendor_invoice_id: row for row in existing_rows}
        existing_amounts = {
            invoice_id: to_money(row.amount) for invoice_id, row in existing_by_invoice.items()
        }
        requested = {
            request.vendor_invoice_id: to_money(request.amount) for request in payload.allocations
        }
        requested_notes = {
            request.vendor_invoice_id: request.notes for request in payload.allocations
        }
        plan = plan_allocation(
            existing=existing_amounts, requested=requested, replace=payload.replace
        )

        affected = set(plan.final) | set(plan.removed)
        invoices = {row.id: row for row in await invoice_repo.lock_many(sorted(affected, key=str))}
        missing = affected - set(invoices)
        if missing:
            raise NotFoundError("Vendor invoice", ", ".join(sorted(str(item) for item in missing)))

        # Bound 1: the invoice. Its outstanding is read after the lock, with
        # whatever this payment had already put against it added back, because
        # an amendment replaces that amount rather than stacking on it.
        for invoice_id, amount in plan.added.items():
            invoice = invoices[invoice_id]
            _assert_allocatable(payment, invoice, amending=invoice_id in existing_amounts)
            # Whatever this payment already put against the invoice is added
            # back whichever way the request came in: an amendment replaces that
            # amount and a replace removes it outright, so in both cases it is
            # about to stop counting against the invoice's outstanding.
            already = existing_amounts.get(invoice_id, ZERO)
            outstanding = to_money(invoice.total_amount - invoice.paid_amount) + already
            if amount > outstanding:
                raise BusinessRuleError(
                    f"{invoice.code} owes {outstanding}; {amount} cannot be allocated to it.",
                    details={
                        "invoice_code": invoice.code,
                        "outstanding_amount": str(outstanding),
                        "requested_amount": str(amount),
                    },
                )

        # Bound 2: the payment. What left the bank cannot settle more than
        # itself, whatever the invoices could absorb.
        new_allocated = plan.total
        if new_allocated > to_money(payment.amount):
            raise BusinessRuleError(
                f"{payment.code} is worth {payment.amount}; {new_allocated} cannot be "
                "allocated from it.",
                details={
                    "payment_amount": str(payment.amount),
                    "requested_allocation": str(new_allocated),
                },
            )

        await _apply_allocation_plan(
            session,
            tenant,
            payment=payment,
            invoices=invoices,
            existing_by_invoice=existing_by_invoice,
            plan=plan,
            notes=requested_notes,
            replace=payload.replace,
        )

        payment.allocated_amount = new_allocated
        payment_repo.touch(payment)
        await _transition_payment(
            session,
            tenant,
            payment,
            payment_status_after_allocation(amount=payment.amount, allocated_amount=new_allocated),
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.PAYMENT,
            entity_type=AUDIT_PAYMENT,
            entity_id=payment.id,
            entity_code=payment.code,
            summary=(
                f"Allocated {new_allocated} of {payment.code} across {len(plan.final)} invoice(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                "allocated_amount": new_allocated,
                "allocations": {
                    str(invoice_id): str(amount) for invoice_id, amount in plan.final.items()
                },
            },
            context={"replace": payload.replace},
        )
        await _tell_vendor_about_payment(
            session,
            tenant,
            payment,
            title=f"Payment {payment.code} allocated to your invoices",
            body=(
                f"{payment.currency} {to_money(new_allocated)} allocated across "
                f"{len(plan.final)} invoice(s)."
            ),
        )

    return await _payment_detail(session, tenant, payment)


def _assert_allocatable(
    payment: Payment, invoice: VendorInvoice, *, amending: bool = False
) -> None:
    """The invoice must be the same vendor's, in the same currency, and open.

    ``amending`` says this payment already has an allocation against the
    invoice, in which case PAID is accepted as well. An invoice this payment
    settled in full is PAID *because of the very allocation being corrected*,
    and refusing it would leave a mis-keyed figure with no way back. Nothing is
    weakened by it: the outstanding bound in :func:`allocate_payment` is
    computed with the old amount added back, so the new figure can still only be
    as large as the invoice can absorb once that amount is taken away.
    """
    if invoice.vendor_id != payment.party_id:
        raise BusinessRuleError(
            f"{invoice.code} belongs to a different vendor from payment {payment.code}.",
            details={"invoice_code": invoice.code},
        )
    if invoice.currency != payment.currency:
        raise BusinessRuleError(
            f"{invoice.code} is billed in {invoice.currency} and {payment.code} is in "
            f"{payment.currency}.",
            details={"invoice_currency": invoice.currency, "payment_currency": payment.currency},
        )
    status = VendorInvoiceStatus(invoice.status)
    allowed = (
        (*PAYABLE_INVOICE_STATUSES, VendorInvoiceStatus.PAID)
        if amending
        else PAYABLE_INVOICE_STATUSES
    )
    if status not in allowed:
        raise BusinessRuleError(
            f"{invoice.code} is {status}; only an approved invoice can be settled. A disputed "
            "invoice must be resolved first.",
            details={"invoice_code": invoice.code, "status": str(status)},
        )


async def _apply_allocation_plan(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    payment: Payment,
    invoices: dict[uuid.UUID, VendorInvoice],
    existing_by_invoice: dict[uuid.UUID, PaymentAllocation],
    plan: AllocationPlan,
    notes: dict[uuid.UUID, str | None],
    replace: bool,
) -> None:
    """Write the allocation rows and move each invoice's paid total and status.

    An invoice's paid total moves by the *difference* between what this payment
    used to put against it and what it now does, so re-sending an allocation
    with the same figures is a no-op rather than a second payment.
    """
    allocation_repo = PaymentAllocationRepository(session, tenant)

    if replace:
        for invoice_id, amount in plan.removed.items():
            invoices[invoice_id].paid_amount = to_money(invoices[invoice_id].paid_amount - amount)
        await allocation_repo.delete_for_payment(payment.id)
        existing_by_invoice = {}

    for invoice_id, amount in plan.added.items():
        invoice = invoices[invoice_id]
        previous = existing_by_invoice.get(invoice_id)
        if previous is not None:
            invoice.paid_amount = to_money(invoice.paid_amount - to_money(previous.amount))
            previous.amount = amount
            previous.notes = notes.get(invoice_id)
        else:
            allocation_repo.add(
                PaymentAllocation(
                    payment_id=payment.id,
                    vendor_invoice_id=invoice_id,
                    amount=amount,
                    notes=notes.get(invoice_id),
                )
            )
        invoice.paid_amount = to_money(invoice.paid_amount + amount)

    await session.flush()

    for invoice_id in sorted(set(plan.added) | set(plan.removed), key=str):
        invoice = invoices[invoice_id]
        target = invoice_status_after_payment(
            VendorInvoiceStatus(invoice.status),
            total_amount=invoice.total_amount,
            paid_amount=invoice.paid_amount,
        )
        await _transition_invoice(
            session,
            tenant,
            invoice,
            target,
            reason=f"Allocation from {payment.code}",
            action=AuditAction.PAYMENT,
        )


# -----------------------------------------------------------------------------
# Customer adjustments - debit and credit notes posted by hand
# -----------------------------------------------------------------------------


def customer_adjustment_delta(amount: Decimal, adjustment_type: CustomerAdjustmentType) -> Decimal:
    """The signed change an adjustment makes to the cached outstanding balance.

    A charge is money the customer now owes on top; a credit is money the
    company agrees not to collect. Pure, so the sign convention is unit-tested
    against the journal legs rather than assumed to agree with them.
    """
    money = to_money(amount)
    return money if adjustment_type is CustomerAdjustmentType.CHARGE else -money


def customer_adjustment_lines(
    *,
    receivable_account_id: uuid.UUID,
    contra_account_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount: Decimal,
    adjustment_type: CustomerAdjustmentType,
    narration: str,
) -> list[JournalLine]:
    """The balanced pair a customer adjustment posts.

    A charge debits the receivable - the customer owes more - and credits the
    account the caller named (revenue, interest, freight recovered). A credit
    note is the mirror image: it credits the receivable and debits the caller's
    account (a sales return, a discount, a write-off). Only the receivable leg
    carries the party, so the customer's statement and the ageing report see
    the adjustment while the contra account does not acquire a counterparty it
    never had.
    """
    money = to_money(amount)
    charge = adjustment_type is CustomerAdjustmentType.CHARGE
    return [
        JournalLine(
            account_id=receivable_account_id,
            debit=money if charge else ZERO,
            credit=ZERO if charge else money,
            party_type=PartyType.CUSTOMER,
            party_id=customer_id,
            narration=narration,
        ),
        JournalLine(
            account_id=contra_account_id,
            debit=ZERO if charge else money,
            credit=money if charge else ZERO,
            narration=narration,
        ),
    ]


async def post_customer_adjustment(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    customer_id: uuid.UUID,
    payload: CustomerAdjustmentCreate,
) -> CustomerAdjustmentRead:
    """Post a debit or credit note against a customer's account.

    The replacement for the customers module's old ``POST /customers/{id}/outstanding``,
    which moved the cached balance and nothing else - a figure the ledger could
    not explain. Here the ledger moves and the cache follows it, in **one**
    transaction: the receivable journal, the outstanding-balance change and the
    audit row commit together or not at all, so the balance a credit check reads
    can always be reconciled to ``ledger_entries`` by ``reference_code``.

    ``principal`` is the approver. The route is gated on ``payment.approve``
    because a credit note is money the company agrees not to collect, and the
    person who agreed is recorded on the audit row.

    ASSUMPTION: the posting is denominated in the company's own currency - the
    same default an invoice and a receipt take when none is named. Receivable
    ageing nets per (party, currency), so a note in a currency nobody chose
    could never be settled by the receipt that follows it. Recorded in
    ``docs/assumptions.md``.
    """
    entry_date = payload.entry_date or today()
    amount = to_money(payload.amount)
    label = CUSTOMER_ADJUSTMENT_LABELS[payload.adjustment_type]

    async with transaction(session):
        # Locked first: the balance change below serialises on this row anyway,
        # and taking the lock before the journal is written means two notes for
        # the same customer cannot interleave their postings and their deltas.
        customer = await get_customer_for_order(session, tenant, customer_id, lock=True)

        accounts = await ensure_system_accounts(session, tenant)
        receivable = accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)]
        if payload.account_id == receivable.id:
            raise ValidationError(
                "The contra account cannot be the receivable control account; name the "
                "revenue, expense or write-off account the adjustment belongs to.",
                details={"fields": {"account_id": ["Cannot be the receivable control account."]}},
            )

        code = await next_document_number(
            session, tenant.company_id, DocTypes.CUSTOMER_ADJUSTMENT, on=entry_date
        )
        journal_id = new_id()
        narration = f"{label} {code} for {customer.code}: {payload.reason}"
        entries = await post_journal(
            session,
            tenant,
            customer_adjustment_lines(
                receivable_account_id=receivable.id,
                contra_account_id=payload.account_id,
                customer_id=customer.id,
                amount=amount,
                adjustment_type=payload.adjustment_type,
                narration=narration,
            ),
            entry_date=entry_date,
            reference_type=REFERENCE_CUSTOMER_ADJUSTMENT,
            reference_id=journal_id,
            reference_code=code,
            narration=narration,
            journal_id=journal_id,
        )

        delta = customer_adjustment_delta(amount, payload.adjustment_type)
        customer = await apply_outstanding_delta(
            session,
            tenant,
            customer.id,
            OutstandingAdjustment(
                delta=delta,
                reason=payload.reason,
                reference_type=REFERENCE_CUSTOMER_ADJUSTMENT,
                reference_id=journal_id,
                reference_code=code,
            ),
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_CUSTOMER_ADJUSTMENT,
            entity_id=journal_id,
            entity_code=code,
            summary=(
                f"Posted {label.lower()} {code} of {amount} {entries[0].currency} against "
                f"customer {customer.code}"
            ),
            user_id=tenant.user_id,
            new_values={
                "customer_id": customer.id,
                "customer_code": customer.code,
                "adjustment_type": str(payload.adjustment_type),
                "amount": amount,
                "delta": delta,
                "account_id": payload.account_id,
                "entry_date": entry_date,
                "outstanding_amount": customer.outstanding_amount,
            },
            context={
                "reason": payload.reason,
                "approved_by_id": str(principal.user_id),
                "journal_id": str(journal_id),
            },
        )

    return CustomerAdjustmentRead(
        journal_id=journal_id,
        reference_code=code,
        customer_id=customer.id,
        customer_code=customer.code,
        adjustment_type=payload.adjustment_type,
        amount=amount,
        delta=delta,
        outstanding_amount=to_money(customer.outstanding_amount),
        entry_date=entry_date,
        currency=entries[0].currency,
        account_id=payload.account_id,
        reason=payload.reason,
        entry_ids=[entry.id for entry in entries],
    )


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


def normalise_currency(raw: str | None) -> str | None:
    """Accept an ISO-4217 alphabetic code, or nothing at all.

    ASSUMPTION: a currency is a code, not a spelling. A caller's string is
    trimmed and upper-cased before it is compared against a stored column, so
    ``inr`` and ``INR`` cannot become two different books; and anything that is
    not three letters is refused outright. The alternative - passing an
    unrecognised string through to the ``WHERE`` clause - matches no row and
    reports a company with a full payables book as owing nothing, which is the
    one answer a finance report must never give by accident.
    """
    if raw is None:
        return None
    candidate = raw.strip().upper()
    if not (len(candidate) == CURRENCY_CODE_LENGTH and candidate.isalpha()):
        raise ValidationError(
            "A currency is a three-letter ISO 4217 code, e.g. INR.",
            details={"currency": raw},
        )
    return candidate


async def company_currency(session: AsyncSession, tenant: TenantContext) -> str:
    """``companies.currency`` - the denomination this tenant keeps its books in.

    ASSUMPTION: a document that does not name a currency is denominated in the
    company's own. That column is the one the organization module already lets
    an administrator set and the one a balance sheet is prepared in; a module
    constant is not, and defaulting to one would let a company keeping books in
    a currency it never chose accumulate invoices in a currency it never chose
    either. This is the single place both halves come from - the denomination a
    report defaults to and the denomination a written document defaults to - so
    the two cannot drift apart.
    """
    return await ReportingCurrencyRepository(session, tenant).reporting_currency()


async def reporting_currency(
    session: AsyncSession, tenant: TenantContext, requested: str | None
) -> str:
    """The denomination the caller asked for, or the company's own.

    Note for report authors: this is *not* the currency an ageing report ends up
    drawn in when the caller asked for nothing. See
    :func:`primary_report_currency` - a company whose whole book is in a
    currency other than its own must not be told it owes nothing.
    """
    chosen = normalise_currency(requested)
    if chosen is not None:
        return chosen
    return await company_currency(session, tenant)


def primary_report_currency(
    requested: str | None, home: str, present: Sequence[AgeingCurrencyTotal]
) -> str:
    """Which denomination an ageing report restates at its top level.

    ASSUMPTION, in three steps, most specific first:

    1. **What the caller asked for**, if they asked. An explicit choice is
       honoured even when nothing is outstanding in it - "we owe nothing in
       dollars" is a real and useful answer.
    2. **The company's own currency**, if anything is actually open in it. That
       is the denomination the books are kept in and the one the balance sheet
       is drawn in, so it is what a finance team expects to read first.
    3. Otherwise **the denomination with the most open documents**, then the
       most parties, then the lowest code. This is the step that matters:
       defaulting to the company's currency come what may is how a company whose
       every invoice is in USD gets handed a report saying it owes nothing.

    Step 3 ranks on *counts*, never on an amount. Ranking on
    ``total_outstanding`` - which is what this did - picks the headline
    denomination by comparing 118,000 USD against 400,000 INR as though the
    numbers were commensurable, and they are not: there is no exchange-rate
    table in this schema, which is the reason every other figure in this report
    is kept inside its own denomination. Under that rule a book of one small
    dollar invoice and forty large rupee ones is headlined in whichever currency
    happens to have the bigger digits, and it flips the day a rate moves in a
    world the schema cannot see. A count of open documents means the same thing
    in every currency, so it can be compared; it also answers the question the
    step is actually asking - which denomination is this company's business
    mostly transacted in? The code breaks the remaining ties so the same book
    always produces the same headline.

    With nothing outstanding at all the report is genuinely empty and falls back
    to ``home``, which is then an honest zero rather than a hidden one.
    """
    chosen = normalise_currency(requested)
    if chosen is not None:
        return chosen
    if any(block.currency == home for block in present):
        return home
    if present:
        # Computed here rather than read off ``present[0]``: the ranking this
        # step depends on is stated once, in this function, instead of resting
        # on the ORDER BY of the query that produced the argument.
        return min(present, key=lambda block: block.activity_rank).currency
    return home


async def list_outstanding(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    params: PageParams,
    vendor_id: uuid.UUID | None = None,
    overdue_only: bool = False,
    as_of: date | None = None,
    currency: str | None = None,
) -> tuple[list[OutstandingRow], OffsetPagination]:
    """Open payables, oldest due first.

    One query. The ageing bucket is derived per row from the same boundaries the
    ageing aggregate uses, so a row's label and the column it is counted in
    cannot disagree.

    This is a list of documents, not an aggregate, so every denomination is
    returned by default and each row states its own. ``currency`` narrows it to
    one, which is how a caller reconciles this list against one block of the
    ageing report.
    """
    reference = as_of or today()
    repo = VendorInvoiceRepository(session, tenant)
    stmt = repo.outstanding_stmt(
        as_of=reference,
        vendor_id=vendor_id,
        overdue_only=overdue_only,
        currency=normalise_currency(currency),
    )
    rows, pagination = await paginate_projection(session, stmt, params)

    return [
        OutstandingRow(
            invoice_id=row.invoice_id,
            code=row.code,
            vendor_id=row.vendor_id,
            vendor_code=row.vendor_code,
            vendor_name=row.vendor_name,
            vendor_invoice_number=row.vendor_invoice_number,
            invoice_date=row.invoice_date,
            due_date=row.due_date,
            status=VendorInvoiceStatus(row.status),
            currency=row.currency,
            total_amount=to_money(row.total_amount),
            paid_amount=to_money(row.paid_amount),
            outstanding_amount=to_money(row.outstanding_amount),
            days_overdue=(reference - row.due_date).days,
            ageing_bucket=ageing_bucket((reference - row.due_date).days),
        )
        for row in rows
    ], pagination


async def ageing_report(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    party_type: PartyType,
    as_of: date | None = None,
    party_id: uuid.UUID | None = None,
    limit: int = 200,
    currency: str | None = None,
) -> AgeingReport:
    """Outstanding split into 0-30 / 31-60 / 61-90 / 90+ days, per party and per currency.

    Payables are aged from the invoice due date, which the invoice froze at the
    vendor's agreed credit days. Receivables have no invoice document in this
    schema, so they are FIFO-netted out of the ledger and aged from the posting
    date - see :meth:`LedgerEntryRepository.receivable_ageing_stmt`.

    **Every denomination with an open item is covered; none of them is added to
    another.** There is no exchange-rate table in this schema, so a total
    spanning two currencies would be meaningless and ranking parties by it worse
    than meaningless. The report therefore comes back as one block per
    denomination, each block internally consistent, and ``currency`` chooses
    which block is restated at the top level rather than which ones exist. A
    company whose entire book is in a currency other than its own gets that
    book, not an empty page.

    Two queries whatever the number of denominations: one aggregate for every
    block's totals, one window-ranked statement for the rows. ``limit`` is
    parties **per denomination**, and detailed rows are fetched for at most
    :data:`MAX_AGEING_ROW_CURRENCIES` of them - every denomination still reports
    its totals and its party count, so nothing disappears, and a report cannot
    be made arbitrarily large by a book with many currencies in it.

    Every block's totals are aggregated from the whole statement rather than
    from the rows that fit under the cap, so a company with more parties than
    the cap still gets the payables figure it reconciles against the balance
    sheet.
    """
    reference = as_of or today()
    bounded = max(1, min(int(limit), MAX_AGEING_PARTIES))
    requested = normalise_currency(currency)

    build: Callable[..., Select[Any]]
    if party_type is PartyType.VENDOR:
        build = partial(
            VendorInvoiceRepository(session, tenant).payable_ageing_stmt,
            as_of=reference,
            vendor_id=party_id,
        )
    elif party_type is PartyType.CUSTOMER:
        account_ids = await LedgerAccountRepository(session, tenant).system_account_ids(
            SystemAccount.ACCOUNTS_RECEIVABLE
        )
        build = partial(
            LedgerEntryRepository(session, tenant).receivable_ageing_stmt,
            account_ids=account_ids,
            as_of=reference,
            party_id=party_id,
        )
    else:
        raise ValidationError("An ageing report is drawn for customers or for vendors.")

    present = await ageing_totals_by_currency(session, build(currencies=None))
    home = await company_currency(session, tenant)
    denomination = primary_report_currency(requested, home, present)

    ordered = _blocks_primary_first(present, denomination)
    detailed = [block.currency for block in ordered[:MAX_AGEING_ROW_CURRENCIES]]

    rows_by_currency: dict[str, list[AgeingRow]] = {code: [] for code in detailed}
    if present:
        result = await session.execute(
            ranked_ageing_rows_stmt(build(currencies=detailed), limit=bounded)
        )
        for row in result.all():
            rows_by_currency[str(row.currency)].append(
                AgeingRow(
                    party_type=party_type,
                    party_id=row.party_id,
                    party_code=row.party_code,
                    party_name=row.party_name,
                    currency=str(row.currency),
                    bucket_0_30=to_money(row.bucket_0_30),
                    bucket_31_60=to_money(row.bucket_31_60),
                    bucket_61_90=to_money(row.bucket_61_90),
                    bucket_90_plus=to_money(row.bucket_90_plus),
                    not_due=to_money(row.not_due),
                    total_outstanding=to_money(row.total_outstanding),
                    oldest_due_date=row.oldest_due_date,
                    open_document_count=int(row.open_document_count or 0),
                )
            )

    blocks = [
        AgeingCurrencyBlock(
            currency=block.currency,
            party_count=block.party_count,
            document_count=block.document_count,
            rows=rows_by_currency.get(block.currency, []),
            total_bucket_0_30=to_money(block.totals["bucket_0_30"]),
            total_bucket_31_60=to_money(block.totals["bucket_31_60"]),
            total_bucket_61_90=to_money(block.totals["bucket_61_90"]),
            total_bucket_90_plus=to_money(block.totals["bucket_90_plus"]),
            total_not_due=to_money(block.totals["not_due"]),
            total_outstanding=to_money(block.totals["total_outstanding"]),
        )
        for block in ordered
    ]
    primary = blocks[0]

    return AgeingReport(
        party_type=party_type,
        as_of=reference,
        currency=primary.currency,
        currencies=blocks,
        other_currencies=[block.currency for block in blocks[1:]],
        bucket_labels=list(AGEING_BUCKET_LABELS),
        rows=primary.rows,
        total_bucket_0_30=primary.total_bucket_0_30,
        total_bucket_31_60=primary.total_bucket_31_60,
        total_bucket_61_90=primary.total_bucket_61_90,
        total_bucket_90_plus=primary.total_bucket_90_plus,
        total_not_due=primary.total_not_due,
        total_outstanding=primary.total_outstanding,
    )


def _blocks_primary_first(
    present: Sequence[AgeingCurrencyTotal], denomination: str
) -> list[AgeingCurrencyTotal]:
    """``present`` with ``denomination`` first, inserted zeroed if it is absent.

    The zero insert is what makes ``currencies[0].currency == report.currency``
    hold unconditionally, including for the caller who asked for a denomination
    nothing is outstanding in. Without it the top-level figures would describe a
    block that was not in the list, which is exactly the kind of quiet
    inconsistency a reader would not think to check for.
    """
    rest = [block for block in present if block.currency != denomination]
    primary = next((block for block in present if block.currency == denomination), None)
    if primary is None:
        primary = AgeingCurrencyTotal(
            currency=denomination,
            party_count=0,
            totals=dict.fromkeys(AGEING_TOTAL_COLUMNS, ZERO),
        )
    return [primary, *rest]


async def inventory_valuation(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: ValuationFilters,
    params: PageParams,
) -> tuple[list[InventoryValuationRow], InventoryValuationSummary, OffsetPagination]:
    """Value on-hand stock at what its FIFO cost layers cost.

    One grouped scan for the page and one for the totals. The full-company run
    is genuinely large, so the export goes to Celery instead - see
    :func:`queue_inventory_valuation`.
    """
    repo = InventoryValuationRepository(session, tenant)
    rows, pagination = await paginate_projection(session, repo.valuation_page_stmt(filters), params)
    count, quantity, value = await repo.valuation_totals(filters)

    return (
        [valuation_row(row) for row in rows],
        InventoryValuationSummary(
            as_of=utcnow(),
            row_count=count,
            total_quantity=to_quantity(quantity),
            total_value=to_money(value),
        ),
        pagination,
    )


def valuation_row(row: Any) -> InventoryValuationRow:
    """Project one valuation row, deriving the cost per unit from the value.

    Value divided by quantity, not the average of the position's per-unit costs:
    a part held 10 @ 100 in one batch and 990 @ 10 in another costs 10.9 a unit,
    not 55, and reporting the latter would misstate the position by a factor of
    five. Under FIFO the value being divided is the total of the open cost
    layers, so this figure is the true blended cost of what is on the shelf -
    not a running average that was maintained separately from it.

    ``recorded_value`` is passed through rather than derived here: it is the
    report's reconciliation against ``inventory_cost_layers`` and deriving it
    from anything on this row would be exactly the tautology it was rewritten to
    stop being.
    """
    quantity = Decimal(row.on_hand_quantity or 0)
    value = Decimal(row.value or 0)
    return InventoryValuationRow(
        item_id=row.item_id,
        item_code=row.item_code,
        item_name=row.item_name,
        warehouse_id=row.warehouse_id,
        warehouse_code=row.warehouse_code,
        warehouse_name=row.warehouse_name,
        on_hand_quantity=to_quantity(quantity),
        average_cost=to_unit_price(value / quantity) if quantity != ZERO else ZERO,
        value=to_money(value),
        recorded_value=to_money(Decimal(row.recorded_value or 0)),
    )


def valuation_export_parameters(filters: ValuationFilters) -> dict[str, Any]:
    """What a valuation export job is a workbook *of*, as stored on its job row.

    Carries the task name as well as the filters so its hash can never collide
    with the catalogue's tabular ``INVENTORY_VALUATION`` report over the same
    filters - that report reuses a live snapshot by ``(report_key, hash)``, and
    handing it this workbook would answer a paginated grid with a spreadsheet
    of different columns.
    """
    return {
        "report_key": str(ReportKey.INVENTORY_VALUATION),
        "task": VALUATION_TASK_NAME,
        "filters": {
            key: str(value)
            for key, value in (
                ("warehouse_id", filters.warehouse_id),
                ("category_id", filters.category_id),
            )
            if value is not None
        },
    }


async def queue_inventory_valuation(
    session: AsyncSession, tenant: TenantContext, filters: ValuationFilters
) -> str:
    """Hand the full-company valuation to Celery and return the job id.

    The job id is a ``report_snapshots`` row, not the Celery task id: the row is
    what ``GET /reports/jobs/{id}`` reads, what the worker marks RUNNING,
    COMPLETED or FAILED, and what carries the ``document_id`` the client
    downloads. A bare task id can be resolved by nothing in the API. The row is
    created and audited in one transaction before the task is published, so a
    broker that refuses the task can mark the job FAILED rather than leave the
    client polling a job that never existed. A valuation is never reused from an
    earlier job - it is a point-in-time position, and section 34 forbids serving
    anything derived from stock on hand from a cache.
    """
    snapshots = ReportSnapshotRepository(session, tenant)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.INVENTORY_VALUATION, valuation_export_parameters(filters), now=utcnow()
        )
        snapshots.add(job)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type="inventory_valuation",
            entity_id=job.id,
            summary="Queued the full inventory valuation",
            user_id=tenant.user_id,
            context={"task": VALUATION_TASK_NAME, "job_id": str(job.id)},
        )

    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name=VALUATION_TASK_NAME,
        queue=VALUATION_QUEUE,
        task_kwargs={
            "warehouse_id": str(filters.warehouse_id) if filters.warehouse_id else None,
            "category_id": str(filters.category_id) if filters.category_id else None,
        },
        subject="The inventory valuation",
    )

    return str(job.id)


async def gst_summary(
    session: AsyncSession, tenant: TenantContext, *, date_from: date, date_to: date
) -> GstSummary:
    """Output tax from dispatched sales, input tax from approved purchases.

    Two aggregate queries, grouped by HSN and rate slab - which is the shape a
    GSTR-1 HSN summary and a GSTR-3B tax computation are both read from. The
    rounding happens once, on the totals, rather than per row on the way there.
    """
    if date_to < date_from:
        raise ValidationError(date_before("period end", "period start"))

    repo = GstRepository(session, tenant)
    output_rows = await _bounded_gst_rows(
        session, repo.output_rows_stmt(date_from=date_from, date_to=date_to), "output"
    )
    input_rows = await _bounded_gst_rows(
        session, repo.input_rows_stmt(date_from=date_from, date_to=date_to), "input"
    )

    output = summarise_gst_rows(output_rows)
    purchases = summarise_gst_rows(input_rows)

    return GstSummary(
        date_from=date_from,
        date_to=date_to,
        output=output,
        input=purchases,
        net_cgst=to_money(output.cgst_amount - purchases.cgst_amount),
        net_sgst=to_money(output.sgst_amount - purchases.sgst_amount),
        net_igst=to_money(output.igst_amount - purchases.igst_amount),
        net_payable=to_money(output.total_tax - purchases.total_tax),
    )


async def _bounded_gst_rows(session: AsyncSession, stmt: Any, side: str) -> list[Any]:
    """Read one side of the return, refusing rather than truncating it.

    One row per HSN and rate slab, and nothing in the schema bounds how many
    distinct slabs a period can produce. Every other listing in this module is
    paged; a return is not - the totals are summed from these rows, so a bare
    LIMIT would quietly report a smaller liability than the period actually
    carries. Fetching one row past the cap detects that and says so.
    """
    rows = list((await session.execute(stmt.limit(MAX_GST_ROWS + 1))).all())
    if len(rows) > MAX_GST_ROWS:
        raise BusinessRuleError(
            f"The {side} tax for this period spans more than {MAX_GST_ROWS} HSN and rate "
            "combinations, which is more than one summary can report without truncating the "
            "totals. Narrow the period and run it again.",
            details={"side": side, "maximum_rows": MAX_GST_ROWS},
        )
    return rows


def summarise_gst_rows(rows: Sequence[Any]) -> GstSection:
    """Roll a set of grouped tax rows into one side of the return.

    Pure: it takes whatever the aggregate returned - anything with the six
    named attributes - and adds it up. Rounding happens once, on each row and
    on the totals, rather than repeatedly on the way there.
    """
    slabs: list[GstRateRow] = []
    taxable = cgst = sgst = igst = ZERO

    for row in rows:
        row_cgst = to_money(row.cgst_amount or 0)
        row_sgst = to_money(row.sgst_amount or 0)
        row_igst = to_money(row.igst_amount or 0)
        row_taxable = to_money(row.taxable_amount or 0)
        slabs.append(
            GstRateRow(
                hsn_code=row.hsn_code,
                tax_rate=Decimal(row.tax_rate or 0),
                taxable_amount=row_taxable,
                cgst_amount=row_cgst,
                sgst_amount=row_sgst,
                igst_amount=row_igst,
                total_tax=to_money(row_cgst + row_sgst + row_igst),
                document_count=int(row.document_count or 0),
            )
        )
        taxable += row_taxable
        cgst += row_cgst
        sgst += row_sgst
        igst += row_igst

    return GstSection(
        rows=slabs,
        taxable_amount=to_money(taxable),
        cgst_amount=to_money(cgst),
        sgst_amount=to_money(sgst),
        igst_amount=to_money(igst),
        total_tax=to_money(cgst + sgst + igst),
    )


__all__ = [
    "AUDIT_CUSTOMER_ADJUSTMENT",
    "AUDIT_JOURNAL",
    "AUDIT_LEDGER_ACCOUNT",
    "AUDIT_PAYMENT",
    "AUDIT_VENDOR_INVOICE",
    "CUSTOMER_ADJUSTMENT_LABELS",
    "DEFAULT_CHART",
    "MAX_AGEING_PARTIES",
    "MAX_AGEING_ROW_CURRENCIES",
    "MAX_GST_ROWS",
    "NOT_DUE_LABEL",
    "PAYMENT_LIFECYCLE",
    "REFERENCE_CUSTOMER_ADJUSTMENT",
    "REFERENCE_PAYMENT",
    "REFERENCE_VENDOR_INVOICE",
    "VALUATION_QUEUE",
    "VALUATION_TASK_NAME",
    "VENDOR_INVOICE_LIFECYCLE",
    "AllocationPlan",
    "ChartSeed",
    "JournalLine",
    "JournalTotals",
    "LinePricing",
    "MatchLineInput",
    "MatchVarianceError",
    "UnbalancedJournalError",
    "ageing_bucket",
    "ageing_report",
    "allocate_payment",
    "approve_invoice",
    "approve_payment",
    "assert_balanced",
    "assert_cost_centre_legs",
    "assert_may_override_variance",
    "cancel_invoice",
    "cancel_payment",
    "company_currency",
    "create_account",
    "create_invoice",
    "create_payment",
    "customer_adjustment_delta",
    "customer_adjustment_lines",
    "delete_account",
    "delete_draft_invoice",
    "delete_draft_payment",
    "dispute_invoice",
    "ensure_system_accounts",
    "evaluate_match",
    "get_account",
    "get_invoice_detail",
    "get_payment_detail",
    "gst_summary",
    "inventory_valuation",
    "invoice_detail",
    "invoice_status_after_payment",
    "invoice_summary",
    "list_accounts",
    "list_invoices",
    "list_outstanding",
    "list_payments",
    "match_invoice",
    "normalise_currency",
    "party_statement",
    "payment_detail",
    "payment_status_after_allocation",
    "payment_summary",
    "plan_allocation",
    "post_customer_adjustment",
    "post_journal",
    "price_invoice_line",
    "primary_report_currency",
    "queue_inventory_valuation",
    "reporting_currency",
    "resolve_dispute",
    "reverse_journal",
    "split_by_cost_centre",
    "statement_direction",
    "submit_invoice",
    "summarise_gst_rows",
    "update_account",
    "update_invoice",
    "update_payment",
    "valuation_export_parameters",
    "variance_ratio",
]
