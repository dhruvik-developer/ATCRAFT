"""Phone consent, channel resolution and provider delivery against PostgreSQL."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.deps import Principal
from src.core.errors import BusinessRuleError
from src.modules.audit.models import AuditLog
from src.modules.auth.phone_service import (
    PHONE_CODE_ATTEMPT_LIMIT,
    PhoneProfileService,
)
from src.modules.auth.schemas import PhoneUpdateRequest
from src.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationChannelTemplate,
    NotificationDelivery,
    NotificationPreference,
    NotificationSeverity,
    NotificationType,
)
from src.modules.notifications.service import notify
from src.modules.users.models import User, UserPhoneVerificationChallenge
from src.workers.tasks import notifications as worker

pytestmark = pytest.mark.integration


def _principal(account: User) -> Principal:
    return Principal(
        user_id=account.id,
        company_id=account.company_id,
        email=account.email,
        full_name=account.full_name,
        is_superuser=False,
        permissions=frozenset(),
        session_id=None,
    )


def _challenge(account: User, *, expires_at=None) -> UserPhoneVerificationChallenge:
    moment = utcnow()
    return UserPhoneVerificationChallenge(
        company_id=account.company_id,
        user_id=account.id,
        phone_e164=account.phone_e164 or "+919876543210",
        code_digest="0" * 64,
        expires_at=expires_at or moment + timedelta(minutes=10),
        last_sent_at=moment,
        attempt_count=0,
        send_count=1,
        send_window_started_at=moment,
        consumed_at=None,
    )


def _messaging_template(
    company_id: uuid.UUID,
    channel: NotificationChannel,
    notification_type: NotificationType,
) -> NotificationChannelTemplate:
    return NotificationChannelTemplate(
        company_id=company_id,
        channel=channel.value,
        notification_type=notification_type.value,
        template_name="announcement" if channel is NotificationChannel.WHATSAPP else None,
        template_id="dlt-announcement" if channel is NotificationChannel.SMS else None,
        language="en",
        parameters=["title"],
        sms_text="Alert: {#var#}" if channel is NotificationChannel.SMS else None,
        is_active=True,
    )


async def test_phone_change_revokes_verification_and_consent(
    session: AsyncSession, user: User
) -> None:
    moment = utcnow()
    user.phone_e164 = "+919876543210"
    user.phone_verified_at = moment
    user.whatsapp_opt_in_at = moment
    user.sms_opt_in_at = moment
    challenge = _challenge(user)
    session.add(challenge)
    await session.flush()

    await PhoneProfileService(session, _principal(user)).update_phone(
        PhoneUpdateRequest(phone="+14155552671")
    )

    state = (
        await session.execute(
            select(
                User.phone_e164,
                User.phone_verified_at,
                User.whatsapp_opt_in_at,
                User.sms_opt_in_at,
                UserPhoneVerificationChallenge.consumed_at,
            )
            .join(
                UserPhoneVerificationChallenge,
                UserPhoneVerificationChallenge.user_id == User.id,
            )
            .where(User.id == user.id)
        )
    ).one()
    assert state.phone_e164 == "+14155552671"
    assert state.phone_verified_at is None
    assert state.whatsapp_opt_in_at is None
    assert state.sms_opt_in_at is None
    assert state.consumed_at is not None
    assert (
        await session.execute(
            select(AuditLog.id).where(
                AuditLog.company_id == user.company_id,
                AuditLog.entity_id == user.id,
                AuditLog.summary == "Updated messaging phone number",
            )
        )
    ).scalar_one_or_none()


async def test_phone_verification_sends_and_consumes_the_code(
    session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.messaging import MessagingOutcome, MessagingResult
    from src.modules.auth import phone_service

    user.phone_e164 = "+919876543210"
    sent: list[tuple[str, str, list[str]]] = []

    class FakeSmsProvider:
        async def send_template(self, phone: str, template: str, values: list[str]):
            sent.append((phone, template, values))
            return MessagingResult(MessagingOutcome.DELIVERED)

    monkeypatch.setattr(phone_service, "sms_provider", FakeSmsProvider)
    monkeypatch.setattr(settings, "sms_auth_key", SecretStr("test-auth-key"))
    monkeypatch.setattr(settings, "sms_sender_id", "ATCRFT")
    monkeypatch.setattr(settings, "sms_dlt_entity_id", "dlt-entity")
    monkeypatch.setattr(settings, "sms_verification_template_id", "dlt-otp")
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr("test-hmac-key"))

    service = PhoneProfileService(session, _principal(user))
    result = await service.request_verification(ip_address=None)

    assert result.expires_in_seconds == 600
    assert len(sent) == 1
    assert sent[0][0:2] == ("+919876543210", "dlt-otp")
    code = sent[0][2][0]
    assert len(code) == 6 and code.isdigit()
    await service.verify_phone(code)

    state = (
        await session.execute(
            select(User.phone_verified_at, UserPhoneVerificationChallenge.consumed_at)
            .join(
                UserPhoneVerificationChallenge,
                UserPhoneVerificationChallenge.user_id == User.id,
            )
            .where(User.id == user.id)
        )
    ).one()
    assert state.phone_verified_at is not None
    assert state.consumed_at is not None


async def test_phone_verification_consumes_after_the_attempt_limit(
    session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.messaging import MessagingOutcome, MessagingResult
    from src.modules.auth import phone_service

    user.phone_e164 = "+919876543210"
    sent_code: list[str] = []

    class FakeSmsProvider:
        async def send_template(self, _phone: str, _template: str, values: list[str]):
            sent_code.extend(values)
            return MessagingResult(MessagingOutcome.DELIVERED)

    monkeypatch.setattr(phone_service, "sms_provider", FakeSmsProvider)
    monkeypatch.setattr(settings, "sms_auth_key", SecretStr("test-auth-key"))
    monkeypatch.setattr(settings, "sms_sender_id", "ATCRFT")
    monkeypatch.setattr(settings, "sms_dlt_entity_id", "dlt-entity")
    monkeypatch.setattr(settings, "sms_verification_template_id", "dlt-otp")
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr("test-hmac-key"))

    service = PhoneProfileService(session, _principal(user))
    await service.request_verification(ip_address=None)
    wrong_code = f"{(int(sent_code[0]) + 1) % 1_000_000:06d}"
    for _ in range(PHONE_CODE_ATTEMPT_LIMIT):
        with pytest.raises(BusinessRuleError):
            await service.verify_phone(wrong_code)

    challenge = (
        await session.execute(
            select(UserPhoneVerificationChallenge).where(
                UserPhoneVerificationChallenge.user_id == user.id
            )
        )
    ).scalar_one()
    assert challenge.attempt_count == PHONE_CODE_ATTEMPT_LIMIT
    assert challenge.consumed_at is not None

    with pytest.raises(BusinessRuleError):
        await service.verify_phone(sent_code[0])


async def test_sms_notification_requires_enabled_preference_consent_and_template(
    session: AsyncSession, company, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = utcnow()
    user.phone_e164 = "+919876543210"
    user.phone_verified_at = moment
    user.sms_opt_in_at = moment
    notification_type = NotificationType.SYSTEM_ANNOUNCEMENT
    session.add_all(
        [
            NotificationPreference(
                company_id=company.id,
                user_id=user.id,
                notification_type=notification_type,
                sms_enabled=True,
            ),
            _messaging_template(company.id, NotificationChannel.SMS, notification_type),
        ]
    )
    await session.flush()
    monkeypatch.setattr(settings, "sms_enabled", True)

    rows = await notify(
        session,
        company_id=company.id,
        user_ids=[user.id],
        notification_type=notification_type,
        title="System update",
        body="A new system update is available.",
        severity=NotificationSeverity.INFO,
        template_data={"title": "System update"},
    )

    assert len(rows) == 1
    assert rows[0].channels == ["IN_APP", "SMS"]
    assert rows[0].template_data == {
        "title": "System update",
        "body": "A new system update is available.",
        "link": "",
    }


@pytest.mark.parametrize("outcome_name", ["DELIVERED", "FAILED", "RETRY"])
async def test_sms_worker_claims_sends_and_records_delivery(
    session: AsyncSession,
    company,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
    outcome_name: str,
) -> None:
    from src.core.messaging import MessagingOutcome, MessagingResult

    outcome = MessagingOutcome(outcome_name)
    moment = utcnow()
    user.phone_e164 = "+919876543210"
    user.phone_verified_at = moment
    user.sms_opt_in_at = moment
    notification = Notification(
        company_id=company.id,
        user_id=user.id,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        title="System update",
        body="A new update is available.",
        severity="INFO",
        channels=["SMS"],
        template_data={"title": "System update"},
        occurrence_count=1,
    )
    session.add_all(
        [
            notification,
            NotificationPreference(
                company_id=company.id,
                user_id=user.id,
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                sms_enabled=True,
            ),
            _messaging_template(
                company.id,
                NotificationChannel.SMS,
                NotificationType.SYSTEM_ANNOUNCEMENT,
            ),
        ]
    )
    await session.flush()
    monkeypatch.setattr(settings, "sms_enabled", True)
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    state: dict[str, Any] = {"session_open": False, "calls": 0}

    @asynccontextmanager
    async def test_session_scope():
        async with AsyncSession(
            bind=session.bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as scoped:
            state["session_open"] = True
            try:
                yield scoped
                await scoped.commit()
            except BaseException:
                await scoped.rollback()
                raise
            finally:
                state["session_open"] = False

    class FakeSmsProvider:
        async def send_template(self, phone: str, template_id: str, values: list[str]):
            assert not state["session_open"]
            state["calls"] += 1
            assert (phone, template_id, values) == (
                "+919876543210",
                "dlt-announcement",
                ["System update"],
            )
            return MessagingResult(
                outcome,
                provider_message_id="provider-message-1"
                if outcome is MessagingOutcome.DELIVERED
                else None,
                error_code="transport_error"
                if outcome is MessagingOutcome.RETRY
                else "provider_rejected"
                if outcome is MessagingOutcome.FAILED
                else None,
            )

    monkeypatch.setattr(worker, "session_scope", test_session_scope)
    monkeypatch.setattr(worker, "sms_provider", FakeSmsProvider)
    args = str(notification.id), str(company.id), str(user.id)

    if outcome is MessagingOutcome.RETRY:
        with pytest.raises(worker.MessagingRetryableError):
            await worker._deliver_notification_messaging(*args, channel=NotificationChannel.SMS)
    else:
        result = await worker._deliver_notification_messaging(
            *args, channel=NotificationChannel.SMS
        )
        assert result["delivered"] is (outcome is MessagingOutcome.DELIVERED)
    assert state["calls"] == 1
    await session.refresh(notification)
    assert (notification.sms_delivered_at is None) is (outcome is MessagingOutcome.RETRY)
    assert (
        await session.execute(
            select(NotificationDelivery.provider_message_id, NotificationDelivery.outcome).where(
                NotificationDelivery.notification_id == notification.id
            )
        )
    ).one() == (
        "provider-message-1" if outcome is MessagingOutcome.DELIVERED else None,
        outcome.value,
    )
    if outcome is MessagingOutcome.RETRY:
        pending = (await session.execute(worker.pending_deliveries_statement(utcnow()))).all()
        assert {row.id for row in pending} == {notification.id}
