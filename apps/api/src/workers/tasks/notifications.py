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

``notifications.send_email`` and ``notifications.send_password_reset_email``
perform SMTP delivery. Both re-read their database row under a lock before
sending, then stamp a channel-specific delivery timestamp. That makes ordinary
late-acknowledgement re-delivery harmless while preserving at-least-once retry
semantics for a worker crash during the SMTP conversation.
"""

from __future__ import annotations

import html
import smtplib
import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import and_, select, update

from src.core.clock import utcnow
from src.core.config import EmailProvider, settings
from src.core.database import affected_rows, session_scope
from src.core.logging import get_logger
from src.core.mail import OutboundEmail, deliver_email
from src.core.security import (
    CredentialDecryptionError,
    constant_time_compare,
    decrypt_password_reset_delivery_token,
    hash_secret_token,
)
from src.modules.dispatch import service as dispatch_service
from src.modules.notifications.models import Notification, NotificationChannel
from src.modules.users.models import PasswordResetToken, User
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


def _message_id(kind: str, record_id: uuid.UUID) -> str:
    """A stable provider-visible id for duplicate suppression and tracing."""
    sender = str(settings.email_from_address or "no-reply@localhost")
    domain = sender.rsplit("@", 1)[-1]
    return f"<{kind}.{record_id}@{domain}>"


def _site_url(path: str) -> str:
    base = str(settings.public_web_url).rstrip("/")
    clean_path = path if path.startswith("/") and not path.startswith("//") else "/notifications"
    return f"{base}{clean_path}"


def _notification_mail(notification: Notification, account: User) -> OutboundEmail:
    action_url = _site_url(notification.action_url or "/notifications")
    title = notification.title
    body = notification.body
    return OutboundEmail(
        recipient=account.email,
        subject=f"[{settings.app_name}] {title}",
        text_body=f"{body}\n\nOpen in {settings.app_name}: {action_url}\n",
        html_body=(
            f"<p>{html.escape(body)}</p>"
            f'<p><a href="{html.escape(action_url, quote=True)}">Open in '
            f"{html.escape(settings.app_name)}</a></p>"
        ),
        message_id=_message_id("notification", notification.id),
    )


def _password_reset_mail(account: User, record: PasswordResetToken, token: str) -> OutboundEmail:
    # The token travels in the URL *fragment*, which the browser never sends
    # to the server. As a query parameter it was written, for the hour it was
    # live, into the request line of every nginx access log on the path - a
    # credential that resets the account, disables MFA and lifts lockout -
    # while the service refused to log it, the row stored only its digest and
    # the page scrubbed it from history. The reset page reads the fragment.
    reset_url = _site_url("/reset-password") + "#" + urlencode({"token": token})
    expiry = record.expires_at.isoformat()
    safe_url = html.escape(reset_url, quote=True)
    return OutboundEmail(
        recipient=account.email,
        subject=f"Reset your {settings.app_name} password",
        text_body=(
            f"A password reset was requested for your {settings.app_name} account.\n\n"
            f"Reset your password: {reset_url}\n\n"
            f"This link expires at {expiry}. If you did not request it, you can ignore this email."
        ),
        html_body=(
            f"<p>A password reset was requested for your "
            f"{html.escape(settings.app_name)} account.</p>"
            f'<p><a href="{safe_url}">Reset your password</a></p>'
            f"<p>This link expires at {html.escape(expiry)}. If you did not request it, "
            "you can ignore this email.</p>"
        ),
        message_id=_message_id("password-reset", record.id),
    )


async def _deliver_notification_email(
    notification_id: str,
    company_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Load, claim, send and - on failure - release one ordinary notification.

    Same shape as :func:`_deliver_password_reset_email`, for the same reason:
    the SMTP conversation runs with no transaction open.
    """
    notification_uuid = uuid.UUID(notification_id)
    company_uuid = uuid.UUID(company_id)
    user_uuid = uuid.UUID(user_id)

    if settings.email_provider is EmailProvider.disabled:
        return {"notification_id": notification_id, "delivered": False, "detail": "disabled"}

    async with session_scope() as session:
        stmt = (
            select(Notification, User)
            .join(
                User,
                and_(
                    User.id == Notification.user_id,
                    User.company_id == Notification.company_id,
                ),
            )
            .where(
                Notification.id == notification_uuid,
                Notification.company_id == company_uuid,
                Notification.user_id == user_uuid,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .with_for_update(of=Notification)
        )
        found = (await session.execute(stmt)).one_or_none()
        if found is None:
            return {"notification_id": notification_id, "delivered": False, "detail": "missing"}

        notification, account = found
        if NotificationChannel.EMAIL.value not in (notification.channels or ()):
            return {
                "notification_id": notification_id,
                "delivered": False,
                "detail": "email_not_requested",
            }
        if notification.email_delivered_at is not None:
            return {
                "notification_id": notification_id,
                "delivered": True,
                "already_delivered": True,
            }

        mail = _notification_mail(notification, account)
        claimed_at = utcnow()
        notification.email_delivered_at = claimed_at
        stamped_in_app = notification.delivered_at is None
        if stamped_in_app:
            notification.delivered_at = claimed_at

    try:
        await deliver_email(mail)
    except Exception:
        release: dict[str, Any] = {"email_delivered_at": None}
        if stamped_in_app:
            release["delivered_at"] = None
        async with session_scope() as session:
            await session.execute(
                update(Notification)
                .where(
                    Notification.id == notification_uuid,
                    Notification.company_id == company_uuid,
                    Notification.email_delivered_at == claimed_at,
                )
                .values(**release)
                .execution_options(synchronize_session=False)
            )
        raise

    log.info(
        "notifications.email_delivered",
        notification_id=notification_id,
        company_id=company_id,
        user_id=user_id,
    )
    return {"notification_id": notification_id, "delivered": True}


async def _deliver_password_reset_email(
    company_id: str,
    reset_token_id: str,
) -> dict[str, Any]:
    """Deliver a still-live reset grant, with no token in logs or results.

    Two short transactions around the SMTP conversation, never one long one.
    The first locks the row, validates it and *claims* it - stamps
    ``email_sent_at`` and drops the delivery ciphertext - then commits. The
    mail is sent with no transaction open. Sending inside the locked
    transaction held an idle asyncpg connection for the whole SMTP exchange
    (up to five socket phases of ``SMTP_TIMEOUT_SECONDS`` each), which the
    60 s ``idle_in_transaction_session_timeout`` terminates: a relay that was
    slow but accepted the message left the mail delivered and the stamp lost,
    and the recovery sweep re-sent the live reset link every minute until the
    token expired.

    If delivery raises, a second transaction releases the claim - restores
    the ciphertext and clears the stamp - so the retry and the sweep can pick
    the row up again. A worker killed between the claim and the send leaves
    one mail unsent, which the user cures by requesting another link; the
    failure the other way round, a live credential mailed once a minute, is
    the one that must not happen.
    """
    company_uuid = uuid.UUID(company_id)
    reset_uuid = uuid.UUID(reset_token_id)

    if settings.email_provider is EmailProvider.disabled:
        return {"reset_token_id": reset_token_id, "delivered": False, "detail": "disabled"}

    async with session_scope() as session:
        stmt = (
            select(PasswordResetToken, User)
            .join(
                User,
                and_(
                    User.id == PasswordResetToken.user_id,
                    User.company_id == PasswordResetToken.company_id,
                ),
            )
            .where(
                PasswordResetToken.company_id == company_uuid,
                PasswordResetToken.id == reset_uuid,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .with_for_update(of=PasswordResetToken)
        )
        found = (await session.execute(stmt)).one_or_none()
        if found is None:
            return {"reset_token_id": reset_token_id, "delivered": False, "detail": "missing"}

        record, account = found
        if record.email_sent_at is not None:
            return {
                "reset_token_id": reset_token_id,
                "delivered": True,
                "already_delivered": True,
            }
        if record.used_at is not None or record.expires_at <= utcnow():
            record.email_token_ciphertext = None
            return {"reset_token_id": reset_token_id, "delivered": False, "detail": "inactive"}

        if not record.email_token_ciphertext:
            return {
                "reset_token_id": reset_token_id,
                "delivered": False,
                "detail": "delivery_credential_missing",
            }
        try:
            token = decrypt_password_reset_delivery_token(record.email_token_ciphertext)
        except CredentialDecryptionError:
            log.error(
                "auth.password_reset.delivery_credential_unreadable",
                company_id=company_id,
                reset_token_id=reset_token_id,
            )
            return {
                "reset_token_id": reset_token_id,
                "delivered": False,
                "detail": "delivery_credential_unreadable",
            }
        digest = hash_secret_token(token)
        if not constant_time_compare(record.token_hash, digest):
            record.email_token_ciphertext = None
            log.error(
                "auth.password_reset.delivery_credential_mismatch",
                company_id=company_id,
                reset_token_id=reset_token_id,
            )
            return {"reset_token_id": reset_token_id, "delivered": False, "detail": "invalid"}

        mail = _password_reset_mail(account, record, token)
        ciphertext = record.email_token_ciphertext
        claimed_at = utcnow()
        record.email_sent_at = claimed_at
        record.email_token_ciphertext = None

    try:
        await deliver_email(mail)
    except Exception:
        async with session_scope() as session:
            await session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == reset_uuid,
                    PasswordResetToken.company_id == company_uuid,
                    PasswordResetToken.email_sent_at == claimed_at,
                )
                .values(email_sent_at=None, email_token_ciphertext=ciphertext)
                .execution_options(synchronize_session=False)
            )
        raise

    log.info(
        "auth.password_reset.email_delivered",
        company_id=company_id,
        reset_token_id=reset_token_id,
    )
    return {"reset_token_id": reset_token_id, "delivered": True}


async def _dispatch_pending_password_resets(*, limit: int = 100) -> dict[str, int]:
    """Recover committed reset e-mails whose immediate broker publish was lost."""
    async with session_scope() as session:
        expired = await session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.email_token_ciphertext.is_not(None),
                PasswordResetToken.expires_at <= utcnow(),
            )
            .values(email_token_ciphertext=None)
            .execution_options(synchronize_session=False)
        )
        purged = affected_rows(expired)
        stmt = (
            select(PasswordResetToken.id, PasswordResetToken.company_id)
            .where(
                PasswordResetToken.email_sent_at.is_(None),
                PasswordResetToken.email_token_ciphertext.is_not(None),
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > utcnow(),
            )
            .order_by(PasswordResetToken.created_at)
            .limit(limit)
        )
        pending = list((await session.execute(stmt)).all())

    delivered = 0
    for reset_id, company_id in pending:
        result = await _deliver_password_reset_email(str(company_id), str(reset_id))
        delivered += int(result.get("delivered") is True)
    return {"scanned": len(pending), "delivered": delivered, "expired_credentials_purged": purged}


_MAIL_RETRY_OPTIONS = {
    "autoretry_for": (OSError, smtplib.SMTPException),
    "retry_backoff": True,
    "retry_jitter": True,
    "retry_kwargs": {"max_retries": 5},
}


@async_task(
    name="notifications.send_email",
    queue="notifications",
    **_MAIL_RETRY_OPTIONS,
)
async def send_email(
    notification_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return await _deliver_notification_email(notification_id, company_id, user_id)


@async_task(
    name="notifications.send_password_reset_email",
    queue="notifications",
    **_MAIL_RETRY_OPTIONS,
)
async def send_password_reset_email(
    company_id: str,
    reset_token_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return await _deliver_password_reset_email(company_id, reset_token_id)


@async_task(
    name="notifications.dispatch_pending_password_resets",
    queue="notifications",
)
async def dispatch_pending_password_resets(
    request_id: str | None = None,
    **_: Any,
) -> dict[str, int]:
    return await _dispatch_pending_password_resets()


__all__ = [
    "dispatch_pending_password_resets",
    "scan_dispatch_delays",
    "send_email",
    "send_password_reset_email",
]
