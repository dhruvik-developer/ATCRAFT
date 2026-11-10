"""Request and response shapes for payment follow-up.

The worklist and the follow-up list answer two different questions and therefore
have two shapes. The worklist is *money*: one row per customer and currency whose
invoices are overdue, whether or not anybody has started chasing them yet. The
follow-up list is *work*: the rows themselves, including resolved and cancelled
ones, because a collections manager reviews what was closed as often as what is
open.

Every money column carries the invoice's own currency. Nothing in this module
adds two denominations together, which is why ``currency`` is part of every row
key rather than a display column.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.core.constants import REASON_MIN_LENGTH
from src.modules.collections.models import FollowUpChannel, FollowUpOutcome, FollowUpStatus
from src.modules.customers.models import CustomerType

# -----------------------------------------------------------------------------
# Filters
# -----------------------------------------------------------------------------


class WorklistFilters(BaseModel):
    """Narrowing for the overdue worklist."""

    search: Annotated[str | None, Field(max_length=120)] = None
    customer_id: uuid.UUID | None = None
    #: Follow-up status. ``None`` returns rows with and without a live follow-up.
    status: FollowUpStatus | None = None
    #: Restrict to follow-ups assigned to the caller. Rows with no follow-up are
    #: excluded: an unopened chase belongs to nobody yet.
    assigned_to_me: bool = False
    #: Minimum days past its due date of the *oldest* overdue invoice.
    min_overdue_days: Annotated[int | None, Field(ge=0, le=3650)] = None
    #: Dealer and distributor channels only. The dealers screen links here.
    dealer_only: bool = False


class FollowUpFilters(BaseModel):
    """Narrowing for the follow-up register."""

    search: Annotated[str | None, Field(max_length=120)] = None
    status: FollowUpStatus | None = None
    customer_id: uuid.UUID | None = None
    assigned_to_id: uuid.UUID | None = None
    assigned_to_me: bool = False
    #: Exclude RESOLVED and CANCELLED. The screen's default.
    live_only: bool = False


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------


class FollowUpCreate(BaseModel):
    """Open a chase by hand, for a customer that already owes overdue money."""

    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    assigned_to_id: uuid.UUID | None = None
    next_action_date: date | None = None


class FollowUpUpdate(BaseModel):
    """Reassign or reschedule. Both fields are nullable: clearing is meaningful."""

    model_config = ConfigDict(extra="forbid")

    assigned_to_id: uuid.UUID | None = None
    next_action_date: date | None = None


class FollowUpNoteCreate(BaseModel):
    """One contact attempt. ``SYSTEM`` is refused - that channel is the sweep's."""

    model_config = ConfigDict(extra="forbid")

    channel: FollowUpChannel
    outcome: FollowUpOutcome | None = None
    note: Annotated[str, Field(min_length=1, max_length=4000)]
    next_action_date: date | None = None


class FollowUpPromise(BaseModel):
    """A dated commitment from the customer."""

    model_config = ConfigDict(extra="forbid")

    promised_amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    promised_date: date


class FollowUpEscalate(BaseModel):
    """Hand the chase to a manager, optionally putting the account on hold."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=500)]
    #: Requires ``customer.update`` in addition to ``collection.manage``; the
    #: router checks the second right before the block is attempted.
    block_customer: bool = False


class FollowUpResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=2000)]


class FollowUpCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=500)]


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


class FollowUpSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    customer_id: uuid.UUID
    customer_code: str
    customer_name: str
    currency: str
    status: FollowUpStatus
    assigned_to_id: uuid.UUID | None
    assigned_to_name: str | None
    opened_overdue_amount: Decimal
    current_overdue_amount: Decimal
    oldest_due_date: date
    next_action_date: date | None
    promised_amount: Decimal | None
    promised_date: date | None
    last_note: str | None = None
    last_note_at: datetime | None = None
    last_reminded_at: datetime | None
    escalated_at: datetime | None
    resolution_note: str | None
    resolved_at: datetime | None
    resolved_by_id: uuid.UUID | None
    cancellation_reason: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class FollowUpInvoiceRow(BaseModel):
    """One overdue invoice behind a follow-up, with its settled balance."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    invoice_date: date
    due_date: date
    currency: str
    total_amount: Decimal
    paid_amount: Decimal
    credited_amount: Decimal
    balance_amount: Decimal
    overdue_days: int
    status: str


class FollowUpDetail(FollowUpSummary):
    invoices: list[FollowUpInvoiceRow] = Field(default_factory=list)


class FollowUpNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    follow_up_id: uuid.UUID
    channel: FollowUpChannel
    outcome: FollowUpOutcome | None
    note: str
    next_action_date: date | None
    created_at: datetime
    created_by_id: uuid.UUID | None
    created_by_name: str | None = None


class WorklistRow(BaseModel):
    """One customer's overdue position in one currency.

    ``follow_up_id`` is null until somebody - a person or the nightly sweep -
    opens a chase. The row still appears, because the money is overdue whether or
    not it has an owner.
    """

    customer_id: uuid.UUID
    customer_code: str
    customer_name: str
    customer_type: CustomerType
    currency: str
    assigned_to_id: uuid.UUID | None = None
    assigned_to_name: str | None = None
    overdue_amount: Decimal
    overdue_invoice_count: int
    oldest_overdue_days: int
    bucket_0_30_amount: Decimal = Decimal("0")
    bucket_31_60_amount: Decimal = Decimal("0")
    bucket_61_90_amount: Decimal = Decimal("0")
    bucket_90_plus_amount: Decimal = Decimal("0")
    last_note_at: datetime | None = None
    next_action_date: date | None = None
    promised_amount: Decimal | None = None
    promised_date: date | None = None
    status: FollowUpStatus | None = None
    follow_up_id: uuid.UUID | None = None
    follow_up_code: str | None = None


class SweepResult(BaseModel):
    """What one company's nightly run did. Returned by the task for the log."""

    opened: int = 0
    refreshed: int = 0
    broken_promises: int = 0
    resolved: int = 0
    reminded: int = 0
    changed: bool = False


__all__ = [
    "FollowUpCancel",
    "FollowUpCreate",
    "FollowUpDetail",
    "FollowUpEscalate",
    "FollowUpFilters",
    "FollowUpInvoiceRow",
    "FollowUpNoteCreate",
    "FollowUpNoteRead",
    "FollowUpPromise",
    "FollowUpResolve",
    "FollowUpSummary",
    "FollowUpUpdate",
    "SweepResult",
    "WorklistFilters",
    "WorklistRow",
]
