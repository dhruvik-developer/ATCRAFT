"""Why a dependency is not ready, said once each time that changes.

A readiness probe answers a boolean, and a boolean says nothing about why.
``/health/ready`` answering 503 with ``{"redis": false}`` while ``redis-cli
ping`` answers instantly cost real debugging time more than once (CLAUDE.md:
``localhost`` resolving to ``::1``, a cancelled command poisoning the pool),
and every time the reason was an exception the probe had swallowed. The probes
still return the same booleans; they now also report the exception that made
them false - once, on the transition to not ready, and once more when the
dependency answers again - so a probe polled every few seconds cannot fill the
log while it stays down.

The state is per process: each API worker reports its own transitions, which is
also what its own ``/health/ready`` reflects.
"""

from __future__ import annotations

from src.core.logging import get_logger

log = get_logger("health")

#: The dependency names, as ``/health/ready`` reports them.
DATABASE = "database"
REDIS = "redis"

#: How much of an exception's text a not-ready line keeps. A connection failure
#: says which address was tried, which is the diagnosis; nothing longer is.
UNREADY_ERROR_TEXT_LIMIT = 300

_ready: dict[str, bool] = {}


def note_ready(dependency: str) -> None:
    """Record a successful check; log only a recovery."""
    if _ready.get(dependency) is False:
        log.info("health.dependency_ready", dependency=dependency)
    _ready[dependency] = True


def note_unready(dependency: str, error: BaseException) -> None:
    """Record a failed check; log only the first failure after a success.

    The text of a connection error names the host and port that refused - the
    ``::1`` that told ``localhost`` apart from ``127.0.0.1`` - and never a row,
    because a probe binds no values.
    """
    if _ready.get(dependency) is not False:
        log.warning(
            "health.dependency_unready",
            dependency=dependency,
            error_type=type(error).__name__,
            error=str(error)[:UNREADY_ERROR_TEXT_LIMIT],
        )
    _ready[dependency] = False


def reset() -> None:
    """Forget every dependency's last state (tests)."""
    _ready.clear()


__all__ = [
    "DATABASE",
    "REDIS",
    "UNREADY_ERROR_TEXT_LIMIT",
    "note_ready",
    "note_unready",
    "reset",
]
