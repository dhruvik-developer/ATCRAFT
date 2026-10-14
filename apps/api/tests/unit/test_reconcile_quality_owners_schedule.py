"""The owner reconciliation sweep is scheduled daily and once at beat start.

A deploy is when the sweep matters most - it repairs what decisions stranded
before quality called its owners back - so it must not wait a day for its first
run. These pin the schedule entry and the start-up hook without a broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.workers import celery_app as worker

pytestmark = pytest.mark.unit


def test_the_sweep_is_scheduled_daily_on_the_default_queue() -> None:
    entry = worker.celery_app.conf.beat_schedule[worker.RECONCILE_QUALITY_OWNERS_ENTRY]
    assert entry["task"] == worker.RECONCILE_QUALITY_OWNERS_TASK
    assert entry["task"] == "maintenance.reconcile_quality_owners"
    assert entry["schedule"] == 86400.0
    assert entry["options"]["queue"] == "default"
    # Routed by its prefix too, so a manual send lands where beat sends it.
    assert worker.celery_app.conf.task_routes["maintenance.*"] == {"queue": "default"}


def test_beat_starting_sends_the_sweep_once(monkeypatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        worker.celery_app, "send_task", lambda name, **options: sent.append((name, options))
    )

    worker._reconcile_on_beat_start()

    assert sent == [("maintenance.reconcile_quality_owners", {"queue": "default", "expires": 3600})]


def test_the_task_is_registered_under_its_name() -> None:
    from src.workers.tasks import maintenance

    assert maintenance.reconcile_quality_owners.name == "maintenance.reconcile_quality_owners"
