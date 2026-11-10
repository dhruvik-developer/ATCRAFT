"""An open payment follow-up for a customer, for the collections suites.

``_follow_up`` writes the row straight through the ORM - 250 overdue, the oldest
due ten days ago - so a test of who may see or work a follow-up does not first
have to age an invoice. Both collections HTTP suites use it; it lives here so
neither is the other's library.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.modules.collections.models import FollowUpStatus, PaymentFollowUp


async def _follow_up(
    session: AsyncSession, *, company_id: uuid.UUID, customer_id: uuid.UUID
) -> PaymentFollowUp:
    follow = PaymentFollowUp(
        company_id=company_id,
        code=f"HTTP-FU-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer_id,
        currency="INR",
        status=FollowUpStatus.OPEN,
        opened_overdue_amount=Decimal("250"),
        current_overdue_amount=Decimal("250"),
        oldest_due_date=today() - timedelta(days=10),
    )
    session.add(follow)
    await session.flush()
    return follow


__all__ = ["_follow_up"]
