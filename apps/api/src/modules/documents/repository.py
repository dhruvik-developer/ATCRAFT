"""Queries for stored documents.

Everything that touches the database lives here. The service decides *whether* a
file may be written, read or removed; this file decides *how* the rows are found.

The one thing worth reading twice is :meth:`DocumentRepository.get_live`. A
document past ``expires_at`` must behave as if it were not there - for metadata,
for downloads and for listings alike - so the expiry predicate lives beside the
tenant predicate rather than in a caller that might forget it. The tenant filter
itself comes from :class:`~src.common.repository.BaseRepository`, which is what
makes another company's document a 404 rather than a 403: confirming that an id
exists somewhere else is itself a disclosure.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.document_access import document_read_predicates
from src.common.repository import BaseRepository
from src.core.deps import Principal
from src.core.pagination import SortParams
from src.modules.documents.models import Document
from src.modules.documents.schemas import DocumentFilters

#: Upper bound on one purge batch. The sweep is a background job; taking the
#: whole backlog in a single transaction would hold row locks and an open
#: transaction for as long as the unlinks take.
MAX_PURGE_BATCH = 500


def document_not_expired(now: datetime) -> ColumnElement[bool]:
    """The expiry half of a live document: no retention date, or one still ahead.

    The one statement of the rule. Every read here applies it, and so must a
    query in another module that names document ids for a client to fetch - the
    dealer catalogue's item images - or it lists a file the download answers
    404 for, between the retention date passing and the purge sweep removing it.
    """
    return or_(Document.expires_at.is_(None), Document.expires_at > now)


def _expired(now: datetime) -> ColumnElement[bool]:
    """Past its retention date: the rows the purge deletes. ``ix_documents_expiring`` serves it."""
    return and_(Document.expires_at.is_not(None), Document.expires_at <= now)


async def companies_with_expired_documents(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    """Every company holding a document past its retention date at ``now``.

    Unscoped by necessity: the nightly purge sweep finds the companies it has work
    for before it opens one session per company, and each then reads its own
    rows through :meth:`DocumentRepository.list_expired`, on the same predicate -
    so a company is visited exactly when its purge will find a row.
    """
    stmt = select(Document.company_id).where(_expired(now)).distinct()
    return list((await session.execute(stmt)).scalars().all())


class DocumentRepository(BaseRepository[Document]):
    """Persistence for :class:`~src.modules.documents.models.Document`."""

    model = Document

    def _scoped(
        self,
        *,
        include_deleted: bool = False,
        principal: Principal | None = None,
    ) -> Select[tuple[Document]]:
        return (
            super()
            ._scoped(include_deleted=include_deleted)
            .where(*document_read_predicates(self.tenant, Document.id, principal))
        )

    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "file_name": Document.file_name,
        "size_bytes": Document.size_bytes,
        "category": Document.category,
        "created_at": Document.created_at,
        "expires_at": Document.expires_at,
    }
    #: Only the display name is searchable. The storage key is server-generated
    #: noise that no human would search for, and exposing it to a LIKE pattern
    #: would let a caller confirm the on-disk layout by probing.
    searchable_fields = ("file_name",)

    # --- reads --------------------------------------------------------------

    def _not_expired(self, stmt: Select[Any], now: datetime) -> Select[Any]:
        return stmt.where(document_not_expired(now))

    async def get_live(
        self, document_id: uuid.UUID, now: datetime, *, principal: Principal | None = None
    ) -> Document | None:
        """Fetch a document that is in this tenant and has not expired."""
        stmt = self._not_expired(
            self._scoped(principal=principal).where(Document.id == document_id), now
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def live_ids(
        self,
        document_ids: Sequence[uuid.UUID],
        now: datetime,
        *,
        principal: Principal | None = None,
    ) -> set[uuid.UUID]:
        """Which of these ids are live documents this caller may read, in one statement."""
        wanted = list(dict.fromkeys(document_ids))
        if not wanted:
            return set()
        stmt = self._not_expired(
            self._scoped(principal=principal).where(Document.id.in_(wanted)), now
        ).with_only_columns(Document.id)
        return set((await self.session.execute(stmt)).scalars().all())

    async def get_for_delete(
        self, document_id: uuid.UUID, *, principal: Principal | None = None
    ) -> Document | None:
        """Fetch a document visible to this caller, including expired documents."""
        stmt = self._scoped(principal=principal).where(Document.id == document_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def build_list_stmt(
        self,
        filters: DocumentFilters,
        sort: SortParams,
        *,
        now: datetime,
        principal: Principal | None = None,
    ) -> Select[Any]:
        stmt = self._scoped(principal=principal)
        stmt = self._apply_search(stmt, filters.search)

        if not filters.include_expired:
            stmt = self._not_expired(stmt, now)
        if filters.category is not None:
            stmt = stmt.where(Document.category == filters.category)
        if filters.entity_type is not None:
            stmt = stmt.where(Document.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            stmt = stmt.where(Document.entity_id == filters.entity_id)
        if filters.mime_type is not None:
            stmt = stmt.where(Document.mime_type == filters.mime_type)
        if filters.uploaded_by_id is not None:
            stmt = stmt.where(Document.uploaded_by_id == filters.uploaded_by_id)
        if filters.is_public is not None:
            stmt = stmt.where(Document.is_public == filters.is_public)

        # Newest first is the only ordering that makes sense as a default for an
        # attachment list; id breaks ties so a row cannot straddle two pages.
        default_order = sort.resolve(self.sortable_fields, Document.created_at.desc())
        return stmt.order_by(default_order, Document.id.desc())

    async def list_expired(
        self,
        now: datetime,
        *,
        limit: int = MAX_PURGE_BATCH,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[Document]:
        """One batch of lapsed documents, oldest expiry first.

        Ordered by ``(expires_at, id)`` so repeated batches make monotonic
        progress through the backlog instead of re-reading the same arbitrary
        slice. ``after`` is the ``(expires_at, id)`` of the last row an earlier
        batch read: the batch starts past it, so a row whose delete failed is not
        read again - and does not hold up the rows behind it - in the same run.
        """
        stmt = self._scoped().where(_expired(now))
        if after is not None:
            expires_at, document_id = after
            stmt = stmt.where(
                or_(
                    Document.expires_at > expires_at,
                    and_(Document.expires_at == expires_at, Document.id > document_id),
                )
            )
        stmt = stmt.order_by(Document.expires_at.asc(), Document.id.asc()).limit(
            min(limit, MAX_PURGE_BATCH)
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def delete_if_present(self, document_id: uuid.UUID) -> bool:
        """Delete one of this tenant's rows; ``False`` when it is already gone.

        A conditional ``DELETE ... RETURNING`` rather than ``session.delete``: of
        two deleters racing for one row - a person and the nightly purge - the
        second waits on the first's row lock and then matches nothing, so it can
        tell it lost and write no second audit row for the same deletion.
        """
        stmt = (
            delete(Document)
            .where(Document.company_id == self.tenant.company_id, Document.id == document_id)
            .returning(Document.id)
            .execution_options(synchronize_session="fetch")
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


__all__ = [
    "MAX_PURGE_BATCH",
    "DocumentRepository",
    "companies_with_expired_documents",
    "document_not_expired",
]
