"""Pagination, sorting and filtering primitives.

Master rules sections 31/85: no endpoint returns an unbounded result set, and
``OFFSET 1000000`` is never the answer for a large table.

Two strategies, chosen per endpoint:

* **Offset** - master data and filtered document lists, where users jump to a
  page number and the result set is bounded by filters.
* **Keyset (cursor)** - append-heavy history: inventory ledger, audit log,
  notifications. Constant cost at any depth.

Both strategies page over a *total* order. Keyset gets it from
``(created_at, id)``; offset gets it from :func:`_stable_order`, which appends
the paged entity's primary key to whatever the caller sorted by. Without that,
a sort key with ties lets OFFSET return the same row on two pages and no page
at all for another.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Select, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import UnmappedColumnError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import UnaryExpression

from src.core.errors import ValidationError
from src.core.logging import get_logger
from src.core.responses import (
    CursorPaginatedResponse,
    CursorPagination,
    OffsetPagination,
    PaginatedResponse,
)
from src.core.validation import bind_request_model

log = get_logger(__name__)

T = TypeVar("T")

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

# Counting is itself a scan. Past this many rows the exact total is not worth
# the second query, so the count stops here and the total is reported as
# *unknown* - ``total: null`` - rather than as the ceiling (docs/assumptions.md
# 15). A capped number returned in the field that means "exact" is a lie the
# caller cannot detect: 10 000 is a plausible total, and a client dividing it
# into pages would show "page 1 of 200" for a set with a million rows in it.
# ``null`` is the one answer that says what actually happened, and the web
# client already reads it that way (``apps/web/src/types/api.ts``).
COUNT_CEILING = 10_000


class PageParams(BaseModel):
    """Offset pagination query parameters."""

    page: Annotated[int, Field(ge=1, le=100_000)] = 1
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE
    with_total: bool = True

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


class SortParams(BaseModel):
    """Sort specification, validated against an endpoint's allow-list.

    The allow-list is mandatory: interpolating a client-supplied column name
    into ORDER BY is an injection vector, and sorting on an unindexed column is
    a performance incident.
    """

    sort_by: str | None = None
    sort_dir: Literal["asc", "desc"] = "asc"

    @field_validator("sort_by")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v else None

    def resolve(self, allowed: dict[str, Any], default: Any) -> Any:
        if not self.sort_by:
            return default
        column = allowed.get(self.sort_by)
        if column is None:
            raise ValidationError(
                f"Cannot sort by '{self.sort_by}'.",
                details={"allowed_sort_fields": sorted(allowed)},
            )
        return column.desc() if self.sort_dir == "desc" else column.asc()


def page_params(
    page: Annotated[int, Query(ge=1, le=100_000, description="1-based page number")] = 1,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Rows per page")
    ] = DEFAULT_PAGE_SIZE,
    with_total: Annotated[bool, Query(description="Include an exact total row count")] = True,
) -> PageParams:
    """FastAPI dependency for offset pagination."""
    return PageParams(page=page, limit=limit, with_total=with_total)


def sort_params(
    sort_by: Annotated[str | None, Query(description="Field to sort by")] = None,
    sort_dir: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "asc",
) -> SortParams:
    """FastAPI dependency for sorting."""
    return SortParams(sort_by=sort_by, sort_dir=sort_dir)


FilterT = TypeVar("FilterT", bound=BaseModel)


def bind_query_filters(model: type[FilterT], **values: object) -> FilterT:
    """Build a filter model from query values FastAPI has already bound.

    Each parameter's *type* is validated by the framework before this runs, so
    ``?date_from=31/12/2024`` is answered 422 with the field named before the
    dependency body starts. What can still fail here is a rule spanning two
    fields - ``date_to`` earlier than ``date_from``. Pydantic raises its own
    error for that, and an error raised inside a dependency body is not one the
    framework translates: it would reach the client as 500 INTERNAL_ERROR,
    asking them to quote a request id for a typo. It is reshaped here into the
    envelope the request validator emits - ``VALIDATION_ERROR`` with
    ``query.<field>`` paths - so a client sees one shape for one condition.

    A rule written as a ``field_validator`` on the upper-bound field reports
    against ``query.<field>``; a ``model_validator`` reports against ``query``.
    """
    return bind_request_model(model, values, source="query")


def _order_key(clause: Any) -> Any:
    """The expression an ORDER BY term sorts on, minus its ASC/DESC/NULLS wrappers.

    ``col.desc().nullslast()`` is a ``UnaryExpression`` wrapping a
    ``UnaryExpression`` wrapping the column, so the unwrapping is a loop.
    """
    element = clause
    while isinstance(element, UnaryExpression):
        element = element.element
    return element


def _is_descending(clause: Any) -> bool:
    """Whether an ORDER BY term sorts descending.

    The NULLS FIRST/LAST wrapper sits *outside* the direction, so the first
    direction modifier found on the way down is the answer. A bare column
    carries none, and SQL's default is ascending.
    """
    element = clause
    while isinstance(element, UnaryExpression):
        if element.modifier is operators.desc_op:
            return True
        if element.modifier is operators.asc_op:
            return False
        element = element.element
    return False


def _clause(term: Any) -> Any:
    """The Core column behind an ORM attribute, for comparison against ORDER BY."""
    unwrap = getattr(term, "__clause_element__", None)
    return unwrap() if unwrap is not None else term


def _primary_key_terms(stmt: Select[Any]) -> tuple[Any, ...]:
    """The mapped primary key of whatever ``stmt`` returns first, or ``()``.

    :func:`paginate` reads the first column of every row (``.scalars()``), so
    the first entry of ``column_descriptions`` is the thing being paged and its
    primary key is the tiebreaker to use. ``()`` means there is no mapper to
    ask: a select of plain Core columns, of a subquery, or of a union.
    """
    descriptions = stmt.column_descriptions
    if not descriptions:
        return ()
    entity = descriptions[0].get("entity")
    if entity is None:
        return ()
    # A mapped class inspects to its Mapper; ``aliased(Model)`` inspects to an
    # AliasedInsp that carries one. Anything else - a Table, a bare Column - has
    # no ``.mapper``, and nothing to key on.
    mapper = getattr(sa_inspect(entity, raiseerr=False), "mapper", None)
    if mapper is None:
        return ()
    terms: list[Any] = []
    for column in mapper.primary_key:
        try:
            key = mapper.get_property_by_column(column).key
        except UnmappedColumnError:
            return ()
        # Read the attribute off ``entity`` rather than off the mapped class:
        # for an aliased entity that is what renders the alias in ORDER BY,
        # instead of a second, unjoined copy of the base table.
        attribute = getattr(entity, key, None)
        if attribute is None:
            return ()
        terms.append(attribute)
    return tuple(terms)


def _stable_order(stmt: Select[Any]) -> Select[Any]:
    """Complete the caller's ORDER BY with the primary key, making it a total order.

    OFFSET only means anything on top of a deterministic sequence. Sorted by a
    non-unique column - a status, a name, a date, or ``created_at``, which under
    PostgreSQL is transaction *start* time and therefore identical for every row
    written in one transaction - tied rows may come back in any order, and not
    the same order twice. Page 2 then repeats rows page 1 already showed and
    silently drops others: nothing errors, the list is just wrong. The UUIDv7
    primary key is the correct tiebreaker - unique by definition, and monotonic,
    so a tie resolves into true insertion order (CLAUDE.md, "traps").

    Doing it here rather than in each of the twenty-odd list endpoints is the
    point: a caller cannot forget it. A caller already ordering by a unique
    column keeps exactly the rows and order it had - the appended key can only
    decide ties that column had already decided.

    The cost is that ``ORDER BY sort_key, id`` cannot always be answered by an
    index on ``sort_key`` alone. Paging that repeats and drops rows is not worth
    saving a sort; where a list is hot, the index should carry the id.
    """
    key_terms = _primary_key_terms(stmt)
    existing = tuple(getattr(stmt, "_order_by_clauses", ()))

    if not key_terms:
        # ASSUMPTION: warn, do not raise. This is the read path of every list
        # endpoint in the product. Refusing to execute a statement that still
        # returns the right rows - only in an order OFFSET cannot page through
        # safely - turns a page-boundary defect into an outage, and the
        # statements that land here (grouped projections, unions, selects from a
        # subquery) are legitimate uses that have to order themselves. The
        # warning is deliberately loud enough to find in the logs. No current
        # caller reaches this branch: every one selects a single mapped entity.
        log.warning(
            "pagination.offset_order_not_total",
            reason="no mapped primary key on the paged selectable",
            ordered_by=len(existing),
        )
        return stmt

    key_clauses = [_clause(term) for term in key_terms]
    sorted_on = [_order_key(clause) for clause in existing]

    def already_sorted_on(key: Any) -> bool:
        return any(target.compare(key) for target in sorted_on if hasattr(target, "compare"))

    if all(already_sorted_on(key) for key in key_clauses):
        # The key is already in the ORDER BY - anywhere in it is enough, since
        # nothing after a unique term is ever reached. Do not append it twice.
        return stmt

    if getattr(stmt, "_group_by_clauses", ()):
        # A grouped projection has no row-level primary key to order by, and
        # naming one in ORDER BY without adding it to GROUP BY is invalid SQL.
        log.warning(
            "pagination.offset_order_not_total",
            reason="grouped statement cannot take a primary-key tiebreaker",
            ordered_by=len(existing),
        )
        return stmt

    if getattr(stmt, "_distinct", False) and not getattr(stmt, "_distinct_on", ()):
        # SELECT DISTINCT requires every ORDER BY expression to appear in the
        # select list. DISTINCT ON does not, and there a trailing key only
        # breaks ties among rows it was already choosing between.
        selected = list(stmt.selected_columns)
        if not all(any(column.compare(key) for column in selected) for key in key_clauses):
            log.warning(
                "pagination.offset_order_not_total",
                reason="SELECT DISTINCT does not return the primary key",
                ordered_by=len(existing),
            )
            return stmt

    # Follow the direction of the last sort key, so a descending page reads
    # newest-first the whole way down instead of flipping inside a tie.
    descending = _is_descending(existing[-1]) if existing else False
    return stmt.order_by(*(term.desc() if descending else term.asc() for term in key_terms))


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
) -> tuple[list[Any], OffsetPagination]:
    """Execute a SELECT with offset pagination.

    Fetches ``limit + 1`` rows to determine ``has_next`` without a second query
    when the caller does not need an exact total.

    The caller's ORDER BY is completed by :func:`_stable_order` before the
    window is cut: OFFSET over a sort with ties duplicates and skips rows.

    ``total`` is exact up to :data:`COUNT_CEILING` rows and ``None`` beyond it,
    never a capped number presented as exact - ``has_next`` still says whether
    there is more, and ``total_pages`` goes ``None`` with it so no client can
    divide a ceiling into pages.
    """
    rows_stmt = _stable_order(stmt).limit(params.limit + 1).offset(params.offset)
    result = await session.execute(rows_stmt)
    rows = list(result.scalars().unique().all())

    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    total: int | None = None
    total_pages: int | None = None
    if params.with_total:
        # order_by must be stripped: it is meaningless inside COUNT and forces
        # PostgreSQL to sort rows it is only going to count. Built from the
        # caller's statement rather than the ordered one, so the tiebreaker
        # cannot reach the count even if this ``order_by(None)`` is ever lost.
        #
        # ``COUNT_CEILING + 1`` rather than ``COUNT_CEILING``: one row past the
        # ceiling is what distinguishes "exactly ten thousand" from "more than
        # ten thousand", and the two must not report the same number. The extra
        # row costs nothing - the LIMIT is what bounds the scan either way.
        count_stmt = select(func.count()).select_from(
            stmt.order_by(None).limit(COUNT_CEILING + 1).subquery()
        )
        counted = int((await session.execute(count_stmt)).scalar_one())
        if counted <= COUNT_CEILING:
            total = counted
            total_pages = max(1, -(-total // params.limit))

    return rows, OffsetPagination(
        page=params.page,
        limit=params.limit,
        total=total,
        total_pages=total_pages,
        has_next=has_next,
        has_previous=params.page > 1,
    )


def paginated(items: list[T], pagination: OffsetPagination) -> PaginatedResponse[T]:
    return PaginatedResponse[T](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Keyset / cursor pagination
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cursor:
    """Opaque position marker: (created_at, id) of the last row returned.

    ``id`` is UUIDv7 and therefore time-ordered, so the pair is a total order
    with no ties even when many rows share a millisecond.
    """

    created_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = json.dumps({"t": self.created_at.isoformat(), "i": str(self.id)})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Cursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            return cls(created_at=datetime.fromisoformat(payload["t"]), id=uuid.UUID(payload["i"]))
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise ValidationError("Invalid pagination cursor.") from exc


class CursorParams(BaseModel):
    cursor: str | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE

    @property
    def decoded(self) -> Cursor | None:
        return Cursor.decode(self.cursor) if self.cursor else None


def cursor_params(
    cursor: Annotated[str | None, Query(description="Opaque cursor from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorParams:
    """FastAPI dependency for keyset pagination."""
    return CursorParams(cursor=cursor, limit=limit)


async def paginate_keyset(
    session: AsyncSession,
    stmt: Select[Any],
    params: CursorParams,
    *,
    created_at_col: Any,
    id_col: Any,
) -> tuple[list[Any], CursorPagination]:
    """Execute a SELECT with descending keyset pagination.

    The WHERE clause uses a row-value comparison, which PostgreSQL can satisfy
    directly from a composite ``(created_at DESC, id DESC)`` index - unlike the
    ``a < x OR (a = x AND b < y)`` formulation, which usually cannot.
    """
    position = params.decoded
    if position is not None:
        from sqlalchemy import tuple_

        stmt = stmt.where(tuple_(created_at_col, id_col) < (position.created_at, position.id))

    stmt = stmt.order_by(created_at_col.desc(), id_col.desc()).limit(params.limit + 1)
    rows = list((await session.execute(stmt)).scalars().unique().all())

    has_next = len(rows) > params.limit
    rows = rows[: params.limit]

    next_cursor: str | None = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = Cursor(created_at=last.created_at, id=last.id).encode()

    return rows, CursorPagination(limit=params.limit, next_cursor=next_cursor, has_next=has_next)


def cursor_paginated(items: list[T], pagination: CursorPagination) -> CursorPaginatedResponse[T]:
    return CursorPaginatedResponse[T](data=items, pagination=pagination)


__all__ = [
    "COUNT_CEILING",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Cursor",
    "CursorParams",
    "PageParams",
    "SortParams",
    "bind_query_filters",
    "cursor_paginated",
    "cursor_params",
    "page_params",
    "paginate",
    "paginate_keyset",
    "paginated",
    "sort_params",
]
