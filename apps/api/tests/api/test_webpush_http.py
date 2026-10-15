"""Real PostgreSQL subscription ownership and channel selection."""

from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from src.core.clock import utcnow
from src.core.config import settings
from src.core.deps import get_current_principal
from src.core.security import decrypt_push_subscription
from src.core.webpush import b64url
from src.main import app
from src.modules.auth.repository import UserSessionRepository
from src.modules.notifications import service as notification_service
from src.modules.notifications.models import NotificationType, PushSubscription
from src.modules.notifications.schemas import PreferencesUpdateRequest
from src.modules.notifications.service import NotificationService, notify, queue_push_delivery
from tests.api.vendor_portal_support import approved_vendor, sign_in, vendor_principal

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
def push_enabled(monkeypatch):
    monkeypatch.setattr(settings, "web_push_enabled", True)
    monkeypatch.setattr(notification_service, "_send_push_tasks", lambda rows: len(rows))


def _payload() -> dict:
    key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": f"https://fcm.googleapis.com/send/{uuid.uuid4()}",
        "keys": {"p256dh": b64url(key), "auth": b64url(b"0123456789abcdef")},
        "device_label": "Plant PC",
    }


async def test_subscribe_list_foreign_delete_takeover(
    api_client, session, tenant, principal, push_enabled
):
    from src.core.deps import Principal
    from tests.integration.test_notifications_service import _make_user

    payload = _payload()
    response = await api_client.post("/notifications/push/subscriptions", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "endpoint" not in data and "p256dh" not in data and "auth" not in data
    device_id = data["id"]
    stored = await session.get(PushSubscription, uuid.UUID(device_id))
    assert stored.endpoint_ciphertext != payload["endpoint"]
    assert decrypt_push_subscription(stored.endpoint_ciphertext) == payload["endpoint"]
    listed = await api_client.get("/notifications/push/subscriptions")
    assert listed.json()["data"][0]["id"] == device_id
    second = await _make_user(session, tenant.company_id)
    next_principal = Principal(
        user_id=second.id,
        company_id=tenant.company_id,
        email=second.email,
        full_name=second.full_name,
        permissions=principal.permissions,
        role_codes=principal.role_codes,
        is_superuser=False,
    )
    app.dependency_overrides[get_current_principal] = lambda: next_principal
    forbidden = await api_client.delete(f"/notifications/push/subscriptions/{device_id}")
    assert forbidden.status_code == 404
    takeover = await api_client.post("/notifications/push/subscriptions", json=payload)
    assert takeover.status_code == 200, takeover.text
    assert takeover.json()["data"]["id"] == device_id
    await session.refresh(stored)
    assert stored.user_id == second.id
    removed = await api_client.post(
        "/notifications/push/subscriptions/unregister", json={"endpoint": payload["endpoint"]}
    )
    assert removed.status_code == 200 and removed.json()["data"]["removed"]


@pytest.mark.parametrize(
    "invalid", ["http://fcm.googleapis.com/x", "https://evil.example/x", "https://127.0.0.1/x"]
)
async def test_disallowed_endpoint_is_422(api_client, invalid, push_enabled):
    payload = _payload()
    payload["endpoint"] = invalid
    response = await api_client.post("/notifications/push/subscriptions", json=payload)
    assert response.status_code == 422


async def test_vendor_can_register(api_client, session, tenant, principal, push_enabled):
    vendor = await approved_vendor(session, tenant, "Push vendor")
    sign_in(vendor_principal(principal, vendor.id))
    response = await api_client.post("/notifications/push/subscriptions", json=_payload())
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("expiration", [0, 1, 10**40])
async def test_expired_or_out_of_range_device_expiration_is_refused(
    api_client, push_enabled, expiration
):
    payload = {**_payload(), "expiration_time": expiration}
    response = await api_client.post("/notifications/push/subscriptions", json=payload)
    assert response.status_code == 422, response.text


async def test_device_preferences_collapse_and_logout_cleanup(
    api_client, session, tenant, push_enabled
):
    response = await api_client.post("/notifications/push/subscriptions", json=_payload())
    assert response.status_code == 200, response.text

    async def raise_notification():
        return (
            await notify(
                session,
                company_id=tenant.company_id,
                user_ids=[tenant.user_id],
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                title="Hello",
                body="World",
                group_key="push-collapse",
            )
        )[0]

    first = await raise_notification()
    assert first.channels == ["IN_APP", "PUSH"]
    first.push_delivered_at = utcnow()
    await session.flush()
    repeated = await raise_notification()
    assert repeated.id == first.id and repeated.push_delivered_at is not None
    assert queue_push_delivery(session, [repeated]) == 0
    service = NotificationService(session, tenant)
    await service.update_preferences(
        PreferencesUpdateRequest(
            preferences=[
                {
                    "notification_type": "SYSTEM_ANNOUNCEMENT",
                    "in_app_enabled": True,
                    "email_enabled": False,
                    "push_enabled": False,
                }
            ]
        )
    )
    await service.update_preferences(
        PreferencesUpdateRequest(
            preferences=[
                {
                    "notification_type": "SYSTEM_ANNOUNCEMENT",
                    "in_app_enabled": True,
                    "email_enabled": False,
                }
            ]
        )
    )
    preferences = await service.get_preferences()
    assert (
        next(p for p in preferences if p.notification_type == "SYSTEM_ANNOUNCEMENT").push_enabled
        is False
    )
    muted = await notify(
        session,
        company_id=tenant.company_id,
        user_ids=[tenant.user_id],
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        title="New",
        body="Muted",
    )
    assert muted[0].channels == ["IN_APP"]
    await UserSessionRepository(session, tenant).revoke_all_for_user(
        tenant.user_id, reason="logout_all"
    )
    assert not list(
        (
            await session.execute(
                select(PushSubscription).where(PushSubscription.user_id == tenant.user_id)
            )
        ).scalars()
    )
