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
from src.core.logging import (
    get_request_id,
    ip_address_ctx,
    user_agent_ctx,
)
from src.modules.audit.models import AuditAction, AuditLog

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


__all__ = ["diff_values", "record_audit", "record_status_change", "snapshot"]
