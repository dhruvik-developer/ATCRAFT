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
from datetime import datetime
from typing import Any

from sqlalchemy import Select
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
        return stmt.where((Document.expires_at.is_(None)) | (Document.expires_at > now))

    async def get_live(
        self, document_id: uuid.UUID, now: datetime, *, principal: Principal | None = None
    ) -> Document | None:
        """Fetch a document that is in this tenant and has not expired."""
        stmt = self._not_expired(
            self._scoped(principal=principal).where(Document.id == document_id), now
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

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

    async def list_expired(self, now: datetime, *, limit: int = MAX_PURGE_BATCH) -> list[Document]:
        """One batch of lapsed documents, oldest expiry first.

        Ordered by ``expires_at`` so repeated batches make monotonic progress
        through the backlog instead of re-reading the same arbitrary slice.
        """
        stmt = (
            self._scoped()
            .where(Document.expires_at.is_not(None), Document.expires_at <= now)
            .order_by(Document.expires_at.asc(), Document.id.asc())
            .limit(min(limit, MAX_PURGE_BATCH))
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())


__all__ = ["MAX_PURGE_BATCH", "DocumentRepository"]
