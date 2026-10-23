"""Request and response schemas for the audit query API.

The audit surface is read-only, and the shapes here say so: there is no
``AuditLogCreate`` and no ``AuditLogUpdate``. Audit rows are written by
:func:`src.modules.audit.service.record_audit` inside the transaction of the
business change they describe, never by an HTTP client (master rules section 41).

Three decisions are load-bearing.

* **Two read shapes, not one.** ``audit_logs`` carries three JSONB columns -
  ``old_values``, ``new_values`` and ``context`` - which are the widest thing in
  the table. A 200-row page of them is megabytes of payload nobody reads, so the
  list shape deliberately omits them and the detail shape carries them. The
  repository projects the same split at the SQL level, so those columns are not
  even fetched for a listing (section 30: no SELECT * on a wide table).
* **The date range is part of the query contract, not an optional convenience.**
  ``audit_logs`` is the largest table in the system and grows forever. The window
  is therefore always present; see
  :func:`src.modules.audit.query_service.resolve_window` for the defaulting rule
  and the ceiling.
* **An approval row reports whether *this* caller may decide it.** The queue is
  rendered as buttons, and the frontend must not have to re-derive segregation of
  duties from a permission list it only half understands. The backend decides,
  and re-checks on the decision request itself (section 38).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.constants import MAX_REQUEST_ID_LENGTH
from src.core.types import IpAddress
from src.modules.audit.models import AuditAction

#: ``audit_logs.entity_type`` and ``approval_requests.entity_type`` are
#: ``String(64)``; a longer value is a caller bug, not a business error, and is
#: rejected rather than truncated into a filter that silently matches nothing.
MAX_ENTITY_TYPE_LENGTH = 64
MAX_DECISION_NOTE_LENGTH = 2000


class ApprovalStatus(StrEnum):
    """Lifecycle of an :class:`~src.modules.audit.models.ApprovalRequest`.

    Declared here rather than on the model because the model stores the column as
    ``String(24)`` and belongs to the recording half of this module, which must
    not change. The set of strings is nonetheless part of the API contract, so it
    belongs with the other contract types.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AuditWindow(BaseModel):
    """The resolved, bounded time range a log query actually ran over.

    Frozen because it is computed once per request and then handed to the
    repository: a query able to widen its own window after validation would
    defeat the point of having a ceiling.
    """

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86_400


class AuditLogListItem(BaseModel):
    """One row of the audit feed.

    Carries ``changed_fields`` - a short list of column names - but not the
    before/after values themselves. That is what makes a page of this shape cheap
    enough to be the default listing; ``GET /audit/logs/{id}`` returns the values.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: AuditAction
    entity_type: str
    entity_id: uuid.UUID | None
    entity_code: str | None
    summary: str
    changed_fields: list[str] | None
    request_id: str | None
    #: Denormalised onto the row: the log stays readable after the user is
    #: renamed or deactivated, and rendering a page needs no join and no second
    #: query (section 32).
    actor_label: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime


class AuditLogRead(AuditLogListItem):
    """One audit entry in full, including the before/after diff."""

    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    context: dict[str, Any] | None
    ip_address: IpAddress
    user_agent: str | None


class AuditLogFilters(BaseModel):
    """Filters accepted by ``GET /audit/logs``.

    ``date_from``/``date_to`` are optional *here* and mandatory *in effect*: an
    omitted bound is filled in by ``resolve_window``, which also enforces the
    ceiling. Keeping the raw values optional on the schema is what lets the guard
    live in one testable place instead of being spread across three validators.
    """

    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    action: AuditAction | None = None
    #: The actor, i.e. ``audit_logs.created_by_id``. Called ``user_id`` on the
    #: query string because that is what an investigator is looking for.
    user_id: uuid.UUID | None = None
    request_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    @field_validator("entity_type")
    @classmethod
    def _normalise_entity_type(cls, value: str | None) -> str | None:
        # Every module records entity_type in lower snake case; folding the
        # filter the same way stops "Purchase_Order" returning nothing at all,
        # which an investigator reads as "no history" rather than as a typo.
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if len(cleaned) > MAX_ENTITY_TYPE_LENGTH:
            raise ValueError(f"entity_type exceeds {MAX_ENTITY_TYPE_LENGTH} characters.")
        return cleaned

    @field_validator("request_id")
    @classmethod
    def _normalise_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_REQUEST_ID_LENGTH:
            raise ValueError(f"request_id exceeds {MAX_REQUEST_ID_LENGTH} characters.")
        return cleaned


class ApprovalRequestRead(BaseModel):
    """One row of the approval queue.

    ``can_decide`` and ``blocked_reason`` are computed per caller, which is why
    this model is built through :meth:`from_model` rather than validated straight
    off the ORM object: the same row is decidable by one user and not by another.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    entity_code: str | None
    requested_action: str
    reason: str | None
    payload: dict[str, Any] | None
    status: ApprovalStatus
    decided_by_id: uuid.UUID | None
    decision_note: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime

    #: The permission the underlying action requires. ``None`` when
    #: ``requested_action`` is not a permission the registry knows - such a
    #: request is decidable only by a superuser, because nothing can verify what
    #: authority it would take.
    required_permission: str | None = None
    #: True when this caller may approve or reject this row right now.
    can_decide: bool = False
    #: Why not, when ``can_decide`` is false. Rendered as a disabled-button
    #: tooltip; the decision endpoints re-check regardless.
    blocked_reason: str | None = None

    @classmethod
    def from_model(
        cls,
        record: Any,
        *,
        required_permission: str | None,
        can_decide: bool,
        blocked_reason: str | None,
    ) -> ApprovalRequestRead:
        return cls.model_validate(record).model_copy(
            update={
                "required_permission": required_permission,
                "can_decide": can_decide,
                "blocked_reason": blocked_reason,
            }
        )


class ApprovalFilters(BaseModel):
    """Filters accepted by ``GET /audit/approvals``."""

    entity_type: str | None = None

    @field_validator("entity_type")
    @classmethod
    def _normalise_entity_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if len(cleaned) > MAX_ENTITY_TYPE_LENGTH:
            raise ValueError(f"entity_type exceeds {MAX_ENTITY_TYPE_LENGTH} characters.")
        return cleaned


class ApprovalDecisionRequest(BaseModel):
    """Body of ``POST /audit/approvals/{id}/approve`` and ``.../reject``.

    One shape for both decisions so the note is validated identically; whether a
    note is *required* depends on the decision and is therefore a service rule,
    not a schema rule.
    """

    note: Annotated[str | None, Field(default=None, max_length=MAX_DECISION_NOTE_LENGTH)] = None

    @field_validator("note")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        # A note of spaces must not satisfy "a rejection needs a stated reason".
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


__all__ = [
    "MAX_DECISION_NOTE_LENGTH",
    "MAX_ENTITY_TYPE_LENGTH",
    "MAX_REQUEST_ID_LENGTH",
    "ApprovalDecisionRequest",
    "ApprovalFilters",
    "ApprovalRequestRead",
    "ApprovalStatus",
    "AuditLogFilters",
    "AuditLogListItem",
    "AuditLogRead",
    "AuditWindow",
]
