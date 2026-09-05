"""Password-reset e-mail is published only after the token row commits."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession

from src.modules.auth import email_delivery

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_async_session() -> SimpleNamespace:
    return SimpleNamespace(sync_session=SyncSession())


def _stage(session: SimpleNamespace, reset_token_id: uuid.UUID | None = None) -> uuid.UUID:
    reset_id = reset_token_id or uuid.uuid4()
    email_delivery.queue_password_reset_email(
        session,
        company_id=uuid.uuid4(),
        reset_token_id=reset_id,
    )
    return reset_id


def test_reset_email_is_not_published_before_commit(
    fake_async_session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[object] = []
    monkeypatch.setattr(
        email_delivery,
        "_publish_password_reset_tasks",
        lambda payloads: published.append(payloads),
    )

    _stage(fake_async_session)

    assert published == []


def test_commit_publishes_once_and_rollback_publishes_nothing(
    fake_async_session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        email_delivery,
        "_publish_password_reset_tasks",
        lambda payloads: published.append(payloads),
    )

    _stage(fake_async_session)
    email_delivery._flush_password_reset_tasks(fake_async_session.sync_session)
    email_delivery._flush_password_reset_tasks(fake_async_session.sync_session)
    assert len(published) == 1
    assert uuid.UUID(published[0][0]["reset_token_id"])
    assert "token" not in published[0][0]

    _stage(fake_async_session)
    email_delivery._drop_password_reset_tasks(fake_async_session.sync_session)
    email_delivery._flush_password_reset_tasks(fake_async_session.sync_session)
    assert len(published) == 1


def test_celery_message_contains_only_non_secret_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.workers.celery_app import celery_app

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda _name, **options: calls.append(options),
    )

    reset_id = uuid.uuid4()
    sent = email_delivery._publish_password_reset_tasks(
        [{"company_id": str(uuid.uuid4()), "reset_token_id": str(reset_id)}]
    )

    assert sent == 1
    assert calls[0]["kwargs"]["reset_token_id"] == str(reset_id)
    assert "token" not in calls[0]["kwargs"]


def test_session_hooks_are_installed_once(fake_async_session: SimpleNamespace) -> None:
    for _ in range(3):
        _stage(fake_async_session)

    sync_session = fake_async_session.sync_session
    assert event.contains(
        sync_session,
        "after_commit",
        email_delivery._flush_password_reset_tasks,
    )
    assert event.contains(
        sync_session,
        "after_rollback",
        email_delivery._drop_password_reset_tasks,
    )
