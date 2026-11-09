"""A probe that answers false says why, once, and says when it recovers.

``/health/ready`` answering ``{"redis": false}`` while ``redis-cli ping``
answered instantly cost real debugging time more than once, and each time the
cause was an exception the probe swallowed without a word. The probes still
answer the same booleans; these pin what they now write beside them.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from src import main as main_module
from src.core import cache, database, readiness

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    readiness.reset()
    cache.discard_redis()
    yield
    readiness.reset()
    cache.discard_redis()


def _lines(capsys: pytest.CaptureFixture[str], event: str) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if event in line]


# -----------------------------------------------------------------------------
# Redis
# -----------------------------------------------------------------------------


class _Pong:
    async def ping(self) -> bool:
        return True


def _redis_that(monkeypatch: pytest.MonkeyPatch, *, answers: list[bool]) -> None:
    """Each ``get_redis`` call either refuses or hands back a client that answers."""

    async def get_redis() -> Any:
        if not answers.pop(0):
            raise ConnectionError("Error 10061 connecting to ::1:6380. Connection refused.")
        return _Pong()

    monkeypatch.setattr(cache, "get_redis", get_redis)


async def test_redis_says_why_it_is_not_ready_once_and_when_it_recovers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redis_that(monkeypatch, answers=[False, False, True, True])

    assert await cache.check_redis_ready() is False
    (unready,) = _lines(capsys, "health.dependency_unready")
    assert "dependency=redis" in unready
    assert "error_type=ConnectionError" in unready
    assert "::1:6380" in unready, "the address that refused is the diagnosis"

    assert await cache.check_redis_ready() is False
    assert _lines(capsys, "health.dependency_unready") == [], "a probe polled every few seconds"

    assert await cache.check_redis_ready() is True
    (ready,) = _lines(capsys, "health.dependency_ready")
    assert "dependency=redis" in ready

    assert await cache.check_redis_ready() is True
    assert _lines(capsys, "health.dependency_") == []


async def test_a_healthy_start_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redis_that(monkeypatch, answers=[True])

    assert await cache.check_redis_ready() is True
    assert _lines(capsys, "health.dependency_") == []


# -----------------------------------------------------------------------------
# PostgreSQL
# -----------------------------------------------------------------------------


class _Connection:
    async def execute(self, _statement: object) -> None:
        return None


class _Engine:
    def __init__(self, *, refuse: bool) -> None:
        self.refuse = refuse

    def connect(self) -> Any:
        if self.refuse:
            raise ConnectionRefusedError(1225, "Connect call failed ('::1', 5433)")

        @asynccontextmanager
        async def connection() -> AsyncIterator[_Connection]:
            yield _Connection()

        return connection()


async def test_the_database_says_why_it_is_not_ready_once_and_when_it_recovers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(database, "engine", _Engine(refuse=True))

    assert await database.check_database_ready() is False
    assert await database.check_database_ready() is False
    (unready,) = _lines(capsys, "health.dependency_unready")
    assert "dependency=database" in unready
    assert "error_type=ConnectionRefusedError" in unready
    assert "5433" in unready

    monkeypatch.setattr(database, "engine", _Engine(refuse=False))

    assert await database.check_database_ready() is True
    (ready,) = _lines(capsys, "health.dependency_ready")
    assert "dependency=database" in ready


# -----------------------------------------------------------------------------
# A check that runs out of time
# -----------------------------------------------------------------------------


async def test_a_check_that_times_out_is_noted_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The check is cancelled, so it cannot say why itself."""

    async def never_answers() -> bool:
        await asyncio.Event().wait()
        return True

    async def answers() -> bool:
        return True

    monkeypatch.setattr(main_module, "check_database_ready", answers)
    monkeypatch.setattr(main_module, "check_redis_ready", never_answers)
    monkeypatch.setattr(main_module, "DEPENDENCY_CHECK_TIMEOUT_SECONDS", 0.01)

    assert await main_module._dependency_readiness() == (True, False)

    (unready,) = _lines(capsys, "health.dependency_unready")
    assert "dependency=redis" in unready
    assert "error_type=TimeoutError" in unready


# -----------------------------------------------------------------------------
# Catch-alls that keep their traceback
# -----------------------------------------------------------------------------


def test_a_broken_tracing_setup_logs_its_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tracing stays tolerant - the application starts - but the line says where."""
    from src.core import observability

    broken = types.ModuleType("opentelemetry")

    def refuse(name: str) -> Any:
        raise RuntimeError("tracer provider refused its resource")

    broken.__getattr__ = refuse  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "opentelemetry", broken)
    monkeypatch.setattr(observability.settings, "otel_enabled", True)

    observability.setup_tracing(app=None)

    out = capsys.readouterr().out
    assert "otel.setup_failed" in out
    assert "Traceback (most recent call last)" in out
    assert "RuntimeError: tracer provider refused its resource" in out


def test_a_failed_demo_report_run_logs_its_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.core.demo_data import reports

    async def refuse(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("the invoice renderer could not find its font")

    class _Disposable:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(reports, "queued_demo_jobs", refuse)
    monkeypatch.setattr(reports, "engine", _Disposable())

    assert reports.run_reports("admin@example.com", None) == 1

    out = capsys.readouterr().out
    assert "demo.report_generation_failed" in out
    assert "the invoice renderer could not find its font" in out
    assert "Traceback (most recent call last)" in out
