"""Queries for the ledger, vendor invoices, payments and the finance reports.

Seven shapes here exist for correctness or performance reasons rather than
convenience:

* **The two ageing statements are grouped by currency; the rest of this module
  is not.** Orders, invoices and ledger entries each carry their own
  ``currency`` and this schema has no exchange-rate table to convert between
  them - multi-currency is still an open question, not a settled requirement
  (``docs/assumptions.md``, open question 3). Adding INR to USD produces a
  number that means nothing and then *ranks* vendors by it, so
  ``payable_ageing_stmt`` and ``receivable_ageing_stmt`` carry ``currency`` in
  their grouping key, the FIFO netting behind receivables runs per (party,
  currency), and :func:`ageing_totals_by_currency` totals each denomination
  separately. No caller of those two can obtain a figure that spans
  denominations, because no such figure is ever computed.

  This is a property of the ageing statements and **not** of the module. Three
  aggregates here deliberately still sum across whatever denominations their
  rows happen to carry, and would report a mixed figure for a company that
  really did trade in two:

  - :meth:`LedgerEntryRepository.statement_stmt` and its opening balance - a
    party statement's running balance adds every posting on the account.
  - :meth:`LedgerEntryRepository.statement_totals` - the debit/credit footer of
    that same statement.
  - :class:`GstRepository` - a statutory return is filed in one denomination
    and this one adds invoice lines up regardless of the invoice's.

  They are recorded as a known gap rather than quietly presented as safe. Fixing
  them is a larger change than a ``GROUP BY``: a statement with two currencies
  in it needs a second running balance and a shape the callers do not have
  today, and a GST return needs a rule about foreign-currency purchases that
  nobody has written down. Do not restate the module-wide invariant here until
  those three hold it too.

* **The running balance is computed by PostgreSQL, not by Python.** A statement
  is paged, and an accumulator in the service would be correct only on page one:
  page three would start again from zero. :meth:`LedgerEntryRepository.
  statement_stmt` uses ``sum(...) OVER (ORDER BY entry_date, id ROWS UNBOUNDED
  PRECEDING)`` plus a scalar opening balance, so every page carries the true
  balance after each posting - one statement, whatever the page.

* **Ageing is one aggregate, not a row per invoice brought into Python.** The
  bucket boundaries are compared as *dates* rather than as day arithmetic, so
  PostgreSQL can satisfy the predicate from the partial index on open invoices
  instead of computing an interval per row.

* **Receivable ageing is FIFO-netted in SQL.** There is no customer-invoice
  document in the schema (database.md section 2), so an open receivable item is
  a ledger debit the party's credits have not yet covered. That is a running-sum
  problem, and :meth:`LedgerEntryRepository.receivable_ageing_stmt` solves it
  with a window function rather than by pulling a party's whole ledger into
  memory and looping.

* **Every lookup a write path needs is batched.** Items, purchase-order lines
  and goods-receipt lines are resolved a whole invoice at a time. A twenty-line
  invoice validated with a lookup per line would be sixty round trips on the
  write path (section 32).

* **Narrow projections.** ``items``, ``vendors``, ``purchase_order_lines`` and
  ``inventory_balances`` are wide tables and the matcher needs six columns of
  each. Pulling whole rows to read six fields is the ``SELECT *`` the rules
  forbid.

* **The header is the serialisation point.** ``lock_for_update`` on the invoice
  and on the payment is what makes allocation safe: it is a read-modify-write
  over ``paid_amount`` and ``allocated_amount``, and two allocations racing on
  one invoice would each add their amount to the value they read first.

Reads that cross into ``vendors``, ``customers``, ``items``, ``warehouses``,
``purchase_orders``, ``goods_receipts``, ``sales_orders``, ``dispatches`` and
``inventory_balances`` go through :class:`~src.common.repository.BaseRepository`
so the tenant filter - and the soft-delete filter where the table has one -
stays automatic rather than something each statement has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from src.common.repository import BaseRepository
from src.common.warehouse_scope import warehouse_scope
from src.core.constants import DEFAULT_CURRENCY
from src.core.database import affected_rows
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.core.types import ZERO
from src.modules.customers.models import Customer
from src.modules.dispatch.models import (
    DELIVERED_STATUSES,
    IN_FLIGHT_STATUSES,
    Dispatch,
    DispatchLine,
)
from src.modules.finance.models import (
    AGEING_BUCKET_BOUNDARIES,
    INPUT_TAX_STATUSES,
    OPEN_INVOICE_STATUSES,
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    LedgerAccount,
    LedgerAccountType,
    LedgerEntry,
    PartyType,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    SalesInvoice,
    SalesInvoiceLine,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceLine,
)
from src.modules.finance.schemas import (
    LedgerAccountFilters,
    PaymentFilters,
    ValuationFilters,
    VendorInvoiceFilters,
)
from src.modules.inventory.models import CostLayerPool, InventoryBalance, InventoryCostLayer
from src.modules.job_work.models import JobWorkOrder, JobWorkReceipt, JobWorkReceiptLine
from src.modules.organization.models import Company, FinancialYear
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
)
from src.modules.products.models import Item, UnitOfMeasure
from src.modules.sales.models import SalesOrderLine
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse

#: How far the chart of accounts may nest. Bounds the recursive ancestor walk
#: that prevents a parent cycle - a chart deeper than this is a modelling
#: mistake, not a legitimate hierarchy.
MAX_CHART_DEPTH = 10

#: Dispatch statuses at which goods have left the premises. Removal of goods is
#: the tax point the output-tax summary is computed on - see
#: :meth:`GstRepository.output_rows`.
TAXABLE_DISPATCH_STATUSES = IN_FLIGHT_STATUSES + DELIVERED_STATUSES


# -----------------------------------------------------------------------------
# Projections - what the write paths validate against
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartyFacts:
    """A vendor or a customer, named on a document and on its ledger rows."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    #: The party's agreed credit days: the vendor's payment terms, or the
    #: customer's. It is what freezes an invoice's due date at creation.
    payment_terms_days: int


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """The item-master facts an invoice line snapshots."""

    id: uuid.UUID
    code: str
    name: str
    hsn_code: str | None
    gst_rate: Decimal


@dataclass(frozen=True, slots=True)
class PurchaseOrderFacts:
    """The order half of the three-way match."""

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    status: str
    currency: str


@dataclass(frozen=True, slots=True)
class PurchaseOrderLineFacts:
    """What one ordered line agreed, and what has been accepted against it.

    ``accepted_quantity`` is the order line's cumulative accepted total, which
    is the quantity basis when an invoice names no goods receipt.
    """

    id: uuid.UUID
    purchase_order_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal
    discount: Decimal
    tax_rate: Decimal
    received_quantity: Decimal
    accepted_quantity: Decimal
    returned_quantity: Decimal

    @property
    def net_accepted_quantity(self) -> Decimal:
        return self.accepted_quantity - self.returned_quantity


@dataclass(frozen=True, slots=True)
class GoodsReceiptFacts:
    id: uuid.UUID
    code: str
    purchase_order_id: uuid.UUID
    vendor_id: uuid.UUID
    status: str


@dataclass(frozen=True, slots=True)
class GoodsReceiptLineFacts:
    """The receipt half of the three-way match, net of sent-back goods."""

    id: uuid.UUID
    goods_receipt_id: uuid.UUID
    purchase_order_line_id: uuid.UUID
    item_id: uuid.UUID
    received_quantity: Decimal
    accepted_quantity: Decimal
    returned_quantity: Decimal
    unit_cost: Decimal

    @property
    def net_accepted_quantity(self) -> Decimal:
        return self.accepted_quantity - self.returned_quantity


@dataclass(frozen=True, slots=True)
class JobWorkOrderFacts:
    """The order half of the job-work match: who, at what rate, and how."""

    id: uuid.UUID
    code: str
    vendor_id: uuid.UUID
    status: str
    rate_per_unit: Decimal
    rate_basis: str
    cost_centre_id: uuid.UUID | None
    tax_rate: Decimal | None
    sac_code: str | None


@dataclass(frozen=True, slots=True)
class JobWorkReceiptLineFacts:
    """One job-work receipt line: what it earned and what has been billed.

    ``unbilled_charges`` is the money basis of the match - ``charges`` less
    what approved invoices have already billed. ``pending_vendor_debit`` is the
    recovery basis: ``vendor_debit_amount`` less what approvals already posted.
    """

    id: uuid.UUID
    receipt_id: uuid.UUID
    job_work_order_id: uuid.UUID
    line_number: int
    output_item_id: uuid.UUID
    output_quantity: Decimal
    charges: Decimal
    invoiced_amount: Decimal
    vendor_debit_amount: Decimal
    debit_posted_amount: Decimal

    @property
    def unbilled_charges(self) -> Decimal:
        return self.charges - self.invoiced_amount

    @property
    def pending_vendor_debit(self) -> Decimal:
        return self.vendor_debit_amount - self.debit_posted_amount


@dataclass(frozen=True, slots=True)
class PostingAccount:
    """What a journal needs to know about each account it posts to."""

    id: uuid.UUID
    code: str
    #: Decides whether a leg on this account may carry a cost centre.
    account_type: LedgerAccountType


# -----------------------------------------------------------------------------
# Chart of accounts
# -----------------------------------------------------------------------------


class LedgerAccountRepository(BaseRepository[LedgerAccount]):
    """The chart of accounts."""

    model = LedgerAccount
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": LedgerAccount.code,
        "name": LedgerAccount.name,
        "account_type": LedgerAccount.account_type,
        "created_at": LedgerAccount.created_at,
    }
    searchable_fields = ("code", "name")

    def filtered(self, filters: LedgerAccountFilters) -> Select[Any]:
        stmt = self._apply_search(self._scoped(), filters.search)
        if filters.account_type is not None:
            stmt = stmt.where(LedgerAccount.account_type == filters.account_type)
        if filters.parent_id is not None:
            stmt = stmt.where(LedgerAccount.parent_id == filters.parent_id)
        if filters.is_active is not None:
            stmt = stmt.where(LedgerAccount.is_active.is_(filters.is_active))
        if filters.is_system is not None:
            stmt = stmt.where(LedgerAccount.is_system.is_(filters.is_system))
        return stmt

    async def all_codes(self) -> set[str]:
        stmt = select(LedgerAccount.code).where(LedgerAccount.company_id == self.tenant.company_id)
        return set((await self.session.execute(stmt)).scalars().all())

    async def list_page(
        self,
        params: PageParams,
        *,
        stmt: Select[Any] | None = None,
        order_by: Any = None,
        sort: SortParams | None = None,
    ) -> tuple[list[LedgerAccount], OffsetPagination]:
        resolved = order_by
        if resolved is None and sort is not None:
            resolved = sort.resolve(self.sortable_fields, LedgerAccount.code.asc())
        return await super().list_page(params, stmt=stmt, order_by=resolved)

    async def get_by_code(self, code: str) -> LedgerAccount | None:
        return await self.get_by_field("code", code)

    async def by_system_codes(self, codes: Sequence[SystemAccount]) -> dict[str, LedgerAccount]:
        """Resolve the control accounts an automatic posting needs, in one read."""
        if not codes:
            return {}
        stmt = self._scoped().where(LedgerAccount.system_code.in_([str(c) for c in codes]))
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return {str(row.system_code): row for row in rows}

    async def system_account_ids(self, code: SystemAccount) -> list[uuid.UUID]:
        """The control account's id, if the chart has been provisioned."""
        stmt = select(LedgerAccount.id).where(
            LedgerAccount.company_id == self.tenant.company_id,
            LedgerAccount.deleted_at.is_(None),
            LedgerAccount.system_code == str(code),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def posting_accounts(
        self, account_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, PostingAccount]:
        """Which of these accounts exist and are active, with their code and class.

        A journal validates every leg's account in one statement rather than one
        per leg - a twelve-leg posting would otherwise be twelve round trips. The
        class comes back with it because the cost-centre rule needs it, and a
        second lookup for the same rows would be the round trip this avoids.
        """
        if not account_ids:
            return {}
        stmt = select(LedgerAccount.id, LedgerAccount.code, LedgerAccount.account_type).where(
            LedgerAccount.company_id == self.tenant.company_id,
            LedgerAccount.deleted_at.is_(None),
            LedgerAccount.is_active.is_(True),
            LedgerAccount.id.in_(set(account_ids)),
        )
        return {
            row.id: PostingAccount(
                id=row.id, code=row.code, account_type=LedgerAccountType(row.account_type)
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def ancestor_ids(self, account_id: uuid.UUID) -> set[uuid.UUID]:
        """Every account above this one in the chart, in one recursive query.

        Used to refuse a re-parenting that would create a cycle. Walking the
        chain with one SELECT per level would be a query per generation on a
        write path, and an unbounded walk on a chart that is already cyclic -
        hence the depth guard inside the CTE.
        """
        anchor = (
            select(
                LedgerAccount.id.label("id"),
                LedgerAccount.parent_id.label("parent_id"),
                literal(1).label("depth"),
            )
            .where(
                LedgerAccount.company_id == self.tenant.company_id,
                LedgerAccount.id == account_id,
            )
            .cte("chart_ancestors", recursive=True)
        )
        parent = aliased(LedgerAccount)
        walk = anchor.union_all(
            select(parent.id, parent.parent_id, anchor.c.depth + 1).where(
                parent.id == anchor.c.parent_id,
                parent.company_id == self.tenant.company_id,
                anchor.c.depth < MAX_CHART_DEPTH,
            )
        )
        rows = await self.session.execute(select(walk.c.id).where(walk.c.id != account_id))
        return set(rows.scalars().all())

    async def has_postings(self, account_id: uuid.UUID) -> bool:
        """EXISTS probe - an account with history cannot be retired."""
        stmt = select(LedgerEntry.id).where(
            LedgerEntry.company_id == self.tenant.company_id,
            LedgerEntry.account_id == account_id,
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def has_children(self, account_id: uuid.UUID) -> bool:
        stmt = self._scoped().where(LedgerAccount.parent_id == account_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None


# -----------------------------------------------------------------------------
# The general ledger
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementTotals:
    """Header figures for a party statement."""

    opening_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal


class LedgerEntryRepository(BaseRepository[LedgerEntry]):
    """Postings. Append-only: this repository has no update or delete path."""

    model = LedgerEntry

    def _party_scope(self, party_type: PartyType, party_id: uuid.UUID) -> list[Any]:
        return [
            LedgerEntry.company_id == self.tenant.company_id,
            LedgerEntry.party_type == party_type,
            LedgerEntry.party_id == party_id,
        ]

    @staticmethod
    def _signed(direction: int) -> Any:
        """The movement, signed so a positive balance always means "owed".

        A customer is a receivable (debits increase what they owe us) and a
        vendor is a payable (credits increase what we owe them), so the two
        statements differ by the sign and by nothing else.
        """
        return (LedgerEntry.debit_amount - LedgerEntry.credit_amount) * direction

    def _opening_subquery(
        self, party_type: PartyType, party_id: uuid.UUID, direction: int, date_from: date | None
    ) -> Any:
        """Balance carried in from before the window, as a scalar subquery.

        Folded into the row statement so the running balance on page three is
        the real balance rather than the movement since the window opened.
        """
        if date_from is None:
            return literal(ZERO)
        return (
            select(func.coalesce(func.sum(self._signed(direction)), ZERO))
            .where(*self._party_scope(party_type, party_id), LedgerEntry.entry_date < date_from)
            .scalar_subquery()
        )

    def statement_stmt(
        self,
        *,
        party_type: PartyType,
        party_id: uuid.UUID,
        direction: int,
        date_from: date | None,
        date_to: date,
    ) -> Select[Any]:
        """One party's postings with the balance after each, in one statement.

        ``ROWS UNBOUNDED PRECEDING`` rather than the default ``RANGE``: several
        postings legitimately share an ``entry_date``, and RANGE would give every
        one of them the same end-of-day balance instead of the balance after
        each individual row.
        """
        opening = self._opening_subquery(party_type, party_id, direction, date_from)
        running = opening + func.sum(self._signed(direction)).over(
            order_by=(LedgerEntry.entry_date, LedgerEntry.id), rows=(None, 0)
        )

        stmt = (
            select(
                LedgerEntry.id.label("entry_id"),
                LedgerEntry.journal_id.label("journal_id"),
                LedgerEntry.entry_date.label("entry_date"),
                LedgerAccount.code.label("account_code"),
                LedgerAccount.name.label("account_name"),
                LedgerEntry.reference_type.label("reference_type"),
                LedgerEntry.reference_code.label("reference_code"),
                LedgerEntry.narration.label("narration"),
                LedgerEntry.debit_amount.label("debit_amount"),
                LedgerEntry.credit_amount.label("credit_amount"),
                running.label("running_balance"),
                LedgerEntry.is_reversal.label("is_reversal"),
            )
            .select_from(LedgerEntry)
            .join(
                LedgerAccount,
                and_(
                    LedgerAccount.id == LedgerEntry.account_id,
                    LedgerAccount.company_id == LedgerEntry.company_id,
                ),
            )
            .where(*self._party_scope(party_type, party_id), LedgerEntry.entry_date <= date_to)
            .order_by(LedgerEntry.entry_date.asc(), LedgerEntry.id.asc())
        )
        if date_from is not None:
            stmt = stmt.where(LedgerEntry.entry_date >= date_from)
        return stmt

    async def statement_totals(
        self,
        *,
        party_type: PartyType,
        party_id: uuid.UUID,
        direction: int,
        date_from: date | None,
        date_to: date,
    ) -> StatementTotals:
        """Opening, movement and closing for the window, in one aggregate.

        Separate from the row page on purpose: the header must not change as the
        user pages, and computing it from the page would make it do exactly that.
        """
        window = [LedgerEntry.entry_date <= date_to]
        if date_from is not None:
            window.append(LedgerEntry.entry_date >= date_from)

        opening_expr = (
            func.coalesce(
                func.sum(
                    case(
                        (LedgerEntry.entry_date < date_from, self._signed(direction)),
                        else_=ZERO,
                    )
                ),
                ZERO,
            )
            if date_from is not None
            else literal(ZERO)
        )

        in_window = and_(*window)
        stmt = select(
            opening_expr.label("opening"),
            func.coalesce(
                func.sum(case((in_window, LedgerEntry.debit_amount), else_=ZERO)), ZERO
            ).label("debit"),
            func.coalesce(
                func.sum(case((in_window, LedgerEntry.credit_amount), else_=ZERO)), ZERO
            ).label("credit"),
        ).where(*self._party_scope(party_type, party_id), LedgerEntry.entry_date <= date_to)

        row = (await self.session.execute(stmt)).one()
        opening = Decimal(row.opening or 0)
        debit = Decimal(row.debit or 0)
        credit = Decimal(row.credit or 0)
        movement = (debit - credit) * direction
        return StatementTotals(
            opening_balance=opening,
            total_debit=debit,
            total_credit=credit,
            closing_balance=opening + movement,
        )

    async def journal_lines(self, journal_id: uuid.UUID) -> list[LedgerEntry]:
        stmt = (
            self._scoped()
            .where(LedgerEntry.journal_id == journal_id)
            .order_by(LedgerEntry.line_number.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def journal_balances(self, journal_id: uuid.UUID) -> tuple[Decimal, Decimal]:
        """Debit and credit totals of one journal - the audit query.

        Every journal ever written must return an equal pair. Used by the
        integration tests, and available to a reconciliation report.
        """
        stmt = select(
            func.coalesce(func.sum(LedgerEntry.debit_amount), ZERO),
            func.coalesce(func.sum(LedgerEntry.credit_amount), ZERO),
        ).where(
            LedgerEntry.company_id == self.tenant.company_id,
            LedgerEntry.journal_id == journal_id,
        )
        debit, credit = (await self.session.execute(stmt)).one()
        return Decimal(debit or 0), Decimal(credit or 0)

    async def entries_for_reference(
        self, reference_type: str, reference_id: uuid.UUID
    ) -> list[LedgerEntry]:
        """ "What did this invoice/payment post?" - one indexed lookup."""
        stmt = (
            self._scoped()
            .where(
                LedgerEntry.reference_type == reference_type,
                LedgerEntry.reference_id == reference_id,
            )
            .order_by(LedgerEntry.entry_date.asc(), LedgerEntry.line_number.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    def _open_receivable_items(
        self,
        *,
        account_ids: Sequence[uuid.UUID],
        as_of: date,
        party_id: uuid.UUID | None,
        currencies: Sequence[str] | None = None,
    ) -> Any:
        """The FIFO-netted open receivable items, one row per unsettled debit.

        ASSUMPTION: with no customer-invoice document in the schema, an open
        receivable item is a ledger debit on the receivable control account that
        the customer's credits have not yet covered, aged from its posting date.
        Receipts are applied oldest-first, which is the ordinary rule where a
        customer has not said which invoice they are paying.

        The netting is a running sum: order a party's debits by date, and the
        unsettled part of each is ``least(debit, greatest(0, running_total -
        total_credits))``. Doing that in Python would mean fetching every
        posting for every customer.

        ASSUMPTION: **the netting grain is (party, currency), never the party
        alone.** A customer's USD receipt does not settle their INR charge, and
        there is no exchange-rate table in this schema to say what it would
        settle if it did - see ``docs/assumptions.md``, open question 3.
        Partitioning and grouping by ``currency`` keeps a foreign-currency
        receipt from silently ageing away a rupee debt.

        ``currencies`` narrows the *scan* when the caller already knows which
        denominations it is going to render rows for. It is not what makes the
        arithmetic safe - the partition is - so passing ``None`` returns every
        denomination, each netted against itself, and is the ordinary case.

        ASSUMPTION: **a company with no receivable control account has no
        receivables.** ``account_ids`` is a restriction, never an optional
        filter, so an empty set means an empty report - and the guard is spelled
        out below because the natural ``if account_ids:`` spelling means the
        opposite. A tenant whose chart has not been provisioned with an
        ``ACCOUNTS_RECEIVABLE`` account would then get a statement with *no*
        account predicate at all: every ledger debit in the company - salaries,
        stock, tax, capital - aged and presented as customer debt. Reporting
        nothing is recoverable by provisioning the account; reporting the whole
        general ledger as receivable is not.
        """
        scope = [
            LedgerEntry.company_id == self.tenant.company_id,
            LedgerEntry.party_type == PartyType.CUSTOMER,
            LedgerEntry.party_id.is_not(None),
            LedgerEntry.entry_date <= as_of,
            # Unconditional. ``in_([])`` compiles to a false predicate, which is
            # the empty result an unprovisioned chart should produce.
            LedgerEntry.account_id.in_(list(account_ids)),
        ]
        if party_id is not None:
            scope.append(LedgerEntry.party_id == party_id)
        if currencies is not None:
            scope.append(LedgerEntry.currency.in_(list(currencies)))

        base = (
            select(
                LedgerEntry.party_id.label("party_id"),
                LedgerEntry.currency.label("currency"),
                LedgerEntry.entry_date.label("entry_date"),
                func.coalesce(SalesInvoice.due_date, LedgerEntry.entry_date).label("due_date"),
                LedgerEntry.id.label("entry_id"),
                LedgerEntry.debit_amount.label("debit_amount"),
                LedgerEntry.credit_amount.label("credit_amount"),
            )
            .outerjoin(
                SalesInvoice,
                and_(
                    SalesInvoice.company_id == LedgerEntry.company_id,
                    SalesInvoice.id == LedgerEntry.reference_id,
                    LedgerEntry.reference_type == "sales_invoice",
                ),
            )
            .where(*scope)
            .subquery("ageing_base")
        )

        credits = (
            select(
                base.c.party_id.label("party_id"),
                base.c.currency.label("currency"),
                func.sum(base.c.credit_amount).label("settled"),
            )
            .group_by(base.c.party_id, base.c.currency)
            .subquery("ageing_credits")
        )

        debits = (
            select(
                base.c.party_id.label("party_id"),
                base.c.currency.label("currency"),
                base.c.entry_date.label("entry_date"),
                base.c.due_date.label("due_date"),
                base.c.debit_amount.label("debit_amount"),
                func.sum(base.c.debit_amount)
                .over(
                    partition_by=(base.c.party_id, base.c.currency),
                    order_by=(base.c.entry_date, base.c.entry_id),
                    rows=(None, 0),
                )
                .label("running_debit"),
            )
            .where(base.c.debit_amount > 0)
            .subquery("ageing_debits")
        )

        open_amount = func.greatest(
            literal(ZERO),
            func.least(
                debits.c.debit_amount,
                debits.c.running_debit - func.coalesce(credits.c.settled, ZERO),
            ),
        )
        return (
            select(
                debits.c.party_id.label("party_id"),
                debits.c.currency.label("currency"),
                debits.c.due_date.label("entry_date"),
                open_amount.label("open_amount"),
            )
            .select_from(
                debits.outerjoin(
                    credits,
                    and_(
                        credits.c.party_id == debits.c.party_id,
                        credits.c.currency == debits.c.currency,
                    ),
                )
            )
            .subquery("ageing_open_items")
        )

    def receivable_ageing_stmt(
        self,
        *,
        account_ids: Sequence[uuid.UUID],
        as_of: date,
        party_id: uuid.UUID | None = None,
        currencies: Sequence[str] | None = None,
    ) -> Select[Any]:
        """Customer ageing from the ledger, FIFO-netted, one row per (party, currency).

        ``currency`` is part of the grouping key rather than a filter the caller
        has to remember. Every other column here is a ``sum``, and the rows are
        ranked by one of them: a statement grouped by the party alone would add
        rupees to dollars and then put a customer owing 1,000 USD below one
        owing 50,000 INR. Grouping by the denomination makes that arithmetic
        unavailable rather than merely discouraged.

        ``currencies`` narrows the scan to the denominations the caller is going
        to render; it changes which rows come back, never what any of them mean.
        """
        open_items = self._open_receivable_items(
            account_ids=account_ids, as_of=as_of, party_id=party_id, currencies=currencies
        )
        buckets = _bucket_columns(open_items.c.entry_date, open_items.c.open_amount, as_of)
        return (
            select(
                open_items.c.party_id.label("party_id"),
                open_items.c.currency.label("currency"),
                Customer.code.label("party_code"),
                Customer.name.label("party_name"),
                *buckets,
                func.sum(open_items.c.open_amount).label("total_outstanding"),
                func.min(open_items.c.entry_date).label("oldest_due_date"),
                func.count().label("open_document_count"),
            )
            .select_from(
                open_items.join(
                    Customer,
                    and_(
                        Customer.id == open_items.c.party_id,
                        Customer.company_id == self.tenant.company_id,
                    ),
                )
            )
            .where(open_items.c.open_amount > 0)
            .group_by(open_items.c.party_id, open_items.c.currency, Customer.code, Customer.name)
            .order_by(
                open_items.c.currency.asc(),
                func.sum(open_items.c.open_amount).desc(),
                Customer.code.asc(),
            )
        )


# -----------------------------------------------------------------------------
# Vendor invoices
# -----------------------------------------------------------------------------


def _outstanding() -> Any:
    """What an invoice still owes after recoveries, payments and debit notes.

    Written once so no query invents its own. A job-work recovery and an
    allocated purchase-return debit already reduced the payable, so the gross
    figure would overstate it on the outstanding list, in ageing, and in the
    open-invoice filter.
    """
    return (
        VendorInvoice.total_amount
        - VendorInvoice.recovered_amount
        - VendorInvoice.paid_amount
        - VendorInvoice.debit_allocated_amount
    )


def _bucket_columns(due_column: Any, amount_column: Any, as_of: date) -> list[Any]:
    """The five ageing columns, built from :data:`AGEING_BUCKET_BOUNDARIES`.

    Compared as dates rather than as ``as_of - due_date`` day arithmetic: a
    date predicate is sargable against the index on the due date, an interval
    computed per row is not. The boundaries come from the shared constant, so
    the SQL and :func:`~src.modules.finance.service.ageing_bucket` cannot
    disagree about where a bucket ends.
    """
    first, second, third = AGEING_BUCKET_BOUNDARIES
    edge_1 = as_of - timedelta(days=first)
    edge_2 = as_of - timedelta(days=second)
    edge_3 = as_of - timedelta(days=third)

    def bucket(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, amount_column), else_=ZERO)), ZERO)

    return [
        bucket(and_(due_column <= as_of, due_column >= edge_1)).label("bucket_0_30"),
        bucket(and_(due_column < edge_1, due_column >= edge_2)).label("bucket_31_60"),
        bucket(and_(due_column < edge_2, due_column >= edge_3)).label("bucket_61_90"),
        bucket(due_column < edge_3).label("bucket_90_plus"),
        bucket(due_column > as_of).label("not_due"),
    ]


#: The ageing columns a report carries a grand total for. Both ageing
#: statements - payables and receivables - label their columns from this tuple,
#: so one totals query serves either.
AGEING_TOTAL_COLUMNS: tuple[str, ...] = (
    "bucket_0_30",
    "bucket_31_60",
    "bucket_61_90",
    "bucket_90_plus",
    "not_due",
    "total_outstanding",
)


@dataclass(frozen=True, slots=True)
class AgeingCurrencyTotal:
    """One denomination's grand totals, and how many parties are behind them.

    ``party_count`` is the number of parties with an open item in this
    denomination across the *whole* statement, not the number of rows a capped
    report chose to show. A caller comparing it against ``len(rows)`` can tell
    that it is looking at the worst offenders rather than all of them.

    ``document_count`` is the number of open items behind those parties, on the
    same whole-statement basis. Both are *counts*, and that is what makes them
    useful: they are the only figures here that mean the same thing in every
    denomination, so they are what a caller ranks denominations by. The money
    columns cannot be compared across two of these objects at all - there is no
    exchange rate in this schema with which to do it.
    """

    currency: str
    party_count: int
    totals: dict[str, Decimal]
    document_count: int = 0

    @property
    def activity_rank(self) -> tuple[int, int, str]:
        """Ascending sort key that puts the busiest denomination first.

        The counts are negated so ``min`` and a plain ``sorted`` both pick the
        denomination with the most open documents, and the currency code breaks
        whatever tie is left - so the same book always ranks the same way.
        """
        return (-self.document_count, -self.party_count, self.currency)


async def ageing_totals_by_currency(
    session: AsyncSession, stmt: Select[Any]
) -> list[AgeingCurrencyTotal]:
    """Grand totals per denomination, independent of any row cap.

    Two properties, and the report depends on both.

    **Nothing is summed across denominations.** The ageing statements group by
    ``currency``, so this aggregate keeps that column in its own ``GROUP BY``
    rather than collapsing it. There is no arrangement of these results that
    produces a figure spanning two currencies, because no such figure exists.

    **The totals are the whole statement's, not the returned rows'.** An ageing
    report shows a bounded number of parties. Adding up the rows that came back
    would report that page's totals as the company's, silently understating the
    payables of any company with more parties than the cap - and the figure a
    finance team reconciles against the balance sheet is exactly that total.

    **The denominations come back ordered by activity, not by amount.** Open
    documents descending, then parties, then the code. Ordering them by
    ``sum(total_outstanding)`` - which is what this did - ranks a dollar figure
    against a rupee one, and this whole file exists because that comparison has
    no meaning without an exchange rate the schema does not have. It is not a
    cosmetic ordering either: the caller draws its headline block and its
    detailed rows from the front of this list, so an ordering by unconverted
    magnitude decides which denomination a reader sees first purely on which
    currency has the bigger *numbers*. Counts are the same unit everywhere.
    """
    inner = stmt.order_by(None).subquery("ageing_all")
    document_count = func.coalesce(func.sum(inner.c.open_document_count), 0)
    totals = (
        select(
            inner.c.currency.label("currency"),
            func.count().label("party_count"),
            document_count.label("document_count"),
            *[
                func.coalesce(func.sum(inner.c[name]), ZERO).label(name)
                for name in AGEING_TOTAL_COLUMNS
            ],
        )
        .select_from(inner)
        .group_by(inner.c.currency)
        .order_by(document_count.desc(), func.count().desc(), inner.c.currency.asc())
    )
    return [
        AgeingCurrencyTotal(
            currency=str(row.currency),
            party_count=int(row.party_count or 0),
            document_count=int(row.document_count or 0),
            totals={name: Decimal(getattr(row, name) or 0) for name in AGEING_TOTAL_COLUMNS},
        )
        for row in (await session.execute(totals)).all()
    ]


def ranked_ageing_rows_stmt(stmt: Select[Any], *, limit: int) -> Select[Any]:
    """Cap an ageing statement to the worst ``limit`` parties *per denomination*.

    A single ``LIMIT`` over a multi-currency statement would spend the whole cap
    on whichever denomination happened to sort first and return nothing at all
    for the others - the report would be complete in its totals and empty in its
    detail. ``row_number()`` partitioned by currency gives every denomination
    its own cap.

    The ranking is by outstanding descending within the partition, with the
    party code as the tiebreaker: two parties owing the same amount would
    otherwise swap places between runs, and a report that reorders itself when
    nothing changed is a report nobody trusts.
    """
    base = stmt.order_by(None).subquery("ageing_rows_base")
    rank = (
        func.row_number()
        .over(
            partition_by=base.c.currency,
            order_by=(base.c.total_outstanding.desc(), base.c.party_code.asc()),
        )
        .label("party_rank")
    )
    ranked = select(*base.c, rank).subquery("ageing_rows_ranked")
    return (
        select(*ranked.c)
        .where(ranked.c.party_rank <= limit)
        .order_by(ranked.c.currency.asc(), ranked.c.party_rank.asc())
    )


class VendorInvoiceRepository(BaseRepository[VendorInvoice]):
    """Purchase invoices, the payables list and the ageing report."""

    model = VendorInvoice

    def _scoped(self, *, include_deleted: bool = False) -> Select[Any]:
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(VendorInvoice.vendor_id == self.tenant.vendor_id)
        return stmt

    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": VendorInvoice.code,
        "invoice_date": VendorInvoice.invoice_date,
        "due_date": VendorInvoice.due_date,
        "total_amount": VendorInvoice.total_amount,
        "status": VendorInvoice.status,
        "created_at": VendorInvoice.created_at,
    }
    searchable_fields = ("code", "vendor_invoice_number")

    def filtered(self, filters: VendorInvoiceFilters, *, as_of: date) -> Select[Any]:
        stmt = self._apply_search(self._scoped(), filters.search)
        if filters.status is not None:
            stmt = stmt.where(VendorInvoice.status == filters.status)
        if filters.match_status is not None:
            stmt = stmt.where(VendorInvoice.match_status == filters.match_status)
        if filters.invoice_type is not None:
            stmt = stmt.where(VendorInvoice.invoice_type == filters.invoice_type)
        if filters.vendor_id is not None:
            stmt = stmt.where(VendorInvoice.vendor_id == filters.vendor_id)
        if filters.purchase_order_id is not None:
            stmt = stmt.where(VendorInvoice.purchase_order_id == filters.purchase_order_id)
        if filters.job_work_order_id is not None:
            stmt = stmt.where(VendorInvoice.job_work_order_id == filters.job_work_order_id)
        if filters.invoice_from is not None:
            stmt = stmt.where(VendorInvoice.invoice_date >= filters.invoice_from)
        if filters.invoice_to is not None:
            stmt = stmt.where(VendorInvoice.invoice_date <= filters.invoice_to)
        if filters.due_before is not None:
            stmt = stmt.where(VendorInvoice.due_date < filters.due_before)
        if filters.open_only or filters.overdue_only:
            stmt = stmt.where(
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES), _outstanding() > ZERO
            )
        if filters.overdue_only:
            stmt = stmt.where(VendorInvoice.due_date < as_of)
        return stmt

    async def list_page(
        self,
        params: PageParams,
        *,
        stmt: Select[Any] | None = None,
        order_by: Any = None,
        sort: SortParams | None = None,
    ) -> tuple[list[VendorInvoice], OffsetPagination]:
        resolved = order_by
        if resolved is None and sort is not None:
            resolved = sort.resolve(self.sortable_fields, VendorInvoice.invoice_date.desc())
        return await super().list_page(params, stmt=stmt, order_by=resolved)

    async def lock_for_update(self, invoice_id: uuid.UUID) -> VendorInvoice | None:
        """Take the header's row lock before touching ``paid_amount``.

        Allocation is a read-modify-write over the paid total and the status,
        and two payments settling one invoice would otherwise each add their
        amount to the value they read first.
        """
        stmt = self._scoped().where(VendorInvoice.id == invoice_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_many(self, invoice_ids: Sequence[uuid.UUID]) -> list[VendorInvoice]:
        """Lock a whole allocation's invoices, ordered by primary key.

        The order matters: two payments allocating across the same pair of
        invoices in opposite orders would deadlock. Sorting the set gives every
        caller the same acquisition order (database.md section 4.4).
        """
        if not invoice_ids:
            return []
        stmt = (
            self._scoped()
            .where(VendorInvoice.id.in_(set(invoice_ids)))
            .order_by(VendorInvoice.id.asc())
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def find_duplicate(
        self, vendor_id: uuid.UUID, vendor_invoice_number: str, *, exclude_id: uuid.UUID | None
    ) -> VendorInvoice | None:
        """Has this vendor already billed this number?

        The unique index is the guarantee; this read exists so the caller is
        told *which* invoice collides rather than receiving an integrity error
        naming a constraint they have never heard of.
        """
        stmt = self._scoped().where(
            VendorInvoice.vendor_id == vendor_id,
            VendorInvoice.vendor_invoice_number == vendor_invoice_number,
        )
        if exclude_id is not None:
            stmt = stmt.where(VendorInvoice.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    def outstanding_stmt(
        self,
        *,
        as_of: date,
        vendor_id: uuid.UUID | None,
        overdue_only: bool,
        currency: str | None = None,
    ) -> Select[Any]:
        """Open invoices with the vendor named, newest due first.

        Joined to ``vendors`` without a soft-delete filter: a vendor retired
        last month is still owed the money, and dropping the row would make the
        debt vanish from the report rather than from the balance sheet.

        Each row carries the invoice's own ``currency``. This is a list of
        documents rather than an aggregate, so nothing here adds two
        denominations together - but an amount rendered without its currency is
        still unreadable, and the ``currency`` filter is what lets a caller line
        this list up against a single-currency ageing report.
        """
        stmt = (
            select(
                VendorInvoice.id.label("invoice_id"),
                VendorInvoice.code.label("code"),
                VendorInvoice.vendor_id.label("vendor_id"),
                Vendor.code.label("vendor_code"),
                Vendor.name.label("vendor_name"),
                VendorInvoice.vendor_invoice_number.label("vendor_invoice_number"),
                VendorInvoice.invoice_date.label("invoice_date"),
                VendorInvoice.due_date.label("due_date"),
                VendorInvoice.status.label("status"),
                VendorInvoice.currency.label("currency"),
                VendorInvoice.total_amount.label("total_amount"),
                VendorInvoice.paid_amount.label("paid_amount"),
                _outstanding().label("outstanding_amount"),
            )
            .select_from(VendorInvoice)
            .join(
                Vendor,
                and_(
                    Vendor.id == VendorInvoice.vendor_id,
                    Vendor.company_id == VendorInvoice.company_id,
                ),
            )
            .where(
                VendorInvoice.company_id == self.tenant.company_id,
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES),
                _outstanding() > ZERO,
            )
            .order_by(VendorInvoice.due_date.asc(), VendorInvoice.id.asc())
        )
        if vendor_id is not None:
            stmt = stmt.where(VendorInvoice.vendor_id == vendor_id)
        if overdue_only:
            stmt = stmt.where(VendorInvoice.due_date < as_of)
        if currency is not None:
            stmt = stmt.where(VendorInvoice.currency == currency)
        return stmt

    def payable_ageing_stmt(
        self,
        *,
        as_of: date,
        vendor_id: uuid.UUID | None,
        currencies: Sequence[str] | None = None,
    ) -> Select[Any]:
        """Payables ageing: one row per (vendor, currency), one aggregate query.

        Aged on ``due_date`` rather than on the invoice date, because what
        matters is how long a bill has been *late*, not how long ago it was
        written.

        ``currency`` is in the grouping key rather than being a filter the
        caller must remember to pass. Every bucket is a ``sum`` and the rows are
        ranked by one, so a statement grouped by the vendor alone would add
        rupees to dollars and then sort vendors by the total - putting a vendor
        owed 1,000 USD below one owed 50,000 INR. There is no exchange-rate
        table in this schema to convert with (``docs/assumptions.md``, open
        question 3), so no sum here ever spans two denominations.

        ``currencies`` narrows the scan to the denominations the caller is going
        to render rows for. It is an optimisation, not the safety property.
        """
        buckets = _bucket_columns(VendorInvoice.due_date, _outstanding(), as_of)
        stmt = (
            select(
                VendorInvoice.vendor_id.label("party_id"),
                VendorInvoice.currency.label("currency"),
                Vendor.code.label("party_code"),
                Vendor.name.label("party_name"),
                *buckets,
                func.sum(_outstanding()).label("total_outstanding"),
                func.min(VendorInvoice.due_date).label("oldest_due_date"),
                func.count().label("open_document_count"),
            )
            .select_from(VendorInvoice)
            .join(
                Vendor,
                and_(
                    Vendor.id == VendorInvoice.vendor_id,
                    Vendor.company_id == VendorInvoice.company_id,
                ),
            )
            .where(
                VendorInvoice.company_id == self.tenant.company_id,
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES),
                _outstanding() > ZERO,
            )
            .group_by(VendorInvoice.vendor_id, VendorInvoice.currency, Vendor.code, Vendor.name)
            .order_by(
                VendorInvoice.currency.asc(),
                func.sum(_outstanding()).desc(),
                Vendor.code.asc(),
            )
        )
        if vendor_id is not None:
            stmt = stmt.where(VendorInvoice.vendor_id == vendor_id)
        if currencies is not None:
            stmt = stmt.where(VendorInvoice.currency.in_(list(currencies)))
        return stmt


class VendorInvoiceLineRepository(BaseRepository[VendorInvoiceLine]):
    model = VendorInvoiceLine

    async def list_for_invoice(self, invoice_id: uuid.UUID) -> list[VendorInvoiceLine]:
        stmt = (
            self._scoped()
            .where(VendorInvoiceLine.vendor_invoice_id == invoice_id)
            .order_by(VendorInvoiceLine.line_number.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def list_for_invoices(
        self, invoice_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[VendorInvoiceLine]]:
        """Lines for a page of invoices in one query - the answer to an N+1."""
        if not invoice_ids:
            return {}
        stmt = (
            self._scoped()
            .where(VendorInvoiceLine.vendor_invoice_id.in_(set(invoice_ids)))
            .order_by(
                VendorInvoiceLine.vendor_invoice_id.asc(), VendorInvoiceLine.line_number.asc()
            )
        )
        grouped: dict[uuid.UUID, list[VendorInvoiceLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().unique().all():
            grouped.setdefault(line.vendor_invoice_id, []).append(line)
        return grouped

    async def delete_for_invoice(self, invoice_id: uuid.UUID) -> int:
        """Drop the whole line set before rewriting it.

        A vendor invoice line has no history of its own - the invoice is the
        document - so replacing the set on an unapproved bill is a hard delete
        rather than a soft one (database.md section 1).
        """
        from sqlalchemy import delete

        stmt = delete(VendorInvoiceLine).where(
            VendorInvoiceLine.company_id == self.tenant.company_id,
            VendorInvoiceLine.vendor_invoice_id == invoice_id,
        )
        return affected_rows(await self.session.execute(stmt))


# -----------------------------------------------------------------------------
# Payments
# -----------------------------------------------------------------------------


class PaymentRepository(BaseRepository[Payment]):
    model = Payment

    def _scoped(self, *, include_deleted: bool = False) -> Select[Any]:
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(
                Payment.party_type == PartyType.VENDOR, Payment.party_id == self.tenant.vendor_id
            )
        return stmt

    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Payment.code,
        "payment_date": Payment.payment_date,
        "amount": Payment.amount,
        "status": Payment.status,
        "created_at": Payment.created_at,
    }
    searchable_fields = ("code", "reference_number")

    def filtered(self, filters: PaymentFilters) -> Select[Any]:
        stmt = self._apply_search(self._scoped(), filters.search)
        if filters.status is not None:
            stmt = stmt.where(Payment.status == filters.status)
        if filters.payment_type is not None:
            stmt = stmt.where(Payment.payment_type == filters.payment_type)
        if filters.party_type is not None:
            stmt = stmt.where(Payment.party_type == filters.party_type)
        if filters.party_id is not None:
            stmt = stmt.where(Payment.party_id == filters.party_id)
        if filters.payment_method is not None:
            stmt = stmt.where(Payment.payment_method == filters.payment_method)
        if filters.date_from is not None:
            stmt = stmt.where(Payment.payment_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Payment.payment_date <= filters.date_to)
        if filters.unallocated_only:
            stmt = stmt.where(
                Payment.status.in_((PaymentStatus.APPROVED, PaymentStatus.PARTIALLY_ALLOCATED)),
                Payment.allocated_amount < Payment.amount,
            )
        return stmt

    async def list_page(
        self,
        params: PageParams,
        *,
        stmt: Select[Any] | None = None,
        order_by: Any = None,
        sort: SortParams | None = None,
    ) -> tuple[list[Payment], OffsetPagination]:
        resolved = order_by
        if resolved is None and sort is not None:
            resolved = sort.resolve(self.sortable_fields, Payment.payment_date.desc())
        return await super().list_page(params, stmt=stmt, order_by=resolved)

    async def lock_for_update(self, payment_id: uuid.UUID) -> Payment | None:
        """Serialise allocation on the payment header.

        Allocating is a read-modify-write over ``allocated_amount``; without
        this lock two concurrent allocations could each believe the whole
        payment was free.
        """
        stmt = self._scoped().where(Payment.id == payment_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()


class PaymentAllocationRepository(BaseRepository[PaymentAllocation]):
    model = PaymentAllocation

    async def list_for_payment(self, payment_id: uuid.UUID) -> list[PaymentAllocation]:
        stmt = (
            self._scoped()
            .where(PaymentAllocation.payment_id == payment_id)
            .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def invoice_codes(
        self, invoice_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, str]]:
        """Invoice code and vendor number for a payment's allocations, batched."""
        if not invoice_ids:
            return {}
        stmt = select(
            VendorInvoice.id, VendorInvoice.code, VendorInvoice.vendor_invoice_number
        ).where(
            VendorInvoice.company_id == self.tenant.company_id,
            VendorInvoice.id.in_(set(invoice_ids)),
        )
        return {
            row.id: (row.code, row.vendor_invoice_number)
            for row in (await self.session.execute(stmt)).all()
        }

    async def list_for_invoice(self, invoice_id: uuid.UUID) -> list[PaymentAllocation]:
        stmt = (
            self._scoped()
            .where(PaymentAllocation.vendor_invoice_id == invoice_id)
            # created_at is PostgreSQL now() - transaction START time - so every row
            # written in one transaction shares it. The UUIDv7 id is the
            # tiebreaker that recovers true insertion order.
            .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def delete_for_payment(self, payment_id: uuid.UUID) -> int:
        from sqlalchemy import delete

        stmt = delete(PaymentAllocation).where(
            PaymentAllocation.company_id == self.tenant.company_id,
            PaymentAllocation.payment_id == payment_id,
        )
        return affected_rows(await self.session.execute(stmt))


# -----------------------------------------------------------------------------
# Batched lookups into other modules' tables
# -----------------------------------------------------------------------------


class VendorLookupRepository(BaseRepository[Vendor]):
    """Vendor facts, without dragging the whole wide row across."""

    model = Vendor

    async def get_facts(self, vendor_id: uuid.UUID) -> PartyFacts | None:
        stmt = select(
            Vendor.id,
            Vendor.code,
            Vendor.name,
            Vendor.is_active,
            Vendor.payment_terms_days,
        ).where(
            Vendor.company_id == self.tenant.company_id,
            Vendor.deleted_at.is_(None),
            Vendor.id == vendor_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return PartyFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            payment_terms_days=row.payment_terms_days,
        )


class CustomerLookupRepository(BaseRepository[Customer]):
    model = Customer

    async def get_facts(self, customer_id: uuid.UUID) -> PartyFacts | None:
        stmt = select(
            Customer.id,
            Customer.code,
            Customer.name,
            Customer.is_active,
            Customer.credit_days,
        ).where(
            Customer.company_id == self.tenant.company_id,
            Customer.deleted_at.is_(None),
            Customer.id == customer_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return PartyFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            payment_terms_days=row.credit_days,
        )


class ItemLookupRepository(BaseRepository[Item]):
    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ItemFacts]:
        if not item_ids:
            return {}
        stmt = select(Item.id, Item.code, Item.name, Item.hsn_code, Item.gst_rate).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(set(item_ids)),
        )
        return {
            row.id: ItemFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                hsn_code=row.hsn_code,
                gst_rate=Decimal(row.gst_rate or 0),
            )
            for row in (await self.session.execute(stmt)).all()
        }


class UomLookupRepository(BaseRepository[UnitOfMeasure]):
    model = UnitOfMeasure

    async def existing(self, uom_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not uom_ids:
            return set()
        stmt = select(UnitOfMeasure.id).where(
            UnitOfMeasure.company_id == self.tenant.company_id,
            UnitOfMeasure.deleted_at.is_(None),
            UnitOfMeasure.id.in_(set(uom_ids)),
        )
        return set((await self.session.execute(stmt)).scalars().all())


class PurchaseOrderLookupRepository(BaseRepository[PurchaseOrder]):
    """The order half of the three-way match."""

    model = PurchaseOrder

    async def get_facts(self, order_id: uuid.UUID) -> PurchaseOrderFacts | None:
        stmt = select(
            PurchaseOrder.id,
            PurchaseOrder.code,
            PurchaseOrder.vendor_id,
            PurchaseOrder.status,
            PurchaseOrder.currency,
        ).where(
            PurchaseOrder.company_id == self.tenant.company_id,
            PurchaseOrder.id == order_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return PurchaseOrderFacts(
            id=row.id,
            code=row.code,
            vendor_id=row.vendor_id,
            status=str(row.status),
            currency=row.currency,
        )


class PurchaseOrderLineLookupRepository(BaseRepository[PurchaseOrderLine]):
    model = PurchaseOrderLine

    async def load(self, line_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PurchaseOrderLineFacts]:
        """Every ordered line an invoice references, in one query."""
        if not line_ids:
            return {}
        stmt = select(
            PurchaseOrderLine.id,
            PurchaseOrderLine.purchase_order_id,
            PurchaseOrderLine.line_number,
            PurchaseOrderLine.item_id,
            PurchaseOrderLine.quantity,
            PurchaseOrderLine.unit_price,
            PurchaseOrderLine.discount,
            PurchaseOrderLine.tax_rate,
            PurchaseOrderLine.received_quantity,
            PurchaseOrderLine.accepted_quantity,
            PurchaseOrderLine.returned_quantity,
        ).where(
            PurchaseOrderLine.company_id == self.tenant.company_id,
            PurchaseOrderLine.id.in_(set(line_ids)),
        )
        return {
            row.id: PurchaseOrderLineFacts(
                id=row.id,
                purchase_order_id=row.purchase_order_id,
                line_number=row.line_number,
                item_id=row.item_id,
                quantity=Decimal(row.quantity),
                unit_price=Decimal(row.unit_price),
                discount=Decimal(row.discount),
                tax_rate=Decimal(row.tax_rate),
                received_quantity=Decimal(row.received_quantity),
                accepted_quantity=Decimal(row.accepted_quantity),
                returned_quantity=Decimal(row.returned_quantity),
            )
            for row in (await self.session.execute(stmt)).all()
        }


class GoodsReceiptLookupRepository(BaseRepository[GoodsReceipt]):
    """The receipt half of the three-way match."""

    model = GoodsReceipt

    async def get_facts(self, receipt_id: uuid.UUID) -> GoodsReceiptFacts | None:
        stmt = select(
            GoodsReceipt.id,
            GoodsReceipt.code,
            GoodsReceipt.purchase_order_id,
            GoodsReceipt.vendor_id,
            GoodsReceipt.status,
        ).where(
            GoodsReceipt.company_id == self.tenant.company_id,
            GoodsReceipt.id == receipt_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return GoodsReceiptFacts(
            id=row.id,
            code=row.code,
            purchase_order_id=row.purchase_order_id,
            vendor_id=row.vendor_id,
            status=str(row.status),
        )


class GoodsReceiptLineLookupRepository(BaseRepository[GoodsReceiptLine]):
    model = GoodsReceiptLine

    async def load(self, line_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, GoodsReceiptLineFacts]:
        if not line_ids:
            return {}
        return await self._load_where(GoodsReceiptLine.id.in_(set(line_ids)))

    async def load_for_receipt(
        self, receipt_id: uuid.UUID
    ) -> dict[uuid.UUID, GoodsReceiptLineFacts]:
        """The receipt's lines keyed by the *order line* they received against.

        That is the key the matcher joins on: an invoice line names the order
        line, and the question is what arrived against it.
        """
        rows = await self._load_where(GoodsReceiptLine.goods_receipt_id == receipt_id)
        return {facts.purchase_order_line_id: facts for facts in rows.values()}

    async def _load_where(self, *conditions: Any) -> dict[uuid.UUID, GoodsReceiptLineFacts]:
        stmt = select(
            GoodsReceiptLine.id,
            GoodsReceiptLine.goods_receipt_id,
            GoodsReceiptLine.purchase_order_line_id,
            GoodsReceiptLine.item_id,
            GoodsReceiptLine.received_quantity,
            GoodsReceiptLine.accepted_quantity,
            GoodsReceiptLine.returned_quantity,
            GoodsReceiptLine.unit_cost,
        ).where(GoodsReceiptLine.company_id == self.tenant.company_id, *conditions)
        return {
            row.id: GoodsReceiptLineFacts(
                id=row.id,
                goods_receipt_id=row.goods_receipt_id,
                purchase_order_line_id=row.purchase_order_line_id,
                item_id=row.item_id,
                received_quantity=Decimal(row.received_quantity),
                accepted_quantity=Decimal(row.accepted_quantity),
                returned_quantity=Decimal(row.returned_quantity),
                unit_cost=Decimal(row.unit_cost),
            )
            for row in (await self.session.execute(stmt)).all()
        }


class JobWorkOrderLookupRepository(BaseRepository[JobWorkOrder]):
    """The order half of the job-work match."""

    model = JobWorkOrder

    async def get_facts(self, order_id: uuid.UUID) -> JobWorkOrderFacts | None:
        stmt = select(
            JobWorkOrder.id,
            JobWorkOrder.code,
            JobWorkOrder.vendor_id,
            JobWorkOrder.status,
            JobWorkOrder.rate_per_unit,
            JobWorkOrder.rate_basis,
            JobWorkOrder.cost_centre_id,
            JobWorkOrder.tax_rate,
            JobWorkOrder.sac_code,
        ).where(
            JobWorkOrder.company_id == self.tenant.company_id,
            JobWorkOrder.id == order_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return JobWorkOrderFacts(
            id=row.id,
            code=row.code,
            vendor_id=row.vendor_id,
            status=str(row.status),
            rate_per_unit=Decimal(row.rate_per_unit),
            rate_basis=str(row.rate_basis),
            cost_centre_id=row.cost_centre_id,
            tax_rate=Decimal(row.tax_rate) if row.tax_rate is not None else None,
            sac_code=row.sac_code,
        )


class JobWorkReceiptLineLookupRepository(BaseRepository[JobWorkReceiptLine]):
    """The receipt half of the job-work match: what each lot earned."""

    model = JobWorkReceiptLine

    def _columns(self) -> Any:
        return (
            select(
                JobWorkReceiptLine.id,
                JobWorkReceiptLine.receipt_id,
                JobWorkReceipt.job_work_order_id,
                JobWorkReceiptLine.line_number,
                JobWorkReceiptLine.output_item_id,
                JobWorkReceiptLine.output_quantity,
                JobWorkReceiptLine.charges,
                JobWorkReceiptLine.invoiced_amount,
                JobWorkReceiptLine.vendor_debit_amount,
                JobWorkReceiptLine.debit_posted_amount,
            )
            .select_from(JobWorkReceiptLine)
            .join(
                JobWorkReceipt,
                JobWorkReceipt.id == JobWorkReceiptLine.receipt_id,
            )
        )

    def _facts(self, row: Any) -> JobWorkReceiptLineFacts:
        return JobWorkReceiptLineFacts(
            id=row.id,
            receipt_id=row.receipt_id,
            job_work_order_id=row.job_work_order_id,
            line_number=row.line_number,
            output_item_id=row.output_item_id,
            output_quantity=Decimal(row.output_quantity),
            charges=Decimal(row.charges),
            invoiced_amount=Decimal(row.invoiced_amount),
            vendor_debit_amount=Decimal(row.vendor_debit_amount),
            debit_posted_amount=Decimal(row.debit_posted_amount),
        )

    async def load(self, line_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, JobWorkReceiptLineFacts]:
        """Every job-work receipt line an invoice references, in one query."""
        if not line_ids:
            return {}
        stmt = self._columns().where(
            JobWorkReceiptLine.company_id == self.tenant.company_id,
            JobWorkReceipt.company_id == self.tenant.company_id,
            JobWorkReceiptLine.id.in_(set(line_ids)),
        )
        return {row.id: self._facts(row) for row in (await self.session.execute(stmt)).all()}

    async def lock_lines(
        self, line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, JobWorkReceiptLine]:
        """Lock receipt lines for the billing write, in id order.

        The order matters: two invoices billing overlapping line sets in
        opposite orders would deadlock. Sorting the set gives every caller the
        same acquisition order (database.md section 4.4).
        """
        if not line_ids:
            return {}
        stmt = (
            self._scoped()
            .where(JobWorkReceiptLine.id.in_(set(line_ids)))
            .order_by(JobWorkReceiptLine.id.asc())
            .with_for_update()
        )
        rows = list((await self.session.execute(stmt)).scalars().unique().all())
        return {row.id: row for row in rows}


class FinancialYearRepository(BaseRepository[FinancialYear]):
    """The period a posting belongs to, and whether it is still open."""

    model = FinancialYear

    async def containing(self, on: date) -> FinancialYear | None:
        stmt = self._scoped().where(FinancialYear.start_date <= on, FinancialYear.end_date >= on)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()


class ReportingCurrencyRepository(BaseRepository[Company]):
    """The one currency a finance report may add figures up in.

    ``companies.currency`` is the tenant's own denomination - the one the
    organization module already lets an administrator set, and the one a balance
    sheet is drawn in. With no exchange-rate table in this schema
    (``docs/assumptions.md``, open question 3), it is the only defensible
    default for a report that must not mix denominations.

    ``companies`` has no ``company_id``: the tenant filter is the primary key
    itself, exactly as ``organization.repository.CompanyRepository`` documents.
    Deliberately no soft-delete predicate either - the denomination a company's
    books are kept in is a fact about them whether or not the tenant is still
    live, and falling back to a global default for an archived company would
    silently restate its history in another currency.
    """

    model = Company

    def _scoped(self, *, include_deleted: bool = False) -> Select[Any]:
        return select(Company).where(Company.id == self.tenant.company_id)

    async def reporting_currency(self) -> str:
        stmt = select(Company.currency).where(Company.id == self.tenant.company_id)
        found = (await self.session.execute(stmt.limit(1))).scalar_one_or_none()
        # ASSUMPTION: a tenant whose company row cannot be read falls back to
        # the module's default denomination rather than failing the report. The
        # figures are still single-currency and the response names the currency
        # they are in, so the caller can see the fallback rather than inherit it
        # silently.
        return str(found) if found else DEFAULT_CURRENCY


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


class InventoryValuationRepository(BaseRepository[InventoryBalance]):
    """On-hand stock valued at what its FIFO cost layers actually cost.

    Grouped up from the balance grain (item, warehouse, location, batch) to
    (item, warehouse), because that is the grain a valuation is reported and
    reconciled at. The join to ``items`` deliberately does not filter
    ``deleted_at``: a retired item that still has stock still has value, and
    dropping it would make the report disagree with the balance sheet.

    ``value`` is ``sum(stock_value)`` - what the inventory engine last wrote on
    each position, being the sum of its open cost layers rounded once at the
    column's four-place scale. ``recorded_value`` is the same total recomputed
    **from the layer rows themselves**, and the two are reported side by side so
    a disagreement is visible.

    It has to be recomputed from the layers, and this is the second attempt.
    While valuation was a moving average the cross-check was
    ``sum(on_hand x average_cost)`` against ``sum(stock_value)``, and those were
    two independently maintained numbers, so comparing them was a real check.
    Under FIFO they are not: ``average_cost`` is *derived* from ``stock_value``
    (it is ``stock_value / on_hand``, written by the same statement), so
    multiplying it back up compares a figure with itself and the check became
    tautological - it could no longer detect the one thing it exists to detect,
    a balance that has drifted from the layers behind it. Reaching past the
    balance into ``inventory_cost_layers`` restores it: a position whose
    ``stock_value`` was written by something other than the engine, or whose
    layers were repaired, now shows a difference.

    The subquery is correlated on ``balance_id`` rather than joined, because a
    join to the layers would fan each balance row out to one row per layer and
    ``sum(stock_value)`` would then count the same position once per layer it
    holds. It is one lookup on ``ix_inventory_cost_layers_open`` per balance row
    the report already reads, so it scales with the filter rather than with the
    company.

    A small difference is expected and is not a defect: ``value`` is rounded per
    position at four places, the layer product is not rounded at all, and the
    two are added up in different orders. A material one is a reconciliation
    item.
    """

    model = InventoryBalance

    def valuation_stmt(self, filters: ValuationFilters) -> Select[Any]:
        value = func.sum(InventoryBalance.stock_value)
        quantity = func.sum(InventoryBalance.on_hand_quantity)
        # The independent half of the reconciliation: what this position's open
        # FIFO layers actually add up to, read from the layer rows and not from
        # anything the balance says about itself. The on-hand queue only: the
        # scrap queue remembers what scrapped stock cost and carries no book
        # value (CostLayerPool), so it is no part of what the stock is worth.
        layer_value = (
            select(
                func.coalesce(
                    func.sum(InventoryCostLayer.remaining_quantity * InventoryCostLayer.unit_cost),
                    ZERO,
                )
            )
            .where(
                InventoryCostLayer.company_id == InventoryBalance.company_id,
                InventoryCostLayer.balance_id == InventoryBalance.id,
                InventoryCostLayer.remaining_quantity > ZERO,
                InventoryCostLayer.pool == CostLayerPool.ON_HAND,
            )
            .correlate(InventoryBalance)
            .scalar_subquery()
        )

        stmt = (
            select(
                InventoryBalance.item_id.label("item_id"),
                Item.code.label("item_code"),
                Item.name.label("item_name"),
                InventoryBalance.warehouse_id.label("warehouse_id"),
                Warehouse.code.label("warehouse_code"),
                Warehouse.name.label("warehouse_name"),
                quantity.label("on_hand_quantity"),
                value.label("value"),
                func.sum(layer_value).label("recorded_value"),
            )
            .select_from(InventoryBalance)
            .join(
                Item,
                and_(
                    Item.id == InventoryBalance.item_id,
                    Item.company_id == InventoryBalance.company_id,
                ),
            )
            .join(
                Warehouse,
                and_(
                    Warehouse.id == InventoryBalance.warehouse_id,
                    Warehouse.company_id == InventoryBalance.company_id,
                ),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.on_hand_quantity > ZERO,
            )
            .group_by(
                InventoryBalance.item_id,
                Item.code,
                Item.name,
                InventoryBalance.warehouse_id,
                Warehouse.code,
                Warehouse.name,
            )
        )

        if filters.warehouse_id is not None:
            stmt = stmt.where(InventoryBalance.warehouse_id == filters.warehouse_id)
        if filters.item_id is not None:
            stmt = stmt.where(InventoryBalance.item_id == filters.item_id)
        if filters.category_id is not None:
            stmt = stmt.where(Item.category_id == filters.category_id)
        if filters.min_value is not None:
            stmt = stmt.having(value >= filters.min_value)

        # A warehouse principal must not be able to value stock it cannot see
        # (section 39). The restriction is empty for an unrestricted user.
        return stmt.where(*warehouse_scope(self.tenant, InventoryBalance.warehouse_id))

    def valuation_page_stmt(self, filters: ValuationFilters) -> Select[Any]:
        """Highest value first: a valuation is read from the top."""
        value = func.sum(InventoryBalance.stock_value)
        return self.valuation_stmt(filters).order_by(
            value.desc(), InventoryBalance.item_id.asc(), InventoryBalance.warehouse_id.asc()
        )

    def valuation_export_stmt(self, filters: ValuationFilters) -> Select[Any]:
        """Stable order for the chunked full-company export.

        Ordered by the grouping key rather than by value: the export walks the
        result in fixed-size chunks, and an ordering that ties would let a row
        appear on two chunks or on none.
        """
        return self.valuation_stmt(filters).order_by(
            InventoryBalance.item_id.asc(), InventoryBalance.warehouse_id.asc()
        )

    async def valuation_totals(self, filters: ValuationFilters) -> tuple[int, Decimal, Decimal]:
        """Row count, quantity and value for the whole filtered set."""
        inner = self.valuation_stmt(filters).subquery("valuation")
        stmt = select(
            func.count(),
            func.coalesce(func.sum(inner.c.on_hand_quantity), ZERO),
            func.coalesce(func.sum(inner.c.value), ZERO),
        ).select_from(inner)
        count, quantity, value = (await self.session.execute(stmt)).one()
        return int(count or 0), Decimal(quantity or 0), Decimal(value or 0)


class GstRepository(BaseRepository[VendorInvoiceLine]):
    """Output tax from dispatched sales, input tax from approved purchases.

    ASSUMPTION: output GST is recognised at the point goods leave - the dispatch
    - because the schema defines no sales-invoice document (database.md section
    2). Each dispatch line's share of its sales-order line's tax is apportioned
    by quantity, so a partial dispatch accrues a partial liability instead of
    the whole order's tax landing in whichever period the order was raised.
    """

    model = VendorInvoiceLine

    def output_rows_stmt(self, *, date_from: date, date_to: date) -> Select[Any]:
        invoice_rows = (
            select(
                SalesInvoiceLine.hsn_code.label("hsn_code"),
                SalesInvoiceLine.tax_rate.label("tax_rate"),
                func.sum(SalesInvoiceLine.taxable_amount).label("taxable_amount"),
                func.sum(SalesInvoiceLine.cgst_amount).label("cgst_amount"),
                func.sum(SalesInvoiceLine.sgst_amount).label("sgst_amount"),
                func.sum(SalesInvoiceLine.igst_amount).label("igst_amount"),
                func.count(func.distinct(SalesInvoice.id)).label("document_count"),
            )
            .join(
                SalesInvoice,
                and_(
                    SalesInvoice.id == SalesInvoiceLine.sales_invoice_id,
                    SalesInvoice.company_id == SalesInvoiceLine.company_id,
                ),
            )
            .where(
                SalesInvoiceLine.company_id == self.tenant.company_id,
                SalesInvoice.status != "CANCELLED",
                SalesInvoice.invoice_date >= date_from,
                SalesInvoice.invoice_date <= date_to,
            )
            .group_by(SalesInvoiceLine.hsn_code, SalesInvoiceLine.tax_rate)
        )
        credit_rows = (
            select(
                CreditNoteLine.hsn_code.label("hsn_code"),
                CreditNoteLine.tax_rate.label("tax_rate"),
                (-func.sum(CreditNoteLine.taxable_amount)).label("taxable_amount"),
                (-func.sum(CreditNoteLine.cgst_amount)).label("cgst_amount"),
                (-func.sum(CreditNoteLine.sgst_amount)).label("sgst_amount"),
                (-func.sum(CreditNoteLine.igst_amount)).label("igst_amount"),
                func.count(func.distinct(CreditNote.id)).label("document_count"),
            )
            .join(
                CreditNote,
                and_(
                    CreditNote.id == CreditNoteLine.credit_note_id,
                    CreditNote.company_id == CreditNoteLine.company_id,
                ),
            )
            .where(
                CreditNoteLine.company_id == self.tenant.company_id,
                CreditNote.status == CreditNoteStatus.ISSUED,
                CreditNote.note_date >= date_from,
                CreditNote.note_date <= date_to,
            )
            .group_by(CreditNoteLine.hsn_code, CreditNoteLine.tax_rate)
        )
        net_rows = union_all(invoice_rows, credit_rows).subquery("net_output_gst")
        return (
            select(
                net_rows.c.hsn_code,
                net_rows.c.tax_rate,
                func.sum(net_rows.c.taxable_amount).label("taxable_amount"),
                func.sum(net_rows.c.cgst_amount).label("cgst_amount"),
                func.sum(net_rows.c.sgst_amount).label("sgst_amount"),
                func.sum(net_rows.c.igst_amount).label("igst_amount"),
                func.sum(net_rows.c.document_count).label("document_count"),
            )
            .group_by(net_rows.c.hsn_code, net_rows.c.tax_rate)
            .order_by(net_rows.c.tax_rate, net_rows.c.hsn_code)
        )

    def dispatched_uninvoiced_rows_stmt(self, *, date_from: date, date_to: date) -> Select[Any]:
        # Share of the ordered line this consignment carried. The order line's
        # quantity is > 0 by CHECK, so the division is safe.
        share = DispatchLine.quantity / SalesOrderLine.quantity
        taxable = (
            SalesOrderLine.quantity * SalesOrderLine.unit_price - SalesOrderLine.discount_amount
        ) * share

        return (
            select(
                Item.hsn_code.label("hsn_code"),
                SalesOrderLine.tax_rate.label("tax_rate"),
                func.sum(taxable).label("taxable_amount"),
                func.sum(SalesOrderLine.cgst_amount * share).label("cgst_amount"),
                func.sum(SalesOrderLine.sgst_amount * share).label("sgst_amount"),
                func.sum(SalesOrderLine.igst_amount * share).label("igst_amount"),
                func.count(func.distinct(Dispatch.id)).label("document_count"),
            )
            .select_from(DispatchLine)
            .join(
                Dispatch,
                and_(
                    Dispatch.id == DispatchLine.dispatch_id,
                    Dispatch.company_id == DispatchLine.company_id,
                ),
            )
            .join(
                SalesOrderLine,
                and_(
                    SalesOrderLine.id == DispatchLine.sales_order_line_id,
                    SalesOrderLine.company_id == DispatchLine.company_id,
                ),
            )
            .join(
                Item,
                and_(Item.id == DispatchLine.item_id, Item.company_id == DispatchLine.company_id),
            )
            .where(
                DispatchLine.company_id == self.tenant.company_id,
                ~select(SalesInvoice.id)
                .where(
                    SalesInvoice.company_id == self.tenant.company_id,
                    SalesInvoice.dispatch_id == Dispatch.id,
                    SalesInvoice.status != "CANCELLED",
                )
                .exists(),
                Dispatch.status.in_(TAXABLE_DISPATCH_STATUSES),
                Dispatch.dispatch_date >= date_from,
                Dispatch.dispatch_date <= date_to,
            )
            .group_by(Item.hsn_code, SalesOrderLine.tax_rate)
            .order_by(SalesOrderLine.tax_rate.asc(), Item.hsn_code.asc())
        )

    def input_rows_stmt(self, *, date_from: date, date_to: date) -> Select[Any]:
        """Input tax from invoices accepted into the books.

        A draft or a cancelled invoice is not a credit anybody may claim, so the
        statuses are exactly :data:`INPUT_TAX_STATUSES`.
        """
        return (
            select(
                VendorInvoiceLine.hsn_code.label("hsn_code"),
                VendorInvoiceLine.tax_rate.label("tax_rate"),
                func.sum(VendorInvoiceLine.taxable_amount).label("taxable_amount"),
                func.sum(VendorInvoiceLine.cgst_amount).label("cgst_amount"),
                func.sum(VendorInvoiceLine.sgst_amount).label("sgst_amount"),
                func.sum(VendorInvoiceLine.igst_amount).label("igst_amount"),
                func.count(func.distinct(VendorInvoice.id)).label("document_count"),
            )
            .select_from(VendorInvoiceLine)
            .join(
                VendorInvoice,
                and_(
                    VendorInvoice.id == VendorInvoiceLine.vendor_invoice_id,
                    VendorInvoice.company_id == VendorInvoiceLine.company_id,
                ),
            )
            .where(
                VendorInvoiceLine.company_id == self.tenant.company_id,
                VendorInvoice.status.in_(INPUT_TAX_STATUSES),
                VendorInvoice.invoice_date >= date_from,
                VendorInvoice.invoice_date <= date_to,
            )
            .group_by(VendorInvoiceLine.hsn_code, VendorInvoiceLine.tax_rate)
            .order_by(VendorInvoiceLine.tax_rate.asc(), VendorInvoiceLine.hsn_code.asc())
        )


# -----------------------------------------------------------------------------
# Pagination for projected rows
# -----------------------------------------------------------------------------


async def paginate_projection(
    session: AsyncSession, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], OffsetPagination]:
    """Offset-paginate a statement that selects columns rather than entities.

    ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``, which
    would keep only the first column of a projected row. The semantics are
    reproduced here - one extra row to decide ``has_next``, and a bounded count
    only when the caller asked for a total.

    Bounded the same way the core paginator bounds it: exact up to
    :data:`~src.core.pagination.COUNT_CEILING`, and ``None`` past it. Returning
    the ceiling as though it were the count reports ten thousand open invoices
    for a company with two hundred thousand of them, in the field that means
    "this is how many there are" (docs/assumptions.md 15).
    """
    rows = (await session.execute(stmt.limit(params.limit + 1).offset(params.offset))).all()

    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    total: int | None = None
    total_pages: int | None = None
    if params.with_total:
        count_stmt = select(func.count()).select_from(
            stmt.order_by(None).limit(COUNT_CEILING + 1).subquery()
        )
        counted = int((await session.execute(count_stmt)).scalar_one())
        if counted <= COUNT_CEILING:
            total = counted
            total_pages = max(1, -(-total // params.limit))

    return list(rows), OffsetPagination(
        page=params.page,
        limit=params.limit,
        total=total,
        total_pages=total_pages,
        has_next=has_next,
        has_previous=params.page > 1,
    )


__all__ = [
    "AGEING_TOTAL_COLUMNS",
    "MAX_CHART_DEPTH",
    "TAXABLE_DISPATCH_STATUSES",
    "AgeingCurrencyTotal",
    "CustomerLookupRepository",
    "FinancialYearRepository",
    "GoodsReceiptFacts",
    "GoodsReceiptLineFacts",
    "GoodsReceiptLineLookupRepository",
    "GoodsReceiptLookupRepository",
    "GstRepository",
    "InventoryValuationRepository",
    "ItemFacts",
    "ItemLookupRepository",
    "LedgerAccountRepository",
    "LedgerEntryRepository",
    "PartyFacts",
    "PaymentAllocationRepository",
    "PaymentRepository",
    "PostingAccount",
    "PurchaseOrderFacts",
    "PurchaseOrderLineFacts",
    "PurchaseOrderLineLookupRepository",
    "PurchaseOrderLookupRepository",
    "ReportingCurrencyRepository",
    "StatementTotals",
    "UomLookupRepository",
    "VendorInvoiceLineRepository",
    "VendorInvoiceRepository",
    "VendorLookupRepository",
    "ageing_totals_by_currency",
    "paginate_projection",
    "ranked_ageing_rows_stmt",
]
