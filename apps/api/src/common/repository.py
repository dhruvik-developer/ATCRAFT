"""Base repository: tenant-scoped persistence and query building.

Master rules sections 30/32/74: repositories own persistence and query shape,
services own business rules, routes stay thin. Two things are structural here
rather than left to discipline:

* **Tenant scoping.** Every built statement starts from :meth:`_scoped`, which
  applies ``company_id``. A query that forgets it does not compile into a
  cross-tenant leak, because the filter is not the caller's job.
* **No implicit lazy loading.** Relationships are declared ``lazy="raise"`` on
  the models, so an accidental N+1 raises during development instead of quietly
  issuing 100 queries in production (section 32).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.core.actors import actor_id
from src.core.clock import utcnow
from src.core.database import affected_rows
from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.core.models import Base
from src.core.pagination import CursorParams, PageParams, paginate, paginate_keyset
from src.core.responses import CursorPagination, OffsetPagination

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Persistence for one aggregate, always within one tenant."""

    model: type[ModelT]
    #: Columns a client may sort by. Anything else is rejected - see
    #: :meth:`~src.core.pagination.SortParams.resolve`.
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {}  # noqa: RUF012
    #: Columns included in a free-text search.
    searchable_fields: tuple[str, ...] = ()
    #: How ``get_or_404`` names the record in its message. Defaults to the model's
    #: class name split into words in sentence case ("Physical count"); set it where
    #: that split is wrong (``BomLine`` -> "BOM line", ``QcInspection`` -> "QC inspection")
    #: or where a service and a state machine already share a constant for the
    #: name, so one missing record never answers with two spellings.
    entity_label: str | None = None

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    @classmethod
    def _entity_label(cls) -> str:
        if cls.entity_label:
            return cls.entity_label
        words = re.findall(r"[A-Z][a-z0-9]*", cls.model.__name__)
        if not words:
            return cls.model.__name__
        return " ".join([words[0], *[word.lower() for word in words[1:]]])

    # --- statement construction --------------------------------------------

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[ModelT]]:
        """Base SELECT with tenant isolation (and soft-delete) already applied."""
        stmt = select(self.model).where(self.model.company_id == self.tenant.company_id)  # type: ignore[attr-defined]
        if not include_deleted and hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    def _apply_search(self, stmt: Select[Any], term: str | None) -> Select[Any]:
        """ILIKE across the declared searchable columns.

        Sufficient with a ``pg_trgm`` GIN index on those columns, which is what
        section 62 means by starting with PostgreSQL rather than Elasticsearch.
        """
        if not term or not self.searchable_fields:
            return stmt
        from sqlalchemy import or_

        pattern = f"%{term.strip()}%"
        clauses = [
            getattr(self.model, field).ilike(pattern)
            for field in self.searchable_fields
            if hasattr(self.model, field)
        ]
        return stmt.where(or_(*clauses)) if clauses else stmt

    # --- reads --------------------------------------------------------------

    async def get(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> ModelT | None:
        stmt = self._scoped(include_deleted=include_deleted).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_404(self, entity_id: uuid.UUID) -> ModelT:
        found = await self.get(entity_id)
        if found is None:
            # Deliberately identical to "exists in another tenant" - confirming
            # existence elsewhere is itself a disclosure.
            raise NotFoundError(self._entity_label(), entity_id)
        return found

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[ModelT]:
        """Batch fetch. The answer to a loop of single gets (section 32)."""
        if not ids:
            return []
        stmt = self._scoped().where(self.model.id.in_(list(ids)))  # type: ignore[attr-defined]
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def get_by_field(self, field: str, value: Any) -> ModelT | None:
        stmt = self._scoped().where(getattr(self.model, field) == value)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def exists(self, **filters: Any) -> bool:
        """EXISTS probe - never ``count() > 0``, which would scan the matches."""
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.company_id == self.tenant.company_id  # type: ignore[attr-defined]
            )
        )
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        return bool((await self.session.execute(stmt.limit(1))).scalar_one())

    async def list_page(
        self,
        params: PageParams,
        *,
        stmt: Select[Any] | None = None,
        order_by: Any = None,
    ) -> tuple[list[ModelT], OffsetPagination]:
        query = stmt if stmt is not None else self._scoped()
        if order_by is not None:
            query = query.order_by(order_by)
        elif hasattr(self.model, "created_at"):
            # A deterministic tiebreaker matters: without one, rows can repeat
            # or vanish across pages when the sort key has duplicates.
            query = query.order_by(self.model.created_at.desc(), self.model.id.desc())  # type: ignore[attr-defined]
        return await paginate(self.session, query, params)

    async def list_keyset(
        self,
        params: CursorParams,
        *,
        stmt: Select[Any] | None = None,
    ) -> tuple[list[ModelT], CursorPagination]:
        """Keyset pagination for append-heavy tables (section 31)."""
        query = stmt if stmt is not None else self._scoped()
        return await paginate_keyset(
            self.session,
            query,
            params,
            created_at_col=self.model.created_at,  # type: ignore[attr-defined]
            id_col=self.model.id,  # type: ignore[attr-defined]
        )

    # --- writes -------------------------------------------------------------

    def add(self, instance: ModelT) -> ModelT:
        """Stage an insert. Tenant and actor are stamped from the context, not
        from the payload - a client cannot write into another company.

        The system actor a scheduled sweep carries is stamped as NULL, never as
        the nil placeholder: no user row has that id (``src.core.actors``)."""
        instance.company_id = self.tenant.company_id  # type: ignore[attr-defined]
        if hasattr(instance, "created_by_id") and getattr(instance, "created_by_id", None) is None:
            instance.created_by_id = actor_id(self.tenant.user_id)
        self.session.add(instance)
        return instance

    def touch(self, instance: ModelT) -> ModelT:
        """Mark a row as edited by the current actor.

        Always refresh ``updated_at`` when the model has one. Setting only
        ``updated_by_id`` to the same actor leaves the ORM instance clean, so
        SQLAlchemy issues no UPDATE and an optimistic-lock ``version`` never
        advances - which is how a dealer cart could be changed in another tab
        and still check out with the stale version.
        """
        if hasattr(instance, "updated_by_id"):
            instance.updated_by_id = actor_id(self.tenant.user_id)
        if hasattr(instance, "updated_at"):
            instance.updated_at = utcnow()
        return instance

    async def soft_delete(self, instance: ModelT) -> ModelT:
        """Retire a master record.

        Never call this on ledger, audit or posted financial rows - section 28
        forbids hiding a historically traceable record behind a flag.
        """
        if not hasattr(instance, "deleted_at"):
            raise TypeError(f"{self.model.__name__} does not support soft deletion")
        instance.deleted_at = utcnow()
        self.touch(instance)
        await self.session.flush()
        return instance

    async def hard_delete(self, instance: ModelT) -> None:
        """Physical delete. Only for rows with no historical obligation."""
        await self.session.delete(instance)
        await self.session.flush()

    async def bulk_update(self, ids: Sequence[uuid.UUID], values: dict[str, Any]) -> int:
        """One UPDATE for many rows, still tenant-scoped."""
        if not ids:
            return 0
        stmt = (
            update(self.model)
            .where(
                self.model.company_id == self.tenant.company_id,  # type: ignore[attr-defined]
                self.model.id.in_(list(ids)),  # type: ignore[attr-defined]
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def bulk_delete(self, ids: Sequence[uuid.UUID]) -> int:
        if not ids:
            return 0
        stmt = delete(self.model).where(
            self.model.company_id == self.tenant.company_id,  # type: ignore[attr-defined]
            self.model.id.in_(list(ids)),  # type: ignore[attr-defined]
        )
        return affected_rows(await self.session.execute(stmt))


async def labels_by_id(
    session: AsyncSession,
    tenant: TenantContext,
    ids: Iterable[uuid.UUID | None],
    *columns: InstrumentedAttribute[Any],
) -> dict[uuid.UUID, Row[Any]]:
    """id -> the named columns of one table's rows: what a read shows beside an id.

    A read names what it references by id, and a person reads a code and a name.
    The additive ``*_code`` / ``*_name`` fields that carry them are filled for a
    whole page or document with one IN-query per referenced table - never one per
    row (section 32) - so a screen never has to fetch per row, or fetch a whole
    list and filter, to label one.

    Three rules live here so no caller re-derives them:

    * **Tenant-scoped.** A foreign key to a UUID is not a tenant check: another
      company's id satisfies the constraint, and must label as nothing rather
      than as that company's code.
    * **Archived rows included.** A retired item or a soft-deleted customer is
      still the one a posted document names, and must keep resolving (section
      28), so there is deliberately no ``deleted_at`` filter.
    * **Nothing asked, nothing read.** ``None`` ids are skipped and an empty set
      answers ``{}`` without a query - never an unrestricted ``IN ()``.

    The columns must all belong to one table; read each row's values by column
    name (``labels[item_id].code``).
    """
    if not columns:
        raise ValueError("Name at least one column to label a row by.")
    model: Any = columns[0].class_
    if any(column.class_ is not model for column in columns):
        raise ValueError("Every label column must come from the same table.")
    wanted = list(dict.fromkeys(value for value in ids if value is not None))
    if not wanted:
        return {}
    stmt = select(model.id, *columns).where(
        model.company_id == tenant.company_id,
        model.id.in_(wanted),
    )
    return {row.id: row for row in (await session.execute(stmt)).all()}


def labels_in_order(
    ids: Iterable[uuid.UUID], labels: Mapping[uuid.UUID, Row[Any]], column: str
) -> list[str]:
    """``column`` of each id's label row, in ``ids`` order: a list aligned with the ids.

    How a read labels a list of ids, such as ``serial_numbers`` beside
    ``serial_ids``. An id that resolved to no row - possible only for a
    reference stored before its tenant was checked - is labelled by the id
    itself, so the list stays aligned and the document stays readable.
    """
    return [str(getattr(labels[value], column)) if value in labels else str(value) for value in ids]


__all__ = ["BaseRepository", "ModelT", "labels_by_id", "labels_in_order"]
