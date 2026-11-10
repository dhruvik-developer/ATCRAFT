"""Queries behind authentication.

This module holds the statements only the auth service issues. Two things are
worth reading twice:

* **Pre-authentication lookups are module-level functions, not repository
  methods.** Sign-in has no principal and therefore no
  :class:`~src.core.deps.TenantContext`: the company is a *result* of resolving
  the credentials, never an input to it. Those functions resolve the tenant from
  the email (optionally narrowed by a company code) and are the only place in
  the codebase that reads ``users`` without a tenant filter. They compensate by
  filtering on the company being live, so an account in a deactivated or deleted
  tenant cannot sign in.
* **Sessions and grant invalidation are the users module's.** ``user_sessions``
  and ``password_reset_tokens`` belong to ``users`` (docs/database.md section
  2), and user administration ends sessions and burns links too, so
  :class:`~src.modules.users.repository.UserSessionRepository` - rotation,
  revocation and the recursive-CTE chain revocation alike - and
  ``UserRepository.invalidate_password_reset_grants`` live there, once. Two
  copies of each had already drifted apart.

Once a caller *is* authenticated, issuing and consuming a reset grant goes
through :class:`PasswordResetTokenRepository`, an ordinary tenant-scoped
repository.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from src.common.repository import BaseRepository
from src.core.clock import utcnow

# Read-only references to tables owned by other modules. Auth authenticates
# against the identity master; it never writes to ``users`` outside the narrow
# credential columns the service updates on the instance it loaded here.
from src.modules.organization.models import Company
from src.modules.users.models import (
    LoginAttempt,
    PasswordResetToken,
    User,
    UserPhoneVerificationChallenge,
    UserSession,
)

#: Columns of ``users`` the sign-in path actually needs. Selecting the whole row
#: would drag ``preferences`` (JSONB) into every login for no reason - but the
#: service mutates the loaded instance (counters, hash upgrade), so the ORM
#: entity is loaded rather than a column tuple. The narrow projection is used
#: only where nothing is written back; see :func:`get_account_summary`.
_ACCOUNT_SUMMARY_COLUMNS = (
    User.must_change_password,
    User.mfa_enabled,
    User.last_login_at,
    User.phone_e164,
    User.phone_verified_at,
    User.whatsapp_opt_in_at,
    User.sms_opt_in_at,
)


# -----------------------------------------------------------------------------
# Pre-authentication lookups
# -----------------------------------------------------------------------------


def _live_account_filter(stmt: Select[tuple[User]]) -> Select[tuple[User]]:
    """Restrict to accounts in a company that still exists and is switched on.

    A tenant that has been deactivated must not be reachable through any of its
    users' credentials, and the check belongs in SQL: doing it in Python after
    the fact means a forgotten branch becomes an authentication bypass.
    """
    return stmt.join(Company, Company.id == User.company_id).where(
        User.deleted_at.is_(None),
        Company.deleted_at.is_(None),
        Company.is_active.is_(True),
    )


async def find_accounts_by_email(
    session: AsyncSession,
    email: str,
    *,
    company_code: str | None = None,
) -> list[User]:
    """Every live account holding this address, optionally in one company.

    Returns a list rather than one row on purpose: ``users.email`` is unique
    *per company*, so one person may legitimately hold accounts in two tenants.
    The service decides what to do about that; the repository does not guess.

    Roles load eagerly via the relationship's ``selectin`` strategy, which the
    login response needs anyway - so this is one extra query, not one per user.
    """
    stmt = _live_account_filter(select(User)).where(User.email == email)
    if company_code:
        stmt = stmt.where(Company.code == company_code)
    # Deterministic ordering so an ambiguous address resolves the same way on
    # every request instead of following the planner's mood.
    stmt = stmt.order_by(Company.code.asc()).limit(10)
    return list((await session.execute(stmt)).scalars().unique().all())


async def get_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> User | None:
    """Load one account for a credential operation (refresh, password change)."""
    stmt = _live_account_filter(select(User)).where(
        User.id == user_id, User.company_id == company_id
    )
    if for_update:
        stmt = stmt.with_for_update(of=User)
    return (await session.execute(stmt)).scalar_one_or_none()


async def increment_failed_attempts(session: AsyncSession, account: User) -> int:
    """Add one to the lockout counter in the database and return the new value.

    Not ``account.failed_login_attempts += 1``: that is a read-modify-write on
    an unlocked row, and parallel wrong passwords for one account each read N
    and write N+1, so the threshold is reached late or never. The Redis
    per-email limiter normally bounds this, but it fails open by design when
    Redis is down - at which point this counter is the only account-level
    defence left, racing across every Gunicorn worker. One ``UPDATE ... SET n
    = n + 1 RETURNING n`` is serialised by the row itself.

    The in-memory copy is updated as a committed value so the flush does not
    issue a second UPDATE writing the same number back.
    """
    stmt = (
        update(User)
        .where(User.id == account.id, User.company_id == account.company_id)
        .values(failed_login_attempts=User.failed_login_attempts + 1)
        .returning(User.failed_login_attempts)
        .execution_options(synchronize_session=False)
    )
    attempts = int((await session.execute(stmt)).scalar_one())
    set_committed_value(account, "failed_login_attempts", attempts)
    return attempts


async def get_account_summary(
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID
) -> (
    tuple[
        bool,
        bool,
        datetime | None,
        str | None,
        datetime | None,
        datetime | None,
        datetime | None,
        str,
    ]
    | None
):
    """The account flags ``/auth/me`` adds to the principal, and the company name.

    A column projection rather than the entities: ``/auth/me`` is polled by every
    page load, and needs only the account flags, messaging profile, and company
    name from two wide rows it will not modify.

    The company name is joined here rather than fetched by the browser from
    ``/organization/company``, and the reason is a permission: that endpoint
    needs ``company.view``, which an ordinary storekeeper does not hold, so the
    name would stay blank for exactly the people who look at the sidebar all
    day. Every caller of ``/auth/me`` is by definition entitled to know which
    company they are signed in to.
    """
    stmt = (
        select(*_ACCOUNT_SUMMARY_COLUMNS, Company.name)
        .join(Company, Company.id == User.company_id)
        .where(
            User.id == user_id,
            User.company_id == company_id,
            User.deleted_at.is_(None),
        )
    )
    row = (await session.execute(stmt)).first()
    return (
        (
            bool(row[0]),
            bool(row[1]),
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        )
        if row is not None
        else None
    )


async def find_reset_token(session: AsyncSession, token_hash: str) -> PasswordResetToken | None:
    """Look a reset grant up by digest.

    Unscoped by necessity - the person clicking the link is not signed in, and
    the digest of a 32-byte secret is itself the only identifier. ``token_hash``
    is unique, so this cannot return another tenant's row by accident; the
    service still re-checks the tenant of the user it leads to.
    """
    stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


def record_login_attempt(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    email: str,
    succeeded: bool,
    user_id: uuid.UUID | None = None,
    failure_reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> LoginAttempt:
    """Stage one row of sign-in history.

    Not flushed here: the caller owns the transaction, and on a failed sign-in
    the attempt row, the counter increment and the audit entry must land or roll
    back together.
    """
    attempt = LoginAttempt(
        company_id=company_id,
        email=email,
        user_id=user_id,
        succeeded=succeeded,
        failure_reason=failure_reason,
        ip_address=ip_address,
        user_agent=user_agent,
        created_by_id=user_id,
    )
    session.add(attempt)
    return attempt


# -----------------------------------------------------------------------------
# Password reset grants
# -----------------------------------------------------------------------------


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    """Single-use reset grants for one tenant."""

    model = PasswordResetToken

    def create_token(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        email_token_ciphertext: str | None,
        expires_at: datetime,
        requested_ip: str | None,
    ) -> PasswordResetToken:
        record = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            email_token_ciphertext=email_token_ciphertext,
            expires_at=expires_at,
            requested_ip=requested_ip,
        )
        record.created_by_id = user_id
        return self.add(record)

    async def mark_used(self, record: PasswordResetToken) -> PasswordResetToken:
        record.used_at = utcnow()
        record.email_token_ciphertext = None
        await self.session.flush()
        return record


class PhoneVerificationRepository:
    """Tenant-and-user-scoped profile records for SMS phone verification."""

    def __init__(self, session: AsyncSession, *, company_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.session = session
        self.company_id = company_id
        self.user_id = user_id

    async def account_for_update(self) -> User | None:
        stmt = (
            select(User)
            .where(
                User.id == self.user_id,
                User.company_id == self.company_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .with_for_update(of=User)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def challenge_for_update(self) -> UserPhoneVerificationChallenge | None:
        stmt = (
            select(UserPhoneVerificationChallenge)
            .where(
                UserPhoneVerificationChallenge.company_id == self.company_id,
                UserPhoneVerificationChallenge.user_id == self.user_id,
            )
            .with_for_update(of=UserPhoneVerificationChallenge)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add_challenge(
        self, challenge: UserPhoneVerificationChallenge
    ) -> UserPhoneVerificationChallenge:
        self.session.add(challenge)
        return challenge


async def session_ids_for(
    session: AsyncSession, company_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> list[str]:
    """Map session row ids to their ``session_id`` claims, tenant-scoped."""
    if not ids:
        return []
    stmt = select(UserSession.session_id).where(
        UserSession.company_id == company_id, UserSession.id.in_(list(ids))
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


__all__ = [
    "PasswordResetTokenRepository",
    "PhoneVerificationRepository",
    "find_accounts_by_email",
    "find_reset_token",
    "get_account",
    "get_account_summary",
    "increment_failed_attempts",
    "record_login_attempt",
    "session_ids_for",
]
