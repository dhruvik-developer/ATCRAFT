"""Authentication business rules (master rules section 38).

What this file is responsible for, and why each rule is written the way it is:

* **Sign-in must not reveal which addresses have accounts.** An unknown email
  still pays for one Argon2 verification against a constant hash and comes back
  with the same :class:`~src.core.errors.InvalidCredentialsError` as a wrong
  password. Without the dummy verify the endpoint answers "no such account" in
  two milliseconds and "wrong password" in fifty, which is an enumeration oracle
  in everything but name.
* **Failures are durable, successes reset them.** A failed attempt increments
  the counter, writes a ``login_attempts`` row and an audit entry, and commits -
  then the error is raised. Raising first would roll the evidence back, and an
  attacker's attempts would leave no trace at all.
* **Refresh tokens rotate, and a replay burns the family.** Every refresh
  revokes the presented session and issues a new one linked through
  ``replaced_by_id``. Presenting a token that has already been replaced can only
  mean it was captured, so the whole chain - including the session the attacker
  or the victim is currently holding - is revoked at once.
* **Only digests are stored.** Refresh tokens and reset links are compared as
  SHA-256 digests with :func:`constant_time_compare`, so a database disclosure
  yields nothing that can be presented as a credential.
* **A password change ends every other session**, because the usual reason to
  change a password is that someone else may know it.

Transaction shape: each operation opens exactly one
:func:`~src.core.database.transaction`. Where an error must still leave a record
behind - a failed sign-in, a detected replay - the write happens inside the
transaction and the exception is raised after the block has committed.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cache import cache_delete, make_key
from src.core.clock import utcnow
from src.core.config import AppEnv, settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AccountLockedError,
    AuthenticationError,
    BusinessRuleError,
    ErrorCode,
    InvalidCredentialsError,
    NotFoundError,
    TokenExpiredError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams
from src.core.ratelimit import RateLimitPolicy, enforce_policy, reset_rate_limit
from src.core.responses import OffsetPagination
from src.core.security import (
    constant_time_compare,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_secret_token,
    hash_password,
    hash_secret_token,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.auth.repository import (
    PasswordResetTokenRepository,
    UserSessionRepository,
    find_accounts_by_email,
    find_reset_token,
    get_account,
    get_account_summary,
    record_login_attempt,
)
from src.modules.auth.schemas import (
    AuthenticatedUser,
    ChangePasswordRequest,
    ChangePasswordResult,
    LoginRequest,
    LoginResponse,
    LogoutResult,
    MeResponse,
    PasswordResetConfirmPayload,
    PasswordResetRequestPayload,
    SessionRead,
    TokenPair,
)
from src.modules.users.models import User, UserSession

log = get_logger(__name__)

AUDIT_ENTITY_USER = "user"
AUDIT_ENTITY_SESSION = "user_session"

# ASSUMPTION: a password-reset link lives for one hour. The master rules and
# config carry no setting for it, and an hour is the shortest window that still
# survives a mail server queueing the message for a few minutes. A named
# constant rather than a literal so it can be promoted to configuration without
# hunting through the service.
PASSWORD_RESET_TTL_MINUTES = 60

# ASSUMPTION: the same address may exist in several companies (``users`` is
# unique on ``(company_id, email)``), and sign-in has no tenant context. When
# more than one live account matches, the request must name a ``company_code``.
# Guessing a tenant would sign somebody into the wrong company's data; answering
# "which company?" without a password would confirm the address exists. So the
# generic credentials error is returned and the attempt is recorded for each
# matching account.
MAX_MATCHING_ACCOUNTS = 1

#: Constant Argon2 hash used only to burn the same CPU time a real verification
#: would, when the address does not exist. Generated per process from a random
#: secret: nothing can ever verify against it, so it cannot become a backdoor
#: the way a checked-in hash of a known string could.
_TIMING_EQUALISER_HASH = hash_password(secrets.token_urlsafe(32))

# Failure reasons persisted on ``login_attempts.failure_reason`` (String(64)).
# A closed vocabulary so the column can be grouped in a security report.
FAILURE_UNKNOWN_EMAIL = "unknown_email"
FAILURE_AMBIGUOUS_EMAIL = "ambiguous_email"
FAILURE_BAD_PASSWORD = "bad_password"  # noqa: S105 - a reason code, not a secret
FAILURE_INACTIVE = "account_inactive"
FAILURE_LOCKED = "account_locked"

REVOKE_REASON_LOGOUT = "logout"
REVOKE_REASON_LOGOUT_ALL = "logout_all"
REVOKE_REASON_ROTATED = "rotated"
REVOKE_REASON_REPLAY = "refresh_replay"
REVOKE_REASON_PASSWORD_CHANGE = "password_changed"  # noqa: S105 - a reason code
REVOKE_REASON_PASSWORD_RESET = "password_reset"  # noqa: S105 - a reason code
REVOKE_REASON_MANUAL = "revoked_by_user"


# -----------------------------------------------------------------------------
# Session lifecycle
# -----------------------------------------------------------------------------


class SessionState(StrEnum):
    """Derived, not stored.

    ``user_sessions`` has no status column - it carries ``revoked_at``,
    ``expires_at`` and ``replaced_by_id``, and those three already determine the
    state. Deriving it keeps one source of truth: a row cannot be marked ACTIVE
    while holding a revocation timestamp.
    """

    ACTIVE = "ACTIVE"
    ROTATED = "ROTATED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


SESSION_STATE_MACHINE: StateMachine[SessionState] = StateMachine(
    entity="user_session",
    initial=SessionState.ACTIVE,
    transitions=(
        Transition(SessionState.ACTIVE, SessionState.ROTATED, label="Rotate refresh token"),
        Transition(SessionState.ACTIVE, SessionState.REVOKED, label="Revoke session"),
        Transition(SessionState.ACTIVE, SessionState.EXPIRED, label="Expire"),
    ),
    terminal=frozenset({SessionState.ROTATED, SessionState.REVOKED, SessionState.EXPIRED}),
)


def session_state(record: UserSession, *, now: datetime | None = None) -> SessionState:
    """Classify a session row.

    Revocation is checked before expiry: a session that was revoked and has
    since passed its expiry is still a revocation as far as an investigation is
    concerned, and ``ROTATED`` must remain distinguishable from ``REVOKED``
    because only the former means "a successor token exists".
    """
    moment = now or utcnow()
    if record.revoked_at is not None:
        return SessionState.ROTATED if record.replaced_by_id is not None else SessionState.REVOKED
    if record.expires_at <= moment:
        return SessionState.EXPIRED
    return SessionState.ACTIVE


def lockout_deadline(failed_attempts: int, *, now: datetime) -> datetime | None:
    """When the account must stay locked until, or ``None`` if under the limit.

    Pure so the threshold arithmetic is unit-testable without a database, and so
    the "off by one at exactly the limit" mistake is caught by a test rather
    than by a locked-out plant manager.
    """
    if failed_attempts < settings.login_max_failed_attempts:
        return None
    return now + timedelta(minutes=settings.login_lockout_minutes)


def _session_cache_key(company_id: uuid.UUID, session_id: str) -> str:
    # Must match src.core.deps._session_is_live exactly, or a revoked session
    # keeps answering requests for the rest of that key's TTL.
    return make_key("sessrevoke", company_id, session_id)


async def _drop_session_cache(company_id: uuid.UUID, session_ids: list[str]) -> None:
    if session_ids:
        await cache_delete(*(_session_cache_key(company_id, sid) for sid in session_ids))


# -----------------------------------------------------------------------------
# Service-level results
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PasswordResetGrant:
    """A freshly minted reset link, for whoever delivers it.

    Returned by the service and deliberately **not** by the route: the plaintext
    token belongs in an email to the address that requested it, and nowhere in
    an HTTP response, where it would let anyone who can guess an address take
    over the account.
    """

    user_id: uuid.UUID
    company_id: uuid.UUID
    email: str
    token: str
    expires_at: datetime


class AuthService:
    """Credential verification, session lifecycle and password management.

    Takes only a session: sign-in happens *before* a tenant exists, so unlike
    every other service in the codebase this one cannot be constructed with a
    :class:`~src.core.deps.TenantContext`. The tenant is derived from the
    resolved account (sign-in) or from the verified token (everything else), and
    a repository is built from it per operation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- sign in ------------------------------------------------------------

    async def login(
        self,
        payload: LoginRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResponse:
        """Verify credentials and open a session."""
        # Two independent budgets: the per-email one stops a slow password spray
        # against one account from behind many addresses, the per-IP one stops a
        # dictionary run from one host across many accounts.
        await enforce_policy(RateLimitPolicy.LOGIN_PER_EMAIL, payload.email)
        await enforce_policy(RateLimitPolicy.LOGIN_PER_IP, ip_address or "unknown")

        candidates = await find_accounts_by_email(
            self.session, payload.email, company_code=payload.company_code
        )
        account, resolution_failure = _resolve_account(candidates)

        if account is None:
            # Same work, same answer, whether or not the address exists.
            verify_password(payload.password, _TIMING_EQUALISER_HASH)
            await self._record_unresolved_attempt(
                candidates,
                email=payload.email,
                reason=resolution_failure or FAILURE_UNKNOWN_EMAIL,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            raise InvalidCredentialsError()

        now = utcnow()
        if account.locked_until is not None and account.locked_until > now:
            await self._register_failure(
                account,
                reason=FAILURE_LOCKED,
                ip_address=ip_address,
                user_agent=user_agent,
                count_against_lockout=False,
            )
            raise AccountLockedError()

        if not account.is_active:
            # Verified anyway so a deactivated account is not distinguishable by
            # response time from an active one with a wrong password.
            verify_password(payload.password, _TIMING_EQUALISER_HASH)
            await self._register_failure(
                account,
                reason=FAILURE_INACTIVE,
                ip_address=ip_address,
                user_agent=user_agent,
                count_against_lockout=False,
            )
            raise InvalidCredentialsError()

        if not verify_password(payload.password, account.password_hash):
            locked = await self._register_failure(
                account,
                reason=FAILURE_BAD_PASSWORD,
                ip_address=ip_address,
                user_agent=user_agent,
                count_against_lockout=True,
            )
            if locked:
                raise AccountLockedError()
            raise InvalidCredentialsError()

        return await self._complete_login(
            account,
            plain_password=payload.password,
            device_label=payload.device_label,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def _complete_login(
        self,
        account: User,
        *,
        plain_password: str,
        device_label: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> LoginResponse:
        tenant = _tenant_for(account.company_id, account.id)
        sessions = UserSessionRepository(self.session, tenant)
        now = utcnow()

        async with transaction(self.session):
            account.failed_login_attempts = 0
            account.locked_until = None
            account.last_login_at = now
            account.last_login_ip = ip_address

            if needs_rehash(account.password_hash):
                # Transparent upgrade of a legacy bcrypt or weak-parameter hash.
                # password_changed_at is deliberately NOT touched: it is the
                # cut-off deps.py uses to invalidate tokens issued before a
                # credential change, and re-hashing the same secret is not one -
                # moving it would sign the user out of their other devices for a
                # maintenance detail they never asked for.
                account.password_hash = hash_password(plain_password)
                log.info(
                    "auth.password_hash_upgraded",
                    user_id=str(account.id),
                    company_id=str(account.company_id),
                )

            record, tokens = await self._open_session(
                sessions,
                account,
                ip_address=ip_address,
                user_agent=user_agent,
                device_label=device_label,
            )

            record_login_attempt(
                self.session,
                company_id=account.company_id,
                email=account.email,
                succeeded=True,
                user_id=account.id,
                ip_address=ip_address,
                user_agent=user_agent[:512] if user_agent else None,
            )
            await record_audit(
                self.session,
                company_id=account.company_id,
                action=AuditAction.LOGIN,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"{account.email} signed in",
                user_id=account.id,
                actor_label=account.full_name,
                context={"session_id": record.session_id, "device_label": device_label},
            )

        # Only after a *successful* sign-in: one mistyped password must not keep
        # counting against a legitimate user for the rest of the window. Guarded
        # by the feature switch because reset_rate_limit() opens a Redis
        # connection unconditionally, and there is nothing to clear when no
        # counter was ever incremented.
        if settings.rate_limit_enabled:
            await reset_rate_limit(account.email, scope=RateLimitPolicy.LOGIN_PER_EMAIL[0])

        return LoginResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            refresh_expires_in=tokens.refresh_expires_in,
            session_id=tokens.session_id,
            user=_authenticated_user(account),
        )

    # --- refresh ------------------------------------------------------------

    async def refresh(
        self,
        refresh_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> TokenPair:
        """Rotate a refresh token, detecting replay of a spent one."""
        payload = decode_token(refresh_token, expected_type="refresh")
        await enforce_policy(RateLimitPolicy.REFRESH, str(payload.subject))

        if not payload.session_id:
            raise AuthenticationError(
                "This refresh token is not bound to a session.", code=ErrorCode.TOKEN_INVALID
            )

        tenant = _tenant_for(payload.company_id, payload.subject)
        sessions = UserSessionRepository(self.session, tenant)
        record = await sessions.get_by_session_id(payload.session_id)

        if record is None or record.user_id != payload.subject:
            raise AuthenticationError("Invalid authentication token.", code=ErrorCode.TOKEN_INVALID)

        presented_digest = hash_secret_token(refresh_token)
        state = session_state(record)

        # A digest that does not match, on a session that otherwise exists, can
        # only be a token from outside this rotation chain - treated exactly
        # like a replay.
        if not constant_time_compare(record.refresh_token_hash, presented_digest):
            await self._handle_replay(sessions, record, detail="digest_mismatch")
            raise AuthenticationError(
                "This session has been ended for security reasons. Please sign in again.",
                code=ErrorCode.TOKEN_REVOKED,
            )

        if state is SessionState.ROTATED:
            await self._handle_replay(sessions, record, detail="token_already_rotated")
            raise AuthenticationError(
                "This session has been ended for security reasons. Please sign in again.",
                code=ErrorCode.TOKEN_REVOKED,
            )
        if state is SessionState.REVOKED:
            raise AuthenticationError("Session has been revoked.", code=ErrorCode.TOKEN_REVOKED)
        if state is SessionState.EXPIRED:
            raise TokenExpiredError()

        account = await get_account(self.session, record.user_id, record.company_id)
        if account is None or not account.is_active:
            raise AuthenticationError("Account is not available.", code=ErrorCode.ACCOUNT_INACTIVE)
        if account.locked_until is not None and account.locked_until > utcnow():
            raise AccountLockedError()

        async with transaction(self.session):
            SESSION_STATE_MACHINE.assert_transition(state, SessionState.ROTATED)
            successor, tokens = await self._open_session(
                sessions,
                account,
                ip_address=ip_address or record.ip_address,
                user_agent=user_agent or record.user_agent,
                device_label=record.device_label,
            )
            record.revoked_at = utcnow()
            record.revoked_reason = REVOKE_REASON_ROTATED
            record.replaced_by_id = successor.id
            await self.session.flush()

        # Routine rotation is not audited. It happens every few minutes per
        # signed-in user and describes no business change; auditing it would add
        # millions of rows a year that no investigation would ever read. The
        # events that matter - sign in, sign out, replay - are audited, and the
        # rotation itself is reconstructible from the session chain.
        await _drop_session_cache(record.company_id, [record.session_id])
        log.info(
            "auth.session_rotated",
            user_id=str(account.id),
            company_id=str(account.company_id),
            from_session=record.session_id,
            to_session=successor.session_id,
        )
        return tokens

    async def _handle_replay(
        self, sessions: UserSessionRepository, record: UserSession, *, detail: str
    ) -> None:
        """Close the whole rotation family and record it.

        Committed before the caller raises: the evidence and the containment
        must survive the failed request, which is the entire point of detecting
        the replay in the first place.
        """
        async with transaction(self.session):
            revoked = await sessions.revoke_chain(record, reason=REVOKE_REASON_REPLAY)
            await record_audit(
                self.session,
                company_id=record.company_id,
                action=AuditAction.LOGIN_FAILED,
                entity_type=AUDIT_ENTITY_SESSION,
                entity_id=record.id,
                entity_code=record.session_id[:64],
                summary=(
                    f"Refresh token replay detected; revoked {len(revoked)} session(s) in the chain"
                ),
                user_id=record.user_id,
                context={"detail": detail, "revoked_sessions": len(revoked)},
            )

        await _drop_session_cache(record.company_id, revoked)
        log.warning(
            "auth.refresh_replay_detected",
            user_id=str(record.user_id),
            company_id=str(record.company_id),
            session_id=record.session_id,
            revoked_sessions=len(revoked),
            detail=detail,
        )

    # --- sign out -----------------------------------------------------------

    async def logout(
        self, principal: Principal, *, refresh_token: str | None = None
    ) -> LogoutResult:
        """Revoke the session this request was made with.

        Idempotent: signing out of a session that is already closed is a
        success, not a 4xx. A client retrying after a dropped response must not
        be told something is wrong.
        """
        session_id = principal.session_id or _session_id_from_refresh(refresh_token)
        tenant = _tenant_for(principal.company_id, principal.user_id)
        sessions = UserSessionRepository(self.session, tenant)

        record = await sessions.get_by_session_id(session_id) if session_id else None
        if record is not None and record.user_id != principal.user_id:
            # Someone else's session id: refuse to say whether it exists.
            record = None

        revoked = 0
        async with transaction(self.session):
            if record is not None and session_state(record) is SessionState.ACTIVE:
                SESSION_STATE_MACHINE.assert_transition(session_state(record), SessionState.REVOKED)
                await sessions.revoke(record, reason=REVOKE_REASON_LOGOUT)
                revoked = 1
            await record_audit(
                self.session,
                company_id=principal.company_id,
                action=AuditAction.LOGOUT,
                entity_type=AUDIT_ENTITY_SESSION,
                entity_id=record.id if record is not None else None,
                entity_code=session_id[:64] if session_id else None,
                summary=f"{principal.email} signed out",
                user_id=principal.user_id,
                actor_label=principal.full_name,
                context={"revoked_sessions": revoked},
            )

        if record is not None and revoked:
            await _drop_session_cache(principal.company_id, [record.session_id])
        return LogoutResult(revoked_sessions=revoked)

    async def logout_all(self, principal: Principal) -> LogoutResult:
        """Revoke every session for the caller, including this one."""
        tenant = _tenant_for(principal.company_id, principal.user_id)
        sessions = UserSessionRepository(self.session, tenant)

        async with transaction(self.session):
            revoked = await sessions.revoke_all_for_user(
                principal.user_id, reason=REVOKE_REASON_LOGOUT_ALL
            )
            await record_audit(
                self.session,
                company_id=principal.company_id,
                action=AuditAction.LOGOUT,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=principal.user_id,
                entity_code=principal.email[:64],
                summary=f"{principal.email} signed out of all devices",
                user_id=principal.user_id,
                actor_label=principal.full_name,
                context={"revoked_sessions": len(revoked)},
            )

        await _drop_session_cache(principal.company_id, revoked)
        return LogoutResult(revoked_sessions=len(revoked))

    # --- current principal --------------------------------------------------

    async def me(self, principal: Principal) -> MeResponse:
        """The caller's identity and effective authority for this request.

        The permission set is resolved per request by
        :func:`src.core.deps.get_current_principal` and returned here rather
        than embedded in the access token, so revoking a role takes effect on
        the next call instead of at the next token expiry (section 39).
        """
        summary = await get_account_summary(self.session, principal.user_id, principal.company_id)
        if summary is None:
            raise NotFoundError("Account", principal.user_id)
        must_change_password, mfa_enabled, last_login_at = summary

        return MeResponse(
            user_id=principal.user_id,
            company_id=principal.company_id,
            email=principal.email,
            full_name=principal.full_name,
            is_superuser=principal.is_superuser,
            must_change_password=must_change_password,
            mfa_enabled=mfa_enabled,
            last_login_at=last_login_at,
            session_id=principal.session_id,
            roles=list(principal.role_codes),
            permissions=sorted(principal.permissions),
            vendor_id=principal.vendor_id,
            branch_id=principal.branch_id,
            plant_id=principal.plant_id,
            department_id=principal.department_id,
            allowed_category_ids=list(principal.allowed_category_ids),
            allowed_warehouse_ids=list(principal.allowed_warehouse_ids),
        )

    # --- passwords ----------------------------------------------------------

    async def change_password(
        self, principal: Principal, payload: ChangePasswordRequest
    ) -> ChangePasswordResult:
        """Replace the caller's password and sign every other device out."""
        account = await get_account(self.session, principal.user_id, principal.company_id)
        if account is None:
            raise NotFoundError("Account", principal.user_id)

        if not verify_password(payload.current_password, account.password_hash):
            # Audited: repeated failures here are how a stolen access token gets
            # turned into a stolen password.
            async with transaction(self.session):
                await record_audit(
                    self.session,
                    company_id=account.company_id,
                    action=AuditAction.LOGIN_FAILED,
                    entity_type=AUDIT_ENTITY_USER,
                    entity_id=account.id,
                    entity_code=account.email[:64],
                    summary="Password change refused: current password did not match",
                    user_id=account.id,
                    actor_label=account.full_name,
                )
            raise InvalidCredentialsError("The current password is incorrect.")

        validate_password_policy(payload.new_password, email=account.email)
        if verify_password(payload.new_password, account.password_hash):
            raise BusinessRuleError("The new password must be different from the current one.")

        tenant = _tenant_for(account.company_id, account.id)
        sessions = UserSessionRepository(self.session, tenant)

        async with transaction(self.session):
            account.password_hash = hash_password(payload.new_password)
            account.password_changed_at = utcnow()
            account.must_change_password = False
            # A user who changes their password has proved possession of the
            # account; any lockout counter from earlier fumbling is stale.
            account.failed_login_attempts = 0
            account.locked_until = None

            # The caller's own refresh session survives so they are not bounced
            # out of the page they are on. Their current *access* token stops
            # working immediately anyway - deps.py rejects tokens issued before
            # password_changed_at - so the client refreshes once and continues.
            revoked = await sessions.revoke_all_for_user(
                account.id,
                reason=REVOKE_REASON_PASSWORD_CHANGE,
                keep_session_id=principal.session_id,
            )
            await record_audit(
                self.session,
                company_id=account.company_id,
                action=AuditAction.PASSWORD_CHANGED,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"{account.email} changed their password",
                user_id=account.id,
                actor_label=account.full_name,
                context={"revoked_sessions": len(revoked), "self_service": True},
            )

        await _drop_session_cache(account.company_id, revoked)
        return ChangePasswordResult(revoked_sessions=len(revoked))

    async def request_password_reset(
        self, payload: PasswordResetRequestPayload, *, ip_address: str | None = None
    ) -> PasswordResetGrant | None:
        """Issue a reset grant if the address resolves to exactly one account.

        Returns ``None`` when it does not. The caller (the route) answers
        identically either way - the return value exists for the delivery step,
        which sends the link to the address that requested it.
        """
        await enforce_policy(RateLimitPolicy.PASSWORD_RESET, payload.email)
        await enforce_policy(RateLimitPolicy.PASSWORD_RESET, f"ip:{ip_address or 'unknown'}")

        candidates = await find_accounts_by_email(
            self.session, payload.email, company_code=payload.company_code
        )
        account, _ = _resolve_account(candidates)
        if account is None or not account.is_active:
            log.info("auth.password_reset.no_match", email_domain=_email_domain(payload.email))
            return None

        plaintext, digest = generate_secret_token()
        expires_at = utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
        tenant = _tenant_for(account.company_id, account.id)
        tokens = PasswordResetTokenRepository(self.session, tenant)

        async with transaction(self.session):
            superseded = await tokens.invalidate_outstanding(account.id)
            tokens.create_token(
                user_id=account.id,
                token_hash=digest,
                expires_at=expires_at,
                requested_ip=ip_address,
            )
            await record_audit(
                self.session,
                company_id=account.company_id,
                action=AuditAction.PASSWORD_RESET_REQUESTED,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"Password reset requested for {account.email}",
                user_id=account.id,
                actor_label=account.full_name,
                context={"superseded_tokens": superseded, "expires_at": expires_at},
            )

        # Delivery belongs to the notifications module. Until it is mounted the
        # link is available to an operator from the application log, and only
        # outside production - printing a live credential into a production log
        # would defeat storing nothing but its digest.
        if settings.app_env is AppEnv.production:
            log.info("auth.password_reset.issued", user_id=str(account.id))
        else:
            log.warning(
                "auth.password_reset.issued_dev",
                user_id=str(account.id),
                reset_token=plaintext,
                expires_at=expires_at.isoformat(),
            )

        return PasswordResetGrant(
            user_id=account.id,
            company_id=account.company_id,
            email=account.email,
            token=plaintext,
            expires_at=expires_at,
        )

    async def confirm_password_reset(
        self, payload: PasswordResetConfirmPayload, *, ip_address: str | None = None
    ) -> None:
        """Redeem a reset link, set the new password and end every session."""
        digest = hash_secret_token(payload.token)
        grant = await find_reset_token(self.session, digest)

        # One message for "no such token", "already used" and "expired": each
        # distinct answer tells a token-guessing script something useful.
        invalid = AuthenticationError(
            "This password reset link is invalid or has expired.",
            code=ErrorCode.TOKEN_INVALID,
        )
        if grant is None or not constant_time_compare(grant.token_hash, digest):
            raise invalid
        if grant.used_at is not None or grant.expires_at <= utcnow():
            raise invalid

        account = await get_account(self.session, grant.user_id, grant.company_id)
        if account is None or not account.is_active:
            raise invalid

        validate_password_policy(payload.new_password, email=account.email)

        tenant = _tenant_for(account.company_id, account.id)
        sessions = UserSessionRepository(self.session, tenant)
        reset_tokens = PasswordResetTokenRepository(self.session, tenant)

        async with transaction(self.session):
            account.password_hash = hash_password(payload.new_password)
            account.password_changed_at = utcnow()
            account.must_change_password = False
            # The lockout is lifted: whoever holds the mailbox has proved
            # control of the account, and leaving it locked would hand an
            # attacker a denial of service through failed sign-ins alone.
            account.failed_login_attempts = 0
            account.locked_until = None

            await reset_tokens.mark_used(grant)
            # Every session, including any the attacker opened: a reset is the
            # response to a suspected compromise, not a routine edit.
            revoked = await sessions.revoke_all_for_user(
                account.id, reason=REVOKE_REASON_PASSWORD_RESET
            )
            await record_audit(
                self.session,
                company_id=account.company_id,
                action=AuditAction.PASSWORD_CHANGED,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"{account.email} completed a password reset",
                user_id=account.id,
                actor_label=account.full_name,
                context={
                    "revoked_sessions": len(revoked),
                    "via_reset_link": True,
                    "requested_ip": ip_address,
                },
            )

        await _drop_session_cache(account.company_id, revoked)

    # --- session management -------------------------------------------------

    async def list_sessions(
        self, principal: Principal, params: PageParams
    ) -> tuple[list[SessionRead], OffsetPagination]:
        """The caller's own live sessions, paginated."""
        tenant = _tenant_for(principal.company_id, principal.user_id)
        sessions = UserSessionRepository(self.session, tenant)
        rows, pagination = await sessions.list_active_for_user(principal.user_id, params)
        return [_session_read(row, principal.session_id) for row in rows], pagination

    async def revoke_session(self, principal: Principal, session_row_id: uuid.UUID) -> None:
        """Revoke one of the caller's sessions.

        A session belonging to somebody else is reported as not found rather
        than forbidden - confirming that an id exists in another user's session
        list is itself a disclosure.
        """
        tenant = _tenant_for(principal.company_id, principal.user_id)
        sessions = UserSessionRepository(self.session, tenant)
        record = await sessions.get(session_row_id)
        if record is None or record.user_id != principal.user_id:
            raise NotFoundError("Session", session_row_id)

        state = session_state(record)
        async with transaction(self.session):
            SESSION_STATE_MACHINE.assert_transition(state, SessionState.REVOKED)
            await sessions.revoke(record, reason=REVOKE_REASON_MANUAL)
            await record_audit(
                self.session,
                company_id=principal.company_id,
                action=AuditAction.LOGOUT,
                entity_type=AUDIT_ENTITY_SESSION,
                entity_id=record.id,
                entity_code=record.session_id[:64],
                summary=f"Session {record.session_id} revoked by its owner",
                user_id=principal.user_id,
                actor_label=principal.full_name,
                context={"device_label": record.device_label},
            )

        await _drop_session_cache(principal.company_id, [record.session_id])

    # --- internals ----------------------------------------------------------

    async def _open_session(
        self,
        sessions: UserSessionRepository,
        account: User,
        *,
        ip_address: str | None,
        user_agent: str | None,
        device_label: str | None,
    ) -> tuple[UserSession, TokenPair]:
        """Mint a session row and the token pair that addresses it.

        The row stores only the SHA-256 of the refresh token, so the table is
        worthless to anyone who reads it.
        """
        session_id = secrets.token_urlsafe(24)
        refresh_token, refresh_jti = create_refresh_token(
            account.id, account.company_id, session_id=session_id
        )
        access_token, _ = create_access_token(account.id, account.company_id, session_id=session_id)
        expires_at = utcnow() + timedelta(days=settings.jwt_refresh_token_expire_days)

        record = sessions.create_session(
            user_id=account.id,
            session_id=session_id,
            refresh_token_hash=hash_secret_token(refresh_token),
            refresh_jti=refresh_jti,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
            device_label=device_label,
        )
        # Flushed here because the predecessor's replaced_by_id needs this row's
        # primary key within the same transaction.
        await self.session.flush()

        return record, TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
            refresh_expires_in=settings.jwt_refresh_token_expire_days * 86_400,
            session_id=session_id,
        )

    async def _register_failure(
        self,
        account: User,
        *,
        reason: str,
        ip_address: str | None,
        user_agent: str | None,
        count_against_lockout: bool,
    ) -> bool:
        """Persist a failed sign-in. Returns True when this attempt locked the
        account.

        Committed on its own so the counter, the attempt row and the audit entry
        survive the exception the caller is about to raise.
        """
        locked_now = False
        async with transaction(self.session):
            if count_against_lockout:
                account.failed_login_attempts += 1
                deadline = lockout_deadline(account.failed_login_attempts, now=utcnow())
                if deadline is not None:
                    account.locked_until = deadline
                    locked_now = True

            record_login_attempt(
                self.session,
                company_id=account.company_id,
                email=account.email,
                succeeded=False,
                user_id=account.id,
                failure_reason=reason,
                ip_address=ip_address,
                user_agent=user_agent[:512] if user_agent else None,
            )
            await record_audit(
                self.session,
                company_id=account.company_id,
                action=AuditAction.LOGIN_FAILED,
                entity_type=AUDIT_ENTITY_USER,
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"Failed sign-in for {account.email} ({reason})",
                user_id=account.id,
                actor_label=account.full_name,
                context={
                    "reason": reason,
                    "failed_attempts": account.failed_login_attempts,
                    "locked": locked_now,
                },
            )

        if locked_now:
            log.warning(
                "auth.account_locked",
                user_id=str(account.id),
                company_id=str(account.company_id),
                attempts=account.failed_login_attempts,
            )
        return locked_now

    async def _record_unresolved_attempt(
        self,
        candidates: list[User],
        *,
        email: str,
        reason: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """History for a sign-in that never reached an account.

        ``login_attempts.company_id`` is NOT NULL, so an address belonging to no
        company cannot be given a row - there is no tenant to file it under.
        Those land in the log instead, where the rate limiter's own counters
        already make the pattern visible. An *ambiguous* address does resolve to
        real tenants, so each of them gets a row: from each company's point of
        view somebody tried to sign in and failed.
        """
        if not candidates:
            log.warning(
                "auth.login_failed_unknown_email",
                email_domain=_email_domain(email),
                reason=reason,
                ip_address=ip_address,
            )
            return

        async with transaction(self.session):
            for account in candidates:
                record_login_attempt(
                    self.session,
                    company_id=account.company_id,
                    email=email,
                    succeeded=False,
                    user_id=account.id,
                    failure_reason=reason,
                    ip_address=ip_address,
                    user_agent=user_agent[:512] if user_agent else None,
                )
                await record_audit(
                    self.session,
                    company_id=account.company_id,
                    action=AuditAction.LOGIN_FAILED,
                    entity_type=AUDIT_ENTITY_USER,
                    entity_id=account.id,
                    entity_code=account.email[:64],
                    summary=f"Failed sign-in for {email} ({reason})",
                    user_id=account.id,
                    context={"reason": reason, "matching_accounts": len(candidates)},
                )


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------


def _resolve_account(candidates: list[User]) -> tuple[User | None, str | None]:
    """Pick the single account a sign-in refers to.

    More than one match is refused rather than guessed - see
    :data:`MAX_MATCHING_ACCOUNTS`.
    """
    if not candidates:
        return None, FAILURE_UNKNOWN_EMAIL
    if len(candidates) > MAX_MATCHING_ACCOUNTS:
        return None, FAILURE_AMBIGUOUS_EMAIL
    return candidates[0], None


def _tenant_for(company_id: uuid.UUID, user_id: uuid.UUID) -> TenantContext:
    """Scope object for a repository built from verified identity only.

    Never from a request body: the company here is the one the credentials or
    the signed token resolved to (section 40).
    """
    return TenantContext(company_id=company_id, user_id=user_id)


def _session_id_from_refresh(refresh_token: str | None) -> str | None:
    """Read the ``sid`` claim, ignoring a token that will not verify.

    Sign-out must not fail because the token it was handed is rubbish; the
    session simply cannot be identified, and the outcome is "nothing revoked".
    """
    if not refresh_token:
        return None
    try:
        return decode_token(refresh_token, expected_type="refresh").session_id
    except AuthenticationError:
        return None


def _authenticated_user(account: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=account.id,
        email=account.email,
        full_name=account.full_name,
        company_id=account.company_id,
        is_superuser=account.is_superuser,
        must_change_password=account.must_change_password,
        roles=sorted(role.code for role in account.roles if role.is_active),
    )


def _session_read(record: UserSession, current_session_id: str | None) -> SessionRead:
    return SessionRead(
        id=record.id,
        session_id=record.session_id,
        created_at=record.created_at,
        expires_at=record.expires_at,
        last_used_at=record.last_used_at,
        # asyncpg hands back an IPv4Address/IPv6Address for an INET column, not
        # a string - it has to be rendered before it can be serialised.
        ip_address=str(record.ip_address) if record.ip_address else None,
        user_agent=record.user_agent,
        device_label=record.device_label,
        is_current=record.session_id == current_session_id,
    )


def _email_domain(email: str) -> str:
    """The domain half only - enough to spot a targeted run in the log without
    writing an unverified address into it."""
    _, _, domain = email.partition("@")
    return domain or "unknown"


__all__ = [
    "PASSWORD_RESET_TTL_MINUTES",
    "SESSION_STATE_MACHINE",
    "AuthService",
    "PasswordResetGrant",
    "SessionState",
    "lockout_deadline",
    "session_state",
]
