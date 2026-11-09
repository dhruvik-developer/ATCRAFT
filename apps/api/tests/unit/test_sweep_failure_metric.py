"""A sweep that skips a failing unit still says so to the metrics.

Every beat sweep runs each company, order, account or row on its own, logs a
failure and carries on, so Celery records the task as a SUCCESS: a summary
refresh broken for every tenant on every tick looked healthy on
``atcraft_celery_tasks_total``. ``atcraft_sweep_unit_failures_total`` counts the
units each sweep skipped.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from src.core import observability
from src.workers import celery_app as worker
from src.workers.tasks import maintenance

pytestmark = pytest.mark.unit

TASKS = Path(__file__).resolve().parents[2] / "src" / "workers" / "tasks"
METRIC = "atcraft_sweep_unit_failures_total"


def _failures(task: str) -> float:
    return observability.registry.get_sample_value(METRIC, {"task": task}) or 0.0


def _patch_summary_refresh(
    monkeypatch: pytest.MonkeyPatch, companies: list[uuid.UUID], failing: set[uuid.UUID]
) -> None:
    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    async def companies_with_stock(_session: object) -> list[uuid.UUID]:
        return companies

    class _Refreshed:
        position_count = 3

    async def refresh(_session: object, tenant: Any) -> _Refreshed:
        if tenant.company_id in failing:
            raise RuntimeError("the summary query failed")
        return _Refreshed()

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(maintenance.reports_service, "companies_with_stock", companies_with_stock)
    monkeypatch.setattr(maintenance.reports_service, "refresh_inventory_summary", refresh)


def _refresh() -> dict[str, Any]:
    return asyncio.run(asyncio.to_thread(maintenance.refresh_summary_views))


def test_a_company_the_refresh_skips_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    broken, healthy = uuid.uuid4(), uuid.uuid4()
    _patch_summary_refresh(monkeypatch, [broken, healthy], failing={broken})
    before = _failures("maintenance.refresh_summary_views")

    result = _refresh()

    assert result == {"companies": 1, "positions": 3, "failed": 1}
    assert _failures("maintenance.refresh_summary_views") == before + 1


def test_a_clean_refresh_counts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_summary_refresh(monkeypatch, [uuid.uuid4(), uuid.uuid4()], failing=set())
    before = _failures("maintenance.refresh_summary_views")

    result = _refresh()

    assert result["failed"] == 0
    assert _failures("maintenance.refresh_summary_views") == before


def test_counting_no_units_adds_no_series() -> None:
    observability.count_sweep_failure("tests.never_failed", 0)

    assert observability.registry.get_sample_value(METRIC, {"task": "tests.never_failed"}) is None


# -----------------------------------------------------------------------------
# Guard: every label is a task that exists
# -----------------------------------------------------------------------------


def _labels_in(source: str) -> list[tuple[int, object]]:
    """Each ``count_sweep_failure(<first argument>)`` call: its line and literal."""
    labels: list[tuple[int, object]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "count_sweep_failure"
        ):
            first = node.args[0] if node.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            labels.append((node.lineno, literal))
    return labels


def _unknown_labels(source: str, registered: set[str]) -> list[str]:
    return [
        f"line {line}: {label!r}"
        for line, label in _labels_in(source)
        if not isinstance(label, str) or label not in registered
    ]


def _registered_tasks() -> set[str]:
    import src.workers.tasks.maintenance
    import src.workers.tasks.mrp
    import src.workers.tasks.notifications  # noqa: F401 - registers the tasks

    return set(worker.celery_app.tasks)


def test_every_sweep_failure_is_labelled_with_a_registered_task() -> None:
    """A mistyped label is a series nobody's alert reads."""
    registered = _registered_tasks()
    calls = 0
    problems: list[str] = []
    for path in sorted(TASKS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        calls += len(_labels_in(source))
        problems += [f"{path.name} {problem}" for problem in _unknown_labels(source, registered)]

    assert calls >= 16, "the sweeps count their skipped units"
    assert problems == []


def test_the_guard_refuses_a_label_no_task_has() -> None:
    source = (
        "count_sweep_failure('maintenance.refresh_summary_view')\n"
        "count_sweep_failure(name)\n"
        "count_sweep_failure('maintenance.expire_batches')\n"
    )

    assert _unknown_labels(source, _registered_tasks()) == [
        "line 1: 'maintenance.refresh_summary_view'",
        "line 2: None",
    ]
