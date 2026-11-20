"""The nightly purge of expired documents: its schedule, and how it walks the tenants.

A document past ``expires_at`` reads as absent at once, but deleting its row and
its file is this sweep's job - and until it was scheduled nothing ever called
``DocumentService.purge_expired``, so a file the upload dialog promised to
delete stayed on disk and in every backup for good. These pin the beat entry and
the per-company loop without a broker or a database;
tests/integration/test_documents_service.py runs the sweep against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from celery.schedules import crontab

from src.core import observability
from src.core.actors import SYSTEM_ACTOR_ID
from src.core.deps import TenantContext
from src.modules.documents.repository import MAX_PURGE_BATCH
from src.workers import celery_app as worker
from src.workers.tasks import maintenance

pytestmark = pytest.mark.unit

ENTRY = "purge-expired-documents"
TASK = "maintenance.purge_expired_documents"
METRIC = "atcraft_sweep_unit_failures_total"


@dataclass
class _Sweep:
    """What one run did: every session it opened, and every batch it asked for."""

    sessions: list[object] = field(default_factory=list)
    #: ``(company, actor, session, limit)`` for each ``purge_expired`` call.
    calls: list[tuple[uuid.UUID, uuid.UUID, object, int]] = field(default_factory=list)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    companies: list[uuid.UUID],
    batch: Callable[[uuid.UUID, int], int],
) -> _Sweep:
    """Run the sweep over ``companies``; ``batch(company, n)`` is what call ``n`` purges."""
    sweep = _Sweep()

    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        session = object()
        sweep.sessions.append(session)
        yield session

    async def discover(_session: object, _now: object) -> list[uuid.UUID]:
        return companies

    class _Documents:
        def __init__(self, session: object, tenant: TenantContext) -> None:
            self.session, self.tenant, self.batches = session, tenant, 0

        async def purge_expired(self, *, limit: int) -> int:
            self.batches += 1
            company = self.tenant.company_id
            sweep.calls.append((company, self.tenant.user_id, self.session, limit))
            return batch(company, self.batches)

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(maintenance, "companies_with_expired_documents", discover)
    monkeypatch.setattr(maintenance, "DocumentService", _Documents)
    return sweep


def _run() -> dict[str, Any]:
    """The Celery task itself, on a loop of its own as a worker runs it."""
    return asyncio.run(asyncio.to_thread(maintenance.purge_expired_documents))


def _failures() -> float:
    return observability.registry.get_sample_value(METRIC, {"task": TASK}) or 0.0


# -----------------------------------------------------------------------------
# Schedule
# -----------------------------------------------------------------------------


def test_beat_sends_the_purge_nightly_at_its_fixed_hour_on_the_default_queue() -> None:
    entry = worker.celery_app.conf.beat_schedule[ENTRY]

    assert entry["task"] == TASK
    # 21:30 UTC is 03:00 in India, an hour after the nightly MRP run's default.
    assert (worker.DOCUMENT_PURGE_HOUR_UTC, worker.DOCUMENT_PURGE_MINUTE_UTC) == (21, 30)
    assert entry["schedule"] == crontab(minute=30, hour=21)
    # Dropped when not started within the hour: the next night takes the rows.
    assert entry["options"] == {"queue": "default", "expires": 3600}


def test_the_task_beat_sends_is_registered_under_that_name() -> None:
    assert maintenance.purge_expired_documents.name == TASK
    assert TASK in worker.celery_app.tasks


def test_a_batch_fits_the_repository_ceiling_or_every_full_batch_would_read_as_the_last() -> None:
    """``list_expired`` clamps its limit to ``MAX_PURGE_BATCH``; a larger batch
    would come back short every time and stop each night after one call."""
    assert 0 < maintenance.DOCUMENT_PURGE_BATCH <= MAX_PURGE_BATCH


# -----------------------------------------------------------------------------
# The loop
# -----------------------------------------------------------------------------


def test_each_company_is_purged_in_a_session_of_its_own_as_the_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    sweep = _patch(monkeypatch, [first, second], lambda _company, _n: 3)

    assert _run() == {"companies": 2, "removed": 6, "failed": 0}

    discovery, *per_company = sweep.sessions
    assert [(company, actor, limit) for company, actor, _, limit in sweep.calls] == [
        (first, SYSTEM_ACTOR_ID, maintenance.DOCUMENT_PURGE_BATCH),
        (second, SYSTEM_ACTOR_ID, maintenance.DOCUMENT_PURGE_BATCH),
    ]
    assert [session for _, _, session, _ in sweep.calls] == per_company
    assert discovery not in per_company
    assert per_company[0] is not per_company[1]


def test_a_company_is_purged_batch_after_batch_until_one_comes_back_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = maintenance.DOCUMENT_PURGE_BATCH
    sweep = _patch(monkeypatch, [uuid.uuid4()], lambda _company, n: full if n < 3 else 7)

    assert _run() == {"companies": 1, "removed": 2 * full + 7, "failed": 0}
    assert len(sweep.calls) == 3


def test_a_backlog_stops_at_the_batch_ceiling_and_waits_for_the_next_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = maintenance.DOCUMENT_PURGE_BATCH
    sweep = _patch(monkeypatch, [uuid.uuid4()], lambda _company, _n: full)

    result = _run()

    assert len(sweep.calls) == maintenance.MAX_PURGE_BATCHES
    assert result == {
        "companies": 1,
        "removed": maintenance.MAX_PURGE_BATCHES * full,
        "failed": 0,
    }


def test_a_company_that_fails_is_counted_and_the_next_is_still_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, healthy = uuid.uuid4(), uuid.uuid4()

    def batch(company: uuid.UUID, _n: int) -> int:
        if company == broken:
            raise RuntimeError("the audit insert failed")
        return 2

    sweep = _patch(monkeypatch, [broken, healthy], batch)
    before = _failures()

    assert _run() == {"companies": 2, "removed": 2, "failed": 1}
    assert [company for company, *_ in sweep.calls] == [broken, healthy]
    assert _failures() == before + 1


def test_nothing_expired_opens_no_company_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sweep = _patch(monkeypatch, [], lambda _company, _n: 0)

    assert _run() == {"companies": 0, "removed": 0, "failed": 0}
    assert len(sweep.sessions) == 1
    assert sweep.calls == []
