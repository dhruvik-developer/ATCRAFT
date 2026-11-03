"""Collections demonstration data built from stable demo dispatch identities."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from src.core.clock import today
from src.core.demo_data import DemoContext
from src.modules.collections import service as collections_service
from src.modules.collections.models import (
    FollowUpChannel,
    FollowUpStatus,
    PaymentFollowUp,
    PaymentFollowUpNote,
)
from src.modules.collections.repository import PaymentFollowUpRepository
from src.modules.collections.schemas import FollowUpNoteCreate, FollowUpPromise
from src.modules.finance.models import SalesInvoice, SalesInvoiceStatus

STAGE = "collections"
CALL_NOTE = "Called accounts payable to confirm the invoice was received."


async def _invoice_for_dispatch(ctx: DemoContext, dispatch_code: str) -> SalesInvoice | None:
    dispatch_id = ctx.maybe_id("dispatch", dispatch_code)
    if dispatch_id is None:
        ctx.skipped.append((STAGE, f"no demo dispatch registered as {dispatch_code}"))
        return None
    return (
        await ctx.session.execute(
            select(SalesInvoice).where(
                SalesInvoice.company_id == ctx.company_id,
                SalesInvoice.dispatch_id == dispatch_id,
                SalesInvoice.status.in_(
                    (SalesInvoiceStatus.ISSUED, SalesInvoiceStatus.PARTIALLY_PAID)
                ),
            )
        )
    ).scalar_one_or_none()


async def _has_note(
    ctx: DemoContext,
    follow_up_id: uuid.UUID,
    *,
    channel: FollowUpChannel | None = None,
    exact_text: str | None = None,
    text_fragment: str | None = None,
) -> bool:
    stmt = select(PaymentFollowUpNote.id).where(
        PaymentFollowUpNote.company_id == ctx.company_id,
        PaymentFollowUpNote.follow_up_id == follow_up_id,
    )
    if channel is not None:
        stmt = stmt.where(PaymentFollowUpNote.channel == channel)
    if exact_text is not None:
        stmt = stmt.where(PaymentFollowUpNote.note == exact_text)
    if text_fragment is not None:
        stmt = stmt.where(PaymentFollowUpNote.note.ilike(f"%{text_fragment}%"))
    return (await ctx.session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def _live_follow_up(ctx: DemoContext, invoice: SalesInvoice) -> PaymentFollowUp | None:
    return await PaymentFollowUpRepository(ctx.session, ctx.tenant).live_for(
        invoice.customer_id, invoice.currency
    )


async def _seed_promised_follow_up(
    ctx: DemoContext, invoice: SalesInvoice, *, anchor: date
) -> None:
    follow = await _live_follow_up(ctx, invoice)
    if follow is None:
        ctx.skipped.append((STAGE, f"no live follow-up for overdue invoice {invoice.code}"))
        return

    if not await _has_note(ctx, follow.id, channel=FollowUpChannel.CALL, exact_text=CALL_NOTE):
        await collections_service.add_note(
            ctx.session,
            ctx.tenant,
            follow.id,
            FollowUpNoteCreate(
                channel=FollowUpChannel.CALL,
                note=CALL_NOTE,
                next_action_date=anchor + timedelta(days=5),
            ),
        )

    if (
        follow.status != FollowUpStatus.PROMISED
        or follow.promised_date is None
        or follow.promised_date < anchor
    ):
        await collections_service.record_promise(
            ctx.session,
            ctx.tenant,
            follow.id,
            FollowUpPromise(
                promised_amount=follow.current_overdue_amount,
                promised_date=anchor + timedelta(days=5),
            ),
        )


async def _seed_broken_promise(ctx: DemoContext, invoice: SalesInvoice, *, anchor: date) -> None:
    follow = await _live_follow_up(ctx, invoice)
    if follow is None:
        ctx.skipped.append((STAGE, f"no live follow-up for overdue invoice {invoice.code}"))
        return

    has_broken_promise = await _has_note(
        ctx,
        follow.id,
        channel=FollowUpChannel.SYSTEM,
        text_fragment="was not met.",
    )
    if has_broken_promise:
        return

    if follow.status != FollowUpStatus.PROMISED:
        promised_date = max(anchor, today())
        await collections_service.record_promise(
            ctx.session,
            ctx.tenant,
            follow.id,
            FollowUpPromise(
                promised_amount=follow.current_overdue_amount,
                promised_date=promised_date,
            ),
        )
    if follow.promised_date is not None and follow.promised_date <= max(anchor, today()):
        promised_date = follow.promised_date
        await collections_service.scan_overdue_receivables(
            ctx.session, ctx.tenant, as_of=promised_date + timedelta(days=1)
        )


async def seed(ctx: DemoContext) -> None:
    """Create one kept promise and one broken promise for the seeded overdue invoices."""
    anchor = today()
    first = await _invoice_for_dispatch(ctx, ctx.code("DSP", "SHIPPED"))
    second = await _invoice_for_dispatch(ctx, ctx.code("DSP", "DELIVERED"))

    result = await collections_service.scan_overdue_receivables(
        ctx.session, ctx.tenant, as_of=anchor
    )
    ctx.note(STAGE, created=result.opened)

    if first is not None and first.due_date < anchor:
        await _seed_promised_follow_up(ctx, first, anchor=anchor)
    elif first is not None:
        ctx.skipped.append(
            (STAGE, f"{first.code} is not overdue; its issued invoice was preserved")
        )
    if second is not None and second.due_date < anchor:
        await _seed_broken_promise(ctx, second, anchor=anchor)
    elif second is not None:
        ctx.skipped.append(
            (STAGE, f"{second.code} is not overdue; its issued invoice was preserved")
        )


__all__ = ["seed"]
