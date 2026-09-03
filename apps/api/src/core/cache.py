"""Redis cache-aside helpers and explicit invalidation.

Master rules sections 33/34: Redis is a cache, never the source of truth; every
entry has a TTL; and correctness beats speed for mutable business state.

Key shape is ``atcraft:{company_id}:{namespace}:{discriminator}``. The company
segment is not decoration - it makes cross-tenant cache bleed structurally
impossible and lets one tenant's cache be dropped without touching another's.

What NOT to cache
-----------------
Live inventory availability, reservation state and anything an allocation
decision reads. Section 34 is explicit: a stale available-quantity that lets two
orders reserve the same stock is a correctness bug, not a performance win.
Cache the item master around it instead.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import redis.asyncio as aioredis
from redis.asyncio.client import Redis

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

KEY_PREFIX = "atcraft"

_redis: Redis | None = None


class CacheNamespace:
    """Namespaces with their default TTLs, in seconds.

    Chosen from how fast the underlying data actually changes (section 33) -
    not from how expensive the query is.
    """

    REFERENCE = ("ref", 3600)  # UoM, categories, warehouse types
    MASTER = ("master", 600)  # items, partners, warehouses
    PERMISSIONS = ("perm", 300)  # user permission set - invalidated on role change
    LIST = ("list", 120)  # filtered list pages
    DASHBOARD = ("dash", 60)  # summary tiles
    REPORT = ("report", 300)  # completed report payloads
    SESSION = ("sess", 900)  # short-lived workflow state
    IDEMPOTENCY = ("idem", 86400)  # request de-duplication keys


async def get_redis() -> Redis:
    """Process-wide Redis client (connection pooled, created lazily)."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=32,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def check_redis_ready() -> bool:
    try:
        client = await get_redis()
        return bool(await client.ping())
    except Exception:
        return False


def make_key(namespace: str, company_id: uuid.UUID | str | None, *parts: Any) -> str:
    """Build a tenant-scoped cache key.

    Long discriminators (a serialised filter set) are hashed so the key stays
    well under Redis' practical length and remains readable in MONITOR output.
    """
    tenant = str(company_id) if company_id else "global"
    tail = ":".join(str(p) for p in parts if p is not None)
    if len(tail) > 120:
        tail = hashlib.sha256(tail.encode()).hexdigest()[:32]
    return (
        f"{KEY_PREFIX}:{tenant}:{namespace}:{tail}"
        if tail
        else f"{KEY_PREFIX}:{tenant}:{namespace}"
    )


async def cache_get(key: str) -> Any | None:
    """Read a JSON value. A Redis outage degrades to a cache miss, never a 500."""
    if not settings.cache_enabled:
        return None
    try:
        client = await get_redis()
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("cache.get_failed", key=key, error=str(exc))
        return None


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    """Write a JSON value with a TTL. Section 33: entries always expire."""
    if not settings.cache_enabled:
        return
    try:
        client = await get_redis()
        await client.set(
            key,
            json.dumps(value, default=str),
            ex=ttl or settings.cache_default_ttl_seconds,
        )
    except Exception as exc:
        log.warning("cache.set_failed", key=key, error=str(exc))


async def cache_delete(*keys: str) -> None:
    # Invalidation has to honour the same switch as reads and writes. Without
    # this, disabling the cache still opens a Redis connection on every write -
    # which in a test run means a connect timeout per mutation.
    if not settings.cache_enabled or not keys:
        return
    try:
        client = await get_redis()
        await client.delete(*keys)
    except Exception as exc:
        log.warning("cache.delete_failed", keys=keys, error=str(exc))


async def cache_invalidate_namespace(namespace: str, company_id: uuid.UUID | str | None) -> None:
    """Drop every key in one namespace for one tenant.

    Uses SCAN, not KEYS: KEYS blocks the Redis event loop for the whole scan and
    a single call can stall every other request in the system.
    """
    if not settings.cache_enabled:
        return
    pattern = f"{KEY_PREFIX}:{company_id or 'global'}:{namespace}:*"
    try:
        client = await get_redis()
        batch: list[str] = []
        async for key in client.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                await client.delete(*batch)
                batch.clear()
        if batch:
            await client.delete(*batch)
    except Exception as exc:
        log.warning("cache.invalidate_failed", pattern=pattern, error=str(exc))


async def cache_aside(
    key: str,
    loader: Callable[[], Awaitable[T]],
    ttl: int | None = None,
) -> T:
    """Classic read-through: HIT returns; MISS loads, stores, returns.

    Deliberately not a decorator - an explicit call keeps it obvious at the call
    site which reads are served from cache, which matters when debugging a
    "why is this stale?" report.
    """
    cached = await cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    value = await loader()
    if value is not None:
        await cache_set(key, value, ttl)
    return value


# -----------------------------------------------------------------------------
# Targeted invalidation
# -----------------------------------------------------------------------------


async def invalidate_user_permissions(user_id: uuid.UUID, company_id: uuid.UUID) -> None:
    """Called whenever a role or permission assignment changes (section 34).

    Without this a revoked approval right would linger for the cache TTL, which
    is exactly the kind of stale authority section 39 forbids.
    """
    ns, _ = CacheNamespace.PERMISSIONS
    await cache_delete(make_key(ns, company_id, str(user_id)))


async def invalidate_entity(entity: str, company_id: uuid.UUID) -> None:
    """Drop cached master data and list pages after a write to ``entity``."""
    master_ns, _ = CacheNamespace.MASTER
    list_ns, _ = CacheNamespace.LIST
    await cache_invalidate_namespace(f"{master_ns}:{entity}", company_id)
    await cache_invalidate_namespace(f"{list_ns}:{entity}", company_id)


# -----------------------------------------------------------------------------
# Distributed lock - for work that must not run twice concurrently
# -----------------------------------------------------------------------------


async def acquire_lock(name: str, ttl_seconds: int = 300, token: str | None = None) -> str | None:
    """Best-effort mutual exclusion for background work (e.g. one MRP run per company).

    NOT a substitute for database locking on inventory: a Redis lock cannot make
    a stock update atomic, and section 10 requires PostgreSQL to arbitrate that.
    Use this only where losing the lock means "do it later", never "corrupt data".
    """
    import secrets

    client = await get_redis()
    value = token or secrets.token_urlsafe(16)
    acquired = await client.set(f"{KEY_PREFIX}:lock:{name}", value, nx=True, ex=ttl_seconds)
    return value if acquired else None


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


async def release_lock(name: str, token: str) -> bool:
    """Release only if we still hold it - a compare-and-delete, done atomically.

    A plain DEL would let a process whose lock had already expired delete the
    lock a different worker now legitimately owns.
    """
    client = await get_redis()
    result = await client.eval(_RELEASE_SCRIPT, 1, f"{KEY_PREFIX}:lock:{name}", token)
    return bool(result)


__all__ = [
    "KEY_PREFIX",
    "CacheNamespace",
    "acquire_lock",
    "cache_aside",
    "cache_delete",
    "cache_get",
    "cache_invalidate_namespace",
    "cache_set",
    "check_redis_ready",
    "close_redis",
    "get_redis",
    "invalidate_entity",
    "invalidate_user_permissions",
    "make_key",
    "release_lock",
]
