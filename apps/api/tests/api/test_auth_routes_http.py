"""Authentication routes over HTTP, signed in for real.

``api_client`` swaps the principal dependency for a fixture. That is the right
tool for a route gated on a permission and the wrong one here: what these
routes own *is* the credential - the token a sign-in issues, the cookie a
refresh rotates, the session a logout ends. So the ``client`` below overrides
the database session and nothing else. Every test signs in through
``POST /auth/login`` and presents the token it was handed, which makes "this
session" a row the request path really read.

The client keeps no cookie jar. A browser holds the refresh and CSRF cookies;
here a test hands them over explicitly (:meth:`SignedIn.browser`), so a cookie
never rides along on a request it was not meant for - and the logout routes
behave differently when one is present.

The rules behind these routes - lockout arithmetic, replay detection, the reset
link's lifetime - are pinned at service level in
``tests/integration/test_auth_service.py``. This file asserts what the routes
add: status codes, the envelope, cookies, field errors and the rows each
request leaves behind. State is read with column-level SELECTs, because several
operations write through ``UPDATE ... RETURNING`` with
``synchronize_session=False`` and an ORM instance still in the identity map can
show its pre-request values.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.cookies import Morsel, SimpleCookie
from typing import Any

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import EmailProvider, settings
from src.core.messaging import MessagingOutcome, MessagingResult
from src.core.security import (
    MFA_CHALLENGE_TTL_MINUTES,
    TOTP_PERIOD_SECONDS,
    decode_token,
    decrypt_password_reset_delivery_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_totp_secret,
    hash_password,
    hash_secret_token,
    verify_password,
)
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.auth import phone_service
from src.modules.auth import service as auth_service
from src.modules.auth.cookies import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    REFRESH_COOKIE_NAME,
    csrf_token_for,
)
from src.modules.auth.phone_service import PHONE_CODE_LIFETIME
from src.modules.auth.service import (
    FAILURE_BAD_MFA,
    FAILURE_BAD_PASSWORD,
    FAILURE_LOCKED,
    REVOKE_REASON_LOCKED,
    REVOKE_REASON_LOGOUT,
    REVOKE_REASON_LOGOUT_ALL,
    REVOKE_REASON_PASSWORD_CHANGE,
    REVOKE_REASON_PASSWORD_RESET,
    REVOKE_REASON_REPLAY,
    REVOKE_REASON_ROTATED,
)
from src.modules.users.models import (
    LoginAttempt,
    PasswordResetToken,
    User,
    UserPhoneVerificationChallenge,
    UserSession,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

PASSWORD = "Sh0p-Floor-Passphrase!"
NEW_PASSWORD = "An0ther-Good-Passphrase!"
WRONG_PASSWORD = "Not-The-Passphrase!7"
MOBILE = "+919876543210"


# -----------------------------------------------------------------------------
# Client, accounts and the browser's half of a session
# -----------------------------------------------------------------------------


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real authentication chain over the test's session.

    Only ``get_db`` is overridden: the bearer token is decoded, the session row
    checked and the principal loaded exactly as in production. A response hook
    empties the cookie jar after every exchange - see the module docstring.
    """
    from src.core.database import get_db
    from src.main import app

    app.dependency_overrides[get_db] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as http:

        async def forget_cookies(_response: Response) -> None:
            http.cookies.clear()

        http.event_hooks = {"request": [], "response": [forget_cookies]}
        yield http
    app.dependency_overrides.clear()


@dataclass(frozen=True, slots=True)
class Account:
    """Plain values, so no test reads an ORM instance a failed request expired."""

    id: uuid.UUID
    company_id: uuid.UUID
    email: str


async def make_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    password: str = PASSWORD,
    email: str | None = None,
    **overrides: Any,
) -> Account:
    record = await seed_support.seed_user(
        session,
        company_id=company_id,
        email=email or f"auth-{uuid.uuid4().hex[:10]}@atcraft-qa.com",
        full_name="Shop Floor User",
        **overrides,
    )
    # seed_support writes a placeholder hash; these accounts really sign in.
    record.password_hash = hash_password(password)
    await session.flush()
    return Account(record.id, record.company_id, record.email)


async def make_mfa_account(session: AsyncSession, company_id: uuid.UUID) -> tuple[Account, str]:
    """An account already enrolled in MFA, and the seed its authenticator holds."""
    secret = generate_totp_secret()
    account = await make_account(
        session, company_id, mfa_enabled=True, mfa_secret=encrypt_totp_secret(secret)
    )
    return account, secret


def credentials(account: Account, password: str = PASSWORD) -> dict[str, str]:
    return {"email": account.email, "password": password}


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


@dataclass(frozen=True, slots=True)
class SignedIn:
    """What a browser holds after a sign-in: a token in memory, two cookies."""

    session_id: str
    access_token: str
    refresh_token: str
    csrf_token: str

    @property
    def bearer(self) -> dict[str, str]:
        return bearer(self.access_token)

    def browser(self, *, with_csrf_header: bool = True) -> dict[str, str]:
        """The cookies the browser would send, and the header its script echoes."""
        headers = {
            "Cookie": (
                f"{REFRESH_COOKIE_NAME}={self.refresh_token}; {CSRF_COOKIE_NAME}={self.csrf_token}"
            )
        }
        if with_csrf_header:
            headers[CSRF_HEADER_NAME] = self.csrf_token
        return headers


def cookies_set_by(response: Response) -> dict[str, Morsel[str]]:
    """Parse ``Set-Cookie`` directly, independent of any client-side jar policy."""
    jar: dict[str, Morsel[str]] = {}
    for header in response.headers.get_list("set-cookie"):
        parsed: SimpleCookie = SimpleCookie()
        parsed.load(header)
        jar.update(parsed)
    return jar


def signed_in(response: Response) -> SignedIn:
    data = data_of(response)
    cookies = cookies_set_by(response)
    return SignedIn(
        session_id=data["session_id"],
        access_token=data["access_token"],
        refresh_token=cookies[REFRESH_COOKIE_NAME].value,
        csrf_token=cookies[CSRF_COOKIE_NAME].value,
    )


async def sign_in(client: AsyncClient, account: Account, password: str = PASSWORD) -> SignedIn:
    return signed_in(await client.post("/auth/login", json=credentials(account, password)))


async def sign_in_with_mfa(
    client: AsyncClient, account: Account, secret: str, *, code: str | None = None
) -> SignedIn:
    challenge = data_of(await client.post("/auth/login", json=credentials(account)))
    assert challenge["mfa_required"] is True
    response = await client.post(
        "/auth/mfa/verify",
        json={
            "challenge_token": challenge["challenge_token"],
            "code": code or pyotp.TOTP(secret).now(),
        },
    )
    return signed_in(response)


def next_code(secret: str) -> str:
    """The authenticator's code for the next 30-second step - inside the drift
    window, and later than any step a code generated now can have consumed."""
    return pyotp.TOTP(secret).at(utcnow() + timedelta(seconds=TOTP_PERIOD_SECONDS))


def wrong_code_for(secret: str) -> str:
    """A six-digit code the authenticator would not produce anywhere near now.

    Picked against the live window rather than hard-coded: a fixed "000000" is
    the current code once in a million runs.
    """
    totp = pyotp.TOTP(secret)
    now = utcnow()
    live = {totp.at(now + timedelta(seconds=offset)) for offset in (-60, -30, 0, 30, 60)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in live)


# -----------------------------------------------------------------------------
# Envelope and row readers
# -----------------------------------------------------------------------------


def data_of(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def error_of(response: Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["message"], body
    return body["error"]


async def account_row(session: AsyncSession, account_id: uuid.UUID) -> Row[Any]:
    stmt = select(
        User.password_hash,
        User.password_changed_at,
        User.must_change_password,
        User.failed_login_attempts,
        User.mfa_enabled,
        User.mfa_secret,
        User.mfa_last_used_step,
        User.phone_e164,
        User.phone_verified_at,
        User.sms_opt_in_at,
        User.whatsapp_opt_in_at,
    ).where(User.id == account_id)
    return (await session.execute(stmt)).one()


async def session_row(session: AsyncSession, session_id: str) -> Row[Any]:
    stmt = select(
        UserSession.id,
        UserSession.revoked_at,
        UserSession.revoked_reason,
        UserSession.replaced_by_id,
    ).where(UserSession.session_id == session_id)
    return (await session.execute(stmt)).one()


async def session_count(session: AsyncSession, account_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(UserSession).where(UserSession.user_id == account_id)
    return int((await session.execute(stmt)).scalar_one())


async def attempts(session: AsyncSession, account_id: uuid.UUID) -> list[tuple[bool, str | None]]:
    stmt = (
        select(LoginAttempt.succeeded, LoginAttempt.failure_reason)
        .where(LoginAttempt.user_id == account_id)
        .order_by(LoginAttempt.created_at, LoginAttempt.id)
    )
    return [(row.succeeded, row.failure_reason) for row in (await session.execute(stmt)).all()]


async def audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_id: uuid.UUID, action: AuditAction
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.context,
        )
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_id == entity_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).all())


async def challenge_row(session: AsyncSession, account_id: uuid.UUID) -> Row[Any] | None:
    stmt = select(
        UserPhoneVerificationChallenge.phone_e164,
        UserPhoneVerificationChallenge.code_digest,
        UserPhoneVerificationChallenge.attempt_count,
        UserPhoneVerificationChallenge.send_count,
        UserPhoneVerificationChallenge.consumed_at,
    ).where(UserPhoneVerificationChallenge.user_id == account_id)
    return (await session.execute(stmt)).one_or_none()


async def grant_rows(session: AsyncSession, account_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(
            PasswordResetToken.used_at,
            PasswordResetToken.expires_at,
            PasswordResetToken.email_token_ciphertext,
        )
        .where(PasswordResetToken.user_id == account_id)
        .order_by(PasswordResetToken.created_at, PasswordResetToken.id)
    )
    return list((await session.execute(stmt)).all())


async def seed_session(session: AsyncSession, account: Account) -> str:
    """Another device's live session, built through the ORM."""
    session_id = secrets.token_urlsafe(24)
    session.add(
        UserSession(
            company_id=account.company_id,
            user_id=account.id,
            session_id=session_id,
            refresh_token_hash=hash_secret_token(secrets.token_urlsafe(32)),
            refresh_jti=uuid.uuid4().hex,
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    await session.flush()
    return session_id


# -----------------------------------------------------------------------------
# Outbound providers
# -----------------------------------------------------------------------------


@dataclass
class SmsGateway:
    """Stands in for the SMS provider factory and records what it was asked to send."""

    sent: list[tuple[str, str, list[str]]] = field(default_factory=list)
    outcome: MessagingOutcome = MessagingOutcome.DELIVERED

    def __call__(self) -> SmsGateway:
        return self

    async def send_template(
        self, phone: str, template_id: str, values: list[str]
    ) -> MessagingResult:
        self.sent.append((phone, template_id, list(values)))
        return MessagingResult(self.outcome)

    @property
    def last_code(self) -> str:
        return self.sent[-1][2][0]


@pytest.fixture
def sms(monkeypatch: pytest.MonkeyPatch) -> SmsGateway:
    """A configured SMS gateway that never leaves the process."""
    gateway = SmsGateway()
    monkeypatch.setattr(phone_service, "sms_provider", gateway)
    monkeypatch.setattr(settings, "sms_auth_key", SecretStr("test-auth-key"))
    monkeypatch.setattr(settings, "sms_sender_id", "ATCRFT")
    monkeypatch.setattr(settings, "sms_dlt_entity_id", "dlt-entity")
    monkeypatch.setattr(settings, "sms_verification_template_id", "dlt-otp")
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr("test-hmac-key"))
    return gateway


@pytest.fixture
def reset_mail(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Mail delivery switched on, with the Celery hand-off replaced by a list.

    With delivery on, the service keeps an encrypted copy of the link for the
    mail worker. Decrypting it the way that worker does is how a test obtains
    the plaintext the route rightly never returns.
    """
    staged: list[dict[str, Any]] = []
    monkeypatch.setattr(settings, "email_provider", EmailProvider.smtp)
    monkeypatch.setattr(
        auth_service,
        "queue_password_reset_email",
        lambda _session, **payload: staged.append(payload),
    )
    return staged


async def mailed_link(
    client: AsyncClient, session: AsyncSession, staged: list[dict[str, Any]], email: str
) -> str:
    """Request a reset over HTTP and read the link the mail worker would send."""
    data_of(await client.post("/auth/password-reset/request", json={"email": email}))
    reset_id = staged[-1]["reset_token_id"]
    ciphertext = await session.scalar(
        select(PasswordResetToken.email_token_ciphertext).where(PasswordResetToken.id == reset_id)
    )
    assert ciphertext is not None
    return decrypt_password_reset_delivery_token(ciphertext)


# -----------------------------------------------------------------------------
# POST /auth/login
# -----------------------------------------------------------------------------


class TestLogin:
    async def test_a_sign_in_issues_a_token_and_puts_the_refresh_token_in_a_cookie(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)

        response = await client.post(
            "/auth/login",
            json={
                "email": account.email.upper(),
                "password": PASSWORD,
                "device_label": "  Stores   PC ",
            },
        )

        data = data_of(response)
        assert data["mfa_required"] is False
        assert data["token_type"] == "bearer"
        assert data["user"]["id"] == str(account.id)
        assert data["user"]["company_id"] == str(account.company_id)
        assert data["user"]["email"] == account.email
        assert (
            decode_token(data["access_token"], expected_type="access").session_id
            == data["session_id"]
        )
        cookies = cookies_set_by(response)
        refresh = cookies[REFRESH_COOKIE_NAME]
        # The long-lived credential is a cookie script cannot read - never JSON.
        assert "refresh_token" not in data
        assert refresh.value not in response.text
        assert refresh["httponly"] is True
        assert refresh["path"] == f"{settings.api_v1_prefix.rstrip('/')}/auth"
        assert cookies[CSRF_COOKIE_NAME].value == csrf_token_for(refresh.value)

        stored = (
            await session.execute(
                select(
                    UserSession.refresh_token_hash,
                    UserSession.device_label,
                    UserSession.revoked_at,
                ).where(UserSession.session_id == data["session_id"])
            )
        ).one()
        assert stored.refresh_token_hash == hash_secret_token(refresh.value)
        assert stored.device_label == "Stores PC"
        assert stored.revoked_at is None
        assert await attempts(session, account.id) == [(True, None)]
        (audit,) = await audit_rows(session, account.company_id, account.id, AuditAction.LOGIN)
        assert audit.created_by_id == account.id
        assert audit.context["session_id"] == data["session_id"]

        me = data_of(await client.get("/auth/me", headers=bearer(data["access_token"])))
        assert me["user_id"] == str(account.id)
        assert me["session_id"] == data["session_id"]

    async def test_a_wrong_password_is_a_401_that_still_leaves_its_evidence(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)

        response = await client.post("/auth/login", json=credentials(account, WRONG_PASSWORD))

        assert error_of(response, 401)["code"] == "INVALID_CREDENTIALS"
        assert cookies_set_by(response) == {}
        assert (await account_row(session, account.id)).failed_login_attempts == 1
        assert await attempts(session, account.id) == [(False, FAILURE_BAD_PASSWORD)]
        assert await audit_rows(session, account.company_id, account.id, AuditAction.LOGIN_FAILED)
        assert await session_count(session, account.id) == 0

    async def test_a_locked_account_is_refused_with_its_own_code(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(
            session, company.id, locked_until=utcnow() + timedelta(minutes=15)
        )

        response = await client.post("/auth/login", json=credentials(account))

        assert error_of(response, 401)["code"] == "ACCOUNT_LOCKED"
        assert await session_count(session, account.id) == 0

    async def test_a_lockout_refuses_every_password_but_keeps_the_devices_already_signed_in(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        """Anyone who knows an address can lock it. That must stop them guessing
        and nothing more: the owner's devices already proved the password, and
        signing them out on a stranger's say-so was a denial of service anyone
        could run with five requests (docs/assumptions.md §AUTH-1)."""
        account = await make_account(session, company.id)
        device = await sign_in(client, account)

        for _ in range(settings.login_max_failed_attempts - 1):
            guess = await client.post("/auth/login", json=credentials(account, WRONG_PASSWORD))
            assert error_of(guess, 401)["code"] == "INVALID_CREDENTIALS"
        locking = await client.post("/auth/login", json=credentials(account, WRONG_PASSWORD))
        assert error_of(locking, 401)["code"] == "ACCOUNT_LOCKED"

        # The right password does not open it while the lock runs.
        right = await client.post("/auth/login", json=credentials(account))
        assert error_of(right, 401)["code"] == "ACCOUNT_LOCKED"
        assert await session_count(session, account.id) == 1

        # The device that signed in before the lock keeps working, and rotating.
        me = data_of(await client.get("/auth/me", headers=device.bearer))
        assert me["session_id"] == device.session_id
        rotated = data_of(await client.post("/auth/refresh", headers=device.browser()))
        assert (await session_row(session, device.session_id)).revoked_reason == (
            REVOKE_REASON_ROTATED
        )
        again = data_of(await client.get("/auth/me", headers=bearer(rotated["access_token"])))
        assert again["session_id"] == rotated["session_id"]

    async def test_a_malformed_sign_in_names_each_field(self, client: AsyncClient) -> None:
        response = await client.post(
            "/auth/login", json={"email": "not-an-address", "password": ""}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"email", "password"} <= set(error["details"]["fields"])

    async def test_an_mfa_account_is_answered_with_a_challenge_and_no_session(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, _secret = await make_mfa_account(session, company.id)

        response = await client.post("/auth/login", json=credentials(account))

        data = data_of(response)
        assert data["mfa_required"] is True
        assert data["expires_in"] == MFA_CHALLENGE_TTL_MINUTES * 60
        assert decode_token(data["challenge_token"], expected_type="mfa").subject == account.id
        assert "access_token" not in data
        assert cookies_set_by(response) == {}
        assert await session_count(session, account.id) == 0

    async def test_an_address_held_in_two_companies_signs_in_only_where_it_is_named(
        self, client: AsyncClient, session: AsyncSession, company, other_company
    ) -> None:
        company_id, company_code = company.id, company.code
        email = f"shared-{uuid.uuid4().hex[:10]}@atcraft-qa.com"
        ours = await make_account(session, company_id, email=email)
        await make_account(session, other_company.id, email=email, password=NEW_PASSWORD)

        # Guessing a tenant would sign somebody into the wrong company's data.
        ambiguous = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert error_of(ambiguous, 401)["code"] == "INVALID_CREDENTIALS"

        named = await client.post(
            "/auth/login",
            json={"email": email, "password": PASSWORD, "company_code": company_code.lower()},
        )
        user = data_of(named)["user"]
        assert user["id"] == str(ours.id)
        assert user["company_id"] == str(company_id)

        # The other tenant's password does not open this tenant's account.
        crossed = await client.post(
            "/auth/login",
            json={"email": email, "password": NEW_PASSWORD, "company_code": company_code},
        )
        assert error_of(crossed, 401)["code"] == "INVALID_CREDENTIALS"


# -----------------------------------------------------------------------------
# POST /auth/refresh
# -----------------------------------------------------------------------------


class TestRefresh:
    async def test_a_refresh_rotates_the_pair_and_retires_the_presented_session(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post("/auth/refresh", headers=signed.browser())

        data = data_of(response)
        assert data["session_id"] != signed.session_id
        assert "refresh_token" not in data
        cookies = cookies_set_by(response)
        rotated = cookies[REFRESH_COOKIE_NAME].value
        assert rotated != signed.refresh_token
        assert cookies[CSRF_COOKIE_NAME].value == csrf_token_for(rotated)
        retired = await session_row(session, signed.session_id)
        successor = await session_row(session, data["session_id"])
        assert retired.revoked_reason == REVOKE_REASON_ROTATED
        assert retired.replaced_by_id == successor.id
        assert successor.revoked_at is None
        me = data_of(await client.get("/auth/me", headers=bearer(data["access_token"])))
        assert me["session_id"] == data["session_id"]

    async def test_a_refresh_with_no_cookie_is_refused(self, client: AsyncClient) -> None:
        assert error_of(await client.post("/auth/refresh"), 401)["code"] == "TOKEN_INVALID"

    async def test_a_refresh_without_the_csrf_header_is_refused_and_keeps_the_session(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/refresh", headers=signed.browser(with_csrf_header=False)
        )

        assert error_of(response, 403)["code"] == "CSRF_VALIDATION_FAILED"
        # A 403 may be a forged cross-site call; the real session must survive it.
        assert cookies_set_by(response) == {}
        assert (await session_row(session, signed.session_id)).revoked_at is None

    async def test_a_deactivated_account_cannot_refresh_and_loses_its_cookies(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        record = await session.get(User, account.id)
        assert record is not None
        record.is_active = False
        await session.flush()

        response = await client.post("/auth/refresh", headers=signed.browser())

        assert error_of(response, 401)["code"] == "ACCOUNT_INACTIVE"
        # A credential that cannot recover is expired in the browser as well.
        assert cookies_set_by(response)[REFRESH_COOKIE_NAME].value == ""
        assert await session_count(session, account.id) == 1  # no successor was minted

    async def test_replaying_a_spent_refresh_token_ends_the_family_and_clears_the_cookies(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))
        successor = data_of(await client.post("/auth/refresh", headers=signed.browser()))

        replay = await client.post("/auth/refresh", headers=signed.browser())

        assert error_of(replay, 401)["code"] == "TOKEN_REVOKED"
        cleared = cookies_set_by(replay)
        assert cleared[REFRESH_COOKIE_NAME].value == ""
        assert cleared[REFRESH_COOKIE_NAME]["max-age"] == "0"
        burnt = await session_row(session, successor["session_id"])
        assert burnt.revoked_reason == REVOKE_REASON_REPLAY
        dead = await client.get("/auth/me", headers=bearer(successor["access_token"]))
        assert error_of(dead, 401)["code"] == "TOKEN_REVOKED"


# -----------------------------------------------------------------------------
# POST /auth/mfa/verify
# -----------------------------------------------------------------------------


class TestMfaVerify:
    async def test_the_challenge_and_a_live_code_complete_the_sign_in(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        challenge = data_of(await client.post("/auth/login", json=credentials(account)))

        response = await client.post(
            "/auth/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": pyotp.TOTP(secret).now(),
            },
        )

        data = data_of(response)
        assert data["mfa_required"] is False
        assert data["user"]["id"] == str(account.id)
        cookies = cookies_set_by(response)
        assert cookies[CSRF_COOKIE_NAME].value == csrf_token_for(cookies[REFRESH_COOKIE_NAME].value)
        assert (await session_row(session, data["session_id"])).revoked_at is None
        # The accepted step is the replay barrier for the next code.
        assert (await account_row(session, account.id)).mfa_last_used_step is not None
        assert await attempts(session, account.id) == [(True, None)]
        assert await audit_rows(session, account.company_id, account.id, AuditAction.LOGIN)
        me = data_of(await client.get("/auth/me", headers=bearer(data["access_token"])))
        assert me["mfa_enabled"] is True

    async def test_a_wrong_code_is_refused_and_counted_against_the_account(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        challenge = data_of(await client.post("/auth/login", json=credentials(account)))

        response = await client.post(
            "/auth/mfa/verify",
            json={"challenge_token": challenge["challenge_token"], "code": wrong_code_for(secret)},
        )

        assert error_of(response, 401)["code"] == "MFA_INVALID"
        assert cookies_set_by(response) == {}
        assert (await account_row(session, account.id)).failed_login_attempts == 1
        assert await attempts(session, account.id) == [(False, FAILURE_BAD_MFA)]
        assert await session_count(session, account.id) == 0

    async def test_an_access_token_is_not_accepted_as_a_challenge(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/mfa/verify", json={"challenge_token": signed.access_token, "code": "123456"}
        )

        assert error_of(response, 401)["code"] == "TOKEN_INVALID"

    async def test_a_malformed_verification_names_each_field(self, client: AsyncClient) -> None:
        response = await client.post(
            "/auth/mfa/verify", json={"challenge_token": "short", "code": "12ab"}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"challenge_token", "code"} <= set(error["details"]["fields"])


# -----------------------------------------------------------------------------
# POST /auth/logout and /auth/logout-all
# -----------------------------------------------------------------------------


class TestLogout:
    async def test_a_logout_ends_this_session_clears_both_cookies_and_kills_the_token(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        elsewhere = await sign_in(client, account)

        response = await client.post("/auth/logout", headers={**signed.bearer, **signed.browser()})

        assert data_of(response) == {"revoked_sessions": 1}
        cleared = cookies_set_by(response)
        assert cleared[REFRESH_COOKIE_NAME].value == ""
        assert cleared[CSRF_COOKIE_NAME].value == ""
        ended = await session_row(session, signed.session_id)
        assert ended.revoked_reason == REVOKE_REASON_LOGOUT
        assert (await session_row(session, elsewhere.session_id)).revoked_at is None
        (audit,) = await audit_rows(session, account.company_id, ended.id, AuditAction.LOGOUT)
        assert audit.entity_type == "user_session"
        assert audit.context["revoked_sessions"] == 1

        dead = await client.get("/auth/me", headers=signed.bearer)
        assert error_of(dead, 401)["code"] == "TOKEN_REVOKED"
        data_of(await client.get("/auth/me", headers=elsewhere.bearer))

    async def test_a_logout_carrying_the_cookie_but_no_csrf_header_is_refused(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/logout", headers={**signed.bearer, **signed.browser(with_csrf_header=False)}
        )

        assert error_of(response, 403)["code"] == "CSRF_VALIDATION_FAILED"
        assert (await session_row(session, signed.session_id)).revoked_at is None
        data_of(await client.get("/auth/me", headers=signed.bearer))


class TestLogoutAll:
    async def test_every_session_of_the_caller_ends_and_nobody_elses(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        first = await sign_in(client, account)
        second = await sign_in(client, account)
        colleague = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/logout-all", headers={**first.bearer, **first.browser()}
        )

        assert data_of(response) == {"revoked_sessions": 2}
        assert cookies_set_by(response)[REFRESH_COOKIE_NAME].value == ""
        for signed in (first, second):
            ended = await session_row(session, signed.session_id)
            assert ended.revoked_reason == REVOKE_REASON_LOGOUT_ALL
        assert (await session_row(session, colleague.session_id)).revoked_at is None
        (audit,) = await audit_rows(session, account.company_id, account.id, AuditAction.LOGOUT)
        assert audit.entity_type == "user"
        assert audit.context["revoked_sessions"] == 2

        dead = await client.get("/auth/me", headers=second.bearer)
        assert error_of(dead, 401)["code"] == "TOKEN_REVOKED"
        data_of(await client.get("/auth/me", headers=colleague.bearer))

    async def test_a_forged_csrf_header_revokes_nothing(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))
        headers = {**signed.bearer, **signed.browser()}
        headers[CSRF_HEADER_NAME] = "forged-by-another-site"

        response = await client.post("/auth/logout-all", headers=headers)

        assert error_of(response, 403)["code"] == "CSRF_VALIDATION_FAILED"
        assert (await session_row(session, signed.session_id)).revoked_at is None


# -----------------------------------------------------------------------------
# PUT /auth/me/phone
# -----------------------------------------------------------------------------


class TestPhone:
    async def test_a_new_number_is_stored_in_e164_and_clears_verification_and_consent(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        moment = utcnow()
        account = await make_account(
            session,
            company.id,
            phone_e164="+14155552671",
            phone_verified_at=moment,
            sms_opt_in_at=moment,
            whatsapp_opt_in_at=moment,
        )
        signed = await sign_in(client, account)

        response = await client.put(
            "/auth/me/phone", json={"phone": "098765 43210"}, headers=signed.bearer
        )

        assert data_of(response) == {"acknowledged": True}
        state = await account_row(session, account.id)
        assert state.phone_e164 == MOBILE
        # Consent was given for the old number, not this one.
        assert state.phone_verified_at is None
        assert state.sms_opt_in_at is None
        assert state.whatsapp_opt_in_at is None
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.CONFIG_CHANGE
        )
        assert audit.context["old_phone_suffix"] == "2671"
        assert audit.context["new_phone_suffix"] == MOBILE[-4:]
        me = data_of(await client.get("/auth/me", headers=signed.bearer))
        assert me["phone_e164"] == MOBILE
        assert me["phone_verified_at"] is None

    async def test_null_clears_the_number(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.put("/auth/me/phone", json={"phone": None}, headers=signed.bearer)

        assert data_of(response) == {"acknowledged": True}
        assert (await account_row(session, account.id)).phone_e164 is None

    async def test_an_unusable_number_is_a_field_error_and_changes_nothing(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.put(
            "/auth/me/phone", json={"phone": "12345"}, headers=signed.bearer
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "phone" in error["details"]["fields"]
        assert (await account_row(session, account.id)).phone_e164 == MOBILE


# -----------------------------------------------------------------------------
# POST /auth/me/phone/verification and .../confirm
# -----------------------------------------------------------------------------


class TestPhoneVerification:
    async def test_a_code_goes_to_the_number_on_file_and_only_its_digest_is_kept(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.post("/auth/me/phone/verification", headers=signed.bearer)

        assert data_of(response) == {"expires_in_seconds": int(PHONE_CODE_LIFETIME.total_seconds())}
        ((phone, template, values),) = sms.sent
        assert (phone, template) == (MOBILE, settings.sms_verification_template_id)
        (code,) = values
        assert len(code) == 6 and code.isdigit()
        challenge = await challenge_row(session, account.id)
        assert challenge is not None
        assert challenge.phone_e164 == MOBILE
        assert (challenge.send_count, challenge.attempt_count) == (1, 0)
        assert challenge.consumed_at is None
        assert code not in challenge.code_digest
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.CONFIG_CHANGE
        )
        assert audit.context["phone_suffix"] == MOBILE[-4:]

    async def test_a_second_request_inside_the_resend_delay_is_rate_limited(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id, phone_e164=MOBILE))
        data_of(await client.post("/auth/me/phone/verification", headers=signed.bearer))

        again = await client.post("/auth/me/phone/verification", headers=signed.bearer)

        assert error_of(again, 429)["code"] == "RATE_LIMITED"
        assert int(again.headers["Retry-After"]) >= 1
        assert len(sms.sent) == 1

    async def test_with_no_number_on_file_nothing_is_sent(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post("/auth/me/phone/verification", headers=signed.bearer)

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert sms.sent == []
        assert await challenge_row(session, account.id) is None

    async def test_an_unconfigured_gateway_is_refused_before_anything_is_written(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        sms: SmsGateway,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "sms_auth_key", SecretStr(""))
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.post("/auth/me/phone/verification", headers=signed.bearer)

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert sms.sent == []
        assert await challenge_row(session, account.id) is None

    async def test_a_provider_failure_is_a_503_and_retires_the_code(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        sms.outcome = MessagingOutcome.FAILED
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.post("/auth/me/phone/verification", headers=signed.bearer)

        assert error_of(response, 503)["code"] == "SERVICE_UNAVAILABLE"
        challenge = await challenge_row(session, account.id)
        assert challenge is not None
        # A code that never arrived must not stay redeemable.
        assert challenge.consumed_at is not None


class TestPhoneVerificationConfirm:
    async def test_the_code_that_was_sent_verifies_the_number(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)
        data_of(await client.post("/auth/me/phone/verification", headers=signed.bearer))

        response = await client.post(
            "/auth/me/phone/verification/confirm",
            json={"code": sms.last_code},
            headers=signed.bearer,
        )

        assert data_of(response) == {"acknowledged": True}
        assert (await account_row(session, account.id)).phone_verified_at is not None
        challenge = await challenge_row(session, account.id)
        assert challenge is not None
        assert challenge.consumed_at is not None
        audits = await audit_rows(
            session, account.company_id, account.id, AuditAction.CONFIG_CHANGE
        )
        assert len(audits) == 2  # the request, then the verification
        me = data_of(await client.get("/auth/me", headers=signed.bearer))
        assert me["phone_verified_at"] is not None

    async def test_a_wrong_code_is_refused_and_counted(
        self, client: AsyncClient, session: AsyncSession, company, sms: SmsGateway
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)
        data_of(await client.post("/auth/me/phone/verification", headers=signed.bearer))
        wrong = f"{(int(sms.last_code) + 1) % 1_000_000:06d}"

        response = await client.post(
            "/auth/me/phone/verification/confirm", json={"code": wrong}, headers=signed.bearer
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        challenge = await challenge_row(session, account.id)
        assert challenge is not None
        assert challenge.attempt_count == 1
        assert challenge.consumed_at is None
        assert (await account_row(session, account.id)).phone_verified_at is None

    async def test_confirming_with_no_code_outstanding_is_refused(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/me/phone/verification/confirm", json={"code": "123456"}, headers=signed.bearer
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert (await account_row(session, account.id)).phone_verified_at is None

    async def test_a_code_that_is_not_six_digits_is_a_field_error(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id, phone_e164=MOBILE))

        response = await client.post(
            "/auth/me/phone/verification/confirm", json={"code": "12345"}, headers=signed.bearer
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "code" in error["details"]["fields"]


# -----------------------------------------------------------------------------
# POST /auth/me/messaging-consent
# -----------------------------------------------------------------------------


class TestMessagingConsent:
    async def test_a_verified_number_can_opt_in_and_out_and_each_is_audited(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "sms_enabled", True)
        account = await make_account(
            session, company.id, phone_e164=MOBILE, phone_verified_at=utcnow()
        )
        signed = await sign_in(client, account)

        opted_in = await client.post(
            "/auth/me/messaging-consent",
            json={"channel": "SMS", "opt_in": True},
            headers=signed.bearer,
        )

        assert data_of(opted_in) == {"channel": "SMS", "opted_in": True}
        state = await account_row(session, account.id)
        assert state.sms_opt_in_at is not None
        assert state.whatsapp_opt_in_at is None
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.CONFIG_CHANGE
        )
        assert audit.old_values == {"sms_opt_in": False}
        assert audit.new_values == {"sms_opt_in": True}

        opted_out = await client.post(
            "/auth/me/messaging-consent",
            json={"channel": "SMS", "opt_in": False},
            headers=signed.bearer,
        )

        assert data_of(opted_out) == {"channel": "SMS", "opted_in": False}
        assert (await account_row(session, account.id)).sms_opt_in_at is None

    async def test_opting_in_before_the_number_is_verified_is_refused(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "sms_enabled", True)
        account = await make_account(session, company.id, phone_e164=MOBILE)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/me/messaging-consent",
            json={"channel": "SMS", "opt_in": True},
            headers=signed.bearer,
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert (await account_row(session, account.id)).sms_opt_in_at is None

    async def test_opting_in_to_a_channel_this_deployment_has_off_is_refused(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "whatsapp_enabled", False)
        account = await make_account(
            session, company.id, phone_e164=MOBILE, phone_verified_at=utcnow()
        )
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/me/messaging-consent",
            json={"channel": "WHATSAPP", "opt_in": True},
            headers=signed.bearer,
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert (await account_row(session, account.id)).whatsapp_opt_in_at is None

    async def test_an_unknown_channel_is_a_field_error(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/me/messaging-consent",
            json={"channel": "EMAIL", "opt_in": True},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "channel" in error["details"]["fields"]


# -----------------------------------------------------------------------------
# POST /auth/mfa/setup, /confirm and /disable
# -----------------------------------------------------------------------------


class TestMfaSetup:
    async def test_setup_stages_an_encrypted_seed_and_leaves_mfa_off(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/mfa/setup", json={"current_password": PASSWORD}, headers=signed.bearer
        )

        data = data_of(response)
        assert data["provisioning_uri"].startswith("otpauth://totp/")
        assert f"secret={data['secret']}" in data["provisioning_uri"]
        state = await account_row(session, account.id)
        assert state.mfa_enabled is False
        assert state.mfa_secret != data["secret"]
        assert decrypt_totp_secret(state.mfa_secret) == data["secret"]
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.CONFIG_CHANGE
        )
        assert audit.context["pending_enrolment"] is True

    async def test_a_wrong_current_password_is_a_field_error_and_a_counted_failure(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/mfa/setup", json={"current_password": WRONG_PASSWORD}, headers=signed.bearer
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_CREDENTIALS"
        assert "current_password" in error["details"]["fields"]
        state = await account_row(session, account.id)
        assert state.failed_login_attempts == 1
        assert state.mfa_secret is None
        assert await attempts(session, account.id) == [(True, None), (False, FAILURE_BAD_PASSWORD)]
        # A typo in the form is not a dead session: the same token still works.
        data_of(await client.get("/auth/me", headers=signed.bearer))

    async def test_setup_is_refused_once_mfa_is_on(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        signed = await sign_in_with_mfa(client, account, secret)

        response = await client.post(
            "/auth/mfa/setup", json={"current_password": PASSWORD}, headers=signed.bearer
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert decrypt_totp_secret((await account_row(session, account.id)).mfa_secret) == secret

    async def test_setup_without_the_password_names_the_field(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post("/auth/mfa/setup", json={}, headers=signed.bearer)

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "current_password" in error["details"]["fields"]


class TestMfaConfirm:
    async def test_a_live_code_enables_mfa_and_signs_the_other_devices_out(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        elsewhere = await sign_in(client, account)
        setup = data_of(
            await client.post(
                "/auth/mfa/setup", json={"current_password": PASSWORD}, headers=signed.bearer
            )
        )

        response = await client.post(
            "/auth/mfa/confirm",
            json={"code": pyotp.TOTP(setup["secret"]).now()},
            headers=signed.bearer,
        )

        assert data_of(response) == {"mfa_enabled": True, "revoked_sessions": 1}
        state = await account_row(session, account.id)
        assert state.mfa_enabled is True
        assert state.mfa_last_used_step is not None
        assert (await session_row(session, elsewhere.session_id)).revoked_reason == "mfa_enabled"
        assert (await session_row(session, signed.session_id)).revoked_at is None
        enabled = (
            await audit_rows(session, account.company_id, account.id, AuditAction.CONFIG_CHANGE)
        )[-1]
        assert enabled.context == {"mfa_enabled": True, "revoked_sessions": 1}
        # From now on a password alone earns a challenge, not a session.
        challenge = data_of(await client.post("/auth/login", json=credentials(account)))
        assert challenge["mfa_required"] is True

    async def test_a_wrong_code_is_a_field_error_and_mfa_stays_off(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        setup = data_of(
            await client.post(
                "/auth/mfa/setup", json={"current_password": PASSWORD}, headers=signed.bearer
            )
        )

        response = await client.post(
            "/auth/mfa/confirm",
            json={"code": wrong_code_for(setup["secret"])},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "MFA_INVALID"
        assert "code" in error["details"]["fields"]
        assert (await account_row(session, account.id)).mfa_enabled is False

    async def test_confirming_before_setup_is_refused(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/mfa/confirm", json={"code": "123456"}, headers=signed.bearer
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert (await account_row(session, account.id)).mfa_enabled is False

    async def test_a_code_that_is_not_six_digits_is_a_validation_error(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/mfa/confirm", json={"code": "12ab56"}, headers=signed.bearer
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "code" in error["details"]["fields"]


class TestMfaDisable:
    async def test_the_password_and_a_fresh_code_turn_mfa_off_and_end_other_sessions(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        signed = await sign_in_with_mfa(client, account, secret)
        stray = await seed_session(session, account)

        response = await client.post(
            "/auth/mfa/disable",
            json={"current_password": PASSWORD, "code": next_code(secret)},
            headers=signed.bearer,
        )

        assert data_of(response) == {"mfa_enabled": False, "revoked_sessions": 1}
        state = await account_row(session, account.id)
        assert state.mfa_enabled is False
        assert state.mfa_secret is None
        assert state.mfa_last_used_step is None
        assert (await session_row(session, stray)).revoked_reason == "mfa_disabled"
        assert (await session_row(session, signed.session_id)).revoked_at is None
        disabled = (
            await audit_rows(session, account.company_id, account.id, AuditAction.CONFIG_CHANGE)
        )[-1]
        assert disabled.context == {"mfa_enabled": False, "revoked_sessions": 1}
        # A password alone signs in again.
        login = data_of(await client.post("/auth/login", json=credentials(account)))
        assert login["mfa_required"] is False

    async def test_a_wrong_password_keeps_mfa_on_and_counts_against_the_account(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        signed = await sign_in_with_mfa(client, account, secret)

        response = await client.post(
            "/auth/mfa/disable",
            json={"current_password": WRONG_PASSWORD, "code": next_code(secret)},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_CREDENTIALS"
        assert "current_password" in error["details"]["fields"]
        state = await account_row(session, account.id)
        assert state.mfa_enabled is True
        # Recorded although the request failed: the refusal commits before it is raised.
        assert state.failed_login_attempts == 1

    async def test_a_code_that_was_already_used_keeps_mfa_on(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account, secret = await make_mfa_account(session, company.id)
        used = pyotp.TOTP(secret).now()
        signed = await sign_in_with_mfa(client, account, secret, code=used)

        response = await client.post(
            "/auth/mfa/disable",
            json={"current_password": PASSWORD, "code": used},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "MFA_INVALID"
        assert "code" in error["details"]["fields"]
        assert (await account_row(session, account.id)).mfa_enabled is True

    async def test_disabling_mfa_that_is_off_is_refused(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        signed = await sign_in(client, await make_account(session, company.id))

        response = await client.post(
            "/auth/mfa/disable",
            json={"current_password": PASSWORD, "code": "123456"},
            headers=signed.bearer,
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"


# -----------------------------------------------------------------------------
# POST /auth/change-password
# -----------------------------------------------------------------------------


class TestChangePassword:
    async def test_a_change_ends_the_other_sessions_and_the_new_password_signs_in(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        elsewhere = await sign_in(client, account)

        response = await client.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=signed.bearer,
        )

        assert data_of(response) == {"revoked_sessions": 1}
        state = await account_row(session, account.id)
        assert verify_password(NEW_PASSWORD, state.password_hash)
        assert state.password_changed_at is not None
        assert state.must_change_password is False
        ended = await session_row(session, elsewhere.session_id)
        assert ended.revoked_reason == REVOKE_REASON_PASSWORD_CHANGE
        assert (await session_row(session, signed.session_id)).revoked_at is None
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.PASSWORD_CHANGED
        )
        assert audit.context == {"revoked_sessions": 1, "self_service": True}

        stale = await client.post("/auth/login", json=credentials(account))
        assert error_of(stale, 401)["code"] == "INVALID_CREDENTIALS"
        data_of(await client.post("/auth/login", json=credentials(account, NEW_PASSWORD)))

    async def test_a_wrong_current_password_is_a_field_error_and_changes_nothing(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/change-password",
            json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_CREDENTIALS"
        assert "current_password" in error["details"]["fields"]
        state = await account_row(session, account.id)
        assert verify_password(PASSWORD, state.password_hash)
        assert state.failed_login_attempts == 1

    async def test_the_wrong_password_that_locks_the_account_ends_only_the_session_it_came_from(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        """A stolen access token guessing the password locks the account and
        loses its own session; the owner's other device has guessed nothing
        and keeps working."""
        account = await make_account(session, company.id)
        guessing = await sign_in(client, account)
        elsewhere = await sign_in(client, account)
        guess = {"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD}

        for _ in range(settings.login_max_failed_attempts - 1):
            miss = await client.post("/auth/change-password", json=guess, headers=guessing.bearer)
            assert error_of(miss, 422)["code"] == "INVALID_CREDENTIALS"
        locking = await client.post("/auth/change-password", json=guess, headers=guessing.bearer)

        assert error_of(locking, 401)["code"] == "ACCOUNT_LOCKED"
        ended = await session_row(session, guessing.session_id)
        assert ended.revoked_at is not None
        assert ended.revoked_reason == REVOKE_REASON_LOCKED
        dead = await client.get("/auth/me", headers=guessing.bearer)
        assert error_of(dead, 401)["code"] == "TOKEN_REVOKED"
        assert error_of(await client.post("/auth/refresh", headers=guessing.browser()), 401)[
            "code"
        ] == ("TOKEN_REVOKED")

        assert (await session_row(session, elsewhere.session_id)).revoked_at is None
        me = data_of(await client.get("/auth/me", headers=elsewhere.bearer))
        assert me["session_id"] == elsewhere.session_id
        state = await account_row(session, account.id)
        assert verify_password(PASSWORD, state.password_hash)
        assert state.failed_login_attempts == settings.login_max_failed_attempts
        last = (
            await audit_rows(session, account.company_id, account.id, AuditAction.LOGIN_FAILED)
        )[-1]
        assert last.context["locked"] is True
        assert last.context["ended_session_id"] == guessing.session_id

    async def test_a_locked_account_keeps_its_session_but_has_no_password_checked(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        """Locked by somebody else's guesses at sign-in: even the right current
        password is refused until the lock runs out - and refused under the
        field, because a 401 would sign out a session that is still alive."""
        account = await make_account(session, company.id)
        device = await sign_in(client, account)
        for _ in range(settings.login_max_failed_attempts):
            await client.post("/auth/login", json=credentials(account, WRONG_PASSWORD))

        response = await client.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=device.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "ACCOUNT_LOCKED"
        assert error["details"]["fields"]["current_password"] == [error["message"]]
        state = await account_row(session, account.id)
        assert verify_password(PASSWORD, state.password_hash)
        assert state.failed_login_attempts == settings.login_max_failed_attempts
        assert (await session_row(session, device.session_id)).revoked_at is None
        assert data_of(await client.get("/auth/me", headers=device.bearer))["session_id"] == (
            device.session_id
        )
        assert (await attempts(session, account.id))[-1] == (False, FAILURE_LOCKED)

    async def test_a_new_password_below_the_policy_names_the_field(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "short"},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "PASSWORD_POLICY"
        assert "new_password" in error["details"]["fields"]
        assert verify_password(PASSWORD, (await account_row(session, account.id)).password_hash)

    async def test_an_unchanged_password_is_refused_at_the_door(
        self, client: AsyncClient, session: AsyncSession, company
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)

        response = await client.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
            headers=signed.bearer,
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["fields"]
        assert (await session_row(session, signed.session_id)).revoked_at is None


# -----------------------------------------------------------------------------
# POST /auth/password-reset/request and /confirm
# -----------------------------------------------------------------------------


class TestPasswordResetRequest:
    async def test_a_known_address_gets_a_grant_and_the_same_answer_as_an_unknown_one(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "email_provider", EmailProvider.disabled)
        account = await make_account(session, company.id)

        known = await client.post(
            "/auth/password-reset/request", json={"email": account.email.upper()}
        )
        unknown = await client.post(
            "/auth/password-reset/request",
            json={"email": f"nobody-{uuid.uuid4().hex[:10]}@atcraft-qa.com"},
        )

        assert data_of(known) == {"acknowledged": True}
        # Byte-for-byte the same answer, or the endpoint is an account oracle.
        assert known.json() == unknown.json()
        (grant,) = await grant_rows(session, account.id)
        assert grant.used_at is None
        assert grant.expires_at > utcnow()
        # Delivery is off here, so no recoverable copy of the link is kept.
        assert grant.email_token_ciphertext is None
        assert await audit_rows(
            session, account.company_id, account.id, AuditAction.PASSWORD_RESET_REQUESTED
        )

    async def test_a_malformed_address_is_a_field_error(self, client: AsyncClient) -> None:
        response = await client.post("/auth/password-reset/request", json={"email": "nobody"})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "email" in error["details"]["fields"]


class TestPasswordResetConfirm:
    async def test_the_mailed_link_sets_the_password_and_ends_every_session(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        reset_mail: list[dict[str, Any]],
    ) -> None:
        account = await make_account(session, company.id)
        signed = await sign_in(client, account)
        token = await mailed_link(client, session, reset_mail, account.email)

        response = await client.post(
            "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
        )

        assert data_of(response) == {"acknowledged": True}
        assert verify_password(NEW_PASSWORD, (await account_row(session, account.id)).password_hash)
        (grant,) = await grant_rows(session, account.id)
        assert grant.used_at is not None
        assert grant.email_token_ciphertext is None
        ended = await session_row(session, signed.session_id)
        assert ended.revoked_reason == REVOKE_REASON_PASSWORD_RESET
        (audit,) = await audit_rows(
            session, account.company_id, account.id, AuditAction.PASSWORD_CHANGED
        )
        assert audit.context["via_reset_link"] is True
        assert audit.context["revoked_sessions"] == 1

        dead = await client.get("/auth/me", headers=signed.bearer)
        assert error_of(dead, 401)["code"] == "TOKEN_REVOKED"
        stale = await client.post("/auth/login", json=credentials(account))
        assert error_of(stale, 401)["code"] == "INVALID_CREDENTIALS"
        data_of(await client.post("/auth/login", json=credentials(account, NEW_PASSWORD)))

    async def test_a_link_works_once(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        reset_mail: list[dict[str, Any]],
    ) -> None:
        account = await make_account(session, company.id)
        token = await mailed_link(client, session, reset_mail, account.email)
        data_of(
            await client.post(
                "/auth/password-reset/confirm",
                json={"token": token, "new_password": NEW_PASSWORD},
            )
        )

        again = await client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "Third-Passphrase!42"},
        )

        assert error_of(again, 401)["code"] == "TOKEN_INVALID"
        assert verify_password(NEW_PASSWORD, (await account_row(session, account.id)).password_hash)

    async def test_a_token_nobody_issued_is_refused_like_a_spent_one(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/auth/password-reset/confirm",
            json={"token": secrets.token_urlsafe(32), "new_password": NEW_PASSWORD},
        )

        assert error_of(response, 401)["code"] == "TOKEN_INVALID"

    async def test_a_weak_password_is_refused_and_the_link_survives(
        self,
        client: AsyncClient,
        session: AsyncSession,
        company,
        reset_mail: list[dict[str, Any]],
    ) -> None:
        account = await make_account(session, company.id)
        token = await mailed_link(client, session, reset_mail, account.email)

        weak = await client.post(
            "/auth/password-reset/confirm", json={"token": token, "new_password": "password"}
        )

        error = error_of(weak, 422)
        assert error["code"] == "PASSWORD_POLICY"
        assert "new_password" in error["details"]["fields"]
        (grant,) = await grant_rows(session, account.id)
        assert grant.used_at is None
        data_of(
            await client.post(
                "/auth/password-reset/confirm",
                json={"token": token, "new_password": NEW_PASSWORD},
            )
        )

    async def test_a_token_too_short_to_be_real_is_a_validation_error(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/auth/password-reset/confirm", json={"token": "abc", "new_password": NEW_PASSWORD}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "token" in error["details"]["fields"]
