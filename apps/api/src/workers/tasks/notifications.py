"""Scheduled alerting and notification delivery (master rules sections 23, 35).

Two tasks live here.

``notifications.scan_dispatch_delays`` is the hourly beat job behind master rules
section 23: a consignment past its promised delivery date is flagged and the
people who can do something about it are told, rather than the delay waiting for
somebody to notice. All of the logic is in
:func:`src.modules.dispatch.service.scan_delays`; this module is the Celery
adapter and nothing else.

**Re-delivery has to be harmless.** ``task_acks_late`` is on, so a worker killed
mid-sweep has its task delivered again. That is survivable because the sweep is
idempotent by construction: it re-derives the overdue set from the clock rather
than from a queue, and the notification's ``group_key`` collapses a repeat onto
the same unread bell row instead of adding another. A second delivery therefore
tells the same people the same thing once, which is the correct outcome.

**One tenant's failure must not stop the others.** The sweep runs each company's
alert in its own nested transaction and logs and skips a failure, so a single
company with, say, no staffed dispatch role cannot silence every other tenant's
alerts.

``notifications.send_email`` is the delivery worker the notifications module
hands rows to by name. **It is deliberately a logging stub:** this deployment has
no SMTP configuration - there is no mail host, sender identity or credential in
``src.core.config`` - so there is nothing to send through. It records what would
have been sent, at INFO, and returns a result saying so. Wiring it up is a matter
of adding the SMTP settings and replacing the body of :func:`send_email`; the
task name, its arguments and the queue it lands on are already the contract the
rest of the system uses, so nothing else has to change when that happens.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.core.database import session_scope
from src.core.logging import get_logger
from src.modules.dispatch import service as dispatch_service
from src.workers.celery_app import async_task

log = get_logger("worker.notifications")


@async_task(name="notifications.scan_dispatch_delays", queue="notifications")
async def scan_dispatch_delays(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Flag every overdue consignment and alert the company that owns it.

    Runs across all tenants: beat has no principal, so the sweep reads
    cross-tenant and partitions by company before writing anything (see
    ``dispatch.service.scan_delays``).

    ``request_id`` is not read here but is picked up by the ``task_prerun``
    signal, so a sweep triggered manually from the API keeps one correlation id
    across both logs (section 55).
    """
    async with session_scope() as session:
        results = await dispatch_service.scan_delays(session)

    delayed = sum(result.delayed_count for result in results)
    notified = sum(result.notified_count for result in results)
    log.info(
        "dispatch.delay_scan_completed",
        companies=len(results),
        delayed=delayed,
        notified=notified,
    )
    return {
        "companies": len(results),
        "delayed": delayed,
        "notified": notified,
        "by_company": [
            {
                "company_id": str(result.company_id),
                "delayed": result.delayed_count,
                "notified": result.notified_count,
            }
            for result in results
        ],
    }


@async_task(name="notifications.send_email", queue="notifications")
async def send_email(
    notification_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Deliver one notification by e-mail.

    STUB, and deliberately so: no SMTP host, sender or credential is configured
    for this deployment, so there is no transport to hand the message to. Sending
    is therefore logged rather than attempted.

    It does not raise. ``notifications.service`` enqueues these *after* the
    business transaction commits precisely so a delivery problem cannot roll back
    a goods receipt, and a task that failed here would be retried forever against
    a mail server that does not exist. The in-app notification is already
    committed and is the system of record.

    The signature is the contract ``notifications.service.queue_email_delivery``
    already publishes against - the notification is identified by id and re-read
    from the database by the worker, so a rolled-back notification produces no
    e-mail even though the enqueue happened.
    """
    log.info(
        "notifications.email_not_sent",
        reason="no SMTP transport is configured for this deployment",
        notification_id=notification_id,
        company_id=company_id,
        user_id=user_id,
    )
    return {
        "notification_id": str(uuid.UUID(notification_id)),
        "delivered": False,
        "detail": "E-mail delivery is not configured; the in-app notification is authoritative.",
    }


__all__ = ["scan_dispatch_delays", "send_email"]
