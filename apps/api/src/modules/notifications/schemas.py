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

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.modules.notifications.alerts import (
    ALERT_RULES,
    MAX_ALERT_WINDOW_MINUTES,
    AlertGranularity,
    AlertRuleKey,
)
from src.modules.notifications.models import (
    NotificationChannel,
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
    push_enabled: bool = True
    whatsapp_enabled: bool = False
    sms_enabled: bool = False
    #: False when this is the module default rather than a stored choice.
    is_customised: bool = False


class NotificationPreferenceUpdate(BaseModel):
    """One checkbox row of the preferences matrix."""

    notification_type: NotificationType
    in_app_enabled: bool
    email_enabled: bool
    push_enabled: bool | None = None
    whatsapp_enabled: bool | None = None
    sms_enabled: bool | None = None


class NotificationAlertRuleRead(BaseModel):
    """One effective alert rule, including code defaults for sparse settings."""

    key: AlertRuleKey
    notification_type: str
    name: str
    description: str
    severity: str
    granularity: AlertGranularity
    is_enabled: bool
    threshold_minutes: int = Field(ge=0)
    remind_every_minutes: int = Field(ge=1)
    is_customized: bool


class NotificationAlertRuleUpdate(BaseModel):
    """A complete override for one changed alert rule."""

    key: AlertRuleKey
    is_enabled: bool
    threshold_minutes: int = Field(ge=0, le=MAX_ALERT_WINDOW_MINUTES)
    remind_every_minutes: int = Field(ge=1, le=MAX_ALERT_WINDOW_MINUTES)


class NotificationAlertRulesUpdate(BaseModel):
    """Only touched alert rows are submitted by the administration screen."""

    rules: Annotated[
        list[NotificationAlertRuleUpdate],
        Field(min_length=1, max_length=len(ALERT_RULES)),
    ]

    @field_validator("rules")
    @classmethod
    def _reject_duplicate_keys(
        cls, value: list[NotificationAlertRuleUpdate]
    ) -> list[NotificationAlertRuleUpdate]:
        keys = {rule.key for rule in value}
        if len(keys) != len(value):
            raise ValueError("Each alert rule may appear only once.")
        return value


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


TemplateParameter = Literal["title", "body", "code", "link", "amount", "date"]
MessagingChannel = Literal["WHATSAPP", "SMS"]


class NotificationChannelsRead(BaseModel):
    whatsapp_enabled: bool
    sms_enabled: bool


class NotificationChannelTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    channel: MessagingChannel
    notification_type: NotificationType
    template_name: str | None
    template_id: str | None
    language: str
    parameters: list[TemplateParameter]
    sms_text: str | None
    is_active: bool


class NotificationChannelTemplateUpdate(BaseModel):
    channel: MessagingChannel
    notification_type: NotificationType
    template_name: str | None = Field(default=None, max_length=128)
    template_id: str | None = Field(default=None, max_length=128)
    language: str = Field(default="en", min_length=2, max_length=16)
    parameters: list[TemplateParameter] = Field(default_factory=list, max_length=6)
    sms_text: str | None = Field(default=None, max_length=2000)
    is_active: bool = True

    @field_validator("template_name", "template_id", "sms_text")
    @classmethod
    def _trim_optional(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @model_validator(mode="after")
    def _fields_match_channel(self) -> NotificationChannelTemplateUpdate:
        if self.channel == NotificationChannel.WHATSAPP.value:
            if not self.template_name or self.template_id is not None or self.sms_text is not None:
                raise ValueError("WhatsApp mappings require a template name only.")
        else:
            if not self.template_id or self.template_name is not None or self.sms_text is None:
                raise ValueError("SMS mappings require a DLT template ID and registered text.")
            if len(re.findall(r"\{#var#\}", self.sms_text)) != len(self.parameters):
                raise ValueError("SMS text placeholders must match the parameter list.")
        return self


class NotificationChannelTemplatesUpdate(BaseModel):
    templates: Annotated[
        list[NotificationChannelTemplateUpdate], Field(min_length=1, max_length=100)
    ]

    @field_validator("templates")
    @classmethod
    def _reject_duplicate_keys(
        cls, value: list[NotificationChannelTemplateUpdate]
    ) -> list[NotificationChannelTemplateUpdate]:
        keys = {(item.channel, item.notification_type) for item in value}
        if len(keys) != len(value):
            raise ValueError("Each channel and notification type may appear only once.")
        return value


class NotificationChannelTestRequest(BaseModel):
    channel: MessagingChannel
    notification_type: NotificationType


class NotificationDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_id: uuid.UUID
    channel: MessagingChannel
    outcome: Literal["DELIVERED", "FAILED", "RETRY"]
    provider_message_id: str | None
    attempted_at: datetime
    error_code: str | None


__all__ = [
    "MarkReadResult",
    "NotificationAlertRuleRead",
    "NotificationAlertRuleUpdate",
    "NotificationAlertRulesUpdate",
    "NotificationChannelTemplateRead",
    "NotificationChannelTemplateUpdate",
    "NotificationChannelTemplatesUpdate",
    "NotificationChannelTestRequest",
    "NotificationChannelsRead",
    "NotificationDeliveryRead",
    "NotificationFilters",
    "NotificationPreferenceRead",
    "NotificationPreferenceUpdate",
    "NotificationRead",
    "PreferencesUpdateRequest",
    "UnreadCountRead",
]
