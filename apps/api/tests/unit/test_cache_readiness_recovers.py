"""The readiness probe recovers from a cancelled Redis command.

``/health/ready`` bounds each dependency check with ``asyncio.wait_for``, and a
timeout cancels the ``ping`` mid-command. redis-py returns that connection to
the pool with its reply unread and hands the same connection to the next
command, which then fails on the stale data - for ever, because nothing repairs
it.

The consequence is not a slow probe, it is a permanently wrong one: the
backend's Docker health check reads this endpoint, so one slow moment while a
container starts under load made it report unhealthy for the life of the
process, ``up --wait`` never returned, and the deployment stalled while the
application served traffic correctly the whole time. This was observed, not
theorised - a development API answered ``{"redis": false}`` for half an hour
while ``redis-cli ping`` and a fresh client both answered instantly.

So the contract is: however a check fails, the cached client is dropped, and the
next check builds a new one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.core import cache

pytestmark = pytest.mark.unit


class _Client:
    """A Redis stand-in whose first ping hangs, exactly as a slow server does."""

    def __init__(self, *, hang_first: bool = True) -> None:
        self.pings = 0
        self.closed = False
        self._hang_first = hang_first

    async def ping(self) -> bool:
        self.pings += 1
        if self._hang_first and self.pings == 1:
            await asyncio.sleep(3600)
        return True

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_shared_client() -> Any:
    """Never let one case leave a client behind for the next."""
    cache.discard_redis()
    yield
    cache.discard_redis()


async def test_a_cancelled_ping_drops_the_client_so_the_next_check_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[_Client] = []

    async def fake_get_redis() -> Any:
        if cache._redis is None:
            client = _Client(hang_first=not built)
            built.append(client)
            cache._redis = client  # type: ignore[assignment]
        return cache._redis

    monkeypatch.setattr(cache, "get_redis", fake_get_redis)

    # The probe times out and cancels the ping, as asyncio.wait_for does.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(cache.check_redis_ready(), timeout=0.05)

    # The poisoned client is gone rather than cached for every later call.
    assert cache._redis is None

    # The next probe builds a fresh client and answers truthfully. Before the
    # fix this returned False for ever.
    assert await cache.check_redis_ready() is True
    assert len(built) == 2, "the second check must not reuse the cancelled client"


async def test_an_ordinary_failure_also_drops_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection leaves no client behind either.

    Same reasoning: redis-py's pool survives the exception, and the connection
    that failed mid-command is the one the next command is handed.
    """

    class _Refusing:
        async def ping(self) -> bool:
            raise ConnectionError("connection refused")

    monkeypatch.setattr(cache, "get_redis", lambda: _ready(_Refusing()))
    cache._redis = None

    assert await cache.check_redis_ready() is False
    assert cache._redis is None


async def _ready(value: Any) -> Any:
    cache._redis = value
    return value


def test_discard_returns_the_client_it_forgot() -> None:
    """The caller gets the client back, so it can close it when it safely can."""
    sentinel = _Client(hang_first=False)
    cache._redis = sentinel  # type: ignore[assignment]

    assert cache.discard_redis() is sentinel
    assert cache._redis is None
    # Idempotent: discarding twice is not an error.
    assert cache.discard_redis() is None
