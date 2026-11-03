"""Payment follow-up: who owes what, who is chasing it, and what was said.

Two tables, and the split is the same one the rest of the codebase uses:

``payment_follow_ups``
    One live row per customer and currency while money is overdue. It carries
    the chase's current shape - who owns it, what was promised, when the next
    action is due - and is edited, so it is a ``TenantEntity`` with an
    optimistic-lock version.

``payment_follow_up_notes``
    Append-only history: each call, mail, visit or system observation. A note is
    evidence of what was said, so it is never updated or deleted; the migration
    that creates the table installs the same rejection trigger the stock ledger
    and audit log use.

Money is stored in the invoice's own currency. A customer may owe rupees and
dollars at once, and the two cannot be added: the live-row uniqueness therefore
includes ``currency``, not just the customer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.constants import CURRENCY_CODE_LENGTH
from src.core.models import (
    ImmutableLedgerEntity,
    OptimisticLockMixin,
    TenantEntity,
    sql_in_list,
)
from src.core.types import GUID, Money


class FollowUpStatus(StrEnum):
    """Lifecycle of a payment follow-up.

    ``RESOLVED`` and ``CANCELLED`` are terminal. ``PROMISED`` records a dated
    commitment from the customer; ``ESCALATED`` records that ordinary chasing has
    failed and a manager or a credit hold is now involved.
    """

    OPEN = "OPEN"
    PROMISED = "PROMISED"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


#: Statuses that occupy the one-live-row-per-customer-and-currency slot. The
#: partial unique index below spells exactly this tuple, so the two must change
#: together.
LIVE_FOLLOW_UP_STATUSES: tuple[FollowUpStatus, ...] = (
    FollowUpStatus.OPEN,
    FollowUpStatus.PROMISED,
    FollowUpStatus.ESCALATED,
)


class FollowUpChannel(StrEnum):
    """How the customer was contacted, or that the system acted."""

    CALL = "CALL"
    EMAIL = "EMAIL"
    VISIT = "VISIT"
    MESSAGE = "MESSAGE"
    #: Written by the sweep, never by a person: opened, refreshed, broken promise.
    SYSTEM = "SYSTEM"


class FollowUpOutcome(StrEnum):
    """What the contact produced. Optional on a note."""

    PROMISED = "PROMISED"
    DISPUTED = "DISPUTED"
    NO_RESPONSE = "NO_RESPONSE"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    OTHER = "OTHER"


#: A reminder is sent to the assignee at most this often while the money is still
#: overdue. An ASSUMPTION: the master rules require a follow-up reminder but fix
#: no cadence, and three days is frequent enough to matter without turning the
#: bell into noise. docs/assumptions.md §A4-5.
FOLLOW_UP_REMINDER_DAYS = 3


class PaymentFollowUp(TenantEntity, OptimisticLockMixin):
    """A live chase against one customer's overdue balance in one currency."""

    __tablename__ = "payment_follow_ups"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)

    status: Mapped[FollowUpStatus] = mapped_column(
        String(16), nullable=False, default=FollowUpStatus.OPEN, server_default="OPEN"
    )

    #: The salesperson who owns the account, unless a manager reassigns it. SET
    #: NULL: a departing employee must not block their own deactivation.
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: What was overdue when the chase opened, and what is overdue now. The pair
    #: answers "is this getting better?" without replaying every note.
    opened_overdue_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    current_overdue_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    oldest_due_date: Mapped[date] = mapped_column(Date, nullable=False)

    next_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Both or neither. A promise without a date cannot be broken, and a date
    #: without an amount cannot be checked against the balance.
    promised_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    promised_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    last_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One live chase per customer and currency. Demoting the old row before
        # promoting a new one is the caller's job (CLAUDE.md trap): a partial
        # unique index rejects the second INSERT, not the first.
        Index(
            "uq_payment_follow_ups_live_party",
            "company_id",
            "customer_id",
            "currency",
            unique=True,
            postgresql_where=text(
                f"status IN ({sql_in_list(sorted(LIVE_FOLLOW_UP_STATUSES, key=str))})"
            ),
        ),
        # The daily sweep and the "due today" dashboard tile.
        Index(
            "ix_payment_follow_ups_status_due",
            "company_id",
            "status",
            "next_action_date",
        ),
        # "My follow-ups" and reassignment.
        Index(
            "ix_payment_follow_ups_assignee",
            "company_id",
            "assigned_to_id",
            "status",
        ),
        # The customer detail panel's history, newest first.
        Index(
            "ix_payment_follow_ups_customer",
            "company_id",
            "customer_id",
            "created_at",
        ),
        CheckConstraint(f"status IN ({sql_in_list(FollowUpStatus)})", name="status_known"),
        CheckConstraint(
            "(promised_amount IS NULL) = (promised_date IS NULL)", name="promise_complete"
        ),
        CheckConstraint(
            "promised_amount IS NULL OR promised_amount > 0", name="promised_amount_positive"
        ),
        CheckConstraint("opened_overdue_amount >= 0", name="opened_amount_non_negative"),
        CheckConstraint("current_overdue_amount >= 0", name="current_amount_non_negative"),
        CheckConstraint(
            "status <> 'RESOLVED' OR resolved_at IS NOT NULL", name="resolved_has_timestamp"
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_has_reason",
        ),
        CheckConstraint(
            "status <> 'ESCALATED' OR escalated_at IS NOT NULL", name="escalated_has_timestamp"
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="currency_is_iso_code"
        ),
    )


class PaymentFollowUpNote(ImmutableLedgerEntity):
    """One contact attempt or system observation against a follow-up."""

    __tablename__ = "payment_follow_up_notes"

    follow_up_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_follow_ups.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[FollowUpChannel] = mapped_column(String(16), nullable=False)
    outcome: Mapped[FollowUpOutcome | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    next_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        # The timeline's keyset order. ``created_at`` alone collides when the
        # sweep writes several notes in one transaction, so the id is part of
        # the key as it is for every other keyset-paged table.
        Index(
            "ix_payment_follow_up_notes_timeline",
            "follow_up_id",
            "created_at",
            "id",
        ),
        CheckConstraint(f"channel IN ({sql_in_list(FollowUpChannel)})", name="channel_known"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({sql_in_list(FollowUpOutcome)})",
            name="outcome_known",
        ),
        CheckConstraint("length(btrim(note)) > 0", name="note_not_blank"),
    )


#: The system note a sweep writes, so the reason a row moved is on the timeline
#: and not only in the audit log.
SYSTEM_CHANNEL = FollowUpChannel.SYSTEM


__all__ = [
    "FOLLOW_UP_REMINDER_DAYS",
    "LIVE_FOLLOW_UP_STATUSES",
    "SYSTEM_CHANNEL",
    "FollowUpChannel",
    "FollowUpOutcome",
    "FollowUpStatus",
    "PaymentFollowUp",
    "PaymentFollowUpNote",
]
