"""Audit log - a first-class feature, not a side effect (master rules section 41).

The table is append-only by construction: it inherits
:class:`ImmutableLedgerEntity` (no ``updated_at``, no ``deleted_at``, no version
column), and the migration additionally installs a trigger that raises on UPDATE
and DELETE so "ordinary users must not be able to modify audit records" is
enforced by PostgreSQL rather than by convention.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import ImmutableLedgerEntity
from src.core.types import GUID


class AuditAction(StrEnum):
    """The action vocabulary from master rules section 41."""

    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    STATUS_CHANGE = "STATUS_CHANGE"

    PERMISSION_CHANGE = "PERMISSION_CHANGE"
    ROLE_CHANGE = "ROLE_CHANGE"
    CONFIG_CHANGE = "CONFIG_CHANGE"

    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_DELETE = "DOCUMENT_DELETE"
    DATA_EXPORT = "DATA_EXPORT"

    INVENTORY_MOVEMENT = "INVENTORY_MOVEMENT"
    RESERVATION_CHANGE = "RESERVATION_CHANGE"
    ALLOCATION_CHANGE = "ALLOCATION_CHANGE"
    REALLOCATION = "REALLOCATION"
    STOCK_ADJUSTMENT = "STOCK_ADJUSTMENT"

    QC_DECISION = "QC_DECISION"
    PURCHASE_APPROVAL = "PURCHASE_APPROVAL"
    PRODUCTION_STATUS_CHANGE = "PRODUCTION_STATUS_CHANGE"
    DISPATCH_CHANGE = "DISPATCH_CHANGE"
    PAYMENT = "PAYMENT"

    MRP_RUN = "MRP_RUN"
    JOB_STARTED = "JOB_STARTED"
    JOB_FAILED = "JOB_FAILED"
    SCAN = "SCAN"


class AuditLog(ImmutableLedgerEntity):
    """One recorded business action.

    ``old_values``/``new_values`` hold only the fields that actually changed -
    storing whole row snapshots on every update would bloat the table without
    making an investigation any easier.
    """

    __tablename__ = "audit_logs"

    action: Mapped[AuditAction] = mapped_column(String(48), nullable=False)

    # What was acted upon. entity_id is a bare UUID rather than a foreign key:
    # the audit row must survive the deletion of whatever it describes.
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    entity_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    changed_fields: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Request provenance (section 41): who, from where, on which request.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Denormalised so the log stays readable after a user is renamed or removed.
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # Primary access path: "show me this tenant's recent activity", keyset
        # paginated. created_at DESC, id DESC matches the cursor comparison.
        Index("ix_audit_logs_company_created", "company_id", "created_at", "id"),
        # "Full history of this purchase order" - the traceability requirement.
        Index("ix_audit_logs_entity", "company_id", "entity_type", "entity_id", "created_at"),
        # "What did this user do?" - used in incident review.
        Index("ix_audit_logs_actor", "company_id", "created_by_id", "created_at"),
        Index("ix_audit_logs_action", "company_id", "action", "created_at"),
        Index("ix_audit_logs_request", "request_id"),
        {"comment": "Append-only. UPDATE and DELETE are blocked by a database trigger."},
    )


class ApprovalRequest(ImmutableLedgerEntity):
    """A recorded approval decision (section 42: evidence of who and when).

    Kept alongside the audit log rather than inside it because approvals are
    queried as workflow state - "what is waiting for me?" - not only as history.
    """

    __tablename__ = "approval_requests"

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    entity_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    requested_action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'PENDING'")
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_approval_requests_pending", "company_id", "status", "created_at"),
        Index("ix_approval_requests_entity", "company_id", "entity_type", "entity_id"),
    )


__all__ = ["ApprovalRequest", "AuditAction", "AuditLog"]
