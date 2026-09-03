"""Single source of "now".

Master rules section 28: timestamps are stored in UTC, consistently. Routing
every read of the clock through one function makes that enforceable (ruff's DTZ
rules ban naive ``datetime.now()`` elsewhere) and lets tests freeze time without
monkeypatching the standard library globally.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

_frozen_at: datetime | None = None


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return _frozen_at if _frozen_at is not None else datetime.now(UTC)


def today() -> date:
    """Current UTC date. Business dates are compared in UTC to stay stable."""
    return utcnow().date()


@contextmanager
def frozen_time(moment: datetime) -> Iterator[datetime]:
    """Freeze :func:`utcnow` for the duration of the block (tests only)."""
    global _frozen_at
    if moment.tzinfo is None:
        raise ValueError("frozen_time requires a timezone-aware datetime")
    previous = _frozen_at
    _frozen_at = moment.astimezone(UTC)
    try:
        yield _frozen_at
    finally:
        _frozen_at = previous


__all__ = ["frozen_time", "today", "utcnow"]
