"""Queries for the notification feed, the badge and delivery preferences.

Two of these are worth reading before changing anything.

**The feed is unread-first *and* keyset-paginated.** Those two requirements fight
each other: :class:`~src.core.pagination.Cursor` encodes ``(created_at, id)``,
but the feed's sort key is ``(is_read, created_at, id)``, so the ordinary
row-value predicate would skip every *read* notification newer than the last
unread one on the previous page - silently losing rows in the middle of the list.
:meth:`NotificationRepository.list_feed` therefore recovers the missing leading
component with one primary-key lookup of the cursor row and pages within the
partition it names. That lookup is an index hit on a single row, and it buys a
feed that is correct at any depth without a second cursor format.

**A fan-out is one statement, not one per recipient.** ``notify`` may address
every approver in a company, and the grouping rule ("at most one *unread* row per
recipient, type and group key") has to hold even when two delay sweeps run at
once. Both fall out of a single ``INSERT ... ON CONFLICT DO UPDATE`` against the
partial unique index declared on the model: conflicting rows bump
``occurrence_count`` instead of inserting, PostgreSQL arbitrates the race, and no
caller ever sees an integrity error it would have to translate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Select, and_, case, cast, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.deps import TenantContext
from src.core.logging import get_logger
from src.core.pagination import Cursor, CursorParams
from src.core.responses import CursorPagination
from src.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationPreference,
    NotificationType,
    PushSubscription,
)
from src.modules.notifications.schemas import NotificationFilters

log = get_logger(__name__)

#: Rows per INSERT. A fan-out to an entire company still runs as a handful of
#: bounded statements rather than one enormous VALUES list that would hold locks
#: and a single WAL record for as long as it takes to write.
INSERT_BATCH_SIZE = 200

#: Ceiling for the unread badge. Counting an unbounded backlog on every poll is
#: the kind of avoidable work section 30 warns about, and no bell renders a
#: number this large exactly.
UNREAD_COUNT_CEILING = 500

#: Rows one "mark everything read" request may touch. A single UPDATE over a
#: 100k-row backlog would hold that many row locks in one transaction; the client
#: repeats the call while ``remaining_unread`` is non-zero.
MARK_ALL_BATCH = 2_000

#: Upper bound on recipients resolved from a role. Larger than any plausible
#: approver group, so hitting it means a role was mis-assigned - which is logged
#: rather than silently obeyed.
MAX_ROLE_RECIPIENTS = 500

#: The nil UUID, used as the actor of a notification raised by a scheduled job
#: rather than by a person. It is never written to a foreign key column:
#: ``created_by_id`` stays NULL for system-raised alerts. See
#: :meth:`NotificationRepository.for_company`.
SYSTEM_ACTOR_ID = uuid.UUID(int=0)


class NotificationRepository(BaseRepository[Notification]):
    """Persistence for notifications and preferences.

    Both tables live here because they are read together: a fan-out resolves
    preferences and writes notifications in one operation, and splitting them
    into two repositories would mean two objects sharing one transaction for no
    gain.
    """

    model = Notification

    @classmethod
    def for_company(
        cls,
        session: AsyncSession,
        company_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> NotificationRepository:
        """Repository for a caller with no request principal.

        ``notify`` is called from Celery beat sweeps as well as from request
        handlers, and :class:`~src.core.deps.TenantContext` requires a user id.
        The nil placeholder is only ever used for stamping, which these code
        paths do explicitly, so it cannot reach a column.
        """
        tenant = TenantContext(company_id=company_id, user_id=actor_id or SYSTEM_ACTOR_ID)
        return cls(session, tenant)

    # --- feed ---------------------------------------------------------------

    def _feed_stmt(self, user_id: uuid.UUID, filters: NotificationFilters) -> Select[Any]:
        """Everything the recipient may see in the bell, unfiltered by position.

        The channel predicate is not cosmetic: a notification whose recipient
        turned the in-app channel off exists only to drive an e-mail, and showing
        it in the feed would make that preference meaningless.
        """
        stmt = self._scoped().where(
            Notification.user_id == user_id,
            Notification.channels.contains([NotificationChannel.IN_APP.value]),
        )
        if filters.unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        if filters.notification_type is not None:
            stmt = stmt.where(Notification.notification_type == filters.notification_type)
        if filters.severity is not None:
            stmt = stmt.where(Notification.severity == filters.severity)
        return stmt

    async def _cursor_partition_is_read(self, user_id: uuid.UUID, position: Cursor) -> bool:
        """Which half of the feed the previous page ended in.

        A cursor row that no longer exists (retention sweep, or a row the user
        cannot see any more) resolves to the unread partition: that yields a
        superset of the correct next page, so the client may see a repeat but
        never a gap.
        """
        stmt = select(Notification.is_read).where(
            Notification.company_id == self.tenant.company_id,
            Notification.user_id == user_id,
            Notification.id == position.id,
        )
        found = (await self.session.execute(stmt)).scalar_one_or_none()
        return bool(found)

    async def list_feed(
        self,
        user_id: uuid.UUID,
        filters: NotificationFilters,
        params: CursorParams,
    ) -> tuple[list[Notification], CursorPagination]:
        """One page of the feed: unread first, then newest first within each half."""
        stmt = self._feed_stmt(user_id, filters)
        position = params.decoded

        if position is not None:
            older = tuple_(Notification.created_at, Notification.id) < (
                position.created_at,
                position.id,
            )
            if filters.unread_only or await self._cursor_partition_is_read(user_id, position):
                # Already inside the read half (or there is only one half), so
                # the ordinary keyset predicate is exact.
                stmt = stmt.where(older)
                if not filters.unread_only:
                    stmt = stmt.where(Notification.is_read.is_(True))
            else:
                # Still in the unread half: take the remaining older unread rows,
                # and then the read half in full. Without the second disjunct a
                # read notification newer than the last unread one would be
                # skipped for ever.
                stmt = stmt.where(
                    or_(
                        and_(Notification.is_read.is_(False), older),
                        Notification.is_read.is_(True),
                    )
                )

        stmt = stmt.order_by(
            Notification.is_read.asc(),
            Notification.created_at.desc(),
            Notification.id.desc(),
        ).limit(params.limit + 1)

        rows = list((await self.session.execute(stmt)).scalars().unique().all())
        has_next = len(rows) > params.limit
        rows = rows[: params.limit]

        next_cursor = (
            Cursor(created_at=rows[-1].created_at, id=rows[-1].id).encode()
            if has_next and rows
            else None
        )
        return rows, CursorPagination(
            limit=params.limit, next_cursor=next_cursor, has_next=has_next
        )

    async def unread_count(
        self, user_id: uuid.UUID, *, ceiling: int = UNREAD_COUNT_CEILING
    ) -> tuple[int, bool]:
        """Unread badge value and whether it hit the ceiling.

        The LIMIT is inside the subquery so PostgreSQL stops walking the partial
        unread index once it has enough rows, instead of counting a backlog the
        UI would render as "500+" anyway.
        """
        capped_rows = (
            select(text("1"))
            .select_from(Notification)
            .where(
                Notification.company_id == self.tenant.company_id,
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
                Notification.channels.contains([NotificationChannel.IN_APP.value]),
            )
            .limit(ceiling)
            .subquery()
        )
        total = int(
            (await self.session.execute(select(func.count()).select_from(capped_rows))).scalar_one()
        )
        return total, total >= ceiling

    async def get_for_user(
        self, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> Notification | None:
        """Fetch one notification belonging to this recipient.

        Scoped by ``user_id`` as well as by tenant: another employee's
        notification must be indistinguishable from one that does not exist.
        """
        stmt = self._scoped().where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # --- writes -------------------------------------------------------------

    async def upsert_many(self, rows: Sequence[dict[str, Any]]) -> list[Notification]:
        """Insert a fan-out, collapsing onto any matching open group row.

        Returns the resulting notifications - inserted and bumped alike - so the
        caller can audit and queue e-mail for exactly the rows that exist.
        """
        if not rows:
            return []

        written: list[Notification] = []
        for start in range(0, len(rows), INSERT_BATCH_SIZE):
            batch = list(rows[start : start + INSERT_BATCH_SIZE])
            insert_stmt = pg_insert(Notification).values(batch)
            stmt = (
                insert_stmt.on_conflict_do_update(
                    index_elements=["company_id", "user_id", "notification_type", "group_key"],
                    # Must match the model's partial index predicate exactly, or
                    # PostgreSQL cannot infer the arbiter index.
                    index_where=text("group_key IS NOT NULL AND is_read = false"),
                    set_={
                        # The newest wording wins: "PO-1042 is 6 days late"
                        # replaces "3 days late" rather than accumulating.
                        "title": insert_stmt.excluded.title,
                        "body": insert_stmt.excluded.body,
                        "severity": insert_stmt.excluded.severity,
                        "entity_type": insert_stmt.excluded.entity_type,
                        "entity_id": insert_stmt.excluded.entity_id,
                        "action_url": insert_stmt.excluded.action_url,
                        "channels": insert_stmt.excluded.channels,
                        "delivered_at": insert_stmt.excluded.delivered_at,
                        "updated_at": insert_stmt.excluded.updated_at,
                        "updated_by_id": insert_stmt.excluded.updated_by_id,
                        "occurrence_count": Notification.occurrence_count + 1,
                        # created_at is deliberately NOT refreshed: the feed is
                        # keyset-paginated on it, and moving a row's sort key
                        # under an open cursor makes rows repeat or vanish.
                    },
                )
                .returning(Notification)
                .execution_options(populate_existing=True)
            )
            result = await self.session.execute(stmt)
            written.extend(result.scalars().all())

        return written

    async def mark_read_batch(
        self, user_id: uuid.UUID, moment: datetime, *, limit: int = MARK_ALL_BATCH
    ) -> int:
        """Mark up to ``limit`` unread notifications read. Returns how many."""
        targets = (
            select(Notification.id)
            .where(
                Notification.company_id == self.tenant.company_id,
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
                Notification.channels.contains([NotificationChannel.IN_APP.value]),
            )
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
        stmt = (
            update(Notification)
            .where(
                Notification.company_id == self.tenant.company_id,
                Notification.user_id == user_id,
                Notification.id.in_(targets.scalar_subquery()),
            )
            .values(is_read=True, read_at=moment, updated_at=moment, updated_by_id=user_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    # --- preferences --------------------------------------------------------

    async def preferences_for_type(
        self, user_ids: Sequence[uuid.UUID], notification_type: NotificationType
    ) -> dict[uuid.UUID, tuple[bool, bool, bool]]:
        """``{user_id: (in_app_enabled, email_enabled)}`` for one type.

        One query for the whole recipient list - the fan-out must not turn into a
        preference lookup per person (section 32). Users with no stored row are
        absent from the mapping and take the module defaults.
        """
        if not user_ids:
            return {}
        stmt = select(
            NotificationPreference.user_id,
            NotificationPreference.in_app_enabled,
            NotificationPreference.email_enabled,
            NotificationPreference.push_enabled,
        ).where(
            NotificationPreference.company_id == self.tenant.company_id,
            NotificationPreference.notification_type == notification_type,
            NotificationPreference.user_id.in_(list(user_ids)),
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: (row[1], row[2], row[3]) for row in rows}

    async def users_with_push_devices(self, user_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        from src.core.clock import utcnow

        stmt = (
            select(PushSubscription.user_id)
            .where(
                PushSubscription.company_id == self.tenant.company_id,
                PushSubscription.user_id.in_(user_ids),
                or_(PushSubscription.expires_at.is_(None), PushSubscription.expires_at > utcnow()),
            )
            .distinct()
        )
        return set((await self.session.execute(stmt)).scalars())

    async def preferences_for_user(self, user_id: uuid.UUID) -> list[NotificationPreference]:
        """Every stored preference row for one user - bounded by the enum size."""
        stmt = (
            select(NotificationPreference)
            .where(
                NotificationPreference.company_id == self.tenant.company_id,
                NotificationPreference.user_id == user_id,
            )
            .order_by(NotificationPreference.notification_type.asc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def upsert_preferences(
        self, user_id: uuid.UUID, rows: Sequence[dict[str, Any]]
    ) -> list[NotificationPreference]:
        """Write the submitted preference rows in one statement."""
        if not rows:
            return []
        push_choices = {row["notification_type"]: row.get("push_enabled") for row in rows}
        insert_stmt = pg_insert(NotificationPreference).values(
            [
                {
                    **row,
                    "push_enabled": row.get("push_enabled")
                    if row.get("push_enabled") is not None
                    else True,
                }
                for row in rows
            ]
        )
        stmt = (
            insert_stmt.on_conflict_do_update(
                index_elements=["company_id", "user_id", "notification_type"],
                set_={
                    "in_app_enabled": insert_stmt.excluded.in_app_enabled,
                    "email_enabled": insert_stmt.excluded.email_enabled,
                    "push_enabled": func.coalesce(
                        cast(
                            case(push_choices, value=insert_stmt.excluded.notification_type),
                            Boolean,
                        ),
                        NotificationPreference.push_enabled,
                    ),
                    "updated_at": insert_stmt.excluded.updated_at,
                    "updated_by_id": insert_stmt.excluded.updated_by_id,
                },
            )
            .returning(NotificationPreference)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # --- recipient resolution ----------------------------------------------

    async def user_ids_for_roles(self, role_codes: Iterable[str]) -> list[uuid.UUID]:
        """Active users holding any of the named roles, within this tenant.

        A three-table join rather than "load the roles, then load their members":
        the latter is the N+1 that section 32 exists to prevent, and this query is
        on the path of every approval request.
        """
        codes = sorted({code.strip().upper() for code in role_codes if code and code.strip()})
        if not codes:
            return []

        # Imported here rather than at module scope: the notifications module is
        # imported by every other module through ``notify``, and a top-level
        # import of the identity models would put a cycle one refactor away.
        from src.modules.users.models import Role, User, user_roles

        stmt = (
            select(User.id)
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(
                User.company_id == self.tenant.company_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                Role.company_id == self.tenant.company_id,
                Role.code.in_(codes),
                User.vendor_id.is_(None),
                Role.is_active.is_(True),
                Role.deleted_at.is_(None),
            )
            .distinct()
            .order_by(User.id.asc())
            .limit(MAX_ROLE_RECIPIENTS + 1)
        )
        found = list((await self.session.execute(stmt)).scalars().all())

        if len(found) > MAX_ROLE_RECIPIENTS:
            # Not silently obeyed: a notification aimed at 500+ people is almost
            # always a mis-assigned role, and the operator needs to know some
            # recipients were dropped rather than discovering it from a
            # complaint.
            log.warning(
                "notifications.role_fanout_truncated",
                company_id=str(self.tenant.company_id),
                role_codes=codes,
                limit=MAX_ROLE_RECIPIENTS,
            )
            found = found[:MAX_ROLE_RECIPIENTS]
        return found

    async def user_ids_for_vendor(
        self, vendor_id: uuid.UUID, *, permission: str | None = None
    ) -> list[uuid.UUID]:
        """Active recipients of one vendor, with the same grants/denials as HTTP."""
        from sqlalchemy.orm import selectinload

        from src.core.permissions import resolve_role_grants
        from src.modules.users.models import User

        users = (
            (
                await self.session.execute(
                    select(User)
                    .options(selectinload(User.roles))
                    .where(
                        User.company_id == self.tenant.company_id,
                        User.vendor_id == vendor_id,
                        User.is_active.is_(True),
                        User.deleted_at.is_(None),
                    )
                    .order_by(User.id)
                    .limit(MAX_ROLE_RECIPIENTS)
                )
            )
            .scalars()
            .all()
        )
        return [
            u.id
            for u in users
            if permission is None
            or permission
            in resolve_role_grants(u.roles, u.extra_permissions, u.denied_permissions)[0]
        ]

    async def user_ids_with_permission(self, permission: str) -> list[uuid.UUID]:
        """Internal recipients only; direct denials override role membership."""
        from sqlalchemy.orm import selectinload

        from src.core.permissions import resolve_role_grants
        from src.modules.users.models import User

        users = (
            (
                await self.session.execute(
                    select(User)
                    .options(selectinload(User.roles))
                    .where(
                        User.company_id == self.tenant.company_id,
                        User.vendor_id.is_(None),
                        User.is_active.is_(True),
                        User.deleted_at.is_(None),
                    )
                    .order_by(User.id)
                    .limit(MAX_ROLE_RECIPIENTS)
                )
            )
            .scalars()
            .all()
        )
        return [
            u.id
            for u in users
            if u.is_superuser
            or permission
            in resolve_role_grants(u.roles, u.extra_permissions, u.denied_permissions)[0]
        ]

    async def internal_user_ids(self, user_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        from src.modules.users.models import User

        return list(
            (
                await self.session.execute(
                    select(User.id).where(
                        User.company_id == self.tenant.company_id,
                        User.id.in_(user_ids),
                        User.vendor_id.is_(None),
                        User.is_active.is_(True),
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


__all__ = [
    "INSERT_BATCH_SIZE",
    "MARK_ALL_BATCH",
    "MAX_ROLE_RECIPIENTS",
    "SYSTEM_ACTOR_ID",
    "UNREAD_COUNT_CEILING",
    "NotificationRepository",
]
