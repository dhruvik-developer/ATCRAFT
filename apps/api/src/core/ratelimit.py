"""Application-level rate limiting (master rules sections 38/51).

Nginx already applies a coarse per-IP limit at the edge. This layer exists for
the things Nginx cannot see: *which account* is being attacked, and *which
operation* is expensive. A credential-stuffing run rotating through proxies
looks fine to Nginx and obvious here, because the limit is keyed on the target
email rather than the source address.

Algorithm: sliding window over a Redis sorted set. A fixed window lets an
attacker send 2x the limit across a window boundary; the sorted set costs one
extra command and does not.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, cast

from src.core.cache import KEY_PREFIX, get_redis
from src.core.clock import utcnow
from src.core.config import settings
from src.core.errors import RateLimitedError
from src.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


# ZREMRANGEBYSCORE + ZADD + ZCARD + EXPIRE as one atomic script. Done as separate
# round-trips, two concurrent requests can both observe "under the limit".
_SLIDING_WINDOW = """
local key      = KEYS[1]
local now_ms   = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local member   = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry = window
    if oldest[2] then
        retry = (tonumber(oldest[2]) + window) - now_ms
    end
    return {0, count, retry}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window)
return {1, count + 1, 0}
"""


async def check_rate_limit(
    identifier: str,
    *,
    limit: int,
    window_seconds: int = 60,
    scope: str = "default",
) -> RateLimitResult:
    """Consume one token. Returns the outcome rather than raising.

    Fails **open** if Redis is unavailable: an outage in the cache layer must not
    take authentication offline. Nginx still provides a floor of protection.
    """
    if not settings.rate_limit_enabled:
        return RateLimitResult(True, limit, limit, 0)

    key = f"{KEY_PREFIX}:rl:{scope}:{identifier}"
    now_ms = int(utcnow().timestamp() * 1000)
    window_ms = window_seconds * 1000
    member = f"{now_ms}-{uuid.uuid4().hex[:8]}"

    try:
        client = await get_redis()
        allowed, count, retry_ms = await cast(
            Awaitable[Any],
            client.eval(
                _SLIDING_WINDOW,
                1,
                key,
                str(now_ms),
                str(window_ms),
                str(limit),
                member,
            ),
        )
    except Exception as exc:
        log.warning("ratelimit.unavailable", scope=scope, error=str(exc))
        return RateLimitResult(True, limit, limit, 0)

    return RateLimitResult(
        allowed=bool(allowed),
        limit=limit,
        remaining=max(0, limit - int(count)),
        retry_after_seconds=max(1, int(retry_ms) // 1000) if not allowed else 0,
    )


#: Scopes whose identifier is whatever the caller typed into an e-mail field.
#: The one log line that fires during an attack must not write an unverified,
#: attacker-supplied address into the structured log - the auth service goes to
#: the trouble of logging only the domain half for the same reason, and
#: ``email`` is not a redacted key. The other scopes are keyed on a verified
#: subject id or a client address, which the request context already carries.
_PERSONAL_IDENTIFIER_SCOPES = frozenset({"login_email", "pwd_reset"})


def log_identity(identifier: str, *, scope: str) -> str:
    """What the exceeded-limit log line may say about the identifier.

    Non-reversible for the personal scopes, and still stable enough to see
    that the same identity is being hammered across many lines.
    """
    if scope in _PERSONAL_IDENTIFIER_SCOPES:
        return "sha256:" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
    return identifier


async def enforce_rate_limit(
    identifier: str,
    *,
    limit: int,
    window_seconds: int = 60,
    scope: str = "default",
) -> RateLimitResult:
    """Consume one token and raise :class:`RateLimitedError` when exhausted."""
    result = await check_rate_limit(
        identifier, limit=limit, window_seconds=window_seconds, scope=scope
    )
    if not result.allowed:
        log.warning(
            "ratelimit.exceeded",
            scope=scope,
            identifier=log_identity(identifier, scope=scope),
            limit=limit,
        )
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)
    return result


async def reset_rate_limit(identifier: str, scope: str = "default") -> None:
    """Clear a counter - used after a successful login so one bad password
    does not keep counting against an otherwise legitimate user."""
    try:
        client = await get_redis()
        await client.delete(f"{KEY_PREFIX}:rl:{scope}:{identifier}")
    except Exception as exc:
        log.warning("ratelimit.reset_failed", scope=scope, error=str(exc))


# --- Named policies -----------------------------------------------------------
# Expensive or abuse-prone operations get their own budget rather than sharing
# the general API allowance.


#: The sign-in budgets are measured over five minutes: long enough that a
#: dictionary run cannot reset itself by waiting, short enough that a plant
#: full of people who mistyped once are not locked out for the shift.
LOGIN_WINDOW_SECONDS = 300


class RateLimitPolicy:
    LOGIN_PER_EMAIL = ("login_email", 5, LOGIN_WINDOW_SECONDS)
    # The per-address budget is the one deployments need to tune - hundreds of
    # users behind one NAT share it - so it is the one driven by configuration
    # (RATE_LIMIT_AUTH_PER_MINUTE, scaled to the window). The per-account
    # budget above is a security constant and stays fixed.
    LOGIN_PER_IP = (
        "login_ip",
        settings.rate_limit_auth_per_minute * (LOGIN_WINDOW_SECONDS // 60),
        LOGIN_WINDOW_SECONDS,
    )
    PASSWORD_RESET = ("pwd_reset", 3, 3600)
    REFRESH = ("refresh", 30, 300)
    MFA_VERIFY = ("mfa_verify", 5, 300)
    MRP_RUN = ("mrp_run", 5, 300)
    REPORT_EXPORT = ("report_export", 10, 600)
    FILE_UPLOAD = ("upload", 60, 300)
    BULK_IMPORT = ("bulk_import", 3, 3600)


async def enforce_policy(policy: tuple[str, int, int], identifier: str) -> RateLimitResult:
    """Apply one of the named policies above."""
    scope, limit, window = policy
    return await enforce_rate_limit(identifier, limit=limit, window_seconds=window, scope=scope)


__all__ = [
    "LOGIN_WINDOW_SECONDS",
    "RateLimitPolicy",
    "RateLimitResult",
    "check_rate_limit",
    "enforce_policy",
    "enforce_rate_limit",
    "log_identity",
    "reset_rate_limit",
]
