"""Queries over the audit trail and the approval queue.

``audit_logs`` is the largest and fastest-growing table in the system, so the
shape of every statement here is chosen against a specific index declared on the
model, and nothing in this file can produce an unbounded scan:

* the feed filters on ``company_id`` plus a mandatory ``created_at`` window and
  pages by keyset - ``ix_audit_logs_company_created`` covers both, and the
  row-value cursor comparison lets PostgreSQL seek straight into the index at any
  depth instead of counting rows off the front (section 31);
* the entity trail filters on ``(company_id, entity_type, entity_id)``, which is
  ``ix_audit_logs_entity``. That triple is selective enough on its own that a
  date window would only make the query slower without bounding anything, which
  is why the trail is the one read here that does not take one;
* the listing projections drop ``old_values``, ``new_values``, ``context`` and
  ``user_agent``. Those four are the wide columns; a 200-row page carrying them
  is megabytes of JSONB nobody reads. ``load_only(..., raiseload=True)`` is used
  rather than a bare projection so that touching a column the query did not fetch
  raises immediately instead of emitting a lazy SELECT per row (section 32).

There are deliberately no ``create``, ``update`` or ``delete`` methods for
``audit_logs``: the mutating helpers inherited from
:class:`~src.common.repository.BaseRepository` are overridden to refuse. That is
a second line of defence only - the migration installs a PostgreSQL trigger that
raises on UPDATE and DELETE against ``audit_logs``, so the guarantee does not
depend on every future caller going through this class (master rules section 41).
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

from sqlalchemy import Select
from sqlalchemy.orm import load_only

from src.common.repository import BaseRepository
from src.core.pagination import CursorParams, PageParams
from src.core.responses import CursorPagination, OffsetPagination
from src.modules.audit.models import ApprovalRequest, AuditLog
from src.modules.audit.schemas import (
    ApprovalFilters,
    ApprovalStatus,
    AuditLogFilters,
    AuditWindow,
)

#: Columns a listing needs. Everything absent from this tuple is either wide
#: JSONB or request forensics that only matter once someone opens a single entry.
_LIST_COLUMNS = (
    AuditLog.action,
    AuditLog.entity_type,
    AuditLog.entity_id,
    AuditLog.entity_code,
    AuditLog.summary,
    AuditLog.changed_fields,
    AuditLog.request_id,
    AuditLog.actor_label,
    AuditLog.created_by_id,
    AuditLog.created_at,
)

_IMMUTABLE = (
    "audit_logs is append-only: rows are written by record_audit inside the "
    "transaction of the change they describe, and UPDATE/DELETE are blocked by a "
    "database trigger."
)


class AuditLogRepository(BaseRepository[AuditLog]):
    """Read-only persistence for :class:`~src.modules.audit.models.AuditLog`."""

    model = AuditLog

    # --- statement construction --------------------------------------------

    def _list_projection(self) -> Select[tuple[AuditLog]]:
        return self._scoped().options(load_only(*_LIST_COLUMNS, raiseload=True))

    def _apply_filters(
        self,
        stmt: Select[Any],
        filters: AuditLogFilters,
        window: AuditWindow,
    ) -> Select[Any]:
        """Window first, then the optional predicates.

        The window is applied unconditionally and is not derived from the
        filters: an omitted date range must never widen into "everything", which
        on this table is a denial of service rather than a large response.
        """
        stmt = stmt.where(
            AuditLog.created_at >= window.start,
            AuditLog.created_at <= window.end,
        )
        if filters.entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == filters.entity_id)
        if filters.action is not None:
            stmt = stmt.where(AuditLog.action == filters.action)
        if filters.user_id is not None:
            stmt = stmt.where(AuditLog.created_by_id == filters.user_id)
        if filters.request_id is not None:
            # ix_audit_logs_request is not tenant-scoped, but _scoped() has
            # already added company_id, so one tenant cannot read another
            # tenant's rows by guessing a correlation id.
            stmt = stmt.where(AuditLog.request_id == filters.request_id)
        return stmt

    # --- reads --------------------------------------------------------------

    async def list_logs(
        self,
        filters: AuditLogFilters,
        window: AuditWindow,
        params: CursorParams,
    ) -> tuple[list[AuditLog], CursorPagination]:
        """The audit feed: newest first, keyset paginated, always windowed."""
        stmt = self._apply_filters(self._list_projection(), filters, window)
        return await self.list_keyset(params, stmt=stmt)

    async def entity_history(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        params: CursorParams,
    ) -> tuple[list[AuditLog], CursorPagination]:
        """Every recorded action against one record, newest first.

        Newest first rather than chronological because that is the order the
        keyset cursor is defined in - ``(created_at DESC, id DESC)`` - and paging
        a trail in ascending order with a descending cursor is how rows go
        missing in the middle of a list. A caller rendering a timeline reverses
        one page; a caller looking for "what happened last" needs no page at all.
        """
        stmt = self._list_projection().where(
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
        )
        return await self.list_keyset(params, stmt=stmt)

    async def get_full(self, log_id: uuid.UUID) -> AuditLog | None:
        """One entry with its before/after values - the detail read.

        No projection here: the whole point of opening a single entry is the
        diff, and it is one row.
        """
        return await self.get(log_id)

    # --- writes: refused ----------------------------------------------------
    #
    # BaseRepository offers these to every module. On the audit table they must
    # not be reachable, so they fail loudly here rather than being left available
    # and merely unused.

    def add(self, instance: AuditLog) -> AuditLog:
        raise TypeError(_IMMUTABLE)

    async def soft_delete(self, instance: AuditLog) -> AuditLog:
        raise TypeError(_IMMUTABLE)

    async def hard_delete(self, instance: AuditLog) -> None:
        raise TypeError(_IMMUTABLE)

    async def bulk_update(self, ids: Any, values: Any) -> NoReturn:
        raise TypeError(_IMMUTABLE)

    async def bulk_delete(self, ids: Any) -> NoReturn:
        raise TypeError(_IMMUTABLE)


class ApprovalRequestRepository(BaseRepository[ApprovalRequest]):
    """Persistence for the approval queue.

    Unlike the audit log this table *is* updated - once, when a decision is
    recorded - so the row-level lock in :meth:`get_for_decision` matters: two
    approvers clicking at the same moment must not both pass the "still PENDING"
    check and produce two audit entries for one decision.
    """

    model = ApprovalRequest

    async def list_pending(
        self,
        params: PageParams,
        *,
        decidable_actions: frozenset[str] | None,
        filters: ApprovalFilters | None = None,
    ) -> tuple[list[ApprovalRequest], OffsetPagination]:
        """Pending requests this caller is allowed to see.

        ``decidable_actions`` is the set of ``requested_action`` values the
        caller holds the permission for; ``None`` means unrestricted, which is
        only ever passed for a superuser. An empty set is *not* the same thing
        and short-circuits to an empty page rather than degenerating into "no
        filter" - the failure mode that turns an authorisation bug into a data
        leak.

        Offset paginated, not keyset: a pending queue is small and bounded by
        definition, and approvers page through it by number.
        """
        if decidable_actions is not None and not decidable_actions:
            return [], OffsetPagination(
                page=params.page,
                limit=params.limit,
                total=0,
                total_pages=1,
                has_next=False,
                has_previous=params.page > 1,
            )

        stmt = self._scoped().where(ApprovalRequest.status == ApprovalStatus.PENDING.value)
        if decidable_actions is not None:
            stmt = stmt.where(ApprovalRequest.requested_action.in_(sorted(decidable_actions)))
        if filters is not None and filters.entity_type is not None:
            stmt = stmt.where(ApprovalRequest.entity_type == filters.entity_type)

        # Oldest first: an approval queue is worked from the front, and the
        # request that has been waiting longest is the one most likely to be
        # blocking someone. This is the only list in the module that is not
        # newest-first.
        #
        # Ordered by id rather than by created_at: the primary key is a UUIDv7,
        # so it is both chronological and unique. created_at alone is not unique
        # - every row written by one transaction shares its `now()` - and a
        # non-unique sort key is how rows repeat or vanish between pages.
        return await self.list_page(params, stmt=stmt, order_by=ApprovalRequest.id.asc())

    async def get_for_decision(self, approval_id: uuid.UUID) -> ApprovalRequest | None:
        """Load one request with ``FOR UPDATE`` held for the rest of the transaction.

        The lock is taken before the status is read, so the check and the write
        are one atomic step; without it a concurrent reject could land between
        them and be silently overwritten by an approve.
        """
        stmt = self._scoped().where(ApprovalRequest.id == approval_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["ApprovalRequestRepository", "AuditLogRepository"]
