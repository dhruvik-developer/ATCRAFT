"""Session lifecycle and lockout arithmetic - no database.

``user_sessions`` stores no status column: the state is derived from
``revoked_at``, ``expires_at`` and ``replaced_by_id``. That derivation decides
whether a presented refresh token is rotated normally, rejected, or treated as a
replay that burns the whole chain, so it is worth testing on its own rather than
only through the service.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.core.clock import utcnow
from src.core.config import settings
from src.core.errors import InvalidStateTransitionError
from src.modules.auth.service import (
    SESSION_STATE_MACHINE,
    SessionState,
    lockout_deadline,
    session_state,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeSession:
    """Only the four columns :func:`session_state` reads."""

    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by_id: uuid.UUID | None = None


def _live(**overrides: object) -> FakeSession:
    base = {"expires_at": utcnow() + timedelta(days=7)}
    base.update(overrides)
    return FakeSession(**base)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# State derivation
# -----------------------------------------------------------------------------


def test_unrevoked_unexpired_session_is_active() -> None:
    assert session_state(_live()) is SessionState.ACTIVE


def test_expired_session_is_expired() -> None:
    record = _live(expires_at=utcnow() - timedelta(seconds=1))
    assert session_state(record) is SessionState.EXPIRED


def test_revoked_without_successor_is_revoked() -> None:
    record = _live(revoked_at=utcnow())
    assert session_state(record) is SessionState.REVOKED


def test_revoked_with_successor_is_rotated() -> None:
    """The distinction the replay check depends on.

    A rotated session means a successor token exists, so presenting the old one
    is evidence of capture. A plainly revoked session means the user signed out.
    """
    record = _live(revoked_at=utcnow(), replaced_by_id=uuid.uuid4())
    assert session_state(record) is SessionState.ROTATED


def test_revocation_outranks_expiry() -> None:
    """A session revoked before it expired must not later read as merely expired,
    or a replay after the expiry date would be classified as a routine timeout."""
    record = FakeSession(
        expires_at=utcnow() - timedelta(days=1),
        revoked_at=utcnow() - timedelta(days=2),
        replaced_by_id=uuid.uuid4(),
    )
    assert session_state(record) is SessionState.ROTATED


def test_state_is_evaluated_at_the_supplied_moment() -> None:
    expiry = utcnow() + timedelta(hours=1)
    record = FakeSession(expires_at=expiry)
    assert session_state(record, now=expiry - timedelta(seconds=1)) is SessionState.ACTIVE
    assert session_state(record, now=expiry) is SessionState.EXPIRED


# -----------------------------------------------------------------------------
# State machine
# -----------------------------------------------------------------------------


def test_machine_graph_is_structurally_sound() -> None:
    SESSION_STATE_MACHINE.validate()


def test_active_session_may_rotate_revoke_or_expire() -> None:
    assert set(SESSION_STATE_MACHINE.allowed_targets(SessionState.ACTIVE)) == {
        SessionState.ROTATED,
        SessionState.REVOKED,
        SessionState.EXPIRED,
    }


@pytest.mark.parametrize(
    "current",
    [SessionState.REVOKED, SessionState.ROTATED, SessionState.EXPIRED],
)
def test_terminal_states_cannot_be_revoked_again(current: SessionState) -> None:
    with pytest.raises(InvalidStateTransitionError) as excinfo:
        SESSION_STATE_MACHINE.assert_transition(current, SessionState.REVOKED)
    assert excinfo.value.details["current_status"] == str(current)
    assert excinfo.value.details["allowed"] == []


def test_rotation_is_not_reversible() -> None:
    assert not SESSION_STATE_MACHINE.can_transition(SessionState.ROTATED, SessionState.ACTIVE)


# -----------------------------------------------------------------------------
# Lockout
# -----------------------------------------------------------------------------


def test_no_lockout_below_the_threshold() -> None:
    now = utcnow()
    assert lockout_deadline(settings.login_max_failed_attempts - 1, now=now) is None


def test_lockout_starts_exactly_at_the_threshold() -> None:
    """Off-by-one here either locks a user out one attempt early or gives an
    attacker one extra guess per window."""
    now = utcnow()
    deadline = lockout_deadline(settings.login_max_failed_attempts, now=now)
    assert deadline == now + timedelta(minutes=settings.login_lockout_minutes)


def test_lockout_holds_beyond_the_threshold() -> None:
    now = utcnow()
    deadline = lockout_deadline(settings.login_max_failed_attempts + 3, now=now)
    assert deadline is not None
    assert deadline > now


def test_lockout_deadline_is_timezone_aware() -> None:
    """A naive datetime would compare wrongly against a TIMESTAMPTZ column and
    could unlock the account immediately (section 28)."""
    deadline = lockout_deadline(settings.login_max_failed_attempts, now=utcnow())
    assert deadline is not None
    assert deadline.tzinfo is not None
