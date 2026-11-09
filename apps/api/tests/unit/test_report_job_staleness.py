"""An abandoned report job is never one that is merely slow.

A job still QUEUED or RUNNING after ``REPORT_JOB_STALE_AFTER_SECONDS`` is closed
as abandoned and is no longer handed to an identical request. That is only
safe while no task can legitimately run that long: Celery's hard time limit
kills one first. Raising the limit without the bound would fail live jobs.
"""

from __future__ import annotations

import pytest

from src.modules.reports.models import REPORT_JOB_STALE_AFTER_SECONDS
from src.workers.celery_app import celery_app

pytestmark = pytest.mark.unit


def test_the_abandoned_bound_outlasts_celerys_hard_time_limit() -> None:
    hard_limit = celery_app.conf.task_time_limit

    assert hard_limit is not None, "without a hard limit no job can be called abandoned"
    assert hard_limit < REPORT_JOB_STALE_AFTER_SECONDS


def test_no_report_task_raises_its_own_limit_past_the_bound() -> None:
    import src.workers.tasks.reports  # noqa: F401 - registers the tasks

    limits = {
        name: task.time_limit
        for name, task in celery_app.tasks.items()
        if name.startswith("reports.") and task.time_limit is not None
    }

    assert {
        name: limit for name, limit in limits.items() if limit >= REPORT_JOB_STALE_AFTER_SECONDS
    } == {}


def test_the_daily_sweep_closes_abandoned_jobs_company_by_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One company that cannot be closed does not stop the next."""
    import asyncio
    import uuid
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from typing import Any

    from src.workers.tasks import maintenance

    broken, healthy = uuid.uuid4(), uuid.uuid4()

    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    async def none(_session: object) -> list[uuid.UUID]:
        return []

    async def both(_session: object) -> list[uuid.UUID]:
        return [broken, healthy]

    async def close(_session: object, tenant: Any) -> int:
        if tenant.company_id == broken:
            raise RuntimeError("lock timeout")
        return 2

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(maintenance, "_sweep_temp_directory", lambda _root, _cutoff: (0, 0))
    monkeypatch.setattr(maintenance.reports_service, "companies_with_expired_snapshots", none)
    monkeypatch.setattr(maintenance.reports_service, "companies_with_abandoned_snapshots", both)
    monkeypatch.setattr(maintenance.reports_service, "close_abandoned_snapshots", close)

    result = asyncio.run(asyncio.to_thread(maintenance.purge_temp_files))

    assert result["jobs_closed"] == 2
    assert result["failed"] == 1
