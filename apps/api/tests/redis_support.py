"""A Redis stand-in for tests that drive the real rate limiter.

``src.core.ratelimit`` spends a token with one Lua script, ``_SLIDING_WINDOW``.
Tests replace ``ratelimit.get_redis`` with a function returning this double, so
the limiter's own code runs - key building, the policy's numbers, the refusal and
its ``Retry-After`` - over a window that lives in a dict. No Redis server, no
network, and the window arithmetic is exercised rather than assumed.

It lives here rather than in one test file because the MRP budget, the route
budgets, the login/MFA policies and the vendor portal's HTTP suite all drive the
same limiter. Imported out of ``test_mrp_rate_limit.py`` it made one test file a
library for three others, each importing the MRP module's services to get it,
and renaming or deleting that file would break suites that never mention MRP.
"""

from __future__ import annotations

from typing import Any


class FakeRedis:
    """The sliding window of ``_SLIDING_WINDOW``, in Python.

    ``ZREMRANGEBYSCORE key 0 now-window`` drops everything at or below the cutoff,
    so an entry survives while its score is strictly greater than it. Returns the
    same ``[allowed, count, retry_ms]`` triple the script does.
    """

    def __init__(self) -> None:
        self.windows: dict[str, list[tuple[int, str]]] = {}
        self.connection_requests = 0
        self.deleted: list[str] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        now_ms: Any,
        window_ms: Any,
        limit: Any,
        member: Any,
    ) -> list[int]:
        now, window, cap = int(now_ms), int(window_ms), int(limit)
        entries = [entry for entry in self.windows.get(key, []) if entry[0] > now - window]
        self.windows[key] = entries

        if len(entries) >= cap:
            retry = (entries[0][0] + window) - now if entries else window
            return [0, len(entries), retry]

        entries.append((now, str(member)))
        entries.sort()
        return [1, len(entries), 0]

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        return len(keys)


__all__ = ["FakeRedis"]
