"""Re-proving the password on an authenticated route, and the two locks
behind sign-in failures and refresh rotation - no database.

The statements are compiled against the PostgreSQL dialect and inspected; the
service helper is exercised with its collaborators replaced, because what
matters is *which* collaborators it calls and what it hands back.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from src.core.clock import utcnow
from src.core.errors import AccountLockedError, ErrorCode, ValidationError
from src.core.messages import CURRENT_PASSWORD_INCORRECT
from src.core.ratelimit import RateLimitPolicy
from src.modules.auth import service as auth_service
from src.modules.auth.repository import UserSessionRepository, increment_failed_attempts
from src.modules.auth.service import (
    FAILURE_BAD_PASSWORD,
    FAILURE_LOCKED,
    AuthService,
    _mfa_code_rejected,
)
from src.modules.users.models import User

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value


class _Session:
    def __init__(self, value: Any = None) -> None:
        self.value = value
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.value)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _account() -> User:
    return User(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="person@example.com",
        full_name="Test Person",
        password_hash="argon2-hash-not-verified-here",
        is_active=True,
        is_superuser=False,
        failed_login_attempts=0,
    )


# -----------------------------------------------------------------------------
# Refresh rotation locks the session row
# -----------------------------------------------------------------------------


async def test_refresh_lookup_locks_the_session_row() -> None:
    session = _Session()
    tenant = auth_service._tenant_for(uuid.uuid4(), uuid.uuid4())

    await UserSessionRepository(session, tenant).get_by_session_id("sid", for_update=True)  # type: ignore[arg-type]

    sql = _sql(session.statements[0])
    assert "FOR UPDATE" in sql
    assert "user_sessions.session_id" in sql
    assert "user_sessions.company_id" in sql, "the lock is still tenant-scoped"


async def test_plain_lookup_does_not_lock() -> None:
    session = _Session()
    tenant = auth_service._tenant_for(uuid.uuid4(), uuid.uuid4())

    await UserSessionRepository(session, tenant).get_by_session_id("sid")  # type: ignore[arg-type]

    assert "FOR UPDATE" not in _sql(session.statements[0])


# -----------------------------------------------------------------------------
# Lockout counter is incremented in the database
# -----------------------------------------------------------------------------


async def test_failed_attempts_are_incremented_atomically() -> None:
    account = _account()
    session = _Session(value=3)

    attempts = await increment_failed_attempts(session, account)  # type: ignore[arg-type]

    assert attempts == 3
    sql = _sql(session.statements[0])
    assert "UPDATE users SET failed_login_attempts=(users.failed_login_attempts + " in sql
    assert "RETURNING users.failed_login_attempts" in sql
    assert "users.id = " in sql and "users.company_id = " in sql


async def test_the_in_memory_counter_follows_the_database_without_a_second_update() -> None:
    account = _account()
    session = _Session(value=5)

    await increment_failed_attempts(session, account)  # type: ignore[arg-type]

    assert account.failed_login_attempts == 5
    history = inspect(account).attrs.failed_login_attempts.history
    assert not history.has_changes(), "a dirty attribute would UPDATE the same value again"


# -----------------------------------------------------------------------------
# Current-password check on authenticated routes
# -----------------------------------------------------------------------------


@pytest.fixture
def collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"policies": [], "failures": [], "verify": False, "locks": False}

    async def enforce(policy: tuple[str, int, int], identifier: str) -> None:
        calls["policies"].append((policy, identifier))

    async def verify(password: str, password_hash: str) -> bool:
        # Argon2 verification runs on a worker thread now - see
        # test_password_hashing_offload - so the seam is the async form.
        return bool(calls["verify"])

    monkeypatch.setattr(auth_service, "enforce_policy", enforce)
    monkeypatch.setattr(auth_service, "verify_password_async", verify)
    return calls


def _service(calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> AuthService:
    service = AuthService(_Session())  # type: ignore[arg-type]

    async def register(account: User, **fields: Any) -> bool:
        calls["failures"].append(fields)
        return bool(calls["locks"])

    monkeypatch.setattr(service, "_register_failure", register)
    return service


async def test_a_correct_password_costs_one_token_and_records_nothing(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators["verify"] = True
    account = _account()

    refused = await _service(collaborators, monkeypatch)._check_current_password(
        account,
        "right",
        purpose="Password change",
        session_id="sid",
        ip_address="10.0.0.1",
        user_agent="ua",
    )

    assert refused is None
    assert collaborators["policies"] == [(RateLimitPolicy.LOGIN_PER_EMAIL, account.email)]
    assert collaborators["failures"] == []


async def test_a_wrong_password_is_counted_like_a_failed_sign_in(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account()

    refused = await _service(collaborators, monkeypatch)._check_current_password(
        account,
        "wrong",
        purpose="MFA disable",
        session_id="sid",
        ip_address="10.0.0.1",
        user_agent="ua",
    )

    (failure,) = collaborators["failures"]
    assert failure["reason"] == FAILURE_BAD_PASSWORD
    assert failure["count_against_lockout"] is True
    assert failure["ip_address"] == "10.0.0.1"
    assert failure["user_agent"] == "ua"
    assert failure["summary"].startswith("MFA disable refused for person@example.com")
    # The session the guess came from is handed over, to be ended only if
    # this miss is the one that locks the account.
    assert failure["end_session_id"] == "sid"

    # 422 with a field-level detail - a 401 here signs the user out before the
    # form can show the message.
    assert isinstance(refused, ValidationError)
    assert refused.status_code == 422
    assert refused.code == ErrorCode.INVALID_CREDENTIALS
    assert refused.details == {"fields": {"current_password": [CURRENT_PASSWORD_INCORRECT]}}


async def test_the_budget_is_consumed_before_the_password_is_checked(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    async def enforce(policy: tuple[str, int, int], identifier: str) -> None:
        order.append("throttle")

    async def verify(password: str, password_hash: str) -> bool:
        order.append("verify")
        return True

    monkeypatch.setattr(auth_service, "enforce_policy", enforce)
    monkeypatch.setattr(auth_service, "verify_password_async", verify)

    await _service(collaborators, monkeypatch)._check_current_password(
        _account(),
        "x",
        purpose="Password change",
        session_id="sid",
        ip_address=None,
        user_agent=None,
    )

    assert order == ["throttle", "verify"]


async def test_the_attempt_that_locks_the_account_ends_the_session(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators["locks"] = True

    refused = await _service(collaborators, monkeypatch)._check_current_password(
        _account(),
        "wrong",
        purpose="Password change",
        session_id="sid",
        ip_address=None,
        user_agent=None,
    )

    # The miss that locks the account ended the session it came from, so 401 -
    # "this session is dead" - is the true answer here.
    assert isinstance(refused, AccountLockedError)
    assert refused.status_code == 401
    (failure,) = collaborators["failures"]
    assert failure["end_session_id"] == "sid"


async def test_a_locked_account_is_refused_without_its_password_being_checked(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone else's guesses locked it: no password is checked while the lock
    runs, and the refusal is a field error, because this session is still
    alive and a 401 would sign it out."""
    account = _account()
    account.locked_until = utcnow() + timedelta(minutes=10)
    collaborators["verify"] = True  # the right password still does not get through

    refused = await _service(collaborators, monkeypatch)._check_current_password(
        account,
        "right",
        purpose="Password change",
        session_id="sid",
        ip_address="10.0.0.1",
        user_agent="ua",
    )

    assert collaborators["policies"] == [(RateLimitPolicy.LOGIN_PER_EMAIL, account.email)]
    (failure,) = collaborators["failures"]
    assert failure["reason"] == FAILURE_LOCKED
    assert failure["count_against_lockout"] is False
    assert "end_session_id" not in failure
    assert isinstance(refused, ValidationError)
    assert refused.status_code == 422
    assert refused.code == ErrorCode.ACCOUNT_LOCKED
    assert refused.details == {"fields": {"current_password": [AccountLockedError.message]}}


async def test_a_lapsed_lock_lets_the_password_be_checked_again(
    collaborators: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account()
    account.locked_until = utcnow() - timedelta(seconds=1)
    collaborators["verify"] = True

    refused = await _service(collaborators, monkeypatch)._check_current_password(
        account,
        "right",
        purpose="Password change",
        session_id="sid",
        ip_address=None,
        user_agent=None,
    )

    assert refused is None
    assert collaborators["failures"] == []


@pytest.mark.parametrize(
    ("refused", "dropped"),
    [
        (AccountLockedError(), [["sid"]]),
        (ValidationError(CURRENT_PASSWORD_INCORRECT), []),
        (ValidationError(AccountLockedError.message, code=ErrorCode.ACCOUNT_LOCKED), []),
    ],
)
async def test_only_the_refusal_that_ended_the_session_drops_its_cached_liveness(
    monkeypatch: pytest.MonkeyPatch, refused: Exception, dropped: list[list[str]]
) -> None:
    calls: list[list[str]] = []

    async def drop(company_id: uuid.UUID, session_ids: list[str]) -> None:
        calls.append(session_ids)

    monkeypatch.setattr(auth_service, "_drop_session_cache", drop)
    principal = SimpleNamespace(company_id=uuid.uuid4(), session_id="sid")

    settled = await AuthService(_Session())._settle_refusal(principal, refused)  # type: ignore[arg-type]

    assert settled is refused
    assert calls == dropped


def test_a_wrong_authenticator_code_is_a_field_error_not_a_dead_session() -> None:
    error = _mfa_code_rejected("The authentication code is incorrect.")

    assert error.status_code == 422
    assert error.code == ErrorCode.MFA_INVALID
    assert error.details == {"fields": {"code": ["The authentication code is incorrect."]}}
