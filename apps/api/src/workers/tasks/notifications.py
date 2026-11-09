"""Scheduled alerting and notification delivery (master rules sections 23, 35).

Delivery workers and scheduled recovery tasks live here.

``notifications.scan_smart_alerts`` reconciles timed alert episodes using
bounded scanners in the modules that own the relevant business data.

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
perform SMTP delivery. Push uses the same claim-before-transport pattern. Each
worker commits a channel-specific delivery marker before contacting the provider
and releases it on a retryable failure. Ordinary late-acknowledgement re-delivery
is harmless; a process killed after claiming retains the documented at-most-once
crash window. Recovery re-queues eligible, unclaimed rows after a broker outage.
"""

from __future__ import annotations

import html
import smtplib
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import and_, any_, delete, func, literal, or_, select, update

from src.core.clock import today, utcnow
from src.core.config import EmailProvider, settings
from src.core.database import affected_rows, session_scope, transaction, try_advisory_xact_lock
from src.core.deps import system_tenant
from src.core.logging import get_logger
from src.core.mail import OutboundEmail, deliver_email
from src.core.messaging import (
    MessagingOutcome,
    MessagingResult,
    WhatsAppCloudProvider,
    assert_allowed_url,
    sms_provider,
)
from src.core.security import (
    CredentialDecryptionError,
    constant_time_compare,
    decrypt_password_reset_delivery_token,
    hash_secret_token,
)
from src.core.webpush import PushOutcome, PushRetryableError, deliver_push, push_payload, push_topic
from src.modules.collections import service as collections_service
from src.modules.dispatch import service as dispatch_service
from src.modules.finance import service as finance_service
from src.modules.inventory import service as inventory_service
from src.modules.job_work import service as job_work_service
from src.modules.notifications.alert_service import effective_alert_settings, reconcile_alert
from src.modules.notifications.alerts import ALERT_RULES, AlertRuleKey
from src.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationChannelTemplate,
    NotificationDelivery,
    NotificationDeliveryOutcome,
    NotificationPreference,
    PushSubscription,
)
from src.modules.organization.models import Company
from src.modules.procurement import inbound_service
from src.modules.production import service as production_service
from src.modules.quality import service as quality_service
from src.modules.sales import service as sales_service
from src.modules.users.models import PasswordResetToken, User, UserSession
from src.workers.celery_app import async_task

log = get_logger("worker.notifications")

SMART_ALERT_SCANNERS = {
    AlertRuleKey.PO_NOT_ACKNOWLEDGED: inbound_service.po_not_acknowledged_alert_candidates,
    AlertRuleKey.PO_DELIVERY_MISSED: inbound_service.po_delivery_missed_alert_candidates,
    AlertRuleKey.PO_DISPATCH_NOT_UPDATED: inbound_service.po_dispatch_not_updated_alert_candidates,
    AlertRuleKey.PO_TRACKING_MISSING: inbound_service.po_tracking_missing_alert_candidates,
    AlertRuleKey.GRN_NOT_CREATED: inbound_service.grn_not_created_alert_candidates,
    AlertRuleKey.QC_PENDING: quality_service.pending_alert_candidates,
    AlertRuleKey.VENDOR_INVOICE_PENDING: finance_service.pending_vendor_invoice_alert_candidates,
    AlertRuleKey.VENDOR_PAYMENT_OVERDUE: finance_service.overdue_payment_alert_candidates,
    AlertRuleKey.JOB_WORK_OVERDUE: job_work_service.overdue_alert_candidates,
    AlertRuleKey.DISPATCH_DELAYED: dispatch_service.delayed_alert_candidates,
    AlertRuleKey.PRODUCTION_ORDER_DELAYED: production_service.delayed_alert_candidates,
    AlertRuleKey.SALES_ORDER_AT_RISK: sales_service.at_risk_alert_candidates,
    AlertRuleKey.STOCK_BELOW_REORDER: inventory_service.below_reorder_alert_candidates,
}


def recent_push_session(moment: datetime) -> Any:
    """A recipient who has used a session within the configured refresh window."""
    return (
        select(UserSession.id)
        .where(
            UserSession.company_id == Notification.company_id,
            UserSession.user_id == Notification.user_id,
            func.coalesce(UserSession.last_used_at, UserSession.created_at)
            >= moment - timedelta(days=settings.jwt_refresh_token_expire_days),
        )
        .correlate(Notification)
        .exists()
    )


def push_claim_statement(
    notification_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID
) -> Any:
    return (
        select(Notification)
        .join(
            User, and_(User.id == Notification.user_id, User.company_id == Notification.company_id)
        )
        .where(
            Notification.id == notification_id,
            Notification.company_id == company_id,
            Notification.user_id == user_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            recent_push_session(utcnow()),
        )
        .with_for_update(of=Notification)
    )


async def _deliver_notification_push(
    notification_id: str, company_id: str, user_id: str
) -> dict[str, Any]:
    nid, cid, uid = uuid.UUID(notification_id), uuid.UUID(company_id), uuid.UUID(user_id)
    if not settings.web_push_enabled:
        return {"delivered": False, "detail": "disabled"}
    async with session_scope() as session:
        notification = (
            await session.execute(push_claim_statement(nid, cid, uid))
        ).scalar_one_or_none()
        if notification is None:
            return {"delivered": False, "detail": "ineligible"}
        if NotificationChannel.PUSH.value not in notification.channels:
            return {"delivered": False, "detail": "push_not_requested"}
        if notification.push_delivered_at is not None:
            return {"delivered": True, "already_delivered": True}
        devices = list(
            (
                await session.execute(
                    select(PushSubscription)
                    .where(
                        PushSubscription.company_id == cid,
                        PushSubscription.user_id == uid,
                        or_(
                            PushSubscription.expires_at.is_(None),
                            PushSubscription.expires_at > utcnow(),
                        ),
                    )
                    .order_by(PushSubscription.last_seen_at.desc().nullslast(), PushSubscription.id)
                    .limit(10)
                )
            ).scalars()
        )
        if not devices:
            return {"delivered": False, "detail": "no_devices"}
        payload, topic = push_payload(notification), push_topic(notification)
        urgency = {"CRITICAL": "high", "WARNING": "normal"}.get(notification.severity, "low")
        claimed_at = utcnow()
        notification.push_delivered_at = claimed_at
        first_delivery = notification.delivered_at is None
        if first_delivery:
            notification.delivered_at = claimed_at
        # Detached snapshots retain keys without requiring a live transaction during transport.
        for device in devices:
            session.expunge(device)
    outcomes = []
    for device in devices:
        try:
            outcome = await deliver_push(
                device, payload, ttl=settings.web_push_ttl_seconds, urgency=urgency, topic=topic
            )
        except OSError:
            outcome = PushOutcome.RETRY
        except (ValueError, CredentialDecryptionError):
            outcome = PushOutcome.FAILED
        outcomes.append((device, outcome))
    all_retry = all(outcome is PushOutcome.RETRY for _, outcome in outcomes)
    async with session_scope() as session:
        for device, outcome in outcomes:
            owned = (
                PushSubscription.id == device.id,
                PushSubscription.company_id == cid,
                PushSubscription.user_id == uid,
                PushSubscription.updated_at == device.updated_at,
            )
            if outcome is PushOutcome.GONE:
                await session.execute(delete(PushSubscription).where(*owned))
            else:
                values = (
                    {"last_success_at": utcnow(), "failure_count": 0}
                    if outcome is PushOutcome.DELIVERED
                    else {
                        "last_failure_at": utcnow(),
                        "failure_count": PushSubscription.failure_count + 1,
                    }
                )
                await session.execute(update(PushSubscription).where(*owned).values(**values))
        if all_retry:
            values = {"push_delivered_at": None}
            if first_delivery:
                # Do not clear a concurrent e-mail delivery's stamp.
                await session.execute(
                    update(Notification)
                    .where(
                        Notification.id == nid,
                        Notification.company_id == cid,
                        Notification.push_delivered_at == claimed_at,
                        Notification.delivered_at == claimed_at,
                        Notification.email_delivered_at.is_(None),
                    )
                    .values(delivered_at=None)
                )
            await session.execute(
                update(Notification)
                .where(
                    Notification.id == nid,
                    Notification.company_id == cid,
                    Notification.push_delivered_at == claimed_at,
                )
                .values(**values)
            )
    if all_retry:
        raise PushRetryableError("Push delivery temporarily unavailable")
    return {
        "delivered": any(outcome is PushOutcome.DELIVERED for _, outcome in outcomes),
        "devices": len(outcomes),
    }


@async_task(
    name="notifications.send_push",
    queue="notifications",
    ignore_result=True,
    autoretry_for=(PushRetryableError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
async def send_push(
    notification_id: str, company_id: str, user_id: str, request_id: str | None = None, **_: Any
) -> dict[str, Any]:
    return await _deliver_notification_push(notification_id, company_id, user_id)


class MessagingRetryableError(Exception):
    """A provider asked for a retry without exposing its response body."""


async def _deliver_notification_messaging(
    notification_id: str,
    company_id: str,
    user_id: str,
    *,
    channel: NotificationChannel,
) -> dict[str, Any]:
    nid, cid, uid = uuid.UUID(notification_id), uuid.UUID(company_id), uuid.UUID(user_id)
    if channel is NotificationChannel.WHATSAPP:
        enabled = settings.whatsapp_enabled
        marker_name = "whatsapp_delivered_at"
        consent_name = "whatsapp_opt_in_at"
        preference_name = "whatsapp_enabled"
    else:
        enabled = settings.sms_enabled
        marker_name = "sms_delivered_at"
        consent_name = "sms_opt_in_at"
        preference_name = "sms_enabled"
    if not enabled:
        return {"notification_id": notification_id, "delivered": False, "detail": "disabled"}

    moment = utcnow()
    async with session_scope() as session:
        stmt = (
            select(Notification, User, NotificationPreference, NotificationChannelTemplate)
            .join(
                User,
                and_(
                    User.id == Notification.user_id,
                    User.company_id == Notification.company_id,
                ),
            )
            .outerjoin(
                NotificationPreference,
                and_(
                    NotificationPreference.company_id == Notification.company_id,
                    NotificationPreference.user_id == Notification.user_id,
                    NotificationPreference.notification_type == Notification.notification_type,
                ),
            )
            .outerjoin(
                NotificationChannelTemplate,
                and_(
                    NotificationChannelTemplate.company_id == Notification.company_id,
                    NotificationChannelTemplate.notification_type == Notification.notification_type,
                    NotificationChannelTemplate.channel == channel.value,
                    NotificationChannelTemplate.is_active.is_(True),
                ),
            )
            .where(
                Notification.id == nid,
                Notification.company_id == cid,
                Notification.user_id == uid,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .with_for_update(of=Notification)
        )
        found = (await session.execute(stmt)).one_or_none()
        if found is None:
            return {"notification_id": notification_id, "delivered": False, "detail": "missing"}

        notification, account, preference, template = found
        if channel.value not in (notification.channels or ()):
            return {
                "notification_id": notification_id,
                "delivered": False,
                "detail": "channel_not_requested",
            }
        if getattr(notification, marker_name) is not None:
            return {
                "notification_id": notification_id,
                "delivered": True,
                "already_delivered": True,
            }

        opted_in_at = getattr(account, consent_name)
        preference_enabled = (
            getattr(preference, preference_name, False) if preference is not None else False
        )
        failure_code: str | None = None
        if account.phone_e164 is None or account.phone_verified_at is None or opted_in_at is None:
            failure_code = "consent_unavailable"
        elif not preference_enabled:
            failure_code = "preference_disabled"
        elif template is None:
            failure_code = "template_unavailable"
        elif notification.severity == "CRITICAL":
            failure_code = "critical_channel_blocked"

        if failure_code is not None:
            setattr(notification, marker_name, moment)
            session.add(
                NotificationDelivery(
                    company_id=cid,
                    notification_id=nid,
                    channel=channel.value,
                    outcome=NotificationDeliveryOutcome.FAILED,
                    attempted_at=moment,
                    error_code=failure_code,
                )
            )
            return {
                "notification_id": notification_id,
                "delivered": False,
                "detail": failure_code,
            }

        template_values = notification.template_data or {}
        if any(name not in template_values for name in template.parameters):
            failure_code = "template_data_missing"
            setattr(notification, marker_name, moment)
            session.add(
                NotificationDelivery(
                    company_id=cid,
                    notification_id=nid,
                    channel=channel.value,
                    outcome=NotificationDeliveryOutcome.FAILED,
                    attempted_at=moment,
                    error_code=failure_code,
                )
            )
            return {
                "notification_id": notification_id,
                "delivered": False,
                "detail": failure_code,
            }

        values = [template_values[name] for name in template.parameters]
        setattr(notification, marker_name, moment)
        phone = account.phone_e164
        template_name = template.template_name
        template_id = template.template_id
        language = template.language

    try:
        if channel is NotificationChannel.WHATSAPP:
            if (
                not settings.whatsapp_phone_number_id
                or not settings.whatsapp_api_version
                or template_name is None
            ):
                result = MessagingResult(
                    MessagingOutcome.FAILED, error_code="invalid_provider_config"
                )
            else:
                url = (
                    f"{settings.whatsapp_api_base.rstrip('/')}/"
                    f"{settings.whatsapp_api_version}/"
                    f"{settings.whatsapp_phone_number_id}/messages"
                )
                assert_allowed_url(url)
                result = await WhatsAppCloudProvider().send_template(
                    phone, template_name, language, values
                )
        elif template_id is None:
            result = MessagingResult(MessagingOutcome.FAILED, error_code="invalid_template")
        else:
            assert_allowed_url(settings.sms_api_url)
            result = await sms_provider().send_template(phone, template_id, values)
    except ValueError:
        result = MessagingResult(MessagingOutcome.FAILED, error_code="invalid_provider_config")

    async with session_scope() as session:
        values_to_write: dict[str, datetime | None] = {}
        if result.outcome is MessagingOutcome.RETRY:
            values_to_write[marker_name] = None
        elif result.outcome is MessagingOutcome.DELIVERED:
            values_to_write["delivered_at"] = utcnow()
        await session.execute(
            update(Notification)
            .where(
                Notification.id == nid,
                Notification.company_id == cid,
                Notification.user_id == uid,
                getattr(Notification, marker_name) == moment,
            )
            .values(**values_to_write)
            .execution_options(synchronize_session=False)
        )
        session.add(
            NotificationDelivery(
                company_id=cid,
                notification_id=nid,
                channel=channel.value,
                outcome=NotificationDeliveryOutcome(result.outcome.value),
                provider_message_id=result.provider_message_id,
                attempted_at=utcnow(),
                error_code=result.error_code,
            )
        )

    if result.outcome is MessagingOutcome.RETRY:
        raise MessagingRetryableError("Messaging provider temporarily unavailable")
    log.info(
        "notifications.messaging_delivery",
        notification_id=notification_id,
        company_id=company_id,
        user_id=user_id,
        channel=channel.value,
        outcome=result.outcome.value,
    )
    return {
        "notification_id": notification_id,
        "delivered": result.outcome is MessagingOutcome.DELIVERED,
    }


@async_task(
    name="notifications.send_whatsapp",
    queue="notifications",
    ignore_result=True,
    autoretry_for=(MessagingRetryableError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
async def send_whatsapp(
    notification_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return await _deliver_notification_messaging(
        notification_id,
        company_id,
        user_id,
        channel=NotificationChannel.WHATSAPP,
    )


@async_task(
    name="notifications.send_sms",
    queue="notifications",
    ignore_result=True,
    autoretry_for=(MessagingRetryableError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
async def send_sms(
    notification_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return await _deliver_notification_messaging(
        notification_id,
        company_id,
        user_id,
        channel=NotificationChannel.SMS,
    )


def pending_deliveries_statement(moment: datetime) -> Any:
    """Recover deliverable rows before limiting, so abandoned devices cannot starve the queue."""
    has_device = (
        select(PushSubscription.id)
        .where(
            PushSubscription.company_id == Notification.company_id,
            PushSubscription.user_id == Notification.user_id,
            or_(PushSubscription.expires_at.is_(None), PushSubscription.expires_at > moment),
        )
        .correlate(Notification)
        .exists()
    )
    whatsapp_template = (
        select(NotificationChannelTemplate.id)
        .where(
            NotificationChannelTemplate.company_id == Notification.company_id,
            NotificationChannelTemplate.notification_type == Notification.notification_type,
            NotificationChannelTemplate.channel == NotificationChannel.WHATSAPP.value,
            NotificationChannelTemplate.is_active.is_(True),
        )
        .correlate(Notification)
        .exists()
    )
    sms_template = (
        select(NotificationChannelTemplate.id)
        .where(
            NotificationChannelTemplate.company_id == Notification.company_id,
            NotificationChannelTemplate.notification_type == Notification.notification_type,
            NotificationChannelTemplate.channel == NotificationChannel.SMS.value,
            NotificationChannelTemplate.is_active.is_(True),
        )
        .correlate(Notification)
        .exists()
    )
    return (
        select(
            Notification.id,
            Notification.company_id,
            Notification.user_id,
            Notification.channels,
            Notification.email_delivered_at,
            Notification.push_delivered_at,
            Notification.whatsapp_delivered_at,
            Notification.sms_delivered_at,
        )
        .join(
            User,
            and_(
                User.id == Notification.user_id,
                User.company_id == Notification.company_id,
            ),
        )
        .where(
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            Notification.created_at >= moment - timedelta(hours=24),
            or_(
                and_(
                    Notification.email_delivered_at.is_(None),
                    literal(NotificationChannel.EMAIL.value) == any_(Notification.channels),
                    literal(settings.email_provider is not EmailProvider.disabled),
                ),
                and_(
                    Notification.push_delivered_at.is_(None),
                    literal(NotificationChannel.PUSH.value) == any_(Notification.channels),
                    literal(settings.web_push_enabled),
                    recent_push_session(moment),
                    has_device,
                ),
                and_(
                    Notification.whatsapp_delivered_at.is_(None),
                    literal(NotificationChannel.WHATSAPP.value) == any_(Notification.channels),
                    literal(settings.whatsapp_enabled),
                    User.phone_e164.is_not(None),
                    User.phone_verified_at.is_not(None),
                    User.whatsapp_opt_in_at.is_not(None),
                    whatsapp_template,
                ),
                and_(
                    Notification.sms_delivered_at.is_(None),
                    literal(NotificationChannel.SMS.value) == any_(Notification.channels),
                    literal(settings.sms_enabled),
                    User.phone_e164.is_not(None),
                    User.phone_verified_at.is_not(None),
                    User.sms_opt_in_at.is_not(None),
                    sms_template,
                ),
            ),
        )
        .order_by(Notification.created_at, Notification.id)
        .limit(100)
    )


@async_task(
    name="notifications.dispatch_pending_deliveries", queue="notifications", ignore_result=True
)
async def dispatch_pending_deliveries(request_id: str | None = None, **_: Any) -> dict[str, int]:
    from src.workers.celery_app import celery_app

    async with session_scope() as session:
        pending = (await session.execute(pending_deliveries_statement(utcnow()))).all()
    queued = 0
    for row in pending:
        for channel, marker, task in (
            ("EMAIL", row.email_delivered_at, "notifications.send_email"),
            ("PUSH", row.push_delivered_at, "notifications.send_push"),
            (
                "WHATSAPP",
                row.whatsapp_delivered_at,
                "notifications.send_whatsapp",
            ),
            ("SMS", row.sms_delivered_at, "notifications.send_sms"),
        ):
            enabled = (
                settings.web_push_enabled
                if channel == "PUSH"
                else settings.whatsapp_enabled
                if channel == "WHATSAPP"
                else settings.sms_enabled
                if channel == "SMS"
                else settings.email_provider is not EmailProvider.disabled
            )
            if enabled and channel in row.channels and marker is None:
                try:
                    celery_app.send_task(
                        task,
                        kwargs={
                            "notification_id": str(row.id),
                            "company_id": str(row.company_id),
                            "user_id": str(row.user_id),
                            "request_id": request_id,
                        },
                    )
                    queued += 1
                except Exception as exc:
                    log.error(
                        "notifications.recovery_enqueue_failed",
                        notification_id=str(row.id),
                        error_type=type(exc).__name__,
                    )
    return {"scanned": len(pending), "queued": queued}


@async_task(name="notifications.scan_smart_alerts", queue="notifications")
async def scan_smart_alerts(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Reconcile configured smart-alert episodes, isolated by tenant and rule."""
    if not settings.alerts_enabled:
        return {"enabled": False, "companies": 0, "notified": 0, "failed": 0}

    async with session_scope() as session:
        company_ids = list(
            (
                await session.execute(
                    select(Company.id)
                    .where(Company.is_active.is_(True), Company.deleted_at.is_(None))
                    .order_by(Company.id)
                )
            )
            .scalars()
            .all()
        )

    moment = utcnow()
    notified = failed = processed = skipped = 0
    for company_id in company_ids:
        try:
            async with session_scope() as session:
                tenant = system_tenant(company_id)
                async with transaction(session):
                    if not await try_advisory_xact_lock(session, company_id, "smart-alerts"):
                        skipped += 1
                        continue

                    configured = await effective_alert_settings(session, tenant)
                    for rule_key, rule in ALERT_RULES.items():
                        enabled, threshold_minutes, _remind_minutes = configured[rule_key]
                        if not enabled:
                            continue
                        scanner = SMART_ALERT_SCANNERS.get(rule_key)
                        if scanner is None:
                            failed += 1
                            log.error(
                                "notifications.smart_alert_scanner_missing",
                                company_id=str(company_id),
                                rule=rule_key.value,
                            )
                            continue
                        try:
                            async with transaction(session):
                                candidates = await scanner(
                                    session,
                                    tenant,
                                    as_of=moment,
                                    threshold=timedelta(minutes=threshold_minutes),
                                    limit=500,
                                )
                                result = await reconcile_alert(
                                    session, tenant, rule, candidates, as_of=moment
                                )
                                if rule_key is AlertRuleKey.DISPATCH_DELAYED:
                                    await dispatch_service.mark_delay_notified(
                                        session,
                                        company_id=company_id,
                                        dispatch_ids=result["notified_ids"],
                                        at=moment,
                                    )
                            notified += result["notified"]
                            processed += 1
                        except Exception as exc:
                            failed += 1
                            log.error(
                                "notifications.smart_alert_rule_failed",
                                company_id=str(company_id),
                                rule=rule_key.value,
                                error_type=type(exc).__name__,
                                exc_info=exc,
                            )
        except Exception as exc:
            failed += 1
            log.error(
                "notifications.smart_alert_company_failed",
                company_id=str(company_id),
                error_type=type(exc).__name__,
                exc_info=exc,
            )

    result = {
        "enabled": True,
        "companies": len(company_ids),
        "rules_processed": processed,
        "notified": notified,
        "failed": failed,
        "skipped": skipped,
    }
    log.info("notifications.smart_alert_scan_completed", **result)
    return result


@async_task(name="notifications.scan_overdue_receivables", queue="notifications")
async def scan_overdue_receivables(request_id: str | None = None, **_: Any) -> dict[str, int]:
    """Open and reconcile payment follow-ups once per company."""
    async with session_scope() as session:
        company_ids = list(
            (
                await session.execute(
                    select(Company.id)
                    .where(Company.is_active.is_(True), Company.deleted_at.is_(None))
                    .order_by(Company.id)
                )
            )
            .scalars()
            .all()
        )

    totals = {
        "companies": len(company_ids),
        "opened": 0,
        "refreshed": 0,
        "broken_promises": 0,
        "resolved": 0,
        "reminded": 0,
        "failed": 0,
        "skipped": 0,
    }
    for company_id in company_ids:
        try:
            async with session_scope() as session:
                tenant = system_tenant(company_id)
                async with transaction(session):
                    if not await try_advisory_xact_lock(session, company_id, "collections-overdue"):
                        totals["skipped"] += 1
                        continue
                    result = await collections_service.scan_overdue_receivables(
                        session, tenant, as_of=today()
                    )
                    totals["opened"] += result.opened
                    totals["refreshed"] += result.refreshed
                    totals["broken_promises"] += result.broken_promises
                    totals["resolved"] += result.resolved
                    totals["reminded"] += result.reminded
        except Exception as exc:
            totals["failed"] += 1
            log.error(
                "notifications.overdue_receivables_company_failed",
                company_id=str(company_id),
                request_id=request_id,
                error_type=type(exc).__name__,
                exc_info=exc,
            )

    log.info("notifications.overdue_receivables_scan_completed", **totals)
    return totals


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


#: The first digit of a permanent SMTP reply (RFC 5321 section 4.2.1). A relay
#: that answers 5yz to a recipient will answer it the same way next time.
SMTP_PERMANENT_REPLY_CLASS = 5


def permanently_refused(exc: BaseException) -> bool:
    """Whether the relay refused every recipient with a permanent (5xx) reply.

    ASSUMPTION (docs/assumptions.md section MAIL-1): such a message is never
    retried. ``SMTPRecipientsRefused`` was retried like a dropped connection,
    and every release cleared the delivery marker the recovery sweep looks
    for, so one undeliverable address was re-sent every two minutes for a day -
    thousands of SMTP sessions that invite the relay to throttle everything
    else this company sends, password resets included. A 4xx refusal - a
    greylisting server, a busy mailbox, a relay shedding load - is temporary
    and stays retryable, as is every other transport failure.
    """
    if not isinstance(exc, smtplib.SMTPRecipientsRefused):
        return False
    codes = _refusal_codes(exc)
    return bool(codes) and all(code // 100 == SMTP_PERMANENT_REPLY_CLASS for code in codes)


def _refusal_codes(exc: BaseException) -> list[int]:
    """The relay's reply codes, which - unlike its reply text - name nobody."""
    if not isinstance(exc, smtplib.SMTPRecipientsRefused):
        return []
    return sorted({code for code, _reply in exc.recipients.values()})


async def _deliver_notification_email(
    notification_id: str,
    company_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Load, claim, send and - on failure - release one ordinary notification.

    Same shape as :func:`_deliver_password_reset_email`, for the same reason:
    the SMTP conversation runs with no transaction open. A permanent refusal
    (:func:`permanently_refused`) is not a failure to retry: the claim on the
    e-mail marker stays, and the task reports the message undelivered.
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
    except Exception as exc:
        # A permanent refusal keeps the e-mail marker - the attempt is over, and
        # the retry and the recovery sweep both key on it being NULL - and gives
        # back only the "reached the recipient" stamp, which it never earned.
        refused = permanently_refused(exc)
        release: dict[str, Any] = {} if refused else {"email_delivered_at": None}
        if stamped_in_app:
            release["delivered_at"] = None
        if release:
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
        if not refused:
            raise
        log.warning(
            "notifications.email_refused",
            notification_id=notification_id,
            company_id=company_id,
            user_id=user_id,
            smtp_codes=_refusal_codes(exc),
        )
        return {"notification_id": notification_id, "delivered": False, "detail": "refused"}

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
    the one that must not happen. A permanent refusal
    (:func:`permanently_refused`) clears the stamp but not the purge, for the
    same cure: the address will be refused again, so nothing re-sends it.
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
    except Exception as exc:
        # A permanent refusal un-stamps the send but leaves the delivery copy
        # purged, so neither the retry nor the sweep mails this link again; the
        # person asks for a new one. Anything else puts the claim back.
        refused = permanently_refused(exc)
        async with session_scope() as session:
            await session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == reset_uuid,
                    PasswordResetToken.company_id == company_uuid,
                    PasswordResetToken.email_sent_at == claimed_at,
                )
                .values(email_sent_at=None, email_token_ciphertext=None if refused else ciphertext)
                .execution_options(synchronize_session=False)
            )
        if not refused:
            raise
        log.warning(
            "auth.password_reset.email_refused",
            company_id=company_id,
            reset_token_id=reset_token_id,
            smtp_codes=_refusal_codes(exc),
        )
        return {"reset_token_id": reset_token_id, "delivered": False, "detail": "refused"}

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
    "ignore_result": True,
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
    ignore_result=True,
)
async def dispatch_pending_password_resets(
    request_id: str | None = None,
    **_: Any,
) -> dict[str, int]:
    return await _dispatch_pending_password_resets()


__all__ = [
    "dispatch_pending_deliveries",
    "dispatch_pending_password_resets",
    "scan_smart_alerts",
    "send_email",
    "send_password_reset_email",
    "send_sms",
    "send_whatsapp",
]
