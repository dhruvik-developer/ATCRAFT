"""The nightly MRP dispatcher and its Celery beat schedule."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.actors import SYSTEM_ACTOR_ID
from src.core.config import settings
from src.modules.mrp import service
from src.modules.mrp.repository import ScheduledMrpConfiguration
from src.modules.mrp.schemas import MrpRunRequest, MrpScheduleSettingsUpdate
from src.workers import celery_app as worker
from src.workers.tasks import mrp

pytestmark = pytest.mark.unit


class _Sessions:
    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def scope(self) -> Any:
        self.opened += 1
        yield object()


def _run() -> dict[str, int]:
    return asyncio.run(asyncio.to_thread(mrp.scheduled_run))


def test_scheduled_mrp_is_nightly_on_the_mrp_queue_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mrp_schedule_cron", "30 20 * * *")
    schedule = worker._mrp_schedule()
    entry = worker.celery_app.conf.beat_schedule[worker.MRP_SCHEDULED_RUN_ENTRY]

    assert schedule.minute == {30}
    assert schedule.hour == {20}
    assert entry["task"] == "mrp.scheduled_run"
    assert entry["options"]["queue"] == "mrp"
    assert worker.celery_app.conf.timezone == "UTC"


def test_invalid_cron_ranges_are_rejected_when_the_schedule_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mrp_schedule_cron", "99 20 * * *")

    with pytest.raises(ValueError):
        worker._mrp_schedule()


def test_dispatch_is_async_system_scoped_and_continues_after_company_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companies = [uuid.uuid4() for _ in range(3)]
    configurations = [
        ScheduledMrpConfiguration(
            company_id=companies[0],
            horizon_days=30,
            warehouse_ids=(uuid.uuid4(),),
            respect_safety_stock=False,
        ),
        ScheduledMrpConfiguration(
            company_id=companies[1],
            horizon_days=90,
            warehouse_ids=None,
            respect_safety_stock=True,
        ),
        ScheduledMrpConfiguration(
            company_id=companies[2],
            horizon_days=120,
            warehouse_ids=None,
            respect_safety_stock=True,
        ),
    ]
    sessions = _Sessions()
    starts: list[tuple[uuid.UUID, MrpRunRequest, bool]] = []

    async def discover(session: Any) -> list[ScheduledMrpConfiguration]:
        return configurations

    async def start(
        session: Any,
        tenant: Any,
        payload: MrpRunRequest,
        *,
        force_async: bool = False,
    ) -> tuple[Any, bool]:
        assert tenant.user_id == SYSTEM_ACTOR_ID
        starts.append((tenant.company_id, payload, force_async))
        if tenant.company_id == companies[1]:
            raise service.MrpRunInProgressError()
        if tenant.company_id == companies[2]:
            raise RuntimeError("planning unavailable")
        return object(), True

    monkeypatch.setattr(mrp, "session_scope", sessions.scope)
    monkeypatch.setattr(service, "scheduled_configurations", discover)
    monkeypatch.setattr(service, "start_run", start)

    result = _run()

    assert result == {"companies": 3, "queued": 1, "skipped": 1, "failed": 1}
    assert sessions.opened == 1 + len(configurations)
    assert [company_id for company_id, _, _ in starts] == companies
    assert all(force_async for _, _, force_async in starts)
    assert starts[0][1].horizon_days == 30
    assert starts[0][1].warehouse_ids == [configurations[0].warehouse_ids[0]]
    assert starts[0][1].respect_safety_stock is False
    assert starts[1][1].warehouse_ids == []


def test_scheduled_task_is_registered_under_its_celery_name() -> None:
    assert mrp.scheduled_run.name == "mrp.scheduled_run"


def test_schedule_settings_bound_horizon_and_deduplicate_warehouse_scope() -> None:
    warehouse_id = uuid.uuid4()
    payload = MrpScheduleSettingsUpdate(
        horizon_days=30,
        warehouse_ids=[warehouse_id, warehouse_id],
    )
    assert payload.warehouse_ids == [warehouse_id]

    with pytest.raises(ValidationError):
        MrpScheduleSettingsUpdate(horizon_days=0)
