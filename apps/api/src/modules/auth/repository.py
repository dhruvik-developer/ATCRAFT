"""Queries behind authentication.

This module owns every statement the auth service issues. Two shapes are worth
reading twice, because neither fits the usual tenant-scoped repository:

* **Pre-authentication lookups are module-level functions, not repository
  methods.** Sign-in has no principal and therefore no
  :class:`~src.core.deps.TenantContext`: the company is a *result* of resolving
  the credentials, never an input to it. Those functions resolve the tenant from
  the email (optionally narrowed by a company code) and are the only place in
  the codebase that reads ``users`` without a tenant filter. They compensate by
  filtering on the company being live, so an account in a deactivated or deleted
  tenant cannot sign in.
* **Chain revocation is a recursive CTE**, not a Python loop of
  ``SELECT``/``UPDATE``. A replayed refresh token means the whole token family
  is suspect, and closing that family one round-trip per generation leaves a
  window in which the attacker's newest token still works.

Once a caller *is* authenticated, everything goes through
:class:`UserSessionRepository` and :class:`PasswordResetTokenRepository`, which
are ordinary tenant-scoped repositories.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.clock import utcnow
from src.core.pagination import PageParams, paginate
from src.core.responses import OffsetPagination

# Read-only references to tables owned by other modules. Auth authenticates
# against the identity master; it never writes to ``users`` outside the narrow
# credential columns the service updates on the instance it loaded here.
from src.modules.organization.models import Company
from src.modules.users.models import LoginAttempt, PasswordResetToken, User, UserSession

#: Columns of ``users`` the sign-in path actually needs. Selecting the whole row
#: would drag ``preferences`` (JSONB) into every login for no reason - but the
#: service mutates the loaded instance (counters, hash upgrade), so the ORM
#: entity is loaded rather than a column tuple. The narrow projection is used
#: only where nothing is written back; see :func:`get_account_summary`.
_ACCOUNT_SUMMARY_COLUMNS = (
    User.must_change_password,
    User.mfa_enabled,
    User.last_login_at,
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
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID
) -> User | None:
    """Load one account for a credential operation (refresh, password change)."""
    stmt = _live_account_filter(select(User)).where(
        User.id == user_id, User.company_id == company_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_account_summary(
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID
) -> tuple[bool, bool, datetime | None] | None:
    """The three account flags ``/auth/me`` adds to the principal.

    A column projection rather than the entity: ``/auth/me`` is polled by every
    page load, and it needs three fields out of a wide row it will not modify.
    """
    stmt = select(*_ACCOUNT_SUMMARY_COLUMNS).where(
        User.id == user_id,
        User.company_id == company_id,
        User.deleted_at.is_(None),
    )
    row = (await session.execute(stmt)).first()
    return (bool(row[0]), bool(row[1]), row[2]) if row is not None else None


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
# Sessions
# -----------------------------------------------------------------------------


class UserSessionRepository(BaseRepository[UserSession]):
    """Refresh-token sessions for one tenant."""

    model = UserSession
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "created_at": UserSession.created_at,
        "last_used_at": UserSession.last_used_at,
        "expires_at": UserSession.expires_at,
    }

    async def get_by_session_id(self, session_id: str) -> UserSession | None:
        return await self.get_by_field("session_id", session_id)

    async def list_active_for_user(
        self, user_id: uuid.UUID, params: PageParams
    ) -> tuple[list[UserSession], OffsetPagination]:
        """Live sessions, most recently used first.

        ``last_used_at`` is NULL until a session is first refreshed, so the sort
        has to say where the nulls go - otherwise PostgreSQL puts them first
        under DESC and a brand-new session outranks the one being used.
        """
        stmt = (
            self._scoped()
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
            .order_by(
                UserSession.last_used_at.desc().nullslast(),
                UserSession.created_at.desc(),
            )
        )
        # paginate() directly rather than list_page(): the ordering is two
        # columns with different null handling, which does not survive being
        # passed as a single order_by argument.
        return await paginate(self.session, stmt, params)

    async def count_active_for_user(self, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.company_id == self.tenant.company_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def revoke(self, record: UserSession, *, reason: str) -> UserSession:
        """Close one session. The caller has already validated the transition."""
        record.revoked_at = utcnow()
        record.revoked_reason = reason
        await self.session.flush()
        return record

    async def revoke_all_for_user(
        self,
        user_id: uuid.UUID,
        *,
        reason: str,
        keep_session_id: str | None = None,
    ) -> list[str]:
        """Close every live session for a user in one statement.

        Returns the ``session_id`` of each row actually closed, because each one
        has a cached "is this session live?" entry that must be dropped - see
        :func:`src.core.deps._session_is_live`. A count would not be enough.
        """
        stmt = (
            update(UserSession)
            .where(
                UserSession.company_id == self.tenant.company_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow(), revoked_reason=reason)
            .returning(UserSession.session_id)
            .execution_options(synchronize_session=False)
        )
        if keep_session_id:
            stmt = stmt.where(UserSession.session_id != keep_session_id)
        return [row[0] for row in (await self.session.execute(stmt)).all()]

    async def revoke_chain(self, root: UserSession, *, reason: str) -> list[str]:
        """Close the entire rotation family a session belongs to.

        Walks ``replaced_by_id`` in both directions: a replayed token proves the
        family leaked, and the live descendant of the presented token is exactly
        the one an attacker would be holding. Written as a recursive CTE so the
        whole family closes in a single statement - a Python walk would leave the
        newest session usable for the duration of the walk.

        The join condition references the recursive term once (PostgreSQL allows
        no more), matching parents and children in the same step.
        """
        sql = text(
            """
            WITH RECURSIVE chain AS (
                SELECT id, replaced_by_id
                FROM user_sessions
                WHERE id = :root_id AND company_id = :company_id
                UNION
                SELECT s.id, s.replaced_by_id
                FROM user_sessions s
                JOIN chain c
                  ON s.id = c.replaced_by_id OR s.replaced_by_id = c.id
                WHERE s.company_id = :company_id
            )
            UPDATE user_sessions
               SET revoked_at = :now, revoked_reason = :reason
             WHERE id IN (SELECT id FROM chain)
               AND company_id = :company_id
               AND revoked_at IS NULL
            RETURNING session_id
            """
        )
        result = await self.session.execute(
            sql,
            {
                "root_id": root.id,
                "company_id": self.tenant.company_id,
                "now": utcnow(),
                "reason": reason,
            },
        )
        return [row[0] for row in result.all()]

    def create_session(
        self,
        *,
        user_id: uuid.UUID,
        session_id: str,
        refresh_token_hash: str,
        refresh_jti: str,
        expires_at: datetime,
        ip_address: str | None,
        user_agent: str | None,
        device_label: str | None,
    ) -> UserSession:
        """Stage a new session row. Flushed by the caller inside its transaction."""
        record = UserSession(
            user_id=user_id,
            session_id=session_id,
            refresh_token_hash=refresh_token_hash,
            refresh_jti=refresh_jti,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent[:512] if user_agent else None,
            device_label=device_label,
            last_used_at=utcnow(),
        )
        record.created_by_id = user_id
        return self.add(record)


# -----------------------------------------------------------------------------
# Password reset grants
# -----------------------------------------------------------------------------


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    """Single-use reset grants for one tenant."""

    model = PasswordResetToken

    async def invalidate_outstanding(self, user_id: uuid.UUID) -> int:
        """Burn any unused grant before issuing a new one.

        Only the most recent link may work: otherwise a request made by an
        attacker who later loses access to the mailbox still leaves a valid
        token behind, and the user has no way to cancel it.
        """
        stmt = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.company_id == self.tenant.company_id,
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        return int((await self.session.execute(stmt)).rowcount or 0)

    def create_token(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        requested_ip: str | None,
    ) -> PasswordResetToken:
        record = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            requested_ip=requested_ip,
        )
        record.created_by_id = user_id
        return self.add(record)

    async def mark_used(self, record: PasswordResetToken) -> PasswordResetToken:
        record.used_at = utcnow()
        await self.session.flush()
        return record


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
    "UserSessionRepository",
    "find_accounts_by_email",
    "find_reset_token",
    "get_account",
    "get_account_summary",
    "record_login_attempt",
    "session_ids_for",
]
