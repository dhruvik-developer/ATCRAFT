"""Commit-safe hand-off of password-reset delivery jobs to Celery.

The reset token row and its audit entry must commit before a worker can send the
link.  Publishing inside the database transaction creates the opposite failure
mode: the broker may accept the message and the database may then roll back,
leaving a link that can never be redeemed.  These session hooks stage the job in
memory and publish only from SQLAlchemy's ``after_commit`` event.

The broker message contains only database identifiers. The redeemable token is
stored as an encrypted, short-lived outbox field and erased after delivery,
redemption, supersession or expiry cleanup. A periodic database sweep recovers
jobs lost while the broker is unavailable.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from src.core.logging import get_logger, get_request_id

log = get_logger(__name__)

RESET_EMAIL_TASK_NAME = "notifications.send_password_reset_email"
_PENDING_KEY = "auth.pending_password_reset_email"
_HOOK_KEY = "auth.password_reset_email_hook_installed"


def _publish_password_reset_tasks(payloads: list[dict[str, Any]]) -> int:
    """Publish committed reset messages without leaking task arguments."""
    from src.workers.celery_app import celery_app

    published = 0
    for payload in payloads:
        try:
            celery_app.send_task(
                RESET_EMAIL_TASK_NAME,
                kwargs=payload,
                expires=3600,
            )
            published += 1
        except Exception as exc:
            # This is after the database commit, so raising cannot make the
            # operation atomic again. The durable encrypted outbox is picked up
            # by the periodic recovery sweep once the broker returns.
            log.error(
                "auth.password_reset.email_enqueue_failed",
                company_id=payload.get("company_id"),
                reset_token_id=payload.get("reset_token_id"),
                error_type=type(exc).__name__,
            )
    return published


def _flush_password_reset_tasks(sync_session: SyncSession) -> None:
    payloads = sync_session.info.pop(_PENDING_KEY, None)
    if not payloads:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _publish_password_reset_tasks(payloads)
        return
    loop.run_in_executor(None, _publish_password_reset_tasks, payloads)


def _drop_password_reset_tasks(sync_session: SyncSession) -> None:
    sync_session.info.pop(_PENDING_KEY, None)


def queue_password_reset_email(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    reset_token_id: uuid.UUID,
) -> None:
    """Stage one reset link for publication after the current commit."""
    sync_session = session.sync_session
    pending: list[dict[str, Any]] = sync_session.info.setdefault(_PENDING_KEY, [])
    pending.append(
        {
            "company_id": str(company_id),
            "reset_token_id": str(reset_token_id),
            "request_id": get_request_id(),
        }
    )

    if not sync_session.info.get(_HOOK_KEY):
        sync_session.info[_HOOK_KEY] = True
        event.listen(sync_session, "after_commit", _flush_password_reset_tasks)
        event.listen(sync_session, "after_rollback", _drop_password_reset_tasks)


__all__ = ["RESET_EMAIL_TASK_NAME", "queue_password_reset_email"]
