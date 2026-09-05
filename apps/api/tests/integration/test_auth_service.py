"""Service-level authentication tests against a real PostgreSQL.

These cover what only a database can prove: that a failed sign-in leaves its
evidence behind even though the request ends in an exception, that refresh-token
rotation links the chain and that replaying a spent token closes the whole
family, that a password change ends the other sessions, and that one tenant's
principal cannot touch another tenant's session rows.

Fixtures (``session``, ``company``, ``other_company``, ``user``) come from
``tests/conftest.py``; the outer transaction is rolled back after every test.

Row state is read back with column-level SELECTs rather than by re-reading the
ORM objects: several of the operations under test update through
``UPDATE ... RETURNING`` with ``synchronize_session=False``, so an instance left
in the identity map would still show its pre-update values.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import utcnow
from src.core.deps import Principal, get_current_principal
from src.core.errors import (
    AccountLockedError,
    AuthenticationError,
    BusinessRuleError,
    ErrorCode,
    InvalidCredentialsError,
    NotFoundError,
    PasswordChangeRequiredError,
    ValidationError,
)
from src.core.messages import CURRENT_PASSWORD_INCORRECT
from src.core.pagination import PageParams
from src.core.security import (
    decode_token,
    decrypt_password_reset_delivery_token,
    decrypt_totp_secret,
    hash_password,
    hash_secret_token,
    verify_password,
)
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaSetupRequest,
    MfaVerifyRequest,
    PasswordResetConfirmPayload,
    PasswordResetRequestPayload,
    TokenPair,
)
from src.modules.auth.service import (
    FAILURE_BAD_MFA,
    FAILURE_BAD_PASSWORD,
    REVOKE_REASON_REPLAY,
    REVOKE_REASON_ROTATED,
    AuthService,
)
from src.modules.users.models import LoginAttempt, PasswordResetToken, User, UserSession
from tests import seed_support

pytestmark = pytest.mark.integration

PASSWORD = "Sh0p-Floor-Passphrase!"
NEW_PASSWORD = "An0ther-Good-Passphrase!"
CLIENT_IP = "203.0.113.9"
CLIENT_AGENT = "pytest/auth"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def make_account(
    session: AsyncSession,
    company,
    *,
    password: str = PASSWORD,
    is_active: bool = True,
    email: str | None = None,
) -> User:
    account = User(
        company_id=company.id,
        email=email or f"user-{uuid.uuid4().hex[:8]}@atcraft-qa.com",
        full_name="Shop Floor User",
        password_hash=hash_password(password),
        is_active=is_active,
        is_superuser=False,
    )
    session.add(account)
    await session.flush()
    return account


def login_payload(account: User, password: str = PASSWORD, **extra: object) -> LoginRequest:
    return LoginRequest(email=account.email, password=password, **extra)  # type: ignore[arg-type]


async def sign_in(session: AsyncSession, account: User, password: str = PASSWORD):
    return await AuthService(session).login(
        login_payload(account, password), ip_address=CLIENT_IP, user_agent=CLIENT_AGENT
    )


def principal_for(account: User, session_id: str | None = None) -> Principal:
    return Principal(
        user_id=account.id,
        company_id=account.company_id,
        email=account.email,
        full_name=account.full_name,
        is_superuser=False,
        permissions=frozenset(),
        session_id=session_id,
    )


async def session_row(session: AsyncSession, session_id: str) -> tuple:
    """(revoked_at, revoked_reason, replaced_by_id) straight from the table."""
    stmt = select(
        UserSession.revoked_at, UserSession.revoked_reason, UserSession.replaced_by_id
    ).where(UserSession.session_id == session_id)
    return (await session.execute(stmt)).one()


async def audit_actions(session: AsyncSession, company_id: uuid.UUID) -> list[str]:
    stmt = select(AuditLog.action).where(AuditLog.company_id == company_id)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def attempt_rows(session: AsyncSession, email: str) -> list[tuple]:
    stmt = select(LoginAttempt.succeeded, LoginAttempt.failure_reason).where(
        LoginAttempt.email == email
    )
    return [tuple(row) for row in (await session.execute(stmt)).all()]


async def account_state(session: AsyncSession, account_id: uuid.UUID) -> tuple:
    stmt = select(
        User.failed_login_attempts, User.locked_until, User.password_hash, User.password_changed_at
    ).where(User.id == account_id)
    return (await session.execute(stmt)).one()


async def refusal_summaries(session: AsyncSession, account: User) -> list[str]:
    """Summaries of the LOGIN_FAILED audit rows written against one account."""
    stmt = (
        select(AuditLog.summary)
        .where(
            AuditLog.company_id == account.company_id,
            AuditLog.action == AuditAction.LOGIN_FAILED,
            AuditLog.entity_id == account.id,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def mfa_enabled(session: AsyncSession, account_id: uuid.UUID) -> bool:
    return bool(
        (await session.execute(select(User.mfa_enabled).where(User.id == account_id))).scalar_one()
    )


async def enable_mfa(session: AsyncSession, principal: Principal) -> str:
    """Enrol and confirm an authenticator for the principal's account. Returns the seed."""
    service = AuthService(session)
    setup = await service.setup_mfa(principal, MfaSetupRequest(current_password=PASSWORD))
    await service.confirm_mfa(principal, MfaCodeRequest(code=pyotp.TOTP(setup.secret).now()))
    return setup.secret


def wrong_code_for(secret: str) -> str:
    """A six-digit code the authenticator would not produce anywhere near now.

    Picked against the live window rather than hard-coded: a fixed "000000" is
    the current code once in a million runs, and a test that fails once in a
    million runs is one nobody can act on.
    """
    totp = pyotp.TOTP(secret)
    now = utcnow()
    live = {totp.at(now + timedelta(seconds=offset)) for offset in (-60, -30, 0, 30, 60)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in live)


# -----------------------------------------------------------------------------
# Sign in
# -----------------------------------------------------------------------------


async def test_login_issues_a_usable_pair_and_stores_only_the_digest(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)

    result = await sign_in(session, account)

    access = decode_token(result.access_token, expected_type="access")
    refresh = decode_token(result.refresh_token, expected_type="refresh")
    assert access.subject == account.id
    assert access.company_id == company.id
    assert access.session_id == result.session_id == refresh.session_id

    stored = (
        await session.execute(
            select(UserSession.refresh_token_hash, UserSession.ip_address).where(
                UserSession.session_id == result.session_id
            )
        )
    ).one()
    # The table must be worthless to whoever reads it.
    assert stored[0] == hash_secret_token(result.refresh_token)
    assert result.refresh_token not in stored[0]
    assert str(stored[1]) == CLIENT_IP


async def test_login_writes_history_and_an_audit_entry(session: AsyncSession, company) -> None:
    account = await make_account(session, company)

    await sign_in(session, account)

    assert await attempt_rows(session, account.email) == [(True, None)]
    assert AuditAction.LOGIN in await audit_actions(session, company.id)

    last_login_at = (
        await session.execute(select(User.last_login_at).where(User.id == account.id))
    ).scalar_one()
    assert last_login_at is not None


async def test_mfa_login_requires_and_consumes_a_totp_code(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    initial = await sign_in(session, account)
    setup = await AuthService(session).setup_mfa(
        principal_for(account, initial.session_id),
        MfaSetupRequest(current_password=PASSWORD),
    )

    stored = (
        await session.execute(select(User.mfa_secret).where(User.id == account.id))
    ).scalar_one()
    assert stored != setup.secret
    assert decrypt_totp_secret(stored) == setup.secret

    current_code = pyotp.TOTP(setup.secret).now()
    enabled = await AuthService(session).confirm_mfa(
        principal_for(account, initial.session_id), MfaCodeRequest(code=current_code)
    )
    assert enabled.mfa_enabled is True

    sessions_before = await session.scalar(
        select(func.count()).select_from(UserSession).where(UserSession.user_id == account.id)
    )
    challenge = await sign_in(session, account)
    assert challenge.mfa_required is True
    assert decode_token(challenge.challenge_token, expected_type="mfa").subject == account.id
    sessions_after_password = await session.scalar(
        select(func.count()).select_from(UserSession).where(UserSession.user_id == account.id)
    )
    assert sessions_after_password == sessions_before

    # Confirmation consumed the current step. The next step is within the
    # accepted drift window and proves both replay prevention and clock skew.
    next_code = pyotp.TOTP(setup.secret).at(utcnow() + timedelta(seconds=30))
    completed = await AuthService(session).verify_mfa(
        MfaVerifyRequest(challenge_token=challenge.challenge_token, code=next_code),
        ip_address=CLIENT_IP,
        user_agent=CLIENT_AGENT,
    )
    assert decode_token(completed.access_token, expected_type="access").subject == account.id

    replay_challenge = await sign_in(session, account)
    with pytest.raises(AuthenticationError) as excinfo:
        await AuthService(session).verify_mfa(
            MfaVerifyRequest(
                challenge_token=replay_challenge.challenge_token,
                code=next_code,
            ),
            ip_address=CLIENT_IP,
            user_agent=CLIENT_AGENT,
        )
    assert excinfo.value.code == ErrorCode.MFA_INVALID
    assert (False, FAILURE_BAD_MFA) in await attempt_rows(session, account.email)


async def test_temporary_password_is_enforced_by_shared_principal_dependency(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    account.must_change_password = True
    await session.flush()
    issued = await sign_in(session, account)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=issued.access_token)

    blocked_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/products/items",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )
    with pytest.raises(PasswordChangeRequiredError) as excinfo:
        await get_current_principal(blocked_request, session, credentials)
    assert getattr(excinfo.value, "code", None) == ErrorCode.PASSWORD_CHANGE_REQUIRED

    allowed_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/me",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )
    principal = await get_current_principal(allowed_request, session, credentials)
    assert principal.must_change_password is True


async def test_unknown_address_answers_exactly_like_a_wrong_password(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)

    with pytest.raises(InvalidCredentialsError) as unknown:
        await AuthService(session).login(
            LoginRequest(email="nobody-here@atcraft-qa.com", password=PASSWORD)
        )
    with pytest.raises(InvalidCredentialsError) as wrong:
        await sign_in(session, account, "not-the-password")

    assert unknown.value.message == wrong.value.message
    assert unknown.value.code == wrong.value.code == ErrorCode.INVALID_CREDENTIALS
    # An address with no account belongs to no tenant, so there is no
    # login_attempts row it could be filed under.
    assert await attempt_rows(session, "nobody-here@atcraft-qa.com") == []


async def test_failed_sign_in_survives_the_exception(session: AsyncSession, company) -> None:
    """The counter, the attempt row and the audit entry are committed before the
    error is raised - otherwise an attacker's attempts leave no trace."""
    account = await make_account(session, company)

    with pytest.raises(InvalidCredentialsError):
        await sign_in(session, account, "wrong-password")

    failed, locked_until, _, _ = await account_state(session, account.id)
    assert failed == 1
    assert locked_until is None
    assert await attempt_rows(session, account.email) == [(False, FAILURE_BAD_PASSWORD)]
    assert AuditAction.LOGIN_FAILED in await audit_actions(session, company.id)


async def test_account_locks_after_the_configured_number_of_failures(
    session: AsyncSession, company
) -> None:
    from src.core.config import settings

    account = await make_account(session, company)

    for _ in range(settings.login_max_failed_attempts - 1):
        with pytest.raises(InvalidCredentialsError):
            await sign_in(session, account, "wrong-password")

    with pytest.raises(AccountLockedError):
        await sign_in(session, account, "wrong-password")

    failed, locked_until, _, _ = await account_state(session, account.id)
    assert failed == settings.login_max_failed_attempts
    assert locked_until is not None and locked_until > utcnow()

    # The correct password does not open a locked account, and the refusal is
    # recorded without counting against the lockout again.
    with pytest.raises(AccountLockedError):
        await sign_in(session, account)
    failed_after, _, _, _ = await account_state(session, account.id)
    assert failed_after == settings.login_max_failed_attempts


async def test_successful_sign_in_clears_the_counter(session: AsyncSession, company) -> None:
    account = await make_account(session, company)

    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            await sign_in(session, account, "wrong-password")
    await sign_in(session, account)

    failed, locked_until, _, _ = await account_state(session, account.id)
    assert failed == 0
    assert locked_until is None


async def test_deactivated_account_cannot_sign_in(session: AsyncSession, company) -> None:
    account = await make_account(session, company, is_active=False)

    with pytest.raises(InvalidCredentialsError):
        await sign_in(session, account)

    assert await attempt_rows(session, account.email) == [(False, "account_inactive")]


async def test_a_weakly_hashed_password_is_upgraded_on_sign_in(
    session: AsyncSession, company
) -> None:
    """A hash made with weaker parameters (or an inherited bcrypt one) is
    re-hashed transparently on the first successful sign-in."""
    account = await make_account(session, company)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    account.password_hash = weak
    await session.flush()

    await sign_in(session, account)

    _, _, stored_hash, changed_at = await account_state(session, account.id)
    assert stored_hash != weak
    assert verify_password(PASSWORD, stored_hash)
    # An upgrade is not a credential change: moving password_changed_at would
    # sign the user out of every other device for a maintenance detail.
    assert changed_at is None


# -----------------------------------------------------------------------------
# Refresh rotation
# -----------------------------------------------------------------------------


async def test_refresh_rotates_the_session_and_links_the_chain(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    first = await sign_in(session, account)

    second = await AuthService(session).refresh(first.refresh_token, ip_address=CLIENT_IP)

    assert second.session_id != first.session_id
    assert second.refresh_token != first.refresh_token

    revoked_at, reason, replaced_by = await session_row(session, first.session_id)
    successor_id = (
        await session.execute(
            select(UserSession.id).where(UserSession.session_id == second.session_id)
        )
    ).scalar_one()
    assert revoked_at is not None
    assert reason == "rotated"
    assert replaced_by == successor_id

    # The new token keeps working; rotation is not a sign-out.
    third = await AuthService(session).refresh(second.refresh_token)
    assert third.session_id not in {first.session_id, second.session_id}


async def test_replaying_a_spent_refresh_token_burns_the_whole_chain(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    first = await sign_in(session, account)
    second = await AuthService(session).refresh(first.refresh_token)
    third = await AuthService(session).refresh(second.refresh_token)

    with pytest.raises(AuthenticationError) as excinfo:
        await AuthService(session).refresh(first.refresh_token)
    assert excinfo.value.code == ErrorCode.TOKEN_REVOKED

    # Every generation, including the one the legitimate client is holding.
    for session_id in (first.session_id, second.session_id, third.session_id):
        revoked_at, _, _ = await session_row(session, session_id)
        assert revoked_at is not None, session_id
    assert (await session_row(session, third.session_id))[1] == REVOKE_REASON_REPLAY

    with pytest.raises(AuthenticationError):
        await AuthService(session).refresh(third.refresh_token)

    replay_audits = (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.company_id == company.id,
                AuditLog.action == AuditAction.LOGIN_FAILED,
                AuditLog.entity_type == "user_session",
            )
        )
    ).scalar_one()
    assert replay_audits == 1


async def test_two_concurrent_refreshes_of_one_token_let_exactly_one_through(engine) -> None:
    """The row lock in ``refresh`` is what makes replay detection hold up.

    Two requests present the same refresh token at the same moment - two tabs
    whose refresh guards cannot see each other, or an attacker replaying
    alongside the victim. Without the lock both read ACTIVE off an unlocked
    snapshot and both rotate: two live successors and no replay recorded. With
    it, the second waits, reads the rotation the first committed, and burns the
    family - successor included, which is the token the legitimate client is
    now holding.

    Two sessions on two connections, committing for real: two transactions
    cannot contend for a row lock inside one outer transaction, so the shared
    ``session`` fixture cannot express this. The rows are removed afterwards.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    company_id = uuid.uuid4()
    async with factory() as setup, setup.begin():
        company = await seed_support.seed_company(setup, company_id=company_id, prefix="AUTHC")
        email = (await make_account(setup, company)).email

    try:
        async with factory() as signer:
            issued = await AuthService(signer).login(
                LoginRequest(email=email, password=PASSWORD),
                ip_address=CLIENT_IP,
                user_agent=CLIENT_AGENT,
            )

        async def present_the_token() -> TokenPair | AuthenticationError:
            async with factory() as contender:
                try:
                    return await AuthService(contender).refresh(issued.refresh_token)
                except AuthenticationError as exc:
                    return exc

        outcomes = await asyncio.gather(present_the_token(), present_the_token())
        rotated = [outcome for outcome in outcomes if isinstance(outcome, TokenPair)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, AuthenticationError)]
        assert len(rotated) == 1 and len(refused) == 1, outcomes
        assert refused[0].code == ErrorCode.TOKEN_REVOKED
        successor = rotated[0].session_id

        async with factory() as reader:
            rows = (
                await reader.execute(
                    select(
                        UserSession.session_id, UserSession.revoked_at, UserSession.revoked_reason
                    ).where(UserSession.company_id == company_id)
                )
            ).all()
            replay_audits = await reader.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.company_id == company_id,
                    AuditLog.action == AuditAction.LOGIN_FAILED,
                    AuditLog.entity_type == "user_session",
                )
            )
        chain = {session_id: (revoked_at, reason) for session_id, revoked_at, reason in rows}
        assert set(chain) == {issued.session_id, successor}
        # Every generation is closed. The successor carries the replay reason;
        # the root was already closed by the rotation itself, and revoke_chain
        # leaves an already-revoked row as it found it.
        assert chain[successor][0] is not None
        assert chain[successor][1] == REVOKE_REASON_REPLAY
        assert chain[issued.session_id][0] is not None
        assert chain[issued.session_id][1] == REVOKE_REASON_ROTATED
        assert replay_audits == 1

        async with factory() as later:
            with pytest.raises(AuthenticationError) as excinfo:
                await AuthService(later).refresh(rotated[0].refresh_token)
            assert excinfo.value.code == ErrorCode.TOKEN_REVOKED
    finally:
        async with factory() as janitor, janitor.begin():
            params = {"company_id": company_id}
            await janitor.execute(
                text("DELETE FROM user_sessions WHERE company_id = :company_id"), params
            )
            await janitor.execute(
                text("DELETE FROM login_attempts WHERE company_id = :company_id"), params
            )
            await janitor.execute(
                text("DELETE FROM audit_logs WHERE company_id = :company_id"), params
            )
            await janitor.execute(text("DELETE FROM users WHERE company_id = :company_id"), params)
            await janitor.execute(text("DELETE FROM companies WHERE id = :company_id"), params)


async def test_refresh_is_refused_once_the_session_is_revoked(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    issued = await sign_in(session, account)

    await AuthService(session).logout(principal_for(account, issued.session_id))

    with pytest.raises(AuthenticationError) as excinfo:
        await AuthService(session).refresh(issued.refresh_token)
    assert excinfo.value.code == ErrorCode.TOKEN_REVOKED


async def test_an_access_token_cannot_be_used_to_refresh(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    issued = await sign_in(session, account)

    with pytest.raises(AuthenticationError) as excinfo:
        await AuthService(session).refresh(issued.access_token)
    assert excinfo.value.code == ErrorCode.TOKEN_INVALID


# -----------------------------------------------------------------------------
# Sign out and session management
# -----------------------------------------------------------------------------


async def test_logout_revokes_only_the_current_session(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    laptop = await sign_in(session, account)
    tablet = await sign_in(session, account)

    result = await AuthService(session).logout(principal_for(account, laptop.session_id))

    assert result.revoked_sessions == 1
    assert (await session_row(session, laptop.session_id))[0] is not None
    assert (await session_row(session, tablet.session_id))[0] is None
    assert AuditAction.LOGOUT in await audit_actions(session, company.id)


async def test_logout_is_idempotent(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    issued = await sign_in(session, account)
    principal = principal_for(account, issued.session_id)

    assert (await AuthService(session).logout(principal)).revoked_sessions == 1
    assert (await AuthService(session).logout(principal)).revoked_sessions == 0


async def test_logout_all_closes_every_session(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    issued = [await sign_in(session, account) for _ in range(3)]

    result = await AuthService(session).logout_all(principal_for(account, issued[0].session_id))

    assert result.revoked_sessions == 3
    for token in issued:
        assert (await session_row(session, token.session_id))[0] is not None


async def test_session_list_shows_only_live_sessions_and_marks_the_current_one(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    first = await sign_in(session, account)
    second = await sign_in(session, account)
    await AuthService(session).logout(principal_for(account, first.session_id))

    items, pagination = await AuthService(session).list_sessions(
        principal_for(account, second.session_id), PageParams(page=1, limit=50)
    )

    assert [item.session_id for item in items] == [second.session_id]
    assert items[0].is_current is True
    assert pagination.total == 1


async def test_a_user_cannot_revoke_a_session_they_do_not_own(
    session: AsyncSession, company
) -> None:
    owner = await make_account(session, company)
    other = await make_account(session, company)
    issued = await sign_in(session, owner)
    row_id = (
        await session.execute(
            select(UserSession.id).where(UserSession.session_id == issued.session_id)
        )
    ).scalar_one()

    with pytest.raises(NotFoundError):
        await AuthService(session).revoke_session(principal_for(other), row_id)

    assert (await session_row(session, issued.session_id))[0] is None


async def test_sessions_are_invisible_across_tenants(
    session: AsyncSession, company, other_company
) -> None:
    """Tenant isolation on the session table, not just on business data."""
    outsider = await make_account(session, other_company)
    insider = await make_account(session, company)
    issued = await sign_in(session, insider)
    row_id = (
        await session.execute(
            select(UserSession.id).where(UserSession.session_id == issued.session_id)
        )
    ).scalar_one()

    with pytest.raises(NotFoundError):
        await AuthService(session).revoke_session(principal_for(outsider), row_id)

    items, _ = await AuthService(session).list_sessions(
        principal_for(outsider), PageParams(page=1, limit=50)
    )
    assert items == []


# -----------------------------------------------------------------------------
# Password change
# -----------------------------------------------------------------------------


async def test_change_password_ends_the_other_sessions_only(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    current = await sign_in(session, account)
    other = await sign_in(session, account)

    result = await AuthService(session).change_password(
        principal_for(account, current.session_id),
        ChangePasswordRequest(current_password=PASSWORD, new_password=NEW_PASSWORD),
    )

    assert result.revoked_sessions == 1
    assert (await session_row(session, other.session_id))[0] is not None
    assert (await session_row(session, current.session_id))[0] is None

    _, _, stored_hash, changed_at = await account_state(session, account.id)
    assert verify_password(NEW_PASSWORD, stored_hash)
    assert changed_at is not None
    assert AuditAction.PASSWORD_CHANGED in await audit_actions(session, company.id)

    with pytest.raises(InvalidCredentialsError):
        await sign_in(session, account, PASSWORD)


async def test_change_password_requires_the_current_password(
    session: AsyncSession, company
) -> None:
    """A wrong current password is a field error, not a dead session.

    The web client signs the user out on any 401 from an authenticated route,
    so the refusal is a 422 under the field - and it costs exactly what a wrong
    password costs at sign-in: an attempt row, a counter tick and an audit
    entry, all committed before the error is raised.
    """
    account = await make_account(session, company)
    issued = await sign_in(session, account)

    with pytest.raises(ValidationError) as excinfo:
        await AuthService(session).change_password(
            principal_for(account, issued.session_id),
            ChangePasswordRequest(current_password="not-my-password", new_password=NEW_PASSWORD),
        )
    assert excinfo.value.code == ErrorCode.INVALID_CREDENTIALS
    assert excinfo.value.status_code == 422
    assert excinfo.value.details == {"fields": {"current_password": [CURRENT_PASSWORD_INCORRECT]}}

    failed, locked_until, stored_hash, _ = await account_state(session, account.id)
    assert verify_password(PASSWORD, stored_hash)
    assert failed == 1
    assert locked_until is None
    assert (False, FAILURE_BAD_PASSWORD) in await attempt_rows(session, account.email)
    assert await refusal_summaries(session, account) == [
        f"Password change refused for {account.email}: current password did not match"
    ]


async def test_change_password_enforces_the_policy(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    issued = await sign_in(session, account)

    with pytest.raises(ValidationError) as excinfo:
        await AuthService(session).change_password(
            principal_for(account, issued.session_id),
            ChangePasswordRequest(current_password=PASSWORD, new_password="short1"),
        )
    assert excinfo.value.code == ErrorCode.PASSWORD_POLICY


async def test_change_password_refuses_a_reworded_identical_password(
    session: AsyncSession, company
) -> None:
    """The schema catches a byte-identical resubmission; this covers the service
    check that still runs when a client bypasses the edge."""
    account = await make_account(session, company)
    issued = await sign_in(session, account)
    payload = ChangePasswordRequest(current_password=PASSWORD, new_password=NEW_PASSWORD)
    object.__setattr__(payload, "new_password", PASSWORD)

    with pytest.raises(BusinessRuleError):
        await AuthService(session).change_password(
            principal_for(account, issued.session_id), payload
        )


async def test_repeated_wrong_current_passwords_lock_the_account(
    session: AsyncSession, company
) -> None:
    """A stolen access token must not be a free password oracle: the self-service
    routes draw on the same per-account budget as sign-in."""
    from src.core.config import settings

    account = await make_account(session, company)
    issued = await sign_in(session, account)
    principal = principal_for(account, issued.session_id)
    guess = ChangePasswordRequest(current_password="not-my-password", new_password=NEW_PASSWORD)

    for _ in range(settings.login_max_failed_attempts - 1):
        with pytest.raises(ValidationError):
            await AuthService(session).change_password(principal, guess)
    with pytest.raises(AccountLockedError):
        await AuthService(session).change_password(principal, guess)

    failed, locked_until, stored_hash, _ = await account_state(session, account.id)
    assert failed == settings.login_max_failed_attempts
    assert locked_until is not None and locked_until > utcnow()
    assert verify_password(PASSWORD, stored_hash)


async def test_mfa_enrolment_and_disable_require_the_current_password(
    session: AsyncSession, company
) -> None:
    """Binding or unbinding an authenticator re-proves the password, and a wrong
    one is refused the same way a wrong one at password change is."""
    account = await make_account(session, company)
    issued = await sign_in(session, account)
    principal = principal_for(account, issued.session_id)

    with pytest.raises(ValidationError) as refused_setup:
        await AuthService(session).setup_mfa(
            principal, MfaSetupRequest(current_password="not-my-password")
        )
    assert refused_setup.value.code == ErrorCode.INVALID_CREDENTIALS
    assert refused_setup.value.status_code == 422
    assert refused_setup.value.details == {
        "fields": {"current_password": [CURRENT_PASSWORD_INCORRECT]}
    }

    secret = await enable_mfa(session, principal)
    with pytest.raises(ValidationError) as refused_disable:
        await AuthService(session).disable_mfa(
            principal,
            MfaDisableRequest(current_password="not-my-password", code=pyotp.TOTP(secret).now()),
        )
    assert refused_disable.value.code == ErrorCode.INVALID_CREDENTIALS
    assert refused_disable.value.status_code == 422
    assert refused_disable.value.details == {
        "fields": {"current_password": [CURRENT_PASSWORD_INCORRECT]}
    }

    failed, _, _, _ = await account_state(session, account.id)
    assert failed == 2
    assert await mfa_enabled(session, account.id) is True
    assert await refusal_summaries(session, account) == [
        f"MFA enrolment refused for {account.email}: current password did not match",
        f"MFA disable refused for {account.email}: current password did not match",
    ]


async def test_a_wrong_authenticator_code_is_a_field_error(session: AsyncSession, company) -> None:
    """Confirming or disabling MFA with a code the authenticator did not produce
    answers 422 under the ``code`` field - a 401 here would sign the user out
    of the very page that is trying to show them the mistake."""
    account = await make_account(session, company)
    issued = await sign_in(session, account)
    principal = principal_for(account, issued.session_id)
    setup = await AuthService(session).setup_mfa(
        principal, MfaSetupRequest(current_password=PASSWORD)
    )

    with pytest.raises(ValidationError) as refused_confirm:
        await AuthService(session).confirm_mfa(
            principal, MfaCodeRequest(code=wrong_code_for(setup.secret))
        )
    assert refused_confirm.value.code == ErrorCode.MFA_INVALID
    assert refused_confirm.value.status_code == 422
    assert list(refused_confirm.value.details["fields"]) == ["code"]
    assert await mfa_enabled(session, account.id) is False

    await AuthService(session).confirm_mfa(
        principal, MfaCodeRequest(code=pyotp.TOTP(setup.secret).now())
    )
    assert await mfa_enabled(session, account.id) is True

    with pytest.raises(ValidationError) as refused_disable:
        await AuthService(session).disable_mfa(
            principal,
            MfaDisableRequest(current_password=PASSWORD, code=wrong_code_for(setup.secret)),
        )
    assert refused_disable.value.code == ErrorCode.MFA_INVALID
    assert refused_disable.value.status_code == 422
    assert list(refused_disable.value.details["fields"]) == ["code"]
    assert await mfa_enabled(session, account.id) is True


# -----------------------------------------------------------------------------
# Password reset
# -----------------------------------------------------------------------------


async def test_password_reset_round_trip_ends_every_session(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    live = await sign_in(session, account)

    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email), ip_address=CLIENT_IP
    )
    assert grant is not None

    stored_hash = (
        await session.execute(
            select(PasswordResetToken.token_hash).where(PasswordResetToken.user_id == account.id)
        )
    ).scalar_one()
    # Only the digest is persisted, so a leaked table hands over no live links.
    assert stored_hash == hash_secret_token(grant.token)
    assert stored_hash != grant.token

    await AuthService(session).confirm_password_reset(
        PasswordResetConfirmPayload(token=grant.token, new_password=NEW_PASSWORD)
    )

    _, _, new_hash, changed_at = await account_state(session, account.id)
    assert verify_password(NEW_PASSWORD, new_hash)
    assert changed_at is not None
    assert (await session_row(session, live.session_id))[0] is not None

    actions = await audit_actions(session, company.id)
    assert AuditAction.PASSWORD_RESET_REQUESTED in actions
    assert AuditAction.PASSWORD_CHANGED in actions


async def test_a_reset_token_works_once(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email)
    )
    assert grant is not None

    await AuthService(session).confirm_password_reset(
        PasswordResetConfirmPayload(token=grant.token, new_password=NEW_PASSWORD)
    )
    with pytest.raises(AuthenticationError) as excinfo:
        await AuthService(session).confirm_password_reset(
            PasswordResetConfirmPayload(token=grant.token, new_password="Third-Passphrase!9")
        )
    assert excinfo.value.code == ErrorCode.TOKEN_INVALID


async def test_requesting_a_new_link_invalidates_the_previous_one(
    session: AsyncSession, company
) -> None:
    """Otherwise a request made by somebody else leaves a working link behind
    that the account holder has no way to cancel."""
    account = await make_account(session, company)
    service = AuthService(session)
    first = await service.request_password_reset(PasswordResetRequestPayload(email=account.email))
    second = await service.request_password_reset(PasswordResetRequestPayload(email=account.email))
    assert first is not None and second is not None

    with pytest.raises(AuthenticationError):
        await service.confirm_password_reset(
            PasswordResetConfirmPayload(token=first.token, new_password=NEW_PASSWORD)
        )
    await service.confirm_password_reset(
        PasswordResetConfirmPayload(token=second.token, new_password=NEW_PASSWORD)
    )


async def test_an_expired_reset_token_is_refused(session: AsyncSession, company) -> None:
    account = await make_account(session, company)
    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email)
    )
    assert grant is not None

    record = (
        await session.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == account.id)
        )
    ).scalar_one()
    record.expires_at = utcnow() - timedelta(minutes=1)
    await session.flush()

    with pytest.raises(AuthenticationError):
        await AuthService(session).confirm_password_reset(
            PasswordResetConfirmPayload(token=grant.token, new_password=NEW_PASSWORD)
        )


async def test_reset_request_for_an_unknown_address_writes_nothing(
    session: AsyncSession, company
) -> None:
    before = (
        await session.execute(select(func.count()).select_from(PasswordResetToken))
    ).scalar_one()

    result = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email="nobody-at-all@atcraft-qa.com")
    )

    after = (
        await session.execute(select(func.count()).select_from(PasswordResetToken))
    ).scalar_one()
    assert result is None
    assert after == before


async def test_reset_clears_a_lockout(session: AsyncSession, company) -> None:
    """Leaving the account locked after a successful reset would hand an
    attacker a denial of service through failed sign-ins alone."""
    from src.core.config import settings

    account = await make_account(session, company)
    for _ in range(settings.login_max_failed_attempts - 1):
        with pytest.raises(InvalidCredentialsError):
            await sign_in(session, account, "wrong-password")
    with pytest.raises(AccountLockedError):
        await sign_in(session, account, "wrong-password")

    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email)
    )
    assert grant is not None
    await AuthService(session).confirm_password_reset(
        PasswordResetConfirmPayload(token=grant.token, new_password=NEW_PASSWORD)
    )

    failed, locked_until, _, _ = await account_state(session, account.id)
    assert failed == 0
    assert locked_until is None
    signed_in = await sign_in(session, account, NEW_PASSWORD)
    assert signed_in.session_id


async def test_reset_recovers_an_account_with_a_lost_authenticator(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    account.mfa_enabled = True
    account.mfa_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    account.mfa_last_used_step = 123
    await session.flush()

    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email)
    )
    assert grant is not None
    await AuthService(session).confirm_password_reset(
        PasswordResetConfirmPayload(token=grant.token, new_password=NEW_PASSWORD)
    )

    mfa_state = (
        await session.execute(
            select(User.mfa_enabled, User.mfa_secret, User.mfa_last_used_step).where(
                User.id == account.id
            )
        )
    ).one()
    assert mfa_state == (False, None, None)


async def test_reset_stages_the_link_for_delivery_inside_the_commit(
    session: AsyncSession,
    company,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config import EmailProvider, settings
    from src.modules.auth import service as auth_service_module

    account = await make_account(session, company)
    staged: list[dict[str, object]] = []
    monkeypatch.setattr(settings, "email_provider", EmailProvider.smtp)
    monkeypatch.setattr(
        auth_service_module,
        "queue_password_reset_email",
        lambda _session, **payload: staged.append(payload),
    )

    grant = await AuthService(session).request_password_reset(
        PasswordResetRequestPayload(email=account.email)
    )

    assert grant is not None
    assert len(staged) == 1
    assert staged[0]["company_id"] == company.id
    reset_id = staged[0]["reset_token_id"]
    assert isinstance(reset_id, uuid.UUID)

    stored = await session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.id == reset_id)
    )
    assert stored is not None
    assert stored.email_token_ciphertext is not None
    assert grant.token not in stored.email_token_ciphertext
    assert decrypt_password_reset_delivery_token(stored.email_token_ciphertext) == grant.token


# -----------------------------------------------------------------------------
# Current principal
# -----------------------------------------------------------------------------


async def test_me_returns_the_permissions_resolved_for_this_request(
    session: AsyncSession, company
) -> None:
    account = await make_account(session, company)
    issued = await sign_in(session, account)
    principal = Principal(
        user_id=account.id,
        company_id=company.id,
        email=account.email,
        full_name=account.full_name,
        is_superuser=False,
        permissions=frozenset({"item.view", "bom.view"}),
        session_id=issued.session_id,
        role_codes=("PLANNER",),
    )

    profile = await AuthService(session).me(principal)

    assert profile.permissions == ["bom.view", "item.view"]
    assert profile.roles == ["PLANNER"]
    assert profile.session_id == issued.session_id
    assert profile.last_login_at is not None
    assert profile.must_change_password is False
