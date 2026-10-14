"""Time-ordered identifiers.

Master rules section 28 prefers UUIDv7 for high-write tables. UUIDv7 keeps the
random-looking, non-enumerable surface of UUIDv4 while sorting by creation time,
which matters a great deal here: ``inventory_transactions`` and ``audit_logs``
are append-heavy, and a random primary key scatters inserts across the whole
B-tree instead of appending to the right-hand leaf.

``uuid.uuid7`` only landed in CPython 3.14, and the project targets 3.11+, so
the generator is implemented locally rather than pulling in a dependency for
twenty lines of bit-shifting.

Layout (RFC 9562):

    0                   1                   2                   3
    |unix_ts_ms (48 bits)           |ver|rand_a |var|  rand_b (62 bits)  |
"""

from __future__ import annotations

import os
import time
import uuid
from threading import Lock

from src.core import actors

_lock = Lock()
_last_timestamp_ms = 0
_last_counter = 0

# 12 bits of rand_a are used as a monotonic counter within the same
# millisecond, so ids minted in a tight loop still sort in creation order.
_MAX_COUNTER = 0x0FFF


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7."""
    global _last_timestamp_ms, _last_counter

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000

        if timestamp_ms == _last_timestamp_ms:
            _last_counter += 1
            if _last_counter > _MAX_COUNTER:
                # Counter exhausted inside one millisecond: borrow from the
                # next millisecond rather than emitting a duplicate.
                _last_timestamp_ms += 1
                timestamp_ms = _last_timestamp_ms
                _last_counter = 0
        else:
            if timestamp_ms < _last_timestamp_ms:
                # Clock moved backwards (NTP step). Never regress.
                timestamp_ms = _last_timestamp_ms
                _last_counter += 1
            else:
                _last_timestamp_ms = timestamp_ms
                _last_counter = int.from_bytes(os.urandom(2), "big") & 0x03FF

        counter = _last_counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | (counter & _MAX_COUNTER) << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the embedded creation timestamp (ms since epoch) from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"Not a UUIDv7: {value}")
    return value.int >> 80


def new_id() -> uuid.UUID:
    """Project-wide primary-key factory. Every model uses this."""
    return uuid7()


#: The user id a scheduled job runs under. ``TenantContext`` requires a user id and a
#: beat sweep has no person behind it, so the nil UUID stands in for "the system".
#: It can never collide with a real account (user ids are UUIDv7) and it must never
#: reach an actor column: every one of those is a foreign key to ``users``. Writers
#: pass the tenant's user id through :func:`persisted_actor`, which turns it back
#: into NULL.
#:
#: Single source of truth is :mod:`src.core.actors`; the names here are
#: backwards-compatible aliases so existing callers keep working.
SYSTEM_ACTOR_ID = actors.SYSTEM_ACTOR_ID


def persisted_actor(user_id: uuid.UUID | None) -> uuid.UUID | None:
    """The value to store in an actor column (``created_by_id`` and the like).

    A person is stored as themselves; the system placeholder is stored as NULL,
    which is what "nobody did this by hand" means in every actor column here.
    Storing the placeholder instead violates the foreign key to ``users`` and
    fails the whole operation - which is how a scheduled sweep that reserves or
    releases stock on a tenant's behalf failed on its first write.

    Alias of :func:`src.core.actors.actor_id`; both names stay valid.
    """
    return actors.actor_id(user_id)


__all__ = [
    "SYSTEM_ACTOR_ID",
    "new_id",
    "persisted_actor",
    "uuid7",
    "uuid7_timestamp_ms",
]
