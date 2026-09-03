"""Request and response schemas for the notification bell.

Three things are deliberate here.

* **No ``user_id`` anywhere.** The recipient is always the authenticated
  principal. A client that could name a recipient could read another employee's
  approvals queue by id, so the field simply does not exist on the API surface -
  the same reasoning that keeps ``company_id`` out of every payload (section 40).
* **Preferences are submitted as a whole set, not one row at a time.** The screen
  is a matrix of checkboxes; sending it as one request makes the update a single
  upsert in a single transaction, and removes the half-saved state a per-row
  endpoint produces when the third of five calls fails.
* **The unread count reports whether it was capped.** A bell that renders "99+"
  needs to know it is looking at a ceiling rather than an exact figure; the
  alternative is counting an unbounded backlog on every poll.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.modules.notifications.models import (
    NotificationSeverity,
    NotificationType,
)


class NotificationRead(BaseModel):
    """One row of the bell feed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_type: NotificationType
    severity: NotificationSeverity
    title: str
    body: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    action_url: str | None
    is_read: bool
    read_at: datetime | None
    channels: list[str]
    delivered_at: datetime | None
    group_key: str | None
    #: Times this alert has fired since it was last read; 1 for a single event.
    #: The UI renders anything above 1 as a multiplier rather than as separate
    #: rows - see the grouping design in the models module.
    occurrence_count: int
    created_at: datetime
    updated_at: datetime


class UnreadCountRead(BaseModel):
    """Payload of ``GET /notifications/unread-count``."""

    unread_count: int
    #: True when the real number is at least ``unread_count`` but was not counted
    #: exactly. The UI shows "500+" rather than a precise figure nobody reads.
    is_capped: bool = False


class MarkReadResult(BaseModel):
    """Outcome of a bulk read acknowledgement."""

    marked: int
    #: Unread notifications still left after this call. Non-zero when the backlog
    #: exceeded one batch, in which case the client repeats the request.
    remaining_unread: int


class NotificationFilters(BaseModel):
    """Feed filters. Everything else about the query is fixed by the endpoint."""

    unread_only: bool = False
    notification_type: NotificationType | None = None
    severity: NotificationSeverity | None = None


class NotificationPreferenceRead(BaseModel):
    """Effective preference for one type.

    Returned for *every* :class:`NotificationType`, including the ones with no
    stored row - a preferences screen has to render the whole matrix, and asking
    the client to know the defaults would let the two drift apart.
    """

    model_config = ConfigDict(from_attributes=True)

    notification_type: NotificationType
    in_app_enabled: bool
    email_enabled: bool
    #: False when this is the module default rather than a stored choice.
    is_customised: bool = False


class NotificationPreferenceUpdate(BaseModel):
    """One checkbox row of the preferences matrix."""

    notification_type: NotificationType
    in_app_enabled: bool
    email_enabled: bool


class PreferencesUpdateRequest(BaseModel):
    """The rows the user changed.

    Partial by design: sending only the touched rows keeps an old client from
    silently resetting a type it does not know about yet.

    Turning every channel off for a type is allowed - it is the user's own inbox
    - and is not rejected here. CRITICAL alerts still reach the bell regardless;
    that override lives in the service, next to the constant that names it.
    """

    preferences: Annotated[
        list[NotificationPreferenceUpdate],
        Field(min_length=1, max_length=len(NotificationType)),
    ]

    @field_validator("preferences")
    @classmethod
    def _reject_duplicate_types(
        cls, value: list[NotificationPreferenceUpdate]
    ) -> list[NotificationPreferenceUpdate]:
        # Two rows for one type make the result depend on which one the upsert
        # applied last, and PostgreSQL refuses to touch a row twice in one
        # ON CONFLICT statement anyway.
        seen = {p.notification_type for p in value}
        if len(seen) != len(value):
            raise ValueError("Each notification_type may appear only once.")
        return value


__all__ = [
    "MarkReadResult",
    "NotificationFilters",
    "NotificationPreferenceRead",
    "NotificationPreferenceUpdate",
    "NotificationRead",
    "PreferencesUpdateRequest",
    "UnreadCountRead",
]
