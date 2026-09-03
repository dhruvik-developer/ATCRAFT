"""The MRP run budget: five starts per company per five minutes.

``RateLimitPolicy.MRP_RUN`` was declared long before anything spent it, so the
most expensive operation in the product - every BOM exploded for every demand
line across the horizon - had no budget at all. The in-progress lock bounds two
runs *overlapping*; it does nothing about a client that starts a fresh run the
instant the last one finished, which pins a worker indefinitely.

These tests pin the enforcement and, specifically, the four things about it that
are easy to get wrong:

* **The token is spent before the planning lock.** A refusal must cost one Redis
  round-trip, not a distributed lock plus a read of the whole order book. Every
  test proves the ordering the same way: the planning lock is replaced by a
  sentinel, so a call that reaches the lock is a call that got past the limiter,
  and a call that raises ``RateLimitedError`` demonstrably never reached it. The
  session is a booby trap that raises on any attribute access, so a refusal that
  touched the database would fail loudly rather than merely be slow.

* **The key is the company, and nothing else.** It is derived from the
  ``TenantContext`` built from the verified token. One tenant exhausting its
  budget must leave every other tenant's untouched, no field of the request may
  appear in the key (or the limit is bypassed by varying it), and the user id
  must not appear either (or every planner gets a private five runs).

* **The window really is the policy's window.** Both numbers are read from
  ``RateLimitPolicy.MRP_RUN`` so the tests cannot drift from it, plus one
  explicit assertion of the intended values so quietly loosening the policy is
  caught here rather than in production.

* **Disabled means disabled.** ``settings.rate_limit_enabled = False`` must not
  open a Redis connection - the exact mistake ``cache_delete`` already had to be
  fixed for. The stub records every request for a client and the test asserts
  there were none.

No Redis, no database, no dependency added. ``get_redis`` is replaced with a
stub implementing the same sliding window the Lua script does, so the window
arithmetic is exercised rather than assumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.core import ratelimit
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import ErrorCode, RateLimitedError
from src.core.ratelimit import KEY_PREFIX, RateLimitPolicy
from src.modules.mrp import service
from src.modules.mrp.schemas import ManualDemandIn, MrpRunRequest

pytestmark = pytest.mark.unit

SCOPE, LIMIT, WINDOW_SECONDS = RateLimitPolicy.MRP_RUN


# -----------------------------------------------------------------------------
# Doubles
# -----------------------------------------------------------------------------


class PlanningLockReachedError(Exception):
    """The stubbed planning lock raises this instead of doing any work.

    It is the marker for "this attempt got past the limiter". Nothing in
    ``start_run`` catches it - the lock is acquired outside the ``try`` that
    releases it - so it arrives at the test unchanged.
    """


class ExplodingSession:
    """A session that fails if anything touches it.

    The rate limit is checked before any database work, and this is what proves
    it: a refusal that first read the order book would raise from here instead of
    raising ``RateLimitedError``.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - failure path
        raise AssertionError(
            f"start_run touched the database (session.{name}) on a path that should "
            "have been decided by the rate limiter alone."
        )


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


class FrozenClock:
    """A clock the test moves, so a five-minute window costs no wall time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    frozen = FrozenClock()
    monkeypatch.setattr(ratelimit, "utcnow", frozen)
    return frozen


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """A stub client, and the switch left on. Counts every request for a client."""
    stub = FakeRedis()

    async def _get_redis() -> FakeRedis:
        stub.connection_requests += 1
        return stub

    monkeypatch.setattr(ratelimit, "get_redis", _get_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return stub


@pytest.fixture
def lock_reached(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Replace the planning lock with a tripwire; return the companies that hit it."""
    seen: list[uuid.UUID] = []

    async def _acquire(company_id: uuid.UUID) -> str | None:
        seen.append(company_id)
        raise PlanningLockReachedError()

    monkeypatch.setattr(service, "_acquire_planning_lock", _acquire)
    return seen


def tenant_for(company_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(company_id=company_id, user_id=user_id or uuid.uuid4())


async def attempt_run(tenant: TenantContext, payload: MrpRunRequest | None = None) -> None:
    """One start attempt. Returns normally when the limiter let it through."""
    session: Any = ExplodingSession()
    with pytest.raises(PlanningLockReachedError):
        await service.start_run(session, tenant, payload or MrpRunRequest())


# -----------------------------------------------------------------------------
# The budget
# -----------------------------------------------------------------------------


def test_the_policy_is_five_runs_per_five_minutes() -> None:
    """The numbers these tests are written against, asserted once.

    Everything below reads ``LIMIT`` and ``WINDOW_SECONDS`` from the policy so it
    cannot go stale. That makes this the only place a silent widening of the
    budget - to fifty runs, or to a one-second window - is visible.
    """
    assert RateLimitPolicy.MRP_RUN == ("mrp_run", 5, 300)


async def test_run_over_the_budget_is_refused(
    clock: FrozenClock, redis: FakeRedis, lock_reached: list[uuid.UUID]
) -> None:
    """Five starts go through; the sixth inside the window is a 429."""
    tenant = tenant_for(uuid.uuid4())

    for _ in range(LIMIT):
        await attempt_run(tenant)

    assert len(lock_reached) == LIMIT

    with pytest.raises(RateLimitedError) as raised:
        await service.start_run(ExplodingSession(), tenant, MrpRunRequest())  # type: ignore[arg-type]

    error = raised.value
    assert error.status_code == 429
    assert error.code is ErrorCode.RATE_LIMITED
    # The handler reads this attribute to set Retry-After on a 429, and the body
    # carries it in details - the existing shape, not a new one.
    assert 0 < error.retry_after_seconds <= WINDOW_SECONDS
    assert error.details["retry_after_seconds"] == error.retry_after_seconds

    # The refusal never took the planning lock, and the exploding session proves
    # it never read anything either.
    assert len(lock_reached) == LIMIT


async def test_the_budget_refills_when_the_window_has_passed(
    clock: FrozenClock, redis: FakeRedis, lock_reached: list[uuid.UUID]
) -> None:
    """It is a sliding window, not a permanent ban.

    Still refused one second short of the window, allowed one second past it -
    which is also what proves the window the policy declares is the window that
    reaches Redis.
    """
    tenant = tenant_for(uuid.uuid4())
    for _ in range(LIMIT):
        await attempt_run(tenant)

    clock.advance(WINDOW_SECONDS - 1)
    with pytest.raises(RateLimitedError):
        await service.start_run(ExplodingSession(), tenant, MrpRunRequest())  # type: ignore[arg-type]

    clock.advance(2)
    await attempt_run(tenant)
    assert len(lock_reached) == LIMIT + 1


# -----------------------------------------------------------------------------
# Tenant isolation - the point of keying on the company
# -----------------------------------------------------------------------------


async def test_one_company_cannot_spend_anothers_budget(
    clock: FrozenClock, redis: FakeRedis, lock_reached: list[uuid.UUID]
) -> None:
    """A tenant hammering the planner must not lock out every other tenant."""
    noisy = tenant_for(uuid.uuid4())
    quiet = tenant_for(uuid.uuid4())

    for _ in range(LIMIT):
        await attempt_run(noisy)
    with pytest.raises(RateLimitedError):
        await service.start_run(ExplodingSession(), noisy, MrpRunRequest())  # type: ignore[arg-type]

    # The other tenant has its full allowance, and only then runs out.
    for _ in range(LIMIT):
        await attempt_run(quiet)
    with pytest.raises(RateLimitedError):
        await service.start_run(ExplodingSession(), quiet, MrpRunRequest())  # type: ignore[arg-type]

    assert lock_reached.count(noisy.company_id) == LIMIT
    assert lock_reached.count(quiet.company_id) == LIMIT
    assert len(redis.windows) == 2


async def test_two_users_of_one_company_share_the_budget(
    clock: FrozenClock, redis: FakeRedis, lock_reached: list[uuid.UUID]
) -> None:
    """What is rationed is planning the company's order book, not the planner.

    A per-user key would let a company with ten logins start fifty runs, which is
    the cost this budget exists to bound.
    """
    company_id = uuid.uuid4()
    first = tenant_for(company_id)
    second = tenant_for(company_id)
    assert first.user_id != second.user_id

    for index in range(LIMIT):
        await attempt_run(first if index % 2 == 0 else second)

    with pytest.raises(RateLimitedError):
        await service.start_run(ExplodingSession(), second, MrpRunRequest())  # type: ignore[arg-type]

    assert len(redis.windows) == 1


async def test_the_key_is_the_tenant_company_and_nothing_client_supplied(
    clock: FrozenClock, redis: FakeRedis, lock_reached: list[uuid.UUID]
) -> None:
    """Vary everything a client controls; the key must not move.

    A key that mixed in any request field would be trivially bypassed by sending
    a different horizon, warehouse or note on every call.
    """
    tenant = tenant_for(uuid.uuid4())

    await attempt_run(tenant, MrpRunRequest())
    await attempt_run(
        tenant,
        MrpRunRequest(
            horizon_days=120,
            warehouse_ids=[uuid.uuid4()],
            respect_safety_stock=False,
            notes="a different note",
            manual_demand=[
                ManualDemandIn(
                    item_id=uuid.uuid4(),
                    quantity=Decimal("7"),
                    required_date=date(2026, 10, 1),
                )
            ],
        ),
    )

    assert list(redis.windows) == [f"{KEY_PREFIX}:rl:{SCOPE}:{tenant.company_id}"]
    key = next(iter(redis.windows))
    assert str(tenant.user_id) not in key


# -----------------------------------------------------------------------------
# Degradation
# -----------------------------------------------------------------------------


async def test_disabling_the_limiter_stops_both_the_refusal_and_the_connection(
    clock: FrozenClock,
    redis: FakeRedis,
    lock_reached: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch is checked *before* the client is fetched, on every path.

    Exhausting the budget first is what makes this test mean something: it proves
    the limiter really is in the path (the next attempt would be refused), and
    then that flipping the switch both lets the attempt through and stops Redis
    being asked for at all.

    ``cache_delete`` had to be fixed for exactly this class of mistake - a
    feature switch that still opened a connection, so disabling the feature cost
    a connect timeout per call instead of nothing.
    """
    tenant = tenant_for(uuid.uuid4())
    for _ in range(LIMIT):
        await attempt_run(tenant)
    with pytest.raises(RateLimitedError):
        await service.start_run(ExplodingSession(), tenant, MrpRunRequest())  # type: ignore[arg-type]

    connections_while_enabled = redis.connection_requests
    windows_while_enabled = {key: list(value) for key, value in redis.windows.items()}
    assert connections_while_enabled > 0

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(LIMIT * 4):
        await attempt_run(tenant)

    assert len(lock_reached) == LIMIT * 5
    assert redis.connection_requests == connections_while_enabled
    assert redis.windows == windows_while_enabled


async def test_a_redis_outage_does_not_take_planning_offline(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch, lock_reached: list[uuid.UUID]
) -> None:
    """Fails open, as ``check_rate_limit`` documents.

    A cache outage must not stop a plant from planning. The limit is a guard
    against abuse, not a correctness control - the one-run-at-a-time lock and its
    advisory-lock floor are what protect correctness, and neither is Redis-only.

    The attempt counter is what stops this being a test that would pass with no
    limiter at all: every attempt has to have asked for a client and swallowed
    the failure.
    """
    attempts = 0

    async def _unavailable() -> Any:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("redis is down")

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(ratelimit, "get_redis", _unavailable)
    tenant = tenant_for(uuid.uuid4())

    for _ in range(LIMIT * 2):
        await attempt_run(tenant)

    assert attempts == LIMIT * 2
    assert len(lock_reached) == LIMIT * 2
