"""Real PostgreSQL claim/release and recovery; the provider transport is always fake."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import EmailProvider, settings
from src.core.security import encrypt_push_subscription
from src.core.webpush import PushOutcome, PushRetryableError
from src.modules.notifications.models import Notification, NotificationType, PushSubscription
from src.modules.users.models import UserSession
from src.workers.tasks import notifications as worker

pytestmark = pytest.mark.integration


def pending(tenant, user_id, *, channels=None, created_at=None):
    return Notification(
        company_id=tenant.company_id,
        user_id=user_id,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        title="Recovery test",
        body="A recoverable notification",
        severity="INFO",
        channels=channels or ["PUSH"],
        occurrence_count=1,
        created_at=created_at or utcnow(),
    )


def device(tenant, user_id, *, expires_at=None):
    return PushSubscription(
        company_id=tenant.company_id,
        user_id=user_id,
        endpoint_hash=uuid.uuid4().hex.ljust(64, "0"),
        endpoint_ciphertext=encrypt_push_subscription("https://fcm.googleapis.com/send/test"),
        p256dh="public-key-unused-by-fake-provider",
        auth="auth-unused-by-fake-provider",
        expires_at=expires_at,
        failure_count=0,
    )


def login_session(tenant, user_id, *, moment=None):
    stamp = moment or utcnow()
    return UserSession(
        company_id=tenant.company_id,
        user_id=user_id,
        session_id=uuid.uuid4().hex,
        refresh_token_hash=uuid.uuid4().hex,
        refresh_jti=uuid.uuid4().hex,
        created_at=stamp,
        last_used_at=stamp,
        expires_at=stamp + timedelta(days=settings.jwt_refresh_token_expire_days),
    )


@pytest.mark.parametrize(
    "outcomes",
    [
        (PushOutcome.DELIVERED,),
        (PushOutcome.GONE,),
        (PushOutcome.RETRY,),
        (PushOutcome.DELIVERED, PushOutcome.RETRY),
    ],
)
async def test_worker_claims_releases_and_updates_real_rows(
    session, tenant, user, monkeypatch, outcomes
):
    monkeypatch.setattr(settings, "web_push_enabled", True)
    notification = pending(tenant, user.id)
    devices = [device(tenant, user.id) for _ in outcomes]
    session.add_all([notification, login_session(tenant, user.id), *devices])
    await session.flush()
    device_outcomes = {row.id: outcome for row, outcome in zip(devices, outcomes, strict=True)}
    state = {"open": False, "scopes": 0}

    @asynccontextmanager
    async def scope():
        async with AsyncSession(
            bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as scoped:
            state["open"] = True
            state["scopes"] += 1
            try:
                yield scoped
                await scoped.commit()
            except BaseException:
                await scoped.rollback()
                raise
            finally:
                state["open"] = False

    async def deliver(snapshot, payload, **kwargs):
        assert not state["open"]
        assert not snapshot._sa_instance_state.session
        await session.refresh(notification)
        assert notification.push_delivered_at is not None
        return device_outcomes[snapshot.id]

    monkeypatch.setattr(worker, "session_scope", scope)
    monkeypatch.setattr(worker, "deliver_push", deliver)
    args = str(notification.id), str(tenant.company_id), str(user.id)
    if all(outcome is PushOutcome.RETRY for outcome in outcomes):
        with pytest.raises(PushRetryableError):
            await worker._deliver_notification_push(*args)
    else:
        result = await worker._deliver_notification_push(*args)
        assert result["delivered"] == (PushOutcome.DELIVERED in outcomes)
    assert state["scopes"] == 2
    await session.refresh(notification)
    assert (notification.push_delivered_at is None) == all(
        outcome is PushOutcome.RETRY for outcome in outcomes
    )
    stored = {
        row.id: row
        for row in (
            await session.execute(
                select(PushSubscription)
                .where(PushSubscription.user_id == user.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    for row, outcome in zip(devices, outcomes, strict=True):
        if outcome is PushOutcome.GONE:
            assert row.id not in stored
        elif outcome is PushOutcome.DELIVERED:
            assert stored[row.id].last_success_at is not None and stored[row.id].failure_count == 0
        else:
            assert stored[row.id].failure_count == 1
    if PushOutcome.DELIVERED in outcomes:
        monkeypatch.setattr(
            worker, "deliver_push", lambda *_args, **_kwargs: pytest.fail("Re-sent")
        )
        result = await worker._deliver_notification_push(*args)
        assert result["already_delivered"] is True


async def test_recovery_filters_abandoned_users_before_the_batch_limit(
    session, tenant, user, monkeypatch
):
    from tests.integration.test_notifications_service import _make_user

    monkeypatch.setattr(settings, "web_push_enabled", True)
    monkeypatch.setattr(settings, "email_provider", EmailProvider.smtp)
    inactive = await _make_user(session, tenant.company_id, is_active=False)
    old_login = await _make_user(session, tenant.company_id)
    expired_device = await _make_user(session, tenant.company_id)
    no_device = await _make_user(session, tenant.company_id)
    moment = utcnow()
    abandoned = [
        pending(tenant, inactive.id, created_at=moment - timedelta(hours=1)) for _ in range(100)
    ]
    eligible = pending(tenant, user.id)
    email = pending(tenant, no_device.id, channels=["EMAIL"])
    session.add_all(
        [
            *abandoned,
            pending(tenant, old_login.id),
            pending(tenant, expired_device.id),
            pending(tenant, no_device.id),
            pending(tenant, user.id, created_at=moment - timedelta(days=2)),
            eligible,
            email,
            device(tenant, user.id),
            device(tenant, old_login.id),
            device(tenant, expired_device.id, expires_at=moment - timedelta(seconds=1)),
            login_session(tenant, user.id),
            login_session(
                tenant,
                old_login.id,
                moment=moment - timedelta(days=settings.jwt_refresh_token_expire_days + 1),
            ),
            login_session(tenant, expired_device.id),
            login_session(tenant, no_device.id),
        ]
    )
    await session.flush()
    rows = (await session.execute(worker.pending_deliveries_statement(utcnow()))).all()
    assert {row.id for row in rows} == {eligible.id, email.id}

    monkeypatch.setattr(settings, "web_push_enabled", False)
    rows = (await session.execute(worker.pending_deliveries_statement(utcnow()))).all()
    assert {row.id for row in rows} == {email.id}
    monkeypatch.setattr(settings, "email_provider", EmailProvider.disabled)
    assert not (await session.execute(worker.pending_deliveries_statement(utcnow()))).all()
