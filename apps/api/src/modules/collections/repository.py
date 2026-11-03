"""Tenant-scoped queries for the collections worklist and follow-up register.

Two query shapes, each one statement:

* the **worklist** aggregates ``sales_invoices`` down to one row per customer and
  currency. Invoices are joined to their issued credit notes so the balance is
  the true unsettled amount, and the live follow-up is an outer join so the money
  appears whether or not somebody has started chasing it;
* the **register** lists follow-up rows with their customer and assignee names
  joined, because a list that showed UUIDs would send the browser back for every
  row - the N+1 this repository exists to prevent.

Nothing here paginates with the core ``paginate`` helper: both statements select
column tuples rather than ORM entities, and that helper calls ``.scalars()``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.errors import NotFoundError
from src.core.pagination import COUNT_CEILING, CursorParams, PageParams, SortParams, paginate_keyset
from src.core.responses import CursorPagination, OffsetPagination
from src.core.types import ZERO
from src.modules.collections.models import (
    LIVE_FOLLOW_UP_STATUSES,
    PaymentFollowUp,
    PaymentFollowUpNote,
)
from src.modules.collections.schemas import FollowUpFilters, WorklistFilters
from src.modules.customers.models import Customer, CustomerType
from src.modules.finance.models import (
    AGEING_BUCKET_BOUNDARIES,
    CreditNote,
    CreditNoteStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from src.modules.users.models import User

#: Only these two invoice statuses can still owe money. PAID has nothing left,
#: CREDITED has been reversed and CANCELLED never happened.
OPEN_INVOICE_STATUSES: tuple[SalesInvoiceStatus, ...] = (
    SalesInvoiceStatus.ISSUED,
    SalesInvoiceStatus.PARTIALLY_PAID,
)


async def count_rows(session: AsyncSession, stmt: Any, *, cap: int) -> int:
    """How many rows a statement produces, counted no further than ``cap``.

    One past the cap is enough to tell "exactly the ceiling" from "more than the
    ceiling"; the LIMIT is what bounds the scan either way.
    """
    counted = select(func.count()).select_from(stmt.order_by(None).limit(cap + 1).subquery())
    return int((await session.execute(counted)).scalar_one())


async def paginate_rows(
    session: AsyncSession, stmt: Any, params: PageParams
) -> tuple[list[Any], OffsetPagination]:
    """Offset-paginate a row-shaped SELECT."""
    rows = list((await session.execute(stmt.limit(params.limit + 1).offset(params.offset))).all())
    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    total: int | None = None
    total_pages: int | None = None
    if params.with_total:
        total = await count_rows(session, stmt, cap=COUNT_CEILING)
        if total > COUNT_CEILING:
            total = None
        else:
            total_pages = max(1, -(-total // params.limit))

    return rows, OffsetPagination(
        page=params.page,
        limit=params.limit,
        total=total,
        total_pages=total_pages,
        has_next=has_next,
        has_previous=params.page > 1,
    )


def _credited_subquery(company_id: uuid.UUID) -> Any:
    """Issued credit notes per invoice, as one grouped subquery."""
    return (
        select(
            CreditNote.sales_invoice_id.label("sales_invoice_id"),
            func.sum(CreditNote.total_amount).label("credited_amount"),
        )
        .where(
            CreditNote.company_id == company_id,
            CreditNote.status == CreditNoteStatus.ISSUED,
        )
        .group_by(CreditNote.sales_invoice_id)
        .subquery()
    )


def _balance_expression(credited: Any) -> Any:
    """total - paid - credited. Named, because three queries must agree on it."""
    return (
        SalesInvoice.total_amount
        - SalesInvoice.paid_amount
        - func.coalesce(credited.c.credited_amount, ZERO)
    )


def bucket_columns(balance: Any, due: Any, as_of: date) -> list[Any]:
    """The four overdue ageing columns, from the shared boundaries.

    The same 30/60/90 edges the receivable ageing statement uses, so a row's
    buckets add up to the same ageing the finance screens show. Compared as dates
    rather than as interval arithmetic so the due-date index stays usable.
    """
    first, second, third = AGEING_BUCKET_BOUNDARIES
    edge_1 = as_of - timedelta(days=first)
    edge_2 = as_of - timedelta(days=second)
    edge_3 = as_of - timedelta(days=third)

    def bucket(condition: Any, label: str) -> Any:
        return func.coalesce(func.sum(case((condition, balance), else_=ZERO)), ZERO).label(label)

    return [
        bucket(and_(due <= as_of, due >= edge_1), "bucket_0_30_amount"),
        bucket(and_(due < edge_1, due >= edge_2), "bucket_31_60_amount"),
        bucket(and_(due < edge_2, due >= edge_3), "bucket_61_90_amount"),
        bucket(due < edge_3, "bucket_90_plus_amount"),
    ]


class PaymentFollowUpRepository(BaseRepository[PaymentFollowUp]):
    model = PaymentFollowUp
    entity_label = "Payment follow-up"

    # --- single rows --------------------------------------------------------

    async def live_for(
        self, customer_id: uuid.UUID, currency: str, *, lock: bool = False
    ) -> PaymentFollowUp | None:
        stmt = self._scoped().where(
            PaymentFollowUp.customer_id == customer_id,
            PaymentFollowUp.currency == currency,
            PaymentFollowUp.status.in_(LIVE_FOLLOW_UP_STATUSES),
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock(self, follow_up_id: uuid.UUID) -> PaymentFollowUp:
        stmt = (
            self._scoped()
            .where(PaymentFollowUp.id == follow_up_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFoundError(self._entity_label(), follow_up_id)
        return row

    async def live_all(self) -> list[PaymentFollowUp]:
        stmt = self._scoped().where(PaymentFollowUp.status.in_(LIVE_FOLLOW_UP_STATUSES))
        return list((await self.session.execute(stmt)).scalars().all())

    async def customer_briefs(
        self, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, str, uuid.UUID | None]]:
        """``{id: (code, name, assigned_sales_user_id)}`` for the sweep.

        One query for the whole run: the sweep writes notes and notifications
        that name the customer, and asking per row would be the N+1 section 32
        forbids.
        """
        if not ids:
            return {}
        stmt = select(
            Customer.id, Customer.code, Customer.name, Customer.assigned_sales_user_id
        ).where(
            Customer.company_id == self.tenant.company_id,
            Customer.id.in_(list(ids)),
        )
        return {
            row.id: (row.code, row.name, row.assigned_sales_user_id)
            for row in (await self.session.execute(stmt)).all()
        }

    async def assignee_name(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        return (
            await self.session.execute(
                select(User.full_name).where(
                    User.id == user_id, User.company_id == self.tenant.company_id
                )
            )
        ).scalar_one_or_none()

    async def user_names(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        stmt = select(User.id, User.full_name).where(
            User.company_id == self.tenant.company_id, User.id.in_(list(ids))
        )
        return {row.id: row.full_name for row in (await self.session.execute(stmt)).all()}

    async def summary_row(self, follow_up_id: uuid.UUID) -> Any:
        last_note = (
            select(PaymentFollowUpNote.note)
            .where(PaymentFollowUpNote.follow_up_id == PaymentFollowUp.id)
            .order_by(PaymentFollowUpNote.created_at.desc(), PaymentFollowUpNote.id.desc())
            .limit(1)
            .correlate(PaymentFollowUp)
            .scalar_subquery()
        )
        last_note_at = (
            select(PaymentFollowUpNote.created_at)
            .where(PaymentFollowUpNote.follow_up_id == PaymentFollowUp.id)
            .order_by(PaymentFollowUpNote.created_at.desc(), PaymentFollowUpNote.id.desc())
            .limit(1)
            .correlate(PaymentFollowUp)
            .scalar_subquery()
        )
        stmt = (
            select(
                PaymentFollowUp,
                Customer.code.label("customer_code"),
                Customer.name.label("customer_name"),
                User.full_name.label("assigned_to_name"),
                last_note.label("last_note"),
                last_note_at.label("last_note_at"),
            )
            .join(
                Customer,
                and_(
                    Customer.id == PaymentFollowUp.customer_id,
                    Customer.company_id == PaymentFollowUp.company_id,
                ),
            )
            .outerjoin(
                User,
                and_(
                    User.id == PaymentFollowUp.assigned_to_id,
                    User.company_id == PaymentFollowUp.company_id,
                ),
            )
            .where(
                PaymentFollowUp.company_id == self.tenant.company_id,
                PaymentFollowUp.id == follow_up_id,
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            raise NotFoundError(self._entity_label(), follow_up_id)
        return row

    # --- the register -------------------------------------------------------

    def list_stmt(self, filters: FollowUpFilters, *, assigned_to_id: uuid.UUID) -> Any:
        last_note = (
            select(PaymentFollowUpNote.note)
            .where(PaymentFollowUpNote.follow_up_id == PaymentFollowUp.id)
            .order_by(PaymentFollowUpNote.created_at.desc(), PaymentFollowUpNote.id.desc())
            .limit(1)
            .correlate(PaymentFollowUp)
            .scalar_subquery()
        )
        last_note_at = (
            select(PaymentFollowUpNote.created_at)
            .where(PaymentFollowUpNote.follow_up_id == PaymentFollowUp.id)
            .order_by(PaymentFollowUpNote.created_at.desc(), PaymentFollowUpNote.id.desc())
            .limit(1)
            .correlate(PaymentFollowUp)
            .scalar_subquery()
        )
        stmt = (
            select(
                PaymentFollowUp,
                Customer.code.label("customer_code"),
                Customer.name.label("customer_name"),
                User.full_name.label("assigned_to_name"),
                last_note.label("last_note"),
                last_note_at.label("last_note_at"),
            )
            .join(
                Customer,
                and_(
                    Customer.id == PaymentFollowUp.customer_id,
                    Customer.company_id == PaymentFollowUp.company_id,
                ),
            )
            .outerjoin(
                User,
                and_(
                    User.id == PaymentFollowUp.assigned_to_id,
                    User.company_id == PaymentFollowUp.company_id,
                ),
            )
            .where(PaymentFollowUp.company_id == self.tenant.company_id)
        )
        if filters.live_only:
            stmt = stmt.where(PaymentFollowUp.status.in_(LIVE_FOLLOW_UP_STATUSES))
        if filters.status is not None:
            stmt = stmt.where(PaymentFollowUp.status == filters.status)
        if filters.customer_id is not None:
            stmt = stmt.where(PaymentFollowUp.customer_id == filters.customer_id)
        if filters.assigned_to_id is not None:
            stmt = stmt.where(PaymentFollowUp.assigned_to_id == filters.assigned_to_id)
        if filters.assigned_to_me:
            stmt = stmt.where(PaymentFollowUp.assigned_to_id == assigned_to_id)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(
                or_(
                    Customer.code.ilike(pattern),
                    Customer.name.ilike(pattern),
                    PaymentFollowUp.code.ilike(pattern),
                )
            )
        return stmt.order_by(PaymentFollowUp.created_at.desc(), PaymentFollowUp.id.desc())

    # --- the worklist -------------------------------------------------------

    def worklist_stmt(
        self,
        filters: WorklistFilters,
        *,
        as_of: date,
        assigned_to_id: uuid.UUID,
        sort: SortParams,
    ) -> Any:
        credited = _credited_subquery(self.tenant.company_id)
        balance = _balance_expression(credited)
        overdue_days = literal(as_of, Date) - func.min(SalesInvoice.due_date)
        last_note_at = (
            select(func.max(PaymentFollowUpNote.created_at))
            .where(PaymentFollowUpNote.follow_up_id == PaymentFollowUp.id)
            .correlate(PaymentFollowUp)
            .scalar_subquery()
        )

        stmt = (
            select(
                Customer.id.label("customer_id"),
                Customer.code.label("customer_code"),
                Customer.name.label("customer_name"),
                Customer.customer_type.label("customer_type"),
                SalesInvoice.currency.label("currency"),
                PaymentFollowUp.id.label("follow_up_id"),
                PaymentFollowUp.code.label("follow_up_code"),
                PaymentFollowUp.status.label("status"),
                PaymentFollowUp.assigned_to_id.label("assigned_to_id"),
                User.full_name.label("assigned_to_name"),
                func.sum(balance).label("overdue_amount"),
                func.count(SalesInvoice.id).label("overdue_invoice_count"),
                overdue_days.label("oldest_overdue_days"),
                *bucket_columns(balance, SalesInvoice.due_date, as_of),
                last_note_at.label("last_note_at"),
                PaymentFollowUp.next_action_date.label("next_action_date"),
                PaymentFollowUp.promised_amount.label("promised_amount"),
                PaymentFollowUp.promised_date.label("promised_date"),
            )
            .join(
                Customer,
                and_(
                    Customer.id == SalesInvoice.customer_id,
                    Customer.company_id == SalesInvoice.company_id,
                ),
            )
            .outerjoin(credited, credited.c.sales_invoice_id == SalesInvoice.id)
            .outerjoin(
                PaymentFollowUp,
                and_(
                    PaymentFollowUp.company_id == SalesInvoice.company_id,
                    PaymentFollowUp.customer_id == SalesInvoice.customer_id,
                    PaymentFollowUp.currency == SalesInvoice.currency,
                    PaymentFollowUp.status.in_(LIVE_FOLLOW_UP_STATUSES),
                ),
            )
            .outerjoin(
                User,
                and_(
                    User.id == PaymentFollowUp.assigned_to_id,
                    User.company_id == PaymentFollowUp.company_id,
                ),
            )
            .where(
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.status.in_(OPEN_INVOICE_STATUSES),
                SalesInvoice.due_date < as_of,
                balance > ZERO,
                Customer.deleted_at.is_(None),
            )
            .group_by(
                Customer.id,
                SalesInvoice.currency,
                PaymentFollowUp.id,
                User.id,
            )
        )

        if filters.min_overdue_days is not None:
            stmt = stmt.having(overdue_days >= filters.min_overdue_days)
        if filters.customer_id is not None:
            stmt = stmt.where(Customer.id == filters.customer_id)
        if filters.status is not None:
            stmt = stmt.where(PaymentFollowUp.status == filters.status)
        if filters.assigned_to_me:
            stmt = stmt.where(PaymentFollowUp.assigned_to_id == assigned_to_id)
        if filters.dealer_only:
            stmt = stmt.where(
                Customer.customer_type.in_((CustomerType.DEALER, CustomerType.DISTRIBUTOR))
            )
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(or_(Customer.code.ilike(pattern), Customer.name.ilike(pattern)))

        allowed: dict[str, InstrumentedAttribute[Any] | Any] = {
            "overdue_amount": func.sum(balance),
            "oldest_overdue_days": overdue_days,
            "next_action_date": PaymentFollowUp.next_action_date,
            "customer_code": Customer.code,
        }
        order = sort.resolve(allowed, func.sum(balance).desc())
        # A deterministic tail: two customers can owe the same amount, and an
        # unstable sort duplicates and skips rows across pages.
        return stmt.order_by(order, Customer.code.asc(), Customer.id.asc(), SalesInvoice.currency)

    # --- overdue aggregates ------------------------------------------------

    def overdue_totals_stmt(self, *, as_of: date, customer_id: uuid.UUID | None = None) -> Any:
        """Per (customer, currency) overdue totals, for the sweep.

        The same balance definition as the worklist, but grouped without the
        follow-up join: the sweep is comparing money, not displaying work.
        """
        credited = _credited_subquery(self.tenant.company_id)
        balance = _balance_expression(credited)
        stmt = (
            select(
                SalesInvoice.customer_id.label("customer_id"),
                SalesInvoice.currency.label("currency"),
                func.sum(balance).label("overdue_amount"),
                func.count(SalesInvoice.id).label("overdue_invoice_count"),
                func.min(SalesInvoice.due_date).label("oldest_due_date"),
            )
            .outerjoin(credited, credited.c.sales_invoice_id == SalesInvoice.id)
            .where(
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.status.in_(OPEN_INVOICE_STATUSES),
                SalesInvoice.due_date < as_of,
                balance > ZERO,
            )
            .group_by(SalesInvoice.customer_id, SalesInvoice.currency)
        )
        if customer_id is not None:
            stmt = stmt.where(SalesInvoice.customer_id == customer_id)
        return stmt

    async def overdue_totals(
        self, *, as_of: date, customer_id: uuid.UUID | None = None
    ) -> list[Any]:
        stmt = self.overdue_totals_stmt(as_of=as_of, customer_id=customer_id)
        rows = (await self.session.execute(stmt)).all()
        return list(rows)

    def overdue_invoices_stmt(self, *, customer_id: uuid.UUID, currency: str, as_of: date) -> Any:
        credited = _credited_subquery(self.tenant.company_id)
        balance = _balance_expression(credited)
        return (
            select(
                SalesInvoice.id.label("id"),
                SalesInvoice.code.label("code"),
                SalesInvoice.invoice_date.label("invoice_date"),
                SalesInvoice.due_date.label("due_date"),
                SalesInvoice.currency.label("currency"),
                SalesInvoice.total_amount.label("total_amount"),
                SalesInvoice.paid_amount.label("paid_amount"),
                func.coalesce(credited.c.credited_amount, ZERO).label("credited_amount"),
                balance.label("balance_amount"),
                SalesInvoice.status.label("status"),
            )
            .outerjoin(credited, credited.c.sales_invoice_id == SalesInvoice.id)
            .where(
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.customer_id == customer_id,
                SalesInvoice.currency == currency,
                SalesInvoice.status.in_(OPEN_INVOICE_STATUSES),
                SalesInvoice.due_date < as_of,
                balance > ZERO,
            )
            .order_by(SalesInvoice.due_date.asc(), SalesInvoice.code.asc())
        )

    async def overdue_invoices(
        self, *, customer_id: uuid.UUID, currency: str, as_of: date
    ) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    self.overdue_invoices_stmt(
                        customer_id=customer_id, currency=currency, as_of=as_of
                    )
                )
            ).all()
        )

    # --- notes -------------------------------------------------------------

    async def notes(
        self, follow_up_id: uuid.UUID, params: CursorParams
    ) -> tuple[list[PaymentFollowUpNote], CursorPagination]:
        stmt = select(PaymentFollowUpNote).where(
            PaymentFollowUpNote.company_id == self.tenant.company_id,
            PaymentFollowUpNote.follow_up_id == follow_up_id,
        )
        return await paginate_keyset(
            self.session,
            stmt,
            params,
            created_at_col=PaymentFollowUpNote.created_at,
            id_col=PaymentFollowUpNote.id,
        )

    async def last_note_at(self, follow_up_id: uuid.UUID) -> datetime | None:
        return (
            await self.session.execute(
                select(func.max(PaymentFollowUpNote.created_at)).where(
                    PaymentFollowUpNote.company_id == self.tenant.company_id,
                    PaymentFollowUpNote.follow_up_id == follow_up_id,
                )
            )
        ).scalar_one_or_none()


class PaymentFollowUpNoteRepository(BaseRepository[PaymentFollowUpNote]):
    model = PaymentFollowUpNote
    entity_label = "Payment follow-up note"


__all__ = [
    "OPEN_INVOICE_STATUSES",
    "PaymentFollowUpNoteRepository",
    "PaymentFollowUpRepository",
    "bucket_columns",
    "count_rows",
    "paginate_rows",
]
