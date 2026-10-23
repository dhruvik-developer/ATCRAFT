"""RFC interoperability and delivery boundaries; never contact a push provider."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from src.core.clock import utcnow
from src.core.config import settings
from src.core.webpush import (
    PushOutcome,
    assert_allowed_endpoint,
    decode_b64url,
    encrypt_aes128gcm,
    outcome_for_status,
    push_payload,
    push_topic,
    vapid_authorization,
    vapid_keypair,
)
from src.modules.notifications import service
from src.modules.notifications.models import Notification, NotificationSeverity
from src.workers.celery_app import _failure_is_confidential, celery_app
from src.workers.tasks.notifications import push_claim_statement

pytestmark = pytest.mark.unit


def test_rfc8291_appendix_a_exact_ciphertext() -> None:
    # Primary source: https://www.rfc-editor.org/rfc/rfc8291#appendix-A
    private = ec.derive_private_key(
        int.from_bytes(decode_b64url("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    actual = encrypt_aes128gcm(
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        b"When I grow up, I want to be a watermelon",
        salt=decode_b64url("DGv6ra1nlYgDCS1FRnbzlw"),
        ephemeral_private=private,
    )
    header = decode_b64url(
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    ciphertext = decode_b64url(
        "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    )
    assert actual == header + ciphertext


def test_vapid_signed_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_push_subject", "mailto:admin@example.com")
    header = vapid_authorization("https://fcm.googleapis.com/push/test")
    token = header.split("t=", 1)[1].split(",", 1)[0]
    claims = jwt.decode(
        token,
        vapid_keypair().public_key(),
        algorithms=["ES256"],
        audience="https://fcm.googleapis.com",
    )
    assert claims["sub"] == "mailto:admin@example.com"
    assert 0 < claims["exp"] - utcnow().timestamp() <= 12 * 3600


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://a.notify.windows.com/x",
    ],
)
def test_endpoint_allowlist(endpoint: str) -> None:
    assert_allowed_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://evil.example/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com/x#fragment",
    ],
)
def test_endpoint_rejects_ssrf(endpoint: str) -> None:
    with pytest.raises(ValueError):
        assert_allowed_endpoint(endpoint)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, PushOutcome.DELIVERED),
        (202, PushOutcome.DELIVERED),
        (404, PushOutcome.GONE),
        (410, PushOutcome.GONE),
        (413, PushOutcome.TOO_LARGE),
        (429, PushOutcome.RETRY),
        (503, PushOutcome.RETRY),
        (401, PushOutcome.FAILED),
    ],
)
def test_outcome_mapping(status: int, outcome: PushOutcome) -> None:
    assert outcome_for_status(status) is outcome


@pytest.mark.parametrize("severity", list(NotificationSeverity))
@pytest.mark.parametrize("push", [False, True])
@pytest.mark.parametrize("device", [False, True])
def test_push_channel_matrix(severity: NotificationSeverity, push: bool, device: bool) -> None:
    channels = service.resolve_channels(
        severity, in_app_enabled=True, email_enabled=True, push_enabled=push, has_push_device=device
    )
    assert (
        channels == ("IN_APP", "EMAIL", "PUSH")
        if push and device
        else channels == ("IN_APP", "EMAIL")
    )


def test_payload_unicode_byte_budget() -> None:
    notification = Notification(
        id=uuid.uuid4(), title="Test", body="🙂" * 10000, severity="INFO", occurrence_count=1
    )
    payload = push_payload(notification)
    assert len(payload) <= 3072
    assert json.loads(payload)["url"] == "/notifications"


@pytest.mark.parametrize("symbol", ["🙂", "\x00", "\\"], ids=["unicode", "control", "escape"])
@pytest.mark.parametrize("body", ["", "body 🙂" * 1000], ids=["empty", "long"])
def test_payload_metadata_cannot_hang_worker(symbol: str, body: str) -> None:
    # A timeout makes the former metadata-only infinite loop a bounded failure;
    # a test thread would leave the spinning function alive in the pytest worker.
    source = """
import json
import sys
import uuid
from types import SimpleNamespace
from src.core.webpush import MAX_PUSH_PAYLOAD_BYTES, push_payload, push_topic

symbol, body = json.loads(sys.argv[1])
notification = SimpleNamespace(
    id=uuid.uuid4(), title=symbol * 200, body=body,
    action_url='/' + symbol * 511, group_key=symbol * 120,
    severity='INFO', occurrence_count=1,
)
payload = push_payload(notification)
decoded = json.loads(payload)
assert len(payload) <= MAX_PUSH_PAYLOAD_BYTES
assert decoded['id'] == str(notification.id)
assert decoded['url'] in (notification.action_url, '/notifications')
assert decoded['tag'] == push_topic(notification)
small = SimpleNamespace(**{**vars(notification), 'title': 'Small', 'body': 'Small',
                         'action_url': '/notifications'})
assert json.loads(push_payload(small))['tag'] == decoded['tag']
assert notification.title.startswith(decoded['title'])
assert body.startswith(decoded['body'])
long_headline = SimpleNamespace(**{**vars(notification), 'title': symbol * 4000})
headline_payload = push_payload(long_headline)
headline = json.loads(headline_payload)
assert len(headline_payload) <= MAX_PUSH_PAYLOAD_BYTES
assert 0 < len(headline['title']) < len(long_headline.title)
assert long_headline.title.startswith(headline['title'])
assert headline['tag'] == decoded['tag']
print(len(payload))
"""
    result = subprocess.run(
        [sys.executable, "-c", source, json.dumps([symbol, body])],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert int(result.stdout) <= 3072


def test_payload_preserves_content_that_already_fits() -> None:
    notification = SimpleNamespace(
        id=uuid.uuid4(),
        title="Order ready",
        body="Your order is ready for dispatch.",
        action_url="/sales-orders/123",
        group_key="order:123",
        severity="INFO",
        occurrence_count=2,
    )
    assert json.loads(push_payload(notification)) == {
        "id": str(notification.id),
        "title": notification.title,
        "body": notification.body,
        "url": notification.action_url,
        "tag": push_topic(notification),
        "severity": "INFO",
        "count": 2,
    }


def test_payload_rejects_unbounded_non_text_metadata() -> None:
    source = """
import uuid
from types import SimpleNamespace
from src.core.webpush import push_payload

notification = SimpleNamespace(
    id=uuid.uuid4(), title='Test', body='', action_url=None, group_key=None,
    severity='!' * 4000, occurrence_count=1,
)
try:
    push_payload(notification)
except ValueError as error:
    assert 'metadata exceeds the payload limit' in str(error)
else:
    raise AssertionError('Unbounded metadata must be refused.')
"""
    subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_staging_waits_for_outer_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    published = []
    monkeypatch.setattr(service, "_send_push_tasks", lambda rows: published.extend(rows))
    sync = Session()
    fake = SimpleNamespace(sync_session=sync)
    with sync.begin():
        with sync.begin_nested():
            service.queue_push_delivery(
                fake,
                [
                    Notification(
                        id=uuid.uuid4(),
                        company_id=uuid.uuid4(),
                        user_id=uuid.uuid4(),
                        channels=["PUSH"],
                    )
                ],
            )
        assert published == []
    assert len(published) == 1
    with sync.begin():
        service.queue_push_delivery(
            fake,
            [
                Notification(
                    id=uuid.uuid4(),
                    company_id=uuid.uuid4(),
                    user_id=uuid.uuid4(),
                    channels=["PUSH"],
                )
            ],
        )
        sync.rollback()
    assert len(published) == 1


def test_claim_sql_locks_notification_and_filters_active_recent_user() -> None:
    sql = str(
        push_claim_statement(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FOR UPDATE OF notifications" in sql
    assert "users.is_active IS true" in sql and "user_sessions" in sql


def test_every_beat_task_is_registered() -> None:
    celery_app.loader.import_default_modules()
    assert {entry["task"] for entry in celery_app.conf.beat_schedule.values()} <= set(
        celery_app.tasks
    )


def test_push_failure_logging_is_confidential() -> None:
    assert _failure_is_confidential("notifications.send_push", OSError("secret endpoint"))
