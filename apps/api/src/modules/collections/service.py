"""Payment follow-up rules: the lifecycle, the promise, and the nightly sweep.

Three things live here rather than in the router.

**The lifecycle is a state machine.** ``FOLLOW_UP_LIFECYCLE`` spells every legal
edge once, and every path that moves a follow-up - a person's route, a broken
promise found by the sweep, an automatic resolution - asserts against it. A
transition that is not in the graph cannot be written by any caller.

**The contradiction rules are pure functions.** ``validate_promise`` and
``reminder_due`` take values and return an answer, with no session and no clock
of their own. That is what lets them be tested at the boundary - a promise dated
yesterday, a reminder one second early - without building a database.

**The sweep is idempotent.** It derives what should be true from the invoices
and reconciles the follow-ups to it, so a re-run after a worker crash changes
nothing and writes no second audit row. A quiet night writes nothing at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError, DuplicateError, NotFoundError
from src.core.messages import plural
from src.core.pagination import CursorParams, PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import CursorPagination, OffsetPagination, api_path
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.collections.models import (
    FOLLOW_UP_REMINDER_DAYS,
    LIVE_FOLLOW_UP_STATUSES,
    SYSTEM_CHANNEL,
    FollowUpChannel,
    FollowUpOutcome,
    FollowUpStatus,
    PaymentFollowUp,
    PaymentFollowUpNote,
)
from src.modules.collections.repository import (
    PaymentFollowUpRepository,
    paginate_rows,
)
from src.modules.collections.schemas import (
    FollowUpCancel,
    FollowUpCreate,
    FollowUpDetail,
    FollowUpEscalate,
    FollowUpFilters,
    FollowUpInvoiceRow,
    FollowUpNoteCreate,
    FollowUpNoteRead,
    FollowUpPromise,
    FollowUpResolve,
    FollowUpSummary,
    FollowUpUpdate,
    SweepResult,
    WorklistFilters,
    WorklistRow,
)
from src.modules.customers.models import Customer
from src.modules.customers.service import CustomerService
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.notifications.service import NotificationDraft, notify, notify_many, notify_role
from src.modules.users.models import User

AUDIT_FOLLOW_UP = "payment_follow_up"
AUDIT_FOLLOW_UP_NOTE = "payment_follow_up_note"

#: The collections screen, filtered to one customer. The sweep sends this in the
#: notification body so the recipient lands on the row, not on a list to search.
COLLECTIONS_PATH = "/finance/collections"
DEALER_PAYMENT_REMINDER_DAYS = 7

FOLLOW_UP_LIFECYCLE: StateMachine[FollowUpStatus] = StateMachine(
    entity="Receivables follow up",
    initial=FollowUpStatus.OPEN,
    transitions=(
        Transition(
            FollowUpStatus.OPEN,
            FollowUpStatus.PROMISED,
            permission=Permission.COLLECTION_MANAGE,
            label="Record promise",
        ),
        Transition(
            FollowUpStatus.OPEN,
            FollowUpStatus.ESCALATED,
            permission=Permission.COLLECTION_MANAGE,
            label="Escalate",
            is_irreversible=True,
        ),
        Transition(
            FollowUpStatus.OPEN,
            FollowUpStatus.RESOLVED,
            permission=Permission.COLLECTION_MANAGE,
            label="Resolve",
        ),
        Transition(
            FollowUpStatus.OPEN,
            FollowUpStatus.CANCELLED,
            permission=Permission.COLLECTION_MANAGE,
            label="Cancel",
        ),
        Transition(
            FollowUpStatus.PROMISED,
            FollowUpStatus.OPEN,
            label="Promise broken",
        ),
        Transition(
            FollowUpStatus.PROMISED,
            FollowUpStatus.ESCALATED,
            permission=Permission.COLLECTION_MANAGE,
            label="Escalate",
            is_irreversible=True,
        ),
        Transition(
            FollowUpStatus.PROMISED,
            FollowUpStatus.RESOLVED,
            permission=Permission.COLLECTION_MANAGE,
            label="Resolve",
        ),
        Transition(FollowUpStatus.ESCALATED, FollowUpStatus.OPEN, label="Reopen"),
        Transition(
            FollowUpStatus.ESCALATED,
            FollowUpStatus.RESOLVED,
            permission=Permission.COLLECTION_MANAGE,
            label="Resolve",
        ),
    ),
    terminal=frozenset({FollowUpStatus.RESOLVED, FollowUpStatus.CANCELLED}),
)

#: How stale the last reminder must be before another may go out.
REMINDER_AFTER = timedelta(days=FOLLOW_UP_REMINDER_DAYS)

#: Section 78: the accounts team owns an unassigned chase. Named here rather than
#: inline so the sweep and its test read the same string.
UNASSIGNED_REMINDER_ROLES: tuple[str, ...] = ("ACCOUNTS",)


# -----------------------------------------------------------------------------
# Pure rules
# -----------------------------------------------------------------------------


def normalise_currency(value: str) -> str:
    """Upper-case ISO code, or a refusal. Money has no case-insensitive spelling."""
    candidate = value.strip().upper()
    if len(candidate) != 3 or not candidate.isalpha():
        raise BusinessRuleError(
            f"'{value}' is not an ISO-4217 currency code.",
            details={"currency": value},
        )
    return candidate


def validate_promise(
    *, promised_amount: Decimal, promised_date: date, overdue_amount: Decimal, as_of: date
) -> Decimal:
    """The two rules a promise must satisfy before it is recorded.

    A promise dated in the past cannot be kept - it is either a data-entry error
    or an attempt to make a broken promise look current. A promise larger than
    what is overdue would tell the sweep there is money to collect that the
    invoices do not show, and the sweep would never retire the excess.
    """
    if promised_date < as_of:
        raise BusinessRuleError(
            "A promise date cannot be in the past.",
            details={"promised_date": promised_date.isoformat(), "today": as_of.isoformat()},
        )
    amount = to_money(promised_amount)
    if amount <= ZERO:
        raise BusinessRuleError("A promised amount must be greater than zero.")
    if amount > to_money(overdue_amount):
        raise BusinessRuleError(
            "A promise cannot exceed the overdue amount.",
            details={
                "promised_amount": str(amount),
                "overdue_amount": str(to_money(overdue_amount)),
            },
        )
    return amount


def reminder_due(last_reminded_at: datetime | None, now: datetime) -> bool:
    """Has the reminder interval elapsed? ``None`` means it never has been sent."""
    return last_reminded_at is None or now - last_reminded_at >= REMINDER_AFTER


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


def _summary(row: Any) -> FollowUpSummary:
    """Map a follow-up and its customer, assignee, and latest-note summary."""
    follow: PaymentFollowUp = row[0]
    values = {
        field: getattr(follow, field)
        for field in FollowUpSummary.model_fields
        if hasattr(follow, field)
    }
    values["customer_code"] = row.customer_code
    values["customer_name"] = row.customer_name
    values["assigned_to_name"] = row.assigned_to_name
    values["last_note"] = row.last_note
    values["last_note_at"] = row.last_note_at
    return FollowUpSummary.model_validate(values)


def _invoice_row(row: Any) -> FollowUpInvoiceRow:
    return FollowUpInvoiceRow(
        **dict(row._mapping),
        overdue_days=max((today() - row.due_date).days, 0),
    )


async def get_follow_up(
    session: AsyncSession, tenant: TenantContext, follow_up_id: uuid.UUID
) -> FollowUpDetail:
    repo = PaymentFollowUpRepository(session, tenant)
    row = await repo.summary_row(follow_up_id)
    follow: PaymentFollowUp = row[0]
    invoices = await repo.overdue_invoices(
        customer_id=follow.customer_id, currency=follow.currency, as_of=today()
    )
    summary = _summary(row)
    return FollowUpDetail(
        **summary.model_dump(),
        invoices=[_invoice_row(invoice) for invoice in invoices],
    )


async def list_follow_ups(
    session: AsyncSession,
    tenant: TenantContext,
    filters: FollowUpFilters,
    params: PageParams,
) -> tuple[list[FollowUpSummary], OffsetPagination]:
    repo = PaymentFollowUpRepository(session, tenant)
    stmt = repo.list_stmt(filters, assigned_to_id=tenant.user_id)
    rows, pagination = await paginate_rows(session, stmt, params)
    return [_summary(row) for row in rows], pagination


async def worklist(
    session: AsyncSession,
    tenant: TenantContext,
    filters: WorklistFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[WorklistRow], OffsetPagination]:
    repo = PaymentFollowUpRepository(session, tenant)
    stmt = repo.worklist_stmt(filters, as_of=today(), assigned_to_id=tenant.user_id, sort=sort)
    rows, pagination = await paginate_rows(session, stmt, params)
    return [WorklistRow.model_validate(dict(row._mapping)) for row in rows], pagination


async def list_notes(
    session: AsyncSession,
    tenant: TenantContext,
    follow_up_id: uuid.UUID,
    params: CursorParams,
) -> tuple[list[FollowUpNoteRead], CursorPagination]:
    repo = PaymentFollowUpRepository(session, tenant)
    await repo.get_or_404(follow_up_id)
    notes, pagination = await repo.notes(follow_up_id, params)
    names = await repo.user_names(
        [note.created_by_id for note in notes if note.created_by_id is not None]
    )
    return [
        FollowUpNoteRead.model_validate(note).model_copy(
            update={"created_by_name": names.get(note.created_by_id)}  # type: ignore[arg-type]
        )
        for note in notes
    ], pagination


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------


def _assert_live(follow: PaymentFollowUp) -> None:
    if follow.status in FOLLOW_UP_LIFECYCLE.terminal:
        raise BusinessRuleError(
            f"Follow-up {follow.code} is {str(follow.status).lower()} and cannot be changed.",
            details={"status": str(follow.status)},
        )


def _system_note(
    session: AsyncSession,
    tenant: TenantContext,
    follow: PaymentFollowUp,
    text: str,
    *,
    outcome: FollowUpOutcome | None = None,
    next_action_date: date | None = None,
) -> PaymentFollowUpNote:
    note = PaymentFollowUpNote(
        company_id=tenant.company_id,
        created_by_id=None,
        follow_up_id=follow.id,
        channel=SYSTEM_CHANNEL,
        outcome=outcome,
        note=text,
        next_action_date=next_action_date,
    )
    session.add(note)
    return note


async def _customer(
    session: AsyncSession, tenant: TenantContext, customer_id: uuid.UUID
) -> Customer:
    customer = (
        await session.execute(
            select(Customer).where(
                Customer.company_id == tenant.company_id,
                Customer.id == customer_id,
                Customer.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("Customer", customer_id)
    return customer


async def _assert_user(
    session: AsyncSession, tenant: TenantContext, user_id: uuid.UUID | None
) -> None:
    if user_id is None:
        return
    found = (
        await session.execute(
            select(User.id).where(
                User.company_id == tenant.company_id,
                User.id == user_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError("User", user_id)


async def create_follow_up(
    session: AsyncSession, tenant: TenantContext, payload: FollowUpCreate
) -> FollowUpDetail:
    """Open a chase by hand for a customer that already owes overdue money."""
    currency = normalise_currency(payload.currency)
    repo = PaymentFollowUpRepository(session, tenant)
    as_of = today()
    customer = await _customer(session, tenant, payload.customer_id)

    async with transaction(session):
        existing = await repo.live_for(customer.id, currency)
        if existing is not None:
            raise DuplicateError(
                f"Customer {customer.code} already has a live follow-up in {currency}.",
                details={"follow_up_id": str(existing.id), "follow_up_code": existing.code},
            )
        await _assert_user(session, tenant, payload.assigned_to_id)

        totals = await repo.overdue_totals(as_of=as_of, customer_id=customer.id)
        overdue = next((row for row in totals if row.currency == currency), None)
        if overdue is None:
            raise BusinessRuleError(
                f"Customer {customer.code} has no overdue invoices in {currency}.",
                details={"customer_id": str(customer.id), "currency": currency},
            )

        amount = to_money(overdue.overdue_amount)
        follow = PaymentFollowUp(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            code=await next_document_number(session, tenant.company_id, DocTypes.PAYMENT_FOLLOW_UP),
            customer_id=customer.id,
            currency=currency,
            status=FollowUpStatus.OPEN,
            assigned_to_id=payload.assigned_to_id or customer.assigned_sales_user_id,
            opened_overdue_amount=amount,
            current_overdue_amount=amount,
            oldest_due_date=overdue.oldest_due_date,
            next_action_date=payload.next_action_date or as_of,
        )
        session.add(follow)
        await session.flush()
        _system_note(
            session,
            tenant,
            follow,
            f"Follow-up opened with {currency} {amount} overdue across "
            f"{plural(overdue.overdue_invoice_count, 'invoice')}.",
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_FOLLOW_UP,
            entity_id=follow.id,
            entity_code=follow.code,
            summary=f"Opened payment follow-up {follow.code}",
            user_id=tenant.user_id,
            new_values={
                "customer_id": customer.id,
                "currency": currency,
                "overdue_amount": str(amount),
            },
        )
    return await get_follow_up(session, tenant, follow.id)


async def update_follow_up(
    session: AsyncSession, tenant: TenantContext, follow_up_id: uuid.UUID, payload: FollowUpUpdate
) -> FollowUpDetail:
    repo = PaymentFollowUpRepository(session, tenant)
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        before = {
            "assigned_to_id": follow.assigned_to_id,
            "next_action_date": follow.next_action_date,
        }
        if "assigned_to_id" in payload.model_fields_set:
            await _assert_user(session, tenant, payload.assigned_to_id)
            follow.assigned_to_id = payload.assigned_to_id
        if "next_action_date" in payload.model_fields_set:
            follow.next_action_date = payload.next_action_date
        repo.touch(follow)
        await session.flush()
        after = {
            "assigned_to_id": follow.assigned_to_id,
            "next_action_date": follow.next_action_date,
        }
        if before != after:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                summary=f"Updated payment follow-up {follow.code}",
                user_id=tenant.user_id,
                old_values={k: str(v) for k, v in before.items()},
                new_values={k: str(v) for k, v in after.items()},
            )
    return await get_follow_up(session, tenant, follow_up_id)


async def add_note(
    session: AsyncSession,
    tenant: TenantContext,
    follow_up_id: uuid.UUID,
    payload: FollowUpNoteCreate,
    *,
    author_name: str | None = None,
) -> FollowUpNoteRead:
    if payload.channel is FollowUpChannel.SYSTEM:
        raise BusinessRuleError("The system channel is reserved for automatic notes.")
    repo = PaymentFollowUpRepository(session, tenant)
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        note = PaymentFollowUpNote(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            follow_up_id=follow.id,
            channel=payload.channel,
            outcome=payload.outcome,
            note=payload.note,
            next_action_date=payload.next_action_date,
        )
        session.add(note)
        if payload.next_action_date is not None:
            follow.next_action_date = payload.next_action_date
        repo.touch(follow)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_FOLLOW_UP_NOTE,
            entity_id=note.id,
            entity_code=follow.code,
            summary=f"Logged {payload.channel.value.lower()} note on {follow.code}",
            user_id=tenant.user_id,
            new_values={"channel": str(payload.channel), "outcome": str(payload.outcome or "")},
        )
    return FollowUpNoteRead.model_validate(note).model_copy(update={"created_by_name": author_name})


async def record_promise(
    session: AsyncSession,
    tenant: TenantContext,
    follow_up_id: uuid.UUID,
    payload: FollowUpPromise,
) -> FollowUpDetail:
    repo = PaymentFollowUpRepository(session, tenant)
    as_of = today()
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        amount = validate_promise(
            promised_amount=payload.promised_amount,
            promised_date=payload.promised_date,
            overdue_amount=follow.current_overdue_amount,
            as_of=as_of,
        )
        previous = follow.status
        # Repeating a promise on an already-promised row is an extension, not a
        # new state: the machine has no PROMISED -> PROMISED edge and inventing
        # one would make "is the promise current?" ambiguous.
        if previous != FollowUpStatus.PROMISED:
            FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.PROMISED)
            follow.status = FollowUpStatus.PROMISED
        follow.promised_amount = amount
        follow.promised_date = payload.promised_date
        follow.next_action_date = payload.promised_date
        repo.touch(follow)
        await session.flush()
        _system_note(
            session,
            tenant,
            follow,
            f"Promise of {follow.currency} {amount} due "
            f"{payload.promised_date.isoformat()} recorded.",
            outcome=FollowUpOutcome.PROMISED,
            next_action_date=payload.promised_date,
        )
        if previous != FollowUpStatus.PROMISED:
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                from_status=str(previous),
                to_status=str(FollowUpStatus.PROMISED),
                user_id=tenant.user_id,
                reason=(
                    f"Promised {follow.currency} {amount} on {payload.promised_date.isoformat()}"
                ),
            )
        else:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                summary=f"Extended promise on {follow.code}",
                user_id=tenant.user_id,
                new_values={
                    "promised_amount": str(amount),
                    "promised_date": payload.promised_date.isoformat(),
                },
            )
    return await get_follow_up(session, tenant, follow_up_id)


async def escalate(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    follow_up_id: uuid.UUID,
    payload: FollowUpEscalate,
) -> FollowUpDetail:
    if payload.block_customer:
        # Blocking a customer is a customer-master change, not merely a
        # collections action. The caller must hold both rights.
        principal.require(Permission.CUSTOMER_UPDATE)
    repo = PaymentFollowUpRepository(session, tenant)
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        previous = follow.status
        if previous != FollowUpStatus.ESCALATED:
            FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.ESCALATED)
            follow.status = FollowUpStatus.ESCALATED
            follow.escalated_at = utcnow()
        repo.touch(follow)

        if payload.block_customer:
            customer = await _customer(session, tenant, follow.customer_id)
            if not customer.is_blocked:
                await CustomerService(session, tenant).block(
                    principal,
                    customer.id,
                    reason=f"Collections escalation: {payload.reason}",
                )

        await session.flush()
        _system_note(
            session,
            tenant,
            follow,
            f"Escalated: {payload.reason}"
            + (" Customer placed on credit hold." if payload.block_customer else ""),
        )
        if previous != FollowUpStatus.ESCALATED:
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                from_status=str(previous),
                to_status=str(FollowUpStatus.ESCALATED),
                user_id=tenant.user_id,
                reason=payload.reason,
            )
        else:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                summary=f"Escalated {follow.code} again",
                user_id=tenant.user_id,
                new_values={"reason": payload.reason},
            )
    return await get_follow_up(session, tenant, follow_up_id)


async def resolve(
    session: AsyncSession,
    tenant: TenantContext,
    follow_up_id: uuid.UUID,
    payload: FollowUpResolve,
) -> FollowUpDetail:
    repo = PaymentFollowUpRepository(session, tenant)
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        previous = follow.status
        FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.RESOLVED)
        follow.status = FollowUpStatus.RESOLVED
        follow.resolution_note = payload.note
        follow.resolved_at = utcnow()
        follow.resolved_by_id = tenant.user_id
        follow.promised_amount = None
        follow.promised_date = None
        repo.touch(follow)
        await session.flush()
        _system_note(session, tenant, follow, f"Resolved: {payload.note}")
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type=AUDIT_FOLLOW_UP,
            entity_id=follow.id,
            entity_code=follow.code,
            from_status=str(previous),
            to_status=str(FollowUpStatus.RESOLVED),
            user_id=tenant.user_id,
            reason=payload.note,
        )
    return await get_follow_up(session, tenant, follow_up_id)


async def cancel(
    session: AsyncSession,
    tenant: TenantContext,
    follow_up_id: uuid.UUID,
    payload: FollowUpCancel,
) -> FollowUpDetail:
    repo = PaymentFollowUpRepository(session, tenant)
    async with transaction(session):
        follow = await repo.lock(follow_up_id)
        _assert_live(follow)
        previous = follow.status
        FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.CANCELLED)
        follow.status = FollowUpStatus.CANCELLED
        follow.cancellation_reason = payload.reason
        follow.promised_amount = None
        follow.promised_date = None
        repo.touch(follow)
        await session.flush()
        _system_note(session, tenant, follow, f"Cancelled: {payload.reason}")
        await record_status_change(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CANCEL,
            entity_type=AUDIT_FOLLOW_UP,
            entity_id=follow.id,
            entity_code=follow.code,
            from_status=str(previous),
            to_status=str(FollowUpStatus.CANCELLED),
            user_id=tenant.user_id,
            reason=payload.reason,
        )
    return await get_follow_up(session, tenant, follow_up_id)


# -----------------------------------------------------------------------------
# The nightly sweep
# -----------------------------------------------------------------------------


def _reminder_body(follow: PaymentFollowUp, customer_name: str) -> tuple[str, str]:
    due = follow.next_action_date.isoformat() if follow.next_action_date else "today"
    title = f"Payment follow-up due: {customer_name}"
    body = (
        f"{follow.currency} {to_money(follow.current_overdue_amount)} is overdue. "
        f"Next action {due}."
    )
    return title, body


async def _remind(
    session: AsyncSession, tenant: TenantContext, follow: PaymentFollowUp, customer_name: str
) -> None:
    title, body = _reminder_body(follow, customer_name)
    action_url = api_path(f"{COLLECTIONS_PATH}?customer_id={follow.customer_id}")
    if follow.assigned_to_id is not None:
        await notify(
            session,
            company_id=tenant.company_id,
            user_ids=[follow.assigned_to_id],
            notification_type=NotificationType.PAYMENT_DUE,
            title=title,
            body=body,
            severity=NotificationSeverity.WARNING,
            entity_type=AUDIT_FOLLOW_UP,
            entity_id=follow.id,
            action_url=action_url,
            group_key=f"payment-due:{follow.customer_id}:{follow.currency}",
        )
    else:
        await notify_role(
            session,
            company_id=tenant.company_id,
            role_codes=UNASSIGNED_REMINDER_ROLES,
            notification_type=NotificationType.PAYMENT_DUE,
            title=title,
            body=body,
            severity=NotificationSeverity.WARNING,
            entity_type=AUDIT_FOLLOW_UP,
            entity_id=follow.id,
            action_url=action_url,
            group_key=f"payment-due:{follow.customer_id}:{follow.currency}",
        )


async def scan_overdue_receivables(
    session: AsyncSession, tenant: TenantContext, *, as_of: date
) -> SweepResult:
    """Reconcile every live follow-up with the invoices as they stand.

    Runs inside the caller's transaction. The caller also holds the advisory
    lock, so two workers cannot open the same follow-up twice; the partial unique
    index is the second line of defence.

    The five branches are independent and all idempotent:

    * open a chase for a customer and currency that has overdue money and none;
    * refresh the amount and oldest due date of one that has;
    * return a promise whose date has passed to ``OPEN`` with a system note;
    * resolve one whose money has been settled;
    * remind the owner, at most once per :data:`FOLLOW_UP_REMINDER_DAYS`.

    One audit row per changed run. A quiet run writes nothing, which is what
    makes the audit log a record of changes rather than a nightly heartbeat.
    """
    repo = PaymentFollowUpRepository(session, tenant)
    moment = utcnow()
    result = SweepResult()

    totals = await repo.overdue_totals(as_of=as_of)
    current = {(row.customer_id, row.currency): row for row in totals}
    live = await repo.live_all()
    live_map = {(follow.customer_id, follow.currency): follow for follow in live}

    customer_ids = {key[0] for key in current} | {key[0] for key in live_map}
    briefs = await repo.customer_briefs(sorted(customer_ids))

    # --- (d) auto-resolve, (b) refresh, (c) broken promises -----------------
    for key, follow in list(live_map.items()):
        row = current.get(key)
        if row is None:
            previous = follow.status
            FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.RESOLVED)
            follow.status = FollowUpStatus.RESOLVED
            follow.resolution_note = "No overdue balance remains."
            follow.resolved_at = moment
            follow.resolved_by_id = None
            follow.promised_amount = None
            follow.promised_date = None
            repo.touch(follow)
            _system_note(
                session,
                tenant,
                follow,
                "Auto-resolved: no overdue balance remains.",
            )
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                from_status=str(previous),
                to_status=str(FollowUpStatus.RESOLVED),
                user_id=tenant.user_id,
                reason="No overdue balance remains.",
            )
            result.resolved += 1
            live_map.pop(key, None)
            continue

        amount = to_money(row.overdue_amount)
        if follow.current_overdue_amount != amount or follow.oldest_due_date != row.oldest_due_date:
            follow.current_overdue_amount = amount
            follow.oldest_due_date = row.oldest_due_date
            repo.touch(follow)
            result.refreshed += 1

        if (
            follow.status == FollowUpStatus.PROMISED
            and follow.promised_date is not None
            and follow.promised_date < as_of
        ):
            previous = follow.status
            broken_amount = follow.promised_amount or ZERO
            broken_date = follow.promised_date
            FOLLOW_UP_LIFECYCLE.assert_transition(previous, FollowUpStatus.OPEN)
            follow.status = FollowUpStatus.OPEN
            follow.promised_amount = None
            follow.promised_date = None
            follow.next_action_date = as_of
            repo.touch(follow)
            _system_note(
                session,
                tenant,
                follow,
                f"Promise of {follow.currency} {to_money(broken_amount)} due "
                f"{broken_date.isoformat()} was not met.",
            )
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_FOLLOW_UP,
                entity_id=follow.id,
                entity_code=follow.code,
                from_status=str(previous),
                to_status=str(FollowUpStatus.OPEN),
                user_id=tenant.user_id,
                reason=f"Promise due {broken_date.isoformat()} was not met.",
            )
            result.broken_promises += 1

    # --- (a) open a chase where the money has none --------------------------
    for key, row in current.items():
        if key in live_map:
            continue
        customer_id, currency = key
        assignee = briefs.get(customer_id, ("", "", None))[2]
        amount = to_money(row.overdue_amount)
        follow = PaymentFollowUp(
            company_id=tenant.company_id,
            created_by_id=None,
            code=await next_document_number(session, tenant.company_id, DocTypes.PAYMENT_FOLLOW_UP),
            customer_id=customer_id,
            currency=currency,
            status=FollowUpStatus.OPEN,
            assigned_to_id=assignee,
            opened_overdue_amount=amount,
            current_overdue_amount=amount,
            oldest_due_date=row.oldest_due_date,
            next_action_date=as_of,
        )
        session.add(follow)
        await session.flush()
        _system_note(
            session,
            tenant,
            follow,
            f"Follow-up opened automatically with {currency} {amount} overdue across "
            f"{plural(row.overdue_invoice_count, 'invoice')}.",
        )
        live_map[key] = follow
        result.opened += 1

    # --- (e) reminders ------------------------------------------------------
    reminders_due = [
        (key, follow)
        for key, follow in live_map.items()
        if follow.status in LIVE_FOLLOW_UP_STATUSES
        and follow.current_overdue_amount > ZERO
        and reminder_due(follow.last_reminded_at, moment)
    ]
    dealer_drafts: list[NotificationDraft] = []
    notification_repo = NotificationRepository.for_company(session, tenant.company_id)
    reminder_customer_ids = sorted({key[0] for key, _ in reminders_due})
    dealer_users = await notification_repo.user_ids_by_customer(
        reminder_customer_ids, permission=Permission.DEALER_PORTAL_ACCOUNT
    )
    group_keys = [
        f"dealer-payment-reminder:{follow.customer_id}:{follow.currency}"
        for _, follow in reminders_due
    ]
    dealer_user_ids = sorted(
        {user_id for recipients in dealer_users.values() for user_id in recipients}
    )
    recently_reminded = await notification_repo.recent_group_recipients(
        group_keys,
        dealer_user_ids,
        notification_type=NotificationType.DEALER_PAYMENT_REMINDER,
        since=moment - timedelta(days=DEALER_PAYMENT_REMINDER_DAYS),
    )
    for key, follow in reminders_due:
        name = briefs.get(key[0], ("", "", None))[1] or "the customer"
        await _remind(session, tenant, follow, name)
        group_key = f"dealer-payment-reminder:{follow.customer_id}:{follow.currency}"
        recipients = [
            user_id
            for user_id in dealer_users.get(follow.customer_id, [])
            if (group_key, user_id) not in recently_reminded
        ]
        if recipients:
            dealer_drafts.append(
                NotificationDraft(
                    user_ids=recipients,
                    title=f"Payment reminder: {name}",
                    body=(
                        f"{follow.currency} {to_money(follow.current_overdue_amount)} is overdue. "
                        f"Oldest due date: {follow.oldest_due_date.isoformat()}."
                    ),
                    severity=NotificationSeverity.INFO,
                    entity_type=AUDIT_FOLLOW_UP,
                    entity_id=follow.id,
                    action_url="/dealer/account",
                    group_key=group_key,
                )
            )
        follow.last_reminded_at = moment
        result.reminded += 1
    await notify_many(
        session,
        company_id=tenant.company_id,
        notification_type=NotificationType.DEALER_PAYMENT_REMINDER,
        drafts=dealer_drafts,
        write_audit=False,
    )

    result.changed = bool(
        result.opened
        or result.refreshed
        or result.broken_promises
        or result.resolved
        or result.reminded
    )
    if result.changed:
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.STATUS_CHANGE,
            entity_type=AUDIT_FOLLOW_UP,
            summary=(
                "Collections sweep: "
                f"{result.opened} opened, {result.refreshed} refreshed, "
                f"{result.broken_promises} promises broken, {result.resolved} resolved, "
                f"{result.reminded} reminded"
            ),
            user_id=tenant.user_id,
            context=result.model_dump(),
        )
    return result


__all__ = [
    "FOLLOW_UP_LIFECYCLE",
    "REMINDER_AFTER",
    "UNASSIGNED_REMINDER_ROLES",
    "add_note",
    "cancel",
    "create_follow_up",
    "escalate",
    "get_follow_up",
    "list_follow_ups",
    "list_notes",
    "normalise_currency",
    "record_promise",
    "reminder_due",
    "resolve",
    "scan_overdue_receivables",
    "update_follow_up",
    "validate_promise",
    "worklist",
]
