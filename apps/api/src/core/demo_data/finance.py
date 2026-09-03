"""Finance demonstration data: bills, money that has moved, and a ledger that adds up.

This is the last stage, and it is the one that turns what the earlier ones built
into figures somebody can argue about. Procurement received goods; this stage
books the bills for them, pays some of those bills, charges customers for what
went out, and takes money in against those charges. Nothing here writes a ledger
row, a status or a balance itself - every one of them is produced by
``finance.service``, ``customers.service`` and the state machines behind them,
which is what makes the demonstration database reconcile rather than merely look
populated.

What it builds, and why each document is in the state it is
-----------------------------------------------------------
**Vendor invoices - eight of them, five statuses.** A payables screen showing one
row in one status teaches nobody what the statuses mean.

* ``DM-VI-PACK`` sits in DRAFT, still being keyed.
* ``DM-VI-COMP`` is PENDING_APPROVAL *with a variance the approver has to see*:
  the vendor billed the 12 000 springs they shipped, the gate accepted 11 850,
  and the 150 burred ones were refused. Quantity tolerance is zero, so the match
  blocks it - which is the whole point of having an approval queue.
* ``DM-VI-METALS`` and ``DM-VI-RFQ`` are three-way matched *by construction*:
  their lines are built from the goods receipt's accepted quantity at the
  purchase order's own net price, read out of the same two tables the matcher
  reads. They approve with no override because there is genuinely nothing out of
  tolerance to override.
* Four service bills - plating, slitting, tooling - carry no purchase order and
  are approved over a recorded override reason. That is not a shortcut:
  ``evaluate_match`` reports non-order spend as unmatched deliberately, so
  approving it is a named decision with a justification stored against it. It is
  also what real payables ageing is mostly made of.

**Ageing that means something.** The boundaries live in
``AGEING_BUCKET_BOUNDARIES`` and the SQL compares *due dates* against edges
derived from them, so a payable's bucket is decided by ``due_date`` and nothing
else. Every bucket therefore has a row, on both sides of the ledger:

======================  ==========================  ==============================
bucket                  payable                     receivable
======================  ==========================  ==============================
``not due``             the two matched PO bills    -
``0-30``                ``DM-VI-TOOL``              ``DM-SC-WEST``, ``DM-SC-VASTU``
``31-60``               ``DM-VI-SLIT``              ``DM-SC-ARCH-2``, ``DM-SC-METRO``
``61-90``               ``DM-VI-PLATE-B``           ``DM-SC-SKYLINE``
``90+``                 ``DM-VI-PLATE-A``           ``DM-SC-ARCH-1``
======================  ==========================  ==============================

The PO-linked bills *cannot* be overdue, and that is correct rather than an
oversight: procurement's receipts are all inside the last month and these vendors
give 21 to 45 days, so an aged PO invoice would have to contradict either the
receipt date or the agreed terms. The old money is service spend, which is where
it usually is.

**Payments - four out, two in.** One DRAFT cheque; one approved payment allocated
across *two* invoices of the same vendor; one approved payment left deliberately
unallocated, so the "not fully settled" filter has a row; and two customer
receipts. Allocation is bounded by the party - ``_assert_allocatable`` refuses an
invoice belonging to anyone but the payment's own vendor - so a payment can span
several invoices but never several vendors.

A **RECEIPT cannot be allocated at all**: ``allocate_payment`` refuses one
outright, because there is no customer invoice document in this schema for it to
settle. A receipt reduces the customer's balance directly, and that is recorded
in ``ctx.skipped`` at run time rather than worked around here.

**Receivables, and why they are journals.** ``database.md`` section 2 records
that this schema has no customer invoice: an open receivable is a ledger debit
the customer's credits have not covered yet, FIFO-netted by
``receivable_ageing_stmt``. So the six customer charges here are direct journals
through ``post_journal`` - the one case that function documents itself as being
for - each raising the customer's cached ``outstanding_amount`` through
``customers.service.apply_outstanding_delta`` in the same call. The cached figure
and the ledger are written together, which is the only way the two can be trusted
to agree.

**A chart with a shape.** ``ensure_system_accounts`` provisions the eight control
accounts on first use; this stage calls it up front so the chart screen has rows
before the first document rather than after it, and then opens a small revenue
branch - a parent and two children - because nothing in the finance module posts
income, there being no customer invoice to post it from. Every account it opens
is posted to; an account nobody posts to is clutter.

Where the balances end up
-------------------------
Deliberately not zero, and deliberately not tidy. Archway Interiors is left owing
2,45,600 against a 5,00,000 limit, with the older half of it in the 90+ bucket;
Chromeline Electroplaters is left owed 87,188 on a bill from four months ago. Both
outstanding screens and both party ledgers have rows, and on each side the oldest
open item belongs to a party who has paid *something* - which is the case that
catches a netting bug, where a fully-unpaid one would not.

What later stages address, and by what code
-------------------------------------------
Nothing runs after this one today, but everything created *or found* is still
registered, because a stage cannot know that and must not have to:

======================  =====================================================
kind                    codes
======================  =====================================================
``ledger_account``      the eight system accounts under their own chart code
                        (``1100`` ... ``5900``), plus ``4000``, ``4100`` and
                        ``4200``, aliased as ``DM-ACC-REVENUE``,
                        ``DM-ACC-SALES`` and ``DM-ACC-FREIGHT``
``vendor_invoice``      ``DM-VI-METALS``, ``DM-VI-COMP``, ``DM-VI-PACK``,
                        ``DM-VI-RFQ``, ``DM-VI-PLATE-A``, ``DM-VI-PLATE-B``,
                        ``DM-VI-SLIT``, ``DM-VI-TOOL``
``payment``             ``DM-PAY-BRASS``, ``DM-PAY-PLATE``, ``DM-PAY-FAST``,
                        ``DM-PAY-PACK``, ``DM-RCPT-ARCH``, ``DM-RCPT-VASTU``
``ledger_journal``      ``DM-SC-ARCH-1``, ``DM-SC-ARCH-2``, ``DM-SC-SKYLINE``,
                        ``DM-SC-METRO``, ``DM-SC-VASTU``, ``DM-SC-WEST``
======================  =====================================================

Idempotence
-----------
Invoices and payments take their ``code`` from the company's document sequence
(``VI-2026-00007``), so it is not predictable and ``find_by_code`` cannot be the
key. The marker goes in the first token of ``notes`` instead -
``[DM-VI-METALS] Bar and ingot ...`` - exactly as the procurement stage does it,
and one query per document type recovers the whole set. Journals have no notes
column and no code of their own, so they are keyed by ``reference_code``, which is
what the statement screen displays anyway.

Two consequences worth stating plainly:

* **State is walked from where a document actually is**, not from where a
  previous run is assumed to have left it. A run interrupted between "recorded"
  and "approved" is repaired by the next one rather than frozen halfway.
* **``apply_outstanding_delta`` is never repeated.** It is a signed increment and
  is not idempotent, so it is applied only inside the branch that actually posts
  the journal it describes. Applying it on a re-run that created nothing would
  inflate the cached balance a little further every time this tool was run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.core.clock import utcnow
from src.core.demo_data import DEMO_PREFIX, DemoContext, find_by_code
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.types import ZERO, to_money, to_quantity, to_unit_price
from src.modules.customers.schemas import OutstandingAdjustment
from src.modules.customers.service import apply_outstanding_delta
from src.modules.finance import service as finance
from src.modules.finance.models import (
    ALLOCATABLE_PAYMENT_STATUSES,
    LedgerAccount,
    LedgerAccountType,
    LedgerEntry,
    PartyType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceStatus,
)
from src.modules.finance.schemas import (
    AllocationRequest,
    InvoiceApproval,
    LedgerAccountCreate,
    PaymentAllocationIn,
    PaymentCreate,
    PaymentDetail,
    VendorInvoiceCreate,
    VendorInvoiceDetail,
    VendorInvoiceLineIn,
)
from src.modules.organization.models import Company
from src.modules.procurement.models import GoodsReceiptLine, PurchaseOrderLine
from src.modules.vendors.models import Vendor

STAGE = "finance"


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------
# One anchor, read once, exactly as the procurement stage does it. Two documents
# that should share a date must share it exactly, and a second reading of "today"
# across a midnight boundary is how a re-run stops being idempotent.
#
# A function rather than a module constant: a constant is evaluated at import
# time, which is a different moment from the run under a frozen clock.


def _anchor() -> date:
    """The date every other date here is measured from."""
    return utcnow().date()


def _on(days: int) -> date:
    """``days`` from the anchor. Negative is the past, positive is the future."""
    return _anchor() + timedelta(days=days)


# -----------------------------------------------------------------------------
# The idempotence marker
# -----------------------------------------------------------------------------
# Identical to the procurement stage's, on purpose: both files key documents whose
# codes come from the company's sequence, and two spellings of the same idea would
# eventually disagree about which rows are demonstration data.

_MARKER_OPEN = "["
_MARKER_CLOSE = "]"
_MARKER_LIKE = f"{_MARKER_OPEN}{DEMO_PREFIX}-%"

#: What the receivable journals point back at. ``ledger_entries.reference_type``
#: is a bare string by design - the ledger outlives its source - and there is no
#: customer invoice in this schema for these postings to name, so they name what
#: they are.
REFERENCE_CUSTOMER_CHARGE = "customer_charge"

#: The state the works bills from, used only when the company row has not been
#: given one. The baseline seed records a country and a currency but no state, and
#: every delivery address in this demonstration data is the Rajkot works -
#: Gujarat, GST state code 24. It decides CGST+SGST against IGST and nothing else.
HOME_STATE_CODE = "24"

#: GST on job work and on the goods this business sells. One rate throughout: a
#: demonstration that varied it per line would teach the reader to look for a rule
#: that is not there.
STANDARD_GST_RATE = "18"


def _tagged(marker: str, text: str) -> str:
    """A document note that carries its demonstration marker as the first token."""
    return f"{_MARKER_OPEN}{marker}{_MARKER_CLOSE} {text}"


def _marker_of(notes: str | None) -> str | None:
    if not notes or not notes.startswith(_MARKER_OPEN):
        return None
    end = notes.find(_MARKER_CLOSE)
    return notes[1:end] if end > 1 else None


async def _existing(ctx: DemoContext, model: Any) -> dict[str, Any]:
    """Every demonstration document of one type, keyed by its marker.

    One query per document type rather than one per document: the set is small and
    the alternative is a round trip for each of fourteen documents on every
    re-run, which is exactly the N+1 this codebase treats as a defect.
    """
    stmt = select(model).where(model.company_id == ctx.company_id, model.notes.like(_MARKER_LIKE))
    found: dict[str, Any] = {}
    for row in (await ctx.session.execute(stmt)).scalars():
        marker = _marker_of(row.notes)
        if marker is not None:
            found[marker] = row
    return found


def _seen(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID, *, created: bool) -> uuid.UUID:
    """Register a row and count it, whether this run made it or found it.

    Both cases register: a later stage cannot tell which happened and must not
    have to, which is the whole basis of re-running this tool.
    """
    ctx.note(STAGE, created=1 if created else 0, existing=0 if created else 1)
    return ctx.remember(kind, code, row_id)


def _vendor(ctx: DemoContext, code: str) -> uuid.UUID:
    """A vendor the masters stage registered. Hard dependency - masters always runs."""
    return ctx.id_of("vendor", ctx.code(*code.split("-")))


def _customer(ctx: DemoContext, code: str) -> uuid.UUID:
    return ctx.id_of("customer", ctx.code(*code.split("-")))


# -----------------------------------------------------------------------------
# The chart of accounts
# -----------------------------------------------------------------------------
# The eight control accounts are provisioned by the finance module itself. What it
# does not provision is anything on the income side, because nothing in the module
# posts revenue - there is no customer invoice document to post it from. The
# customer charges below need somewhere to credit, so this stage opens a small
# revenue branch: a parent and two children, which also gives the chart screen a
# hierarchy to render instead of a flat list of eight.
#
# Chart codes here are numeric rather than DM-prefixed, unlike every other code in
# this tool. A chart of accounts is read by people who expect account numbers, and
# "DM-ACC-SALES" sitting next to "1100" would teach the wrong thing about what a
# chart looks like. The rows are still recognisable as demonstration data from
# their description, they are still found before they are created, and each is
# registered under a DM- alias as well so a later stage can address either.


@dataclass(frozen=True, slots=True)
class _Account:
    code: str
    alias: tuple[str, ...]
    name: str
    account_type: LedgerAccountType
    parent: str | None
    description: str


REVENUE_ACCOUNTS: tuple[_Account, ...] = (
    _Account(
        code="4000",
        alias=("ACC", "REVENUE"),
        name="Revenue",
        account_type=LedgerAccountType.INCOME,
        parent=None,
        description="Demonstration data. Parent of the income accounts below.",
    ),
    _Account(
        code="4100",
        alias=("ACC", "SALES"),
        name="Sales - Architectural Hardware",
        account_type=LedgerAccountType.INCOME,
        parent="4000",
        description=(
            "Demonstration data. Credited by the customer charges this tool posts; the "
            "finance module has no customer invoice document to post them from."
        ),
    ),
    _Account(
        code="4200",
        alias=("ACC", "FREIGHT"),
        name="Freight and Insurance Recovered",
        account_type=LedgerAccountType.INCOME,
        parent="4000",
        description=(
            "Demonstration data. Delivery recovered from the customer, kept off the goods "
            "line so the sales figure and the GST base are not the same number."
        ),
    ),
)


# -----------------------------------------------------------------------------
# Vendor invoices billed against a purchase order and a goods receipt
# -----------------------------------------------------------------------------
# The lines are deliberately *not* written out here. They are built from the goods
# receipt's own lines joined to the order lines they were received against, so the
# bill agrees with the order and the receipt by construction rather than through a
# second copy of their numbers that goes stale the day procurement changes a
# price. That is what lets the two matched invoices below be approved with no
# override: there is genuinely nothing out of tolerance to override.


@dataclass(frozen=True, slots=True)
class _OrderedInvoice:
    marker: tuple[str, ...]
    vendor: str
    order: tuple[str, ...]
    receipt: tuple[str, ...]
    #: The vendor's own number, taken from the goods receipt that recorded it -
    #: the storekeeper wrote it down at the gate, and this is the same bill.
    vendor_invoice_number: str
    invoiced_in: int
    due_in: int
    #: True bills what the gate accepted, which is a clean three-way match. False
    #: bills what the vendor shipped, which is the overbill the match exists to
    #: catch when goods were refused on arrival.
    bill_accepted: bool
    #: Where the walk stops. Nothing here is walked past APPROVED; a part-paid
    #: invoice gets there by having a payment allocated to it, not by transition.
    stop_at: VendorInvoiceStatus
    notes: str


ORDERED_INVOICES: tuple[_OrderedInvoice, ...] = (
    # The big one, and the one that ends up part-paid. Ex-works Jamnagar, so the
    # vendor billed on despatch - four days before the lorry reached the gate. Two
    # ingot bundles were refused on arrival; this bills the 780 accepted, not the
    # 800 sent, which is the bill a payables clerk should be prepared to insist on.
    _OrderedInvoice(
        marker=("VI", "METALS"),
        vendor="VEN-BRASS",
        order=("PO", "METALS"),
        receipt=("GRN", "METALS"),
        vendor_invoice_number="SMA/INV/2211",
        invoiced_in=-33,
        due_in=12,
        bill_accepted=True,
        stop_at=VendorInvoiceStatus.APPROVED,
        notes="Bar and ingot against the quarterly call-off. Billed on despatch, ex-works.",
    ),
    # Deliberately out of tolerance, and left in the approver's queue so the
    # variance is visible before anybody opens the document. The vendor billed the
    # 12 000 springs on their delivery note; 150 were burred at the cut end and
    # refused at the gate. Quantity tolerance is zero, so this blocks - correctly.
    _OrderedInvoice(
        marker=("VI", "COMP"),
        vendor="VEN-FAST",
        order=("PO", "COMP"),
        receipt=("GRN", "COMP"),
        vendor_invoice_number="PF/INV/8830",
        invoiced_in=-22,
        due_in=8,
        bill_accepted=False,
        stop_at=VendorInvoiceStatus.PENDING_APPROVAL,
        notes="Screws complete, springs part-shipped. Billed as despatched, not as accepted.",
    ),
    # Still being keyed. A payables inbox always has one of these in it, and DRAFT
    # is the only invoice state that can still be edited or deleted outright.
    _OrderedInvoice(
        marker=("VI", "PACK"),
        vendor="VEN-PACK",
        order=("PO", "PACK"),
        receipt=("GRN", "PACK"),
        vendor_invoice_number="KP/INV/2205",
        invoiced_in=-14,
        due_in=7,
        bill_accepted=True,
        stop_at=VendorInvoiceStatus.DRAFT,
        notes="Cartons and polybags. Entered from the delivery challan, not yet checked.",
    ),
    # The quotation-sourced order. Its price carries the settlement discount the
    # metals house offered, which is exactly why the line price below is read from
    # the order rather than from the item master: billing the list price and
    # dropping the agreed discount is the overbill the matcher is looking for.
    _OrderedInvoice(
        marker=("VI", "RFQ"),
        vendor="VEN-SS",
        order=("PO", "RFQ"),
        receipt=("GRN", "RFQ"),
        vendor_invoice_number="DSC/INV/5512",
        invoiced_in=-5,
        due_in=25,
        bill_accepted=True,
        stop_at=VendorInvoiceStatus.APPROVED,
        notes="Stainless bar and zinc ingot against the enquiry that closed last month.",
    ),
)


# -----------------------------------------------------------------------------
# Vendor invoices with no purchase order behind them
# -----------------------------------------------------------------------------
# Everything on the ageing report older than a month is here, and that is not an
# accident of dating. Procurement's receipts are all inside the last thirty days
# and these vendors give 21 to 45 days, so a PO-linked bill that was overdue would
# have to contradict either the receipt date or the vendor's agreed terms. Old
# payables in this trade are services - plating, slitting, tooling - which nobody
# raises a purchase order for.
#
# ``evaluate_match`` reports an invoice with no purchase order as unmatched on
# purpose: non-order spend is approved deliberately, not by default. So each of
# these carries an override reason, which is stored on the invoice and written to
# the audit log. That is the demonstration - an override with a name and a reason
# against it, rather than an override waved through.


@dataclass(frozen=True, slots=True)
class _ServiceLine:
    description: str
    #: SAC, not HSN - these are services. The column takes either.
    hsn_code: str
    quantity: str
    unit_price: str


@dataclass(frozen=True, slots=True)
class _ServiceInvoice:
    marker: tuple[str, ...]
    vendor: str
    vendor_invoice_number: str
    invoiced_in: int
    #: Chosen to land the row in one ageing bucket, and equal to the invoice date
    #: plus the vendor's agreed credit days in every case. Passed explicitly rather
    #: than left to those terms so the bucket cannot move if somebody edits them in
    #: the masters stage.
    due_in: int
    #: Rounded to the rupee by the vendor, as a bill from a small works is. Puts a
    #: posting on the round-off control account, which nothing else here would.
    round_off: str
    override_reason: str
    notes: str
    lines: tuple[_ServiceLine, ...]


SERVICE_INVOICES: tuple[_ServiceInvoice, ...] = (
    # 90+ bucket. Four months old, part-paid, and the oldest thing the company
    # owes - which is what the payables screen should be sorting to the top.
    _ServiceInvoice(
        marker=("VI", "PLATE", "A"),
        vendor="VEN-PLATE",
        vendor_invoice_number="CHR/2025/0412",
        invoiced_in=-135,
        due_in=-120,
        round_off="0.20",
        override_reason=(
            "Plating is job work against material we issue; there is no purchase order to "
            "match to. Charges checked against the job cards for the quarter."
        ),
        notes="Quarterly plating charges. Rate as per the annual job-work agreement.",
        lines=(
            _ServiceLine(
                description="Antique brass plating - lever handle sets",
                hsn_code="998898",
                quantity="4200",
                unit_price="18.500000",
            ),
            _ServiceLine(
                description="Satin nickel plating - mortise lock forends",
                hsn_code="998898",
                quantity="2600",
                unit_price="21.350000",
            ),
        ),
    ),
    # 61-90 bucket, part-paid by the same transfer. Two open items against one
    # vendor is what makes the allocation screen worth looking at.
    _ServiceInvoice(
        marker=("VI", "PLATE", "B"),
        vendor="VEN-PLATE",
        vendor_invoice_number="CHR/2025/0518",
        invoiced_in=-90,
        due_in=-75,
        round_off="0.00",
        override_reason=(
            "Job work, no purchase order. Charges checked against the job cards for the month."
        ),
        notes="Monthly plating charges. Antique brass only; no nickel work this cycle.",
        lines=(
            _ServiceLine(
                description="Antique brass plating - lever handle sets",
                hsn_code="998898",
                quantity="3800",
                unit_price="18.500000",
            ),
        ),
    ),
    # 31-60 bucket. Deepak Steel is in Maharashtra, so this one is IGST while the
    # plating bills are CGST+SGST - which gives the GST summary both halves of the
    # return rather than one.
    _ServiceInvoice(
        marker=("VI", "SLIT"),
        vendor="VEN-SS",
        vendor_invoice_number="DSC/SVC/1180",
        invoiced_in=-75,
        due_in=-45,
        round_off="0.00",
        override_reason=(
            "Conversion charges on our own strip, billed against the weight processed. No "
            "purchase order; the material was never bought from them."
        ),
        notes="Slitting and shearing charges on our SS 304 strip, sent out and returned.",
        lines=(
            _ServiceLine(
                description="Slitting and shearing - SS 304 strip, 0.8mm",
                hsn_code="998898",
                quantity="12000",
                unit_price="4.200000",
            ),
        ),
    ),
    # 0-30 bucket, and the bill the unallocated payment below was sent against
    # without anybody telling the ledger which invoice it settled.
    _ServiceInvoice(
        marker=("VI", "TOOL"),
        vendor="VEN-FAST",
        vendor_invoice_number="PF/SVC/2214",
        invoiced_in=-55,
        due_in=-25,
        round_off="0.00",
        override_reason=(
            "Tooling repair raised on a works order, not a purchase order. Approved against "
            "the maintenance log."
        ),
        notes="Refurbishment of cold-heading dies returned after the spring run.",
        lines=(
            _ServiceLine(
                description="Refurbishment of cold-heading dies - 6 sets",
                hsn_code="998719",
                quantity="6",
                unit_price="8750.000000",
            ),
        ),
    ),
)


# -----------------------------------------------------------------------------
# Customer charges
# -----------------------------------------------------------------------------
# There is no customer invoice document in this schema (database.md section 2): an
# open receivable is a ledger debit the customer's credits have not covered,
# FIFO-netted in SQL by ``receivable_ageing_stmt``. So a demonstration receivable
# is a direct journal, which is the one case ``post_journal`` documents itself as
# being for.
#
# Each charge does two things in one call and they belong together: it posts
# Dr receivable / Cr revenue / Cr output tax, and it raises the customer's cached
# ``outstanding_amount`` by the same total through the customers module's own
# service. Writing one without the other is how a cached balance starts lying.
#
# Receivable ageing buckets on the *entry date* - a ledger debit has no due date -
# so the dates below are what spread these across the buckets. FIFO netting
# settles the oldest debit first, which is why Archway gets two charges and a
# receipt covering less than the older one: it ends with money in the 90+ bucket
# *and* money in 31-60, and a netting bug that settles the wrong end shows up
# immediately instead of hiding behind a single open item.


@dataclass(frozen=True, slots=True)
class _Charge:
    marker: tuple[str, ...]
    customer: str
    charged_in: int
    #: Goods value, delivery recovered, and the tax on the two of them. Held apart
    #: rather than as one total so the journal has a shape, and so the 18% is
    #: checkable by eye against ``goods + freight``.
    goods: str
    freight: str
    tax: str
    narration: str


CUSTOMER_CHARGES: tuple[_Charge, ...] = (
    # Archway, four months ago. Part-settled by the receipt below and still the
    # oldest thing on the receivable ledger.
    _Charge(
        marker=("SC", "ARCH", "1"),
        customer="CUST-ARCH",
        charged_in=-120,
        goods="300000.00",
        freight="0.00",
        tax="54000.00",
        narration="Mortise locks and lever sets - Lower Parel fit-out, phase 1",
    ),
    _Charge(
        marker=("SC", "SKYLINE"),
        customer="CUST-SKYLINE",
        charged_in=-68,
        goods="480000.00",
        freight="0.00",
        tax="86400.00",
        narration="Lever handles on rose and cylinder locks - Whitefield tower B",
    ),
    _Charge(
        marker=("SC", "METRO"),
        customer="DLR-METRO",
        charged_in=-50,
        goods="88000.00",
        freight="0.00",
        tax="15840.00",
        narration="Dealer stock replenishment - hinges and accessories",
    ),
    _Charge(
        marker=("SC", "ARCH", "2"),
        customer="CUST-ARCH",
        charged_in=-34,
        goods="120000.00",
        freight="0.00",
        tax="21600.00",
        narration="Mortise locks and lever sets - Lower Parel fit-out, phase 2",
    ),
    _Charge(
        marker=("SC", "VASTU"),
        customer="DLR-VASTU",
        charged_in=-18,
        goods="96000.00",
        freight="0.00",
        tax="17280.00",
        narration="Counter stock - brass lever sets and cylinders",
    ),
    # The only one carrying recovered freight, so one journal in the set has four
    # legs and the statement screen is not five copies of the same shape.
    _Charge(
        marker=("SC", "WEST"),
        customer="DIST-WEST",
        charged_in=-12,
        goods="720000.00",
        freight="30000.00",
        tax="135000.00",
        narration="Monthly consolidated despatch - western region",
    ),
)


# -----------------------------------------------------------------------------
# Payments and receipts
# -----------------------------------------------------------------------------
# Money moves at APPROVED: that is where the journal is posted and where a
# customer's outstanding balance changes. Allocation happens afterwards and only
# decides *which invoices* the money settled - it can never change the amount that
# left the bank. Both halves are worth seeing separately, so one payment here is
# approved and left unallocated on purpose.


@dataclass(frozen=True, slots=True)
class _Allocation:
    invoice: tuple[str, ...]
    amount: str
    notes: str


@dataclass(frozen=True, slots=True)
class _Payment:
    marker: tuple[str, ...]
    payment_type: PaymentType
    #: Resolved against the vendors or the customers by the type. The schema
    #: refuses the other pairing: money going *to* a customer is a refund, which is
    #: a different document with different accounting.
    party: str
    paid_in: int
    amount: str
    method: PaymentMethod
    reference_number: str
    bank_name: str | None
    instrument_in: int | None
    #: False leaves it in DRAFT: prepared, not released, nothing posted anywhere.
    approve: bool
    notes: str
    allocations: tuple[_Allocation, ...] = ()


PAYMENTS: tuple[_Payment, ...] = (
    # Part payment against the metals bill, before the due date - which is how this
    # trade actually pays for metal. Fully allocated, so the payment ends ALLOCATED
    # and the invoice it settles becomes PARTIALLY_PAID.
    _Payment(
        marker=("PAY", "BRASS"),
        payment_type=PaymentType.PAYMENT,
        party="VEN-BRASS",
        paid_in=-18,
        amount="500000.00",
        method=PaymentMethod.CHEQUE,
        reference_number="512244",
        bank_name="HDFC Bank",
        instrument_in=-20,
        approve=True,
        notes="Part payment on account against the quarterly metals bill.",
        allocations=(
            _Allocation(
                invoice=("VI", "METALS"),
                amount="500000.00",
                notes="Part payment; balance on the agreed 45-day terms.",
            ),
        ),
    ),
    # One payment across two invoices of one vendor, oldest first, neither cleared.
    # Both stay open, so the 90+ and 61-90 buckets keep their rows and the ageing
    # report is not a single-bucket picture.
    _Payment(
        marker=("PAY", "PLATE"),
        payment_type=PaymentType.PAYMENT,
        party="VEN-PLATE",
        paid_in=-20,
        amount="120000.00",
        method=PaymentMethod.BANK_TRANSFER,
        reference_number="NEFT/CHR/8841",
        bank_name="State Bank of India",
        instrument_in=None,
        approve=True,
        notes="On account against the two oldest plating bills.",
        allocations=(
            _Allocation(
                invoice=("VI", "PLATE", "A"),
                amount="70000.00",
                notes="Against the oldest open bill first.",
            ),
            _Allocation(
                invoice=("VI", "PLATE", "B"),
                amount="50000.00",
                notes="Balance of the transfer.",
            ),
        ),
    ),
    # Approved and deliberately never allocated. The money left the bank and nobody
    # has said which invoice it settled - the row the "not fully settled" filter
    # exists to find, and entirely in character for a vendor the masters stage
    # describes as reliable on quantity and slow on paperwork.
    _Payment(
        marker=("PAY", "FAST"),
        payment_type=PaymentType.PAYMENT,
        party="VEN-FAST",
        paid_in=-12,
        amount="61950.00",
        method=PaymentMethod.UPI,
        reference_number="UPI/PF/77120",
        bank_name=None,
        instrument_in=None,
        approve=True,
        notes="Sent against the tooling bill; remittance advice not yet received.",
    ),
    # Prepared, not released. A DRAFT payment has posted nothing and can still be
    # amended or deleted, which is the whole difference the approval step makes.
    _Payment(
        marker=("PAY", "PACK"),
        payment_type=PaymentType.PAYMENT,
        party="VEN-PACK",
        paid_in=-2,
        amount="105000.00",
        method=PaymentMethod.CHEQUE,
        reference_number="512261",
        bank_name="HDFC Bank",
        instrument_in=-2,
        approve=False,
        notes="Cheque drawn for the carton bill; held until the print check is signed off.",
    ),
    # Receipts. Neither is allocated and neither can be - see the note this stage
    # records at the end. They reduce the customer's cached balance directly.
    _Payment(
        marker=("RCPT", "ARCH"),
        payment_type=PaymentType.RECEIPT,
        party="CUST-ARCH",
        paid_in=-30,
        amount="250000.00",
        method=PaymentMethod.BANK_TRANSFER,
        reference_number="NEFT/ARCH/0921",
        bank_name="ICICI Bank",
        instrument_in=None,
        approve=True,
        notes="Part payment against the phase 1 supply. Balance still under discussion.",
    ),
    _Payment(
        marker=("RCPT", "VASTU"),
        payment_type=PaymentType.RECEIPT,
        party="DLR-VASTU",
        paid_in=-6,
        amount="60000.00",
        method=PaymentMethod.UPI,
        reference_number="UPI/VASTU/44120",
        bank_name=None,
        instrument_in=None,
        approve=True,
        notes="Counter collection remitted against the dealer account.",
    ),
)


# =============================================================================
# The chart of accounts
# =============================================================================


async def _seed_chart(ctx: DemoContext) -> dict[str, uuid.UUID]:
    """Provision the control accounts and open the revenue branch.

    ``ensure_system_accounts`` is called here rather than being left to the first
    invoice approval, so the chart screen has rows before any document does. It is
    idempotent and serialised on an advisory lock, so calling it early costs
    nothing and a re-run creates nothing.

    Returns the account ids the customer charges post to, addressable both by
    system code and by chart code - the two never collide, one being alphabetic
    and the other numeric.
    """
    known_before = set(
        (
            await ctx.session.execute(
                select(LedgerAccount.system_code).where(
                    LedgerAccount.company_id == ctx.company_id,
                    LedgerAccount.system_code.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    system = await finance.ensure_system_accounts(ctx.session, ctx.tenant)
    resolved: dict[str, uuid.UUID] = {}
    for system_code, account in system.items():
        _seen(
            ctx,
            "ledger_account",
            account.code,
            account.id,
            created=account.system_code not in known_before,
        )
        resolved[system_code] = account.id
        resolved[account.code] = account.id

    for spec in REVENUE_ACCOUNTS:
        existing = await find_by_code(ctx.session, LedgerAccount, ctx.company_id, spec.code)
        if existing is not None:
            account_id = _seen(ctx, "ledger_account", spec.code, existing.id, created=False)
        else:
            created = await finance.create_account(
                ctx.session,
                ctx.tenant,
                LedgerAccountCreate(
                    code=spec.code,
                    name=spec.name,
                    account_type=spec.account_type,
                    # Resolved from what this loop has already registered, which is
                    # why the parent is listed before its children.
                    parent_id=resolved.get(spec.parent) if spec.parent else None,
                    description=spec.description,
                ),
            )
            account_id = _seen(ctx, "ledger_account", spec.code, created.id, created=True)
        resolved[spec.code] = account_id
        # Also addressable by the DM- code a later stage would guess. The alias
        # points at the same row: it is not a second account and is not counted.
        ctx.remember("ledger_account", ctx.code(*spec.alias), account_id)

    return resolved


# =============================================================================
# Vendor invoices
# =============================================================================


async def _home_state_code(ctx: DemoContext) -> str:
    """The state code the supply is made from, which decides the tax split.

    Read from the company row when it has one. The baseline seed does not set it,
    so the fallback is the works this demonstration data describes throughout.
    """
    stmt = select(Company.state_code).where(Company.id == ctx.company_id)
    code = (await ctx.session.execute(stmt)).scalars().first()
    return code or HOME_STATE_CODE


async def _interstate_vendors(ctx: DemoContext, home: str) -> set[uuid.UUID]:
    """Which vendors sit outside the home state, in one query.

    A vendor with no state recorded is treated as local. CGST+SGST is the
    conservative reading: an IGST bill from a vendor who turns out to be local is a
    return that has to be revised, and the reverse is not.
    """
    stmt = select(Vendor.id, Vendor.state_code).where(Vendor.company_id == ctx.company_id)
    return {
        row.id
        for row in (await ctx.session.execute(stmt)).all()
        if row.state_code is not None and row.state_code != home
    }


def _ordered_net_price(line: PurchaseOrderLine) -> Decimal:
    """What the order actually agreed per unit, with its discount applied.

    Mirrors ``finance.service._ordered_net_price``, which is the figure the
    three-way match compares a bill against. Billing the *gross* price would look
    exactly like a vendor who quietly dropped the settlement discount they
    offered - and the matcher would say so, correctly, about an invoice that is
    supposed to agree with its own order.
    """
    quantity = to_quantity(line.quantity)
    if quantity == ZERO:
        return to_unit_price(line.unit_price)
    gross = quantity * to_unit_price(line.unit_price)
    return to_unit_price((gross - to_money(line.discount)) / quantity)


async def _lines_from_receipt(
    ctx: DemoContext,
    *,
    order_id: uuid.UUID,
    receipt_id: uuid.UUID,
    accepted: bool,
    interstate: bool,
) -> list[VendorInvoiceLineIn]:
    """Build a bill out of the receipt it is for, joined to the order it was on.

    One query, and every number comes from the same two tables the matcher reads -
    which is what makes the resulting invoice match by construction instead of by
    a hand-kept copy of prices that goes stale the first time procurement changes
    one.
    """
    stmt = (
        select(GoodsReceiptLine, PurchaseOrderLine)
        .join(PurchaseOrderLine, PurchaseOrderLine.id == GoodsReceiptLine.purchase_order_line_id)
        .where(
            GoodsReceiptLine.company_id == ctx.company_id,
            # Redundant against the foreign key, and stated anyway: a join that
            # crosses two tenant-scoped tables spells the scope out on both sides
            # rather than trusting the reader to trace the FK.
            PurchaseOrderLine.company_id == ctx.company_id,
            GoodsReceiptLine.goods_receipt_id == receipt_id,
            PurchaseOrderLine.purchase_order_id == order_id,
        )
        .order_by(GoodsReceiptLine.line_number)
    )

    lines: list[VendorInvoiceLineIn] = []
    for receipt_line, order_line in (await ctx.session.execute(stmt)).all():
        quantity = to_quantity(
            receipt_line.accepted_quantity if accepted else receipt_line.received_quantity
        )
        # A line refused in full is not billed at all rather than billed at zero:
        # the schema requires a positive quantity, and a nil line on a bill is a
        # credit note's job.
        if quantity <= ZERO:
            continue
        lines.append(
            VendorInvoiceLineIn(
                item_id=receipt_line.item_id,
                purchase_order_line_id=order_line.id,
                goods_receipt_line_id=receipt_line.id,
                quantity=quantity,
                uom_id=receipt_line.uom_id,
                unit_price=_ordered_net_price(order_line),
                # The discount is already inside the net price above; charging it a
                # second time here would take it off the line twice.
                discount_amount=ZERO,
                tax_rate=order_line.tax_rate,
                is_interstate=interstate,
            )
        )
    return lines


async def _seed_vendor_invoices(ctx: DemoContext, interstate: set[uuid.UUID]) -> None:
    """Every bill, ordered and non-ordered, from one read of what is already there.

    Ids and statuses are lifted out of the ORM rows up front, before any service
    call. A caught business-rule failure unwinds a SAVEPOINT and expires the
    identity map, and reading an attribute off a row loaded before that point
    would then be an implicit lazy load on an async session - the trap
    ``CLAUDE.md`` records. Plain values cannot be expired.
    """
    found = {
        marker: (row.id, VendorInvoiceStatus(row.status))
        for marker, row in (await _existing(ctx, VendorInvoice)).items()
    }

    for ordered in ORDERED_INVOICES:
        await _seed_ordered_invoice(ctx, ordered, found, interstate)
    for service in SERVICE_INVOICES:
        await _seed_service_invoice(ctx, service, found, interstate)


async def _seed_ordered_invoice(
    ctx: DemoContext,
    spec: _OrderedInvoice,
    found: dict[str, tuple[uuid.UUID, VendorInvoiceStatus]],
    interstate: set[uuid.UUID],
) -> None:
    """One bill that names a purchase order and a goods receipt."""
    marker = ctx.code(*spec.marker)
    known = found.get(marker)
    if known is not None:
        invoice_id, status = known
        _seen(ctx, "vendor_invoice", marker, invoice_id, created=False)
        await _walk_invoice(ctx, marker, invoice_id, status, spec.stop_at, override_reason=None)
        return

    # Soft dependencies, unlike the vendor: procurement records a skip and carries
    # on when a document cannot be raised, so the order or the receipt may
    # legitimately be absent from a partially-built demonstration database.
    order_id = ctx.maybe_id("purchase_order", ctx.code(*spec.order))
    receipt_id = ctx.maybe_id("goods_receipt", ctx.code(*spec.receipt))
    if order_id is None or receipt_id is None:
        missing = "purchase order" if order_id is None else "goods receipt"
        ctx.skipped.append((STAGE, f"{marker} not billed: procurement registered no {missing}"))
        return

    vendor_id = _vendor(ctx, spec.vendor)
    lines = await _lines_from_receipt(
        ctx,
        order_id=order_id,
        receipt_id=receipt_id,
        accepted=spec.bill_accepted,
        interstate=vendor_id in interstate,
    )
    if not lines:
        ctx.skipped.append(
            (STAGE, f"{marker} not billed: its goods receipt has no line left to bill")
        )
        return

    detail = await _create_invoice(
        ctx,
        marker,
        VendorInvoiceCreate(
            vendor_id=vendor_id,
            vendor_invoice_number=spec.vendor_invoice_number,
            invoice_date=_on(spec.invoiced_in),
            due_date=_on(spec.due_in),
            purchase_order_id=order_id,
            goods_receipt_id=receipt_id,
            notes=_tagged(marker, spec.notes),
            lines=lines,
        ),
    )
    if detail is None:
        return
    _seen(ctx, "vendor_invoice", marker, detail.id, created=True)
    await _walk_invoice(ctx, marker, detail.id, detail.status, spec.stop_at, override_reason=None)


async def _seed_service_invoice(
    ctx: DemoContext,
    spec: _ServiceInvoice,
    found: dict[str, tuple[uuid.UUID, VendorInvoiceStatus]],
    interstate: set[uuid.UUID],
) -> None:
    """One service bill: nothing to match to, approved over a recorded override."""
    marker = ctx.code(*spec.marker)
    known = found.get(marker)
    if known is not None:
        invoice_id, status = known
        _seen(ctx, "vendor_invoice", marker, invoice_id, created=False)
        await _walk_invoice(
            ctx,
            marker,
            invoice_id,
            status,
            VendorInvoiceStatus.APPROVED,
            override_reason=spec.override_reason,
        )
        return

    vendor_id = _vendor(ctx, spec.vendor)
    is_interstate = vendor_id in interstate
    detail = await _create_invoice(
        ctx,
        marker,
        VendorInvoiceCreate(
            vendor_id=vendor_id,
            vendor_invoice_number=spec.vendor_invoice_number,
            invoice_date=_on(spec.invoiced_in),
            due_date=_on(spec.due_in),
            round_off_amount=Decimal(spec.round_off),
            notes=_tagged(marker, spec.notes),
            lines=[
                VendorInvoiceLineIn(
                    description=line.description,
                    hsn_code=line.hsn_code,
                    quantity=Decimal(line.quantity),
                    unit_price=Decimal(line.unit_price),
                    tax_rate=Decimal(STANDARD_GST_RATE),
                    is_interstate=is_interstate,
                )
                for line in spec.lines
            ],
        ),
    )
    if detail is None:
        return
    _seen(ctx, "vendor_invoice", marker, detail.id, created=True)
    await _walk_invoice(
        ctx,
        marker,
        detail.id,
        detail.status,
        VendorInvoiceStatus.APPROVED,
        override_reason=spec.override_reason,
    )


async def _create_invoice(
    ctx: DemoContext, marker: str, payload: VendorInvoiceCreate
) -> VendorInvoiceDetail | None:
    """Record one bill, or say plainly why it could not be recorded.

    Narrow on purpose. A business rule refusing one invoice is a fact worth
    reporting and carrying on from; anything else - a bad payload, a field this
    file guessed at - is a defect here and should arrive as a traceback.
    """
    try:
        return await finance.create_invoice(ctx.session, ctx.tenant, payload)
    except (BusinessRuleError, NotFoundError) as exc:
        ctx.skipped.append((STAGE, f"{marker} not recorded: {exc}"))
        return None


async def _walk_invoice(
    ctx: DemoContext,
    marker: str,
    invoice_id: uuid.UUID,
    current: VendorInvoiceStatus,
    stop_at: VendorInvoiceStatus,
    *,
    override_reason: str | None,
) -> None:
    """Move a bill towards its target status from wherever it actually is.

    Driven off the status the row currently holds rather than off an assumption
    about what a previous run reached, so a run interrupted between "recorded" and
    "approved" is repaired by the next one instead of leaving a document stuck
    halfway.

    Nothing is walked past APPROVED. PARTIALLY_PAID and PAID are reached by
    allocating a payment to the invoice, never by a transition: the money has to
    have moved first.
    """
    if stop_at is VendorInvoiceStatus.DRAFT:
        return
    if current not in (VendorInvoiceStatus.DRAFT, VendorInvoiceStatus.PENDING_APPROVAL):
        return

    try:
        if current is VendorInvoiceStatus.DRAFT:
            await finance.submit_invoice(
                ctx.session,
                ctx.tenant,
                invoice_id,
                reason="Checked against the delivery challan and sent for approval.",
            )
        if stop_at is VendorInvoiceStatus.APPROVED:
            await finance.approve_invoice(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                invoice_id,
                InvoiceApproval(override_reason=override_reason),
            )
    except BusinessRuleError as exc:
        # Includes MatchVarianceError. An invoice that was expected to match and
        # did not is worth seeing in the run report rather than taking the rest of
        # the stage down with it; the bill itself is recorded either way.
        ctx.skipped.append((STAGE, f"{marker} not walked to {stop_at}: {exc}"))


# =============================================================================
# Customer charges
# =============================================================================


async def _seed_customer_charges(ctx: DemoContext, accounts: dict[str, uuid.UUID]) -> None:
    """Post the receivable side of the books, and move the cached balance with it.

    Keyed by ``reference_code``, because a journal has no notes column and no code
    of its own. One query recovers the whole set.
    """
    codes = [ctx.code(*spec.marker) for spec in CUSTOMER_CHARGES]
    posted = {
        row.reference_code: row.journal_id
        for row in (
            await ctx.session.execute(
                select(LedgerEntry.reference_code, LedgerEntry.journal_id).where(
                    LedgerEntry.company_id == ctx.company_id,
                    LedgerEntry.reference_type == REFERENCE_CUSTOMER_CHARGE,
                    LedgerEntry.reference_code.in_(codes),
                )
            )
        ).all()
    }

    receivable = accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)]
    output_tax = accounts[str(SystemAccount.OUTPUT_TAX)]

    for spec in CUSTOMER_CHARGES:
        marker = ctx.code(*spec.marker)
        already = posted.get(marker)
        if already is not None:
            _seen(ctx, "ledger_journal", marker, already, created=False)
            continue

        customer_id = _customer(ctx, spec.customer)
        goods = to_money(Decimal(spec.goods))
        freight = to_money(Decimal(spec.freight))
        tax = to_money(Decimal(spec.tax))
        total = to_money(goods + freight + tax)

        lines = [
            finance.JournalLine(
                account_id=receivable,
                debit=total,
                party_type=PartyType.CUSTOMER,
                party_id=customer_id,
                narration=spec.narration,
            ),
            finance.JournalLine(
                account_id=accounts["4100"], credit=goods, narration=spec.narration
            ),
        ]
        if freight > ZERO:
            lines.append(
                finance.JournalLine(
                    account_id=accounts["4200"],
                    credit=freight,
                    narration="Delivery recovered from the customer",
                )
            )
        lines.append(
            finance.JournalLine(
                account_id=output_tax, credit=tax, narration=f"Output GST on {marker}"
            )
        )

        # Currency is left unset so it resolves to the company's own, which is the
        # same default an invoice and a payment take. Receivable ageing nets per
        # (party, currency), so a charge in a denomination nobody chose could never
        # be settled by the receipt further down.
        entries = await finance.post_journal(
            ctx.session,
            ctx.tenant,
            lines,
            entry_date=_on(spec.charged_in),
            reference_type=REFERENCE_CUSTOMER_CHARGE,
            reference_code=marker,
            narration=spec.narration,
        )
        _seen(ctx, "ledger_journal", marker, entries[0].journal_id, created=True)

        # Applied only in the branch that posted the journal above, and in the same
        # transaction as it - exactly as ``approve_payment`` does for a receipt.
        # The cached balance and the ledger commit or roll back together, and a
        # re-run that created nothing adds nothing.
        await apply_outstanding_delta(
            ctx.session,
            ctx.tenant,
            customer_id,
            OutstandingAdjustment(
                delta=total,
                reason=f"Charge {marker}: {spec.narration}",
                reference_type=REFERENCE_CUSTOMER_CHARGE,
                reference_code=marker,
            ),
        )


# =============================================================================
# Payments and receipts
# =============================================================================


async def _seed_payments(ctx: DemoContext) -> None:
    """Six payments, each walked from wherever it is found.

    Ids and amounts are lifted out of the ORM rows before the first service call,
    for the reason ``_seed_vendor_invoices`` gives.
    """
    found = {
        marker: (row.id, PaymentStatus(row.status), to_money(row.allocated_amount))
        for marker, row in (await _existing(ctx, Payment)).items()
    }

    for spec in PAYMENTS:
        marker = ctx.code(*spec.marker)
        known = found.get(marker)
        if known is not None:
            payment_id, status, allocated = known
            _seen(ctx, "payment", marker, payment_id, created=False)
        else:
            detail = await _create_payment(ctx, marker, spec)
            if detail is None:
                continue
            payment_id = _seen(ctx, "payment", marker, detail.id, created=True)
            status, allocated = PaymentStatus(detail.status), to_money(detail.allocated_amount)

        await _walk_payment(ctx, marker, payment_id, status, allocated, spec)


async def _create_payment(ctx: DemoContext, marker: str, spec: _Payment) -> PaymentDetail | None:
    is_receipt = spec.payment_type is PaymentType.RECEIPT
    try:
        return await finance.create_payment(
            ctx.session,
            ctx.tenant,
            PaymentCreate(
                payment_type=spec.payment_type,
                party_type=PartyType.CUSTOMER if is_receipt else PartyType.VENDOR,
                party_id=_customer(ctx, spec.party) if is_receipt else _vendor(ctx, spec.party),
                payment_date=_on(spec.paid_in),
                amount=Decimal(spec.amount),
                payment_method=spec.method,
                reference_number=spec.reference_number,
                bank_name=spec.bank_name,
                instrument_date=(
                    _on(spec.instrument_in) if spec.instrument_in is not None else None
                ),
                notes=_tagged(marker, spec.notes),
            ),
        )
    except (BusinessRuleError, NotFoundError) as exc:
        ctx.skipped.append((STAGE, f"{marker} not recorded: {exc}"))
        return None


async def _walk_payment(
    ctx: DemoContext,
    marker: str,
    payment_id: uuid.UUID,
    status: PaymentStatus,
    allocated: Decimal,
    spec: _Payment,
) -> None:
    """Approve the payment if it should be, then settle whatever it settles.

    Approval is what posts the journal and, for a receipt, what reduces the
    customer's balance - so it is guarded on DRAFT rather than on a flag this file
    carries. Approving twice would credit the bank a second time for money that
    moved once, which is precisely what ``approve_payment`` refuses.
    """
    if spec.approve and status is PaymentStatus.DRAFT:
        try:
            await finance.approve_payment(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                payment_id,
                reason="Released in the weekly payment run.",
            )
            status = PaymentStatus.APPROVED
        except BusinessRuleError as exc:
            ctx.skipped.append((STAGE, f"{marker} not approved: {exc}"))
            return

    if not spec.allocations or status not in ALLOCATABLE_PAYMENT_STATUSES:
        return
    # Only ever allocated from a standing start. Re-sending identical figures is a
    # no-op inside the service, but a payment somebody has since re-allocated by
    # hand is not this tool's to overwrite.
    if allocated > ZERO:
        return

    requested: list[PaymentAllocationIn] = []
    for allocation in spec.allocations:
        invoice_code = ctx.code(*allocation.invoice)
        invoice_id = ctx.maybe_id("vendor_invoice", invoice_code)
        if invoice_id is None:
            ctx.skipped.append(
                (STAGE, f"{marker} not allocated to {invoice_code}: that bill was not recorded")
            )
            continue
        requested.append(
            PaymentAllocationIn(
                vendor_invoice_id=invoice_id,
                amount=Decimal(allocation.amount),
                notes=allocation.notes,
            )
        )
    if not requested:
        return

    try:
        await finance.allocate_payment(
            ctx.session, ctx.tenant, payment_id, AllocationRequest(allocations=requested)
        )
    except (BusinessRuleError, NotFoundError) as exc:
        ctx.skipped.append((STAGE, f"{marker} left unallocated: {exc}"))


def _note_receipt_allocation_gap(ctx: DemoContext) -> None:
    """Record why no customer receipt here settles a document.

    Not an omission and not a workaround. ``allocate_payment`` refuses a RECEIPT
    outright, and it is right to: there is no customer invoice in this schema for a
    receipt to be allocated against (database.md section 2), so a receipt reduces
    the customer's outstanding balance directly and the ageing report nets it FIFO
    out of the ledger instead.
    """
    receipts = [
        ctx.code(*spec.marker) for spec in PAYMENTS if spec.payment_type is PaymentType.RECEIPT
    ]
    if not receipts:
        return
    ctx.skipped.append(
        (
            STAGE,
            f"no allocation on {', '.join(receipts)}: allocation is payables-only - a customer "
            "receipt has no invoice document to settle, so it reduces the customer's "
            "outstanding balance directly and is FIFO-netted on the ageing report",
        )
    )


# =============================================================================
# Entry point
# =============================================================================


async def seed(ctx: DemoContext) -> None:
    """The books, in the order they are actually written.

    The order is not decoration. The chart has to exist before anything posts to
    it; a bill has to be approved before a payment can be allocated against it;
    and a customer charge has to be on the ledger before a receipt has anything to
    be netted against - receivable ageing settles the oldest debit first, and a
    receipt arriving at an empty ledger would age nothing at all.
    """
    accounts = await _seed_chart(ctx)

    home = await _home_state_code(ctx)
    interstate = await _interstate_vendors(ctx, home)
    await _seed_vendor_invoices(ctx, interstate)

    await _seed_customer_charges(ctx, accounts)

    # Last: allocation needs its invoices approved, and a receipt needs the charge
    # it is settling to be on the ledger already.
    await _seed_payments(ctx)

    _note_receipt_allocation_gap(ctx)
