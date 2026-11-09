"""A cacheable report answers a cache hit without running its aggregate.

``run_report`` counted a report's rows before it looked in Redis, and the cache
key does not depend on that count - so every hit on a cacheable report still
ran the report's whole grouping scan once, to count it, and the cache saved only
the page fetch. The count, the page and the statement build are all refused here
on a hit; the miss path still counts, pages and fills the cache.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.reports import service as reports_service
from src.modules.reports.schemas import ReportFilters, ReportKey, ReportPeriod

pytestmark = pytest.mark.unit

KEY = ReportKey.SALES_ORDER_STATUS
PERIOD = ReportPeriod(date_from=date(2026, 10, 1), date_to=date(2026, 10, 31))
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


async def _run(tenant: TenantContext) -> Any:
    return await reports_service.run_report(
        None,  # type: ignore[arg-type]
        tenant,
        key=KEY,
        period=PERIOD,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=False,
        now=NOW,
    )


def test_the_report_under_test_is_cacheable() -> None:
    assert reports_service.definition_for(KEY).cacheable


async def test_a_cache_hit_is_answered_without_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = {
        "rows": [],
        "pagination": OffsetPagination(page=1, limit=50, total=7, total_pages=1).model_dump(
            mode="json"
        ),
        "generated_at": "2026-11-10T07:55:00+00:00",
    }
    asked: list[str] = []

    async def cache_get(key: str) -> Any:
        asked.append(key)
        return cached

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a cache hit ran the report's statement")

    monkeypatch.setattr(reports_service, "cache_get", cache_get)
    monkeypatch.setattr(reports_service, "count_rows", refuse)
    monkeypatch.setattr(reports_service, "paginate_rows", refuse)
    monkeypatch.setattr(reports_service, "ReportQueryRepository", refuse)

    result = await _run(_tenant())

    assert isinstance(result, reports_service.InlineReport)
    assert result.pagination.total == 7
    assert result.generated_at == datetime(2026, 11, 10, 7, 55, tzinfo=UTC)
    assert len(asked) == 1


async def test_a_cache_miss_counts_pages_and_fills_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    stored: dict[str, Any] = {}

    async def cache_get(key: str) -> Any:
        calls.append("cache_get")
        return None

    async def count_rows(session: Any, stmt: Any, *, cap: int) -> int:
        calls.append("count_rows")
        return 0

    async def paginate_rows(
        session: Any, stmt: Any, params: PageParams, *, total: int
    ) -> tuple[list[Any], OffsetPagination]:
        calls.append("paginate_rows")
        return [], OffsetPagination(page=1, limit=50, total=total, total_pages=0)

    async def cache_set(key: str, value: Any, *, ttl: int) -> None:
        calls.append("cache_set")
        stored[key] = value

    monkeypatch.setattr(reports_service, "cache_get", cache_get)
    monkeypatch.setattr(reports_service, "count_rows", count_rows)
    monkeypatch.setattr(reports_service, "paginate_rows", paginate_rows)
    monkeypatch.setattr(reports_service, "cache_set", cache_set)

    result = await _run(_tenant())

    assert calls == ["cache_get", "count_rows", "paginate_rows", "cache_set"]
    assert isinstance(result, reports_service.InlineReport)
    assert result.pagination.total == 0
    [payload] = stored.values()
    assert payload["pagination"]["total"] == 0
