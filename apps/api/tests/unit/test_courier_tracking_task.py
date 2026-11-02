"""The courier poll sweep isolates provider failures to one account."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.workers.tasks import maintenance

pytestmark = pytest.mark.unit


class _Rows:
    def __init__(self, rows: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
        self._rows = rows

    async def execute(self, statement: Any) -> _Rows:
        return _Rows(self._rows)


def _run() -> dict[str, int]:
    return asyncio.run(asyncio.to_thread(maintenance.poll_courier_tracking))


def test_one_account_poll_failure_is_recorded_without_stopping_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        (uuid.uuid4(), uuid.uuid4()),
        (uuid.uuid4(), uuid.uuid4()),
    ]
    failed_account_id = targets[0][1]
    sessions_opened = 0
    recorded_failures: list[uuid.UUID] = []

    @asynccontextmanager
    async def session_scope() -> Any:
        nonlocal sessions_opened
        sessions_opened += 1
        yield _Session(targets)

    async def get_account(session: Any, tenant: Any, account_id: uuid.UUID) -> Any:
        return SimpleNamespace(id=account_id)

    async def poll_account(session: Any, tenant: Any, account: Any) -> tuple[int, None]:
        if account.id == failed_account_id:
            raise RuntimeError("provider unavailable")
        return 2, None

    async def record_poll_failure(session: Any, tenant: Any, account_id: uuid.UUID) -> None:
        recorded_failures.append(account_id)

    monkeypatch.setattr(maintenance, "settings", SimpleNamespace(courier_tracking_enabled=True))
    monkeypatch.setattr(maintenance, "session_scope", session_scope)
    monkeypatch.setattr(maintenance.courier_tracking_service, "get_account", get_account)
    monkeypatch.setattr(maintenance.courier_tracking_service, "poll_account", poll_account)
    monkeypatch.setattr(
        maintenance.courier_tracking_service, "record_poll_failure", record_poll_failure
    )

    result = _run()

    assert result == {"accounts": 2, "events": 2, "failed": 1}
    assert recorded_failures == [failed_account_id]
    # Initial account scan, each account, and a separate failure-marker session.
    assert sessions_opened == 4
