"""E-mail hand-off: what is queued, when, and what happens when it fails.

No database and no broker. The behaviour under test is the ordering contract that
makes "notifications are written in the caller's transaction, but e-mail is not
sent from it" safe:

* nothing is published while the transaction is still open;
* a rollback publishes nothing at all;
* a broker failure is swallowed, because the in-app notification has already
  committed and is the system of record.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession

from src.modules.notifications import service as notifications_service
from src.modules.notifications.models import Notification, NotificationChannel

pytestmark = pytest.mark.unit

PENDING_KEY = "notifications.pending_email"


def _notification(*channels: str) -> Notification:
    """An unattached row - the queue helper only reads its columns."""
    return Notification(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        channels=list(channels),
    )


@pytest.fixture
def fake_async_session() -> SimpleNamespace:
    """Stands in for an AsyncSession: the helper only needs ``sync_session``.

    A real (unbound) ORM Session is used underneath because the helper attaches
    genuine SQLAlchemy event listeners to it, and a mock would prove nothing
    about whether those fire.
    """
    return SimpleNamespace(sync_session=SyncSession())


class TestQueueEmailDelivery:
    def test_only_rows_with_the_email_channel_are_staged(
        self, fake_async_session: SimpleNamespace
    ) -> None:
        in_app_only = _notification(NotificationChannel.IN_APP.value)
        both = _notification(NotificationChannel.IN_APP.value, NotificationChannel.EMAIL.value)

        staged = notifications_service.queue_email_delivery(fake_async_session, [in_app_only, both])

        pending = fake_async_session.sync_session.info[PENDING_KEY]
        assert staged == 1
        assert [p["notification_id"] for p in pending] == [str(both.id)]

    def test_nothing_is_published_before_the_commit(
        self, fake_async_session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[Any] = []
        monkeypatch.setattr(
            notifications_service, "_send_email_tasks", lambda payloads: published.append(payloads)
        )

        notifications_service.queue_email_delivery(
            fake_async_session, [_notification(NotificationChannel.EMAIL.value)]
        )

        assert published == []

    def test_commit_hook_publishes_and_clears_the_queue(
        self, fake_async_session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[list[dict[str, Any]]] = []
        monkeypatch.setattr(
            notifications_service, "_send_email_tasks", lambda payloads: published.append(payloads)
        )
        notification = _notification(NotificationChannel.EMAIL.value)
        notifications_service.queue_email_delivery(fake_async_session, [notification])

        notifications_service._flush_pending_emails(fake_async_session.sync_session)

        assert [p["notification_id"] for p in published[0]] == [str(notification.id)]
        # Drained, so a second commit on the same session cannot re-send it.
        assert PENDING_KEY not in fake_async_session.sync_session.info

        notifications_service._flush_pending_emails(fake_async_session.sync_session)
        assert len(published) == 1

    def test_rollback_discards_the_queue(
        self, fake_async_session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[Any] = []
        monkeypatch.setattr(
            notifications_service, "_send_email_tasks", lambda payloads: published.append(payloads)
        )
        notifications_service.queue_email_delivery(
            fake_async_session, [_notification(NotificationChannel.EMAIL.value)]
        )

        notifications_service._drop_pending_emails(fake_async_session.sync_session)
        notifications_service._flush_pending_emails(fake_async_session.sync_session)

        assert published == []

    def test_hooks_are_registered_once_per_session(
        self, fake_async_session: SimpleNamespace
    ) -> None:
        sync_session = fake_async_session.sync_session
        for _ in range(3):
            notifications_service.queue_email_delivery(
                fake_async_session, [_notification(NotificationChannel.EMAIL.value)]
            )

        assert event.contains(
            sync_session, "after_commit", notifications_service._flush_pending_emails
        )
        assert event.contains(
            sync_session, "after_rollback", notifications_service._drop_pending_emails
        )
        # Three calls, three payloads, one pair of listeners.
        assert len(sync_session.info[PENDING_KEY]) == 3


class TestSendEmailTasks:
    def test_publishes_one_task_per_payload_under_the_contract_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.workers.celery_app import celery_app

        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            celery_app,
            "send_task",
            lambda name, kwargs=None, **_: calls.append((name, kwargs or {})),
        )

        sent = notifications_service._send_email_tasks(
            [{"notification_id": "a"}, {"notification_id": "b"}]
        )

        assert sent == 2
        assert {name for name, _ in calls} == {notifications_service.NOTIFICATION_EMAIL_TASK}
        assert [kwargs["notification_id"] for _, kwargs in calls] == ["a", "b"]

    def test_a_broker_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.workers.celery_app import celery_app

        def _explode(*_: Any, **__: Any) -> None:
            raise ConnectionError("broker unreachable")

        monkeypatch.setattr(celery_app, "send_task", _explode)

        # The notification row is already committed; failing here would raise
        # inside an after-commit hook, long past the point anything can roll back.
        assert notifications_service._send_email_tasks([{"notification_id": "a"}]) == 0
