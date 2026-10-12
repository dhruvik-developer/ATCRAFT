"""Claim before transport, compare-and-release retries, and device disposition."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.dialects import postgresql

from src.core.clock import utcnow
from src.core.config import settings
from src.core.webpush import PushOutcome, PushRetryableError
from src.modules.notifications.models import Notification, PushSubscription
from src.workers.tasks import notifications as worker

pytestmark = pytest.mark.unit


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self.value


@pytest.mark.parametrize(
    "outcomes",
    [
        [PushOutcome.DELIVERED],
        [PushOutcome.GONE],
        [PushOutcome.TOO_LARGE],
        [PushOutcome.RETRY],
        [PushOutcome.DELIVERED, PushOutcome.RETRY],
    ],
)
async def test_claim_transport_cleanup_and_retry(monkeypatch, outcomes):
    monkeypatch.setattr(settings, "web_push_enabled", True)
    notification = Notification(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Test",
        body="Body",
        severity="WARNING",
        channels=["PUSH"],
        occurrence_count=1,
    )
    devices = [
        PushSubscription(
            id=uuid.uuid4(),
            company_id=notification.company_id,
            user_id=notification.user_id,
            updated_at=utcnow(),
        )
        for _ in outcomes
    ]
    statements, requests = [], []
    state = {"open": False, "scope": 0, "query": 0}

    class FakeSession:
        async def execute(self, statement):
            statements.append(statement)
            state["query"] += 1
            return Result(notification if state["query"] == 1 else devices)

        def expunge(self, _device):
            pass

    @asynccontextmanager
    async def scope():
        state["open"] = True
        state["scope"] += 1
        try:
            yield FakeSession()
        finally:
            state["open"] = False

    async def deliver(device, payload, **kwargs):
        assert not state["open"]
        assert notification.push_delivered_at is not None
        requests.append((payload, kwargs))
        return outcomes[devices.index(device)]

    monkeypatch.setattr(worker, "session_scope", scope)
    monkeypatch.setattr(worker, "deliver_push", deliver)
    args = str(notification.id), str(notification.company_id), str(notification.user_id)
    if all(outcome is PushOutcome.RETRY for outcome in outcomes):
        with pytest.raises(PushRetryableError):
            await worker._deliver_notification_push(*args)
        sql = str(statements[-1].compile(dialect=postgresql.dialect()))
        assert "notifications.push_delivered_at =" in sql
        assert "UPDATE notifications" in sql and "push_delivered_at=" in sql
    else:
        result = await worker._deliver_notification_push(*args)
        assert result["delivered"] == (PushOutcome.DELIVERED in outcomes)
    assert len(requests) == len(outcomes) and state["scope"] == 2
    assert requests[0][1]["urgency"] == "normal"
    cleanup = str(statements[2].compile(dialect=postgresql.dialect()))
    assert "push_subscriptions.user_id =" in cleanup
    assert "push_subscriptions.updated_at =" in cleanup
    if outcomes[0] is PushOutcome.GONE:
        assert "DELETE FROM push_subscriptions" in cleanup
