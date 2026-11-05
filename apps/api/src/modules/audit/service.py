"""Audit recording service.

Every module writes through :func:`record_audit`, inside the same transaction as
the change it describes. That coupling is deliberate: if the business write
rolls back, the audit entry must roll back with it, and if the audit write fails,
the business change must not be committed either (master rules sections 29/41).

This is why audit is not an async fire-and-forget queue publish.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.actors import SYSTEM_ACTOR_LABEL, actor_id, is_system_actor
from src.core.database import advisory_xact_lock, transaction
from src.core.logging import (
    get_request_id,
    ip_address_ctx,
    user_agent_ctx,
)
from src.modules.audit.models import AuditAction, AuditLog, CompanyApprovalSettings
from src.modules.audit.repository import CompanyApprovalSettingsRepository
from src.modules.audit.schemas import ApprovalSettingsRead, ApprovalSettingsUpdate

# Never persist these into old_values/new_values, whatever a caller passes.
_NEVER_LOG = frozenset(
    {
        "password",
        "password_hash",
        "hashed_password",
        "new_password",
        "current_password",
        "token",
        "access_token",
        "refresh_token",
        "reset_token_hash",
        "secret",
        "api_key",
        "otp",
    }
)

# A field whose serialised value exceeds this is summarised instead of stored -
# an audit row should describe a change, not duplicate a document.
_MAX_VALUE_CHARS = 2000

DEFAULT_PO_DUAL_APPROVAL_AMOUNT = Decimal("25000.00")
DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD = Decimal("25000.00")
DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD = Decimal("25000.00")
DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT = Decimal("0.00")


def _serialise(value: Any) -> Any:
    """Make a value JSON-storable without losing decimal precision."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)  # never float() - section 84
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple, set)):
        return [_serialise(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else f"{text[:_MAX_VALUE_CHARS]}...[truncated]"


def _clean(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if not values:
        return None
    return {k: _serialise(v) for k, v in values.items() if k.lower() not in _NEVER_LOG}


def diff_values(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Reduce two snapshots to only what changed.

    Comparison is done on the serialised forms so ``Decimal("10.00")`` and
    ``Decimal("10.0000")`` do not register as a change - a scale difference is
    not a business event.
    """
    before_clean = _clean(before) or {}
    after_clean = _clean(after) or {}

    changed = [
        key
        for key in set(before_clean) | set(after_clean)
        if before_clean.get(key) != after_clean.get(key)
    ]
    if not changed:
        return None, None, []

    return (
        {k: before_clean.get(k) for k in changed},
        {k: after_clean.get(k) for k in changed},
        sorted(changed),
    )


async def record_audit(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    action: AuditAction,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    entity_code: str | None = None,
    summary: str = "",
    user_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    flush: bool = True,
) -> AuditLog:
    """Append one audit entry to the current transaction.

    Does not commit - the caller's :func:`~src.core.database.transaction` block
    owns that. Flushes by default so a constraint violation surfaces here
    rather than at an unrelated point later in the request. A caller may pass
    ``flush=False`` only when it flushes itself before its ``transaction()``
    block exits, so the violation still surfaces inside the same operation -
    the inventory posting loop does this to write one flush for a whole
    document's audit rows.

    A scheduled sweep acting as the system actor (``src.core.actors``) is
    recorded with no user and the label ``"System"`` unless the caller names one:
    the nil placeholder it carries is not a user, and the foreign key would
    refuse it.
    """
    if is_system_actor(user_id):
        actor_label = actor_label or SYSTEM_ACTOR_LABEL
    user_id = actor_id(user_id)

    if old_values is not None or new_values is not None:
        old_clean, new_clean, changed = diff_values(old_values, new_values)
    else:
        old_clean, new_clean, changed = None, None, []

    entry = AuditLog(
        company_id=company_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_code=entity_code,
        summary=summary or f"{action} {entity_type}",
        old_values=old_clean,
        new_values=new_clean,
        changed_fields=changed or None,
        context=_clean(context),
        # A scheduled job's placeholder actor is recorded as nobody, not as a
        # foreign key to a user that cannot exist (``src.core.actors``).
        created_by_id=user_id,
        actor_label=actor_label,
        request_id=get_request_id(),
        ip_address=ip_address_ctx.get(),
        user_agent=user_agent_ctx.get(),
    )
    session.add(entry)
    if flush:
        await session.flush()
    return entry


async def record_status_change(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    entity_code: str | None,
    from_status: str,
    to_status: str,
    user_id: uuid.UUID | None,
    action: AuditAction = AuditAction.STATUS_CHANGE,
    reason: str | None = None,
    flush: bool = True,
) -> AuditLog:
    """Record a state-machine transition (sections 17/73).

    Called by every document status change so the full lifecycle of a purchase
    order or production order can be reconstructed from the audit table alone.
    """
    return await record_audit(
        session,
        company_id=company_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_code=entity_code,
        summary=f"{entity_type} {entity_code or entity_id} moved {from_status} -> {to_status}",
        user_id=user_id,
        old_values={"status": from_status},
        new_values={"status": to_status},
        context={"reason": reason} if reason else None,
        flush=flush,
    )


def snapshot(instance: Any, fields: list[str]) -> dict[str, Any]:
    """Capture named attributes of an ORM instance for before/after comparison."""
    return {f: getattr(instance, f, None) for f in fields}


async def get_company_approval_settings(session: AsyncSession, tenant: Any) -> ApprovalSettingsRead:
    """Return the effective approval thresholds for this company."""
    repo = CompanyApprovalSettingsRepository(session, tenant)
    row = await repo.for_company()
    if row is None:
        return ApprovalSettingsRead(
            po_dual_approval_amount=DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
            adjustment_approval_threshold=DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
            internal_issue_approval_threshold=DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD,
            requisition_auto_approve_limit=DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
            is_default=True,
        )
    values = {
        "po_dual_approval_amount": row.po_dual_approval_amount,
        "adjustment_approval_threshold": row.adjustment_approval_threshold,
        "internal_issue_approval_threshold": row.internal_issue_approval_threshold,
        "requisition_auto_approve_limit": row.requisition_auto_approve_limit,
    }
    applied = {
        field: value if value is not None else default
        for field, value, default in (
            (
                "po_dual_approval_amount",
                values["po_dual_approval_amount"],
                DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
            ),
            (
                "adjustment_approval_threshold",
                values["adjustment_approval_threshold"],
                DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
            ),
            (
                "internal_issue_approval_threshold",
                values["internal_issue_approval_threshold"],
                DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD,
            ),
            (
                "requisition_auto_approve_limit",
                values["requisition_auto_approve_limit"],
                DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
            ),
        )
    }
    return ApprovalSettingsRead(
        po_dual_approval_amount=applied["po_dual_approval_amount"],
        adjustment_approval_threshold=applied["adjustment_approval_threshold"],
        internal_issue_approval_threshold=applied["internal_issue_approval_threshold"],
        requisition_auto_approve_limit=applied["requisition_auto_approve_limit"],
        is_default=all(value is None for value in values.values()),
    )


async def update_company_approval_settings(
    session: AsyncSession,
    tenant: Any,
    payload: ApprovalSettingsUpdate,
) -> ApprovalSettingsRead:
    repo = CompanyApprovalSettingsRepository(session, tenant)
    sent = payload.model_dump(exclude_unset=True)

    # An empty body is not a settings change. Creating an all-NULL row for it
    # would turn "nothing was asked" into a stored company override whose four
    # values are exactly the defaults - which reads differently in an audit.
    if not sent:
        return await get_company_approval_settings(session, tenant)

    async with transaction(session):
        # The row is sparse and may not exist yet, so there is nothing for
        # FOR UPDATE to lock on a first save. Two administrators clicking Save
        # together would both see no row and both INSERT; this transaction-scoped
        # advisory lock serialises that check, and the unique constraint remains
        # the second line of defence. The key is scoped to this company so saves
        # for different companies still run in parallel.
        await advisory_xact_lock(session, "company_approval_settings", tenant.company_id)
        row = await repo.for_company(lock=True)
        if row is None and all(value is None for value in sent.values()):
            # "Restore defaults" on a company that never stored an override:
            # what was asked for is already true.
            return await get_company_approval_settings(session, tenant)

        before = {
            "po_dual_approval_amount": None if row is None else row.po_dual_approval_amount,
            "adjustment_approval_threshold": None
            if row is None
            else row.adjustment_approval_threshold,
            "internal_issue_approval_threshold": None
            if row is None
            else row.internal_issue_approval_threshold,
            "requisition_auto_approve_limit": None
            if row is None
            else row.requisition_auto_approve_limit,
        }
        changes = {field: value for field, value in sent.items() if before[field] != value}
        if not changes:
            # Saving the values that are already in force is not a change, and
            # an audit row claiming one would be misleading.
            return await get_company_approval_settings(session, tenant)

        if row is None:
            row = repo.add(CompanyApprovalSettings())
        for field, value in changes.items():
            setattr(row, field, value)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CONFIG_CHANGE,
            entity_type="company_approval_settings",
            entity_id=row.id,
            entity_code="approval settings",
            summary="Updated approval settings",
            user_id=tenant.user_id,
            old_values=before,
            new_values={
                "po_dual_approval_amount": row.po_dual_approval_amount,
                "adjustment_approval_threshold": row.adjustment_approval_threshold,
                "internal_issue_approval_threshold": row.internal_issue_approval_threshold,
                "requisition_auto_approve_limit": row.requisition_auto_approve_limit,
            },
        )
    return await get_company_approval_settings(session, tenant)


__all__ = [
    "DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD",
    "DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD",
    "DEFAULT_PO_DUAL_APPROVAL_AMOUNT",
    "DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT",
    "diff_values",
    "get_company_approval_settings",
    "record_audit",
    "record_status_change",
    "snapshot",
    "update_company_approval_settings",
]
