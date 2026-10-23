"""The local seed refreshes derived stock only when its ledger is newer."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from src.core.demo_data import summaries
from src.core.deps import TenantContext

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("latest_movement", "refreshed_at", "should_refresh"),
    [
        (NOW, None, True),
        (NOW, NOW - timedelta(seconds=1), True),
        (NOW, NOW, False),
        (NOW, NOW + timedelta(seconds=1), False),
        (None, NOW, False),
        (None, None, False),
    ],
)
async def test_only_missing_or_stale_company_summaries_are_refreshed(
    latest_movement, refreshed_at, should_refresh, monkeypatch
):
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    session = SimpleNamespace(scalar=AsyncMock(return_value=latest_movement))
    repository = SimpleNamespace(summary_refreshed_at=AsyncMock(return_value=refreshed_at))
    repository_factory = Mock(return_value=repository)
    refreshed = object()
    refresh = AsyncMock(return_value=refreshed)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(summaries, "session_scope", scope)
    monkeypatch.setattr(summaries, "ReportQueryRepository", repository_factory)
    monkeypatch.setattr(summaries.reports_service, "refresh_inventory_summary", refresh)

    result = await summaries.refresh_if_stale(tenant)

    assert result is (refreshed if should_refresh else None)
    if latest_movement is None:
        repository_factory.assert_not_called()
        repository.summary_refreshed_at.assert_not_awaited()
    else:
        repository_factory.assert_called_once_with(session, tenant)
        repository.summary_refreshed_at.assert_awaited_once()
    if should_refresh:
        refresh.assert_awaited_once_with(session, tenant)
    else:
        refresh.assert_not_awaited()
    statement = session.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    assert "inventory_transactions.company_id =" in str(statement)
    assert list(statement.params.values()) == [tenant.company_id]


async def test_refresh_failure_propagates_and_releases_its_session(monkeypatch):
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    session = SimpleNamespace(scalar=AsyncMock(return_value=NOW))
    released = False

    @asynccontextmanager
    async def scope():
        nonlocal released
        try:
            yield session
        finally:
            released = True

    monkeypatch.setattr(summaries, "session_scope", scope)
    monkeypatch.setattr(
        summaries,
        "ReportQueryRepository",
        Mock(return_value=SimpleNamespace(summary_refreshed_at=AsyncMock(return_value=None))),
    )
    monkeypatch.setattr(
        summaries.reports_service,
        "refresh_inventory_summary",
        AsyncMock(side_effect=RuntimeError("refresh unavailable")),
    )

    with pytest.raises(RuntimeError, match="refresh unavailable"):
        await summaries.refresh_if_stale(tenant)
    assert released is True
