"""Broker-backed queue monitoring stays read-only and reports worker liveness."""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import RedisError

from src.core import queue_monitoring as monitoring

pytestmark = pytest.mark.unit


class _Broker:
    def __init__(self, lengths: dict[str, int]) -> None:
        self.lengths = lengths

    async def llen(self, queue: str) -> int:
        return self.lengths[queue]

    async def aclose(self) -> None:
        return None


async def test_queue_snapshot_reads_all_declared_queues_and_worker_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queues = monitoring.queue_names()
    broker = _Broker({name: index for index, name in enumerate(queues)})
    workers: dict[str, Any] = {"batch@host": {"ok": "pong"}, "notify@host": {"ok": "pong"}}
    monkeypatch.setattr(monitoring, "_broker", broker)
    monkeypatch.setattr(monitoring, "_ping_workers", lambda: workers)

    snapshot = await monitoring.get_queue_snapshot()

    assert set(snapshot.queue_depths) == {"default", "mrp", "reports", "notifications"}
    assert snapshot.queue_depths == {name: index for index, name in enumerate(queues)}
    assert snapshot.workers_online == 2


async def test_workerless_broker_is_a_valid_zero_not_a_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queues = monitoring.queue_names()
    monkeypatch.setattr(monitoring, "_broker", _Broker(dict.fromkeys(queues, 0)))
    monkeypatch.setattr(monitoring, "_ping_workers", lambda: None)

    snapshot = await monitoring.get_queue_snapshot()

    assert snapshot.workers_online == 0
    assert snapshot.queue_depths == dict.fromkeys(queues, 0)


async def test_broker_failure_discards_the_client_and_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenBroker(_Broker):
        async def llen(self, queue: str) -> int:
            raise RedisError("private broker detail")

    monkeypatch.setattr(monitoring, "_broker", BrokenBroker({}))

    with pytest.raises(monitoring.QueueMonitoringUnavailableError, match="queue probe failed"):
        await monitoring.get_queue_snapshot()

    assert monitoring._broker is None


async def test_invalid_broker_scheme_is_reported_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitoring.settings, "celery_broker_url", "amqp://broker")
    monkeypatch.setattr(monitoring, "_broker", None)

    with pytest.raises(monitoring.QueueMonitoringUnavailableError, match="requires a Redis"):
        await monitoring.get_queue_snapshot()
