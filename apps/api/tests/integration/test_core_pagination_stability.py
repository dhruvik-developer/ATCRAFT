"""Offset pagination over a sort with ties, against a real PostgreSQL.

This is the defect these tests exist for: ``created_at`` defaults to
``func.now()``, which is transaction *start* time, so every row written in one
transaction carries the identical timestamp. ``ORDER BY created_at DESC`` over
those rows is not a total order, and OFFSET is free to arrange the tied rows
differently for every page it cuts - so a row appears on two pages while another
appears on none. Nothing raises; the list is quietly wrong.

Only a real database can show that. The statement-shaping rules - what gets
appended, in which direction, and which statements are left alone - are covered
without a database in ``tests/unit/test_core_pagination_order.py``.

Fixtures (``session``, ``company``) come from ``tests/conftest.py``; the outer
transaction is rolled back after every test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Select, delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.pagination import PageParams, paginate
from src.modules.users.models import Role

pytestmark = pytest.mark.integration

# Enough rows that several pages are needed, and small enough to stay fast.
ROW_COUNT = 24
PAGE_SIZE = 5
CODE_PREFIX = "PGSTAB"
# Three names across twenty-four rows: eight-way ties in the business column
# that a user would actually sort by.
NAME_GROUPS = ("Assembly Line", "Inspection", "Warehouse")


@pytest.fixture
async def tied_roles(session: AsyncSession, company: Any) -> list[Role]:
    """``ROW_COUNT`` rows written in one transaction, so their stamps tie.

    Built through the ORM rather than a raw INSERT: several of these columns
    default in Python, not in the database (CLAUDE.md, "traps").
    """
    roles = [
        Role(
            company_id=company.id,
            code=f"{CODE_PREFIX}-{index:03d}",
            name=NAME_GROUPS[index % len(NAME_GROUPS)],
            permissions=[],
            is_system=False,
            is_active=True,
        )
        for index in range(ROW_COUNT)
    ]
    session.add_all(roles)
    await session.flush()
    return roles


def _scoped(company_id: uuid.UUID) -> Select[tuple[Role]]:
    """Only this test's rows: other fixtures seed roles into the same company."""
    return select(Role).where(
        Role.company_id == company_id,
        Role.code.startswith(CODE_PREFIX),
    )


async def _walk_every_page(
    session: AsyncSession, stmt: Select[Any], *, limit: int = PAGE_SIZE
) -> list[uuid.UUID]:
    """Every id the endpoint would hand out, page by page, in order."""
    collected: list[uuid.UUID] = []
    page = 1
    while True:
        rows, pagination = await paginate(
            session, stmt, PageParams(page=page, limit=limit, with_total=False)
        )
        collected.extend(row.id for row in rows)
        if not pagination.has_next:
            return collected
        page += 1
        # A guard, not an assertion about the data: a paging bug that loses
        # ``has_next`` should fail the test rather than hang the suite.
        assert page <= ROW_COUNT, "paging did not terminate"


@contextmanager
def _captured_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Every statement the session executes inside the block, as SQL."""
    statements: list[str] = []

    def record(state: Any) -> None:
        statements.append(str(state.statement))

    event.listen(session.sync_session, "do_orm_execute", record)
    try:
        yield statements
    finally:
        event.remove(session.sync_session, "do_orm_execute", record)


# -----------------------------------------------------------------------------
# The trigger
# -----------------------------------------------------------------------------


async def test_rows_written_in_one_transaction_share_a_timestamp(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """The premise of every test below: these ties are real, not contrived."""
    distinct_stamps = await session.scalar(
        select(func.count(func.distinct(Role.created_at))).where(
            Role.company_id == company.id, Role.code.startswith(CODE_PREFIX)
        )
    )

    assert distinct_stamps == 1


# -----------------------------------------------------------------------------
# Paging a tied sort
# -----------------------------------------------------------------------------


async def test_paging_a_tied_timestamp_returns_every_row_exactly_once(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    stmt = _scoped(company.id).order_by(Role.created_at.desc())

    seen = await _walk_every_page(session, stmt)

    expected = sorted((role.id for role in tied_roles), reverse=True)
    assert len(seen) == ROW_COUNT, "a row was dropped or repeated between pages"
    assert sorted(set(seen), reverse=True) == expected
    # UUIDv7 is monotonic, so descending id is newest-first: the order the
    # caller asked for, made total.
    assert seen == expected


async def test_ascending_pages_read_in_insertion_order(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """The tiebreaker follows the sort direction rather than fixing one."""
    stmt = _scoped(company.id).order_by(Role.created_at.asc())

    seen = await _walk_every_page(session, stmt)

    assert seen == [role.id for role in tied_roles]


async def test_ties_in_a_business_column_are_broken_by_id(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """Sorting by a name with eight duplicates still pages through cleanly."""
    stmt = _scoped(company.id).order_by(Role.name.asc())

    seen = await _walk_every_page(session, stmt)

    expected = [role.id for role in sorted(tied_roles, key=lambda role: (role.name, role.id))]
    assert seen == expected


async def test_page_boundaries_do_not_move_when_a_row_is_updated(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """An edit between two pages must not shuffle the rest of the list.

    An UPDATE writes a new row version at the end of the heap, so a scan returns
    it in a different position than before. With a total order that is
    irrelevant; without one it is enough to make the next page skip a row.
    """
    stmt = _scoped(company.id).order_by(Role.name.asc())
    first_page, pagination = await paginate(
        session, stmt, PageParams(page=1, limit=PAGE_SIZE, with_total=False)
    )
    first_page_ids = [role.id for role in first_page]
    assert pagination.has_next

    edited = first_page[0]
    edited.description = "edited between two page requests"
    await session.flush()

    rest: list[uuid.UUID] = []
    page = 2
    while True:
        rows, pagination = await paginate(
            session, stmt, PageParams(page=page, limit=PAGE_SIZE, with_total=False)
        )
        rest.extend(role.id for role in rows)
        if not pagination.has_next:
            break
        page += 1

    seen = first_page_ids + rest
    assert len(seen) == ROW_COUNT
    assert set(seen) == {role.id for role in tied_roles}


async def test_a_deleted_row_does_not_hide_its_neighbour(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """Rows removed between pages shorten the list; they do not scramble it.

    Roles are master data and the application soft-deletes them, but this is
    about the window OFFSET cuts, so the rows really go.
    """
    stmt = _scoped(company.id).order_by(Role.created_at.desc())
    first_page, _ = await paginate(
        session, stmt, PageParams(page=1, limit=PAGE_SIZE, with_total=False)
    )
    removed = {role.id for role in first_page[:2]}
    await session.execute(delete(Role).where(Role.id.in_(removed)))

    seen = await _walk_every_page(session, stmt)

    expected = sorted((role.id for role in tied_roles if role.id not in removed), reverse=True)
    assert seen == expected


# -----------------------------------------------------------------------------
# The count query
# -----------------------------------------------------------------------------


async def test_count_query_is_never_ordered(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """The tiebreaker must not reach the count, which would sort rows to count them."""
    stmt = _scoped(company.id).order_by(Role.created_at.desc())

    with _captured_sql(session) as statements:
        rows, pagination = await paginate(
            session, stmt, PageParams(page=1, limit=PAGE_SIZE, with_total=True)
        )

    assert len(rows) == PAGE_SIZE
    assert pagination.total == ROW_COUNT
    assert pagination.total_pages == -(-ROW_COUNT // PAGE_SIZE)

    counts = [sql for sql in statements if "count(" in sql]
    windows = [sql for sql in statements if "count(" not in sql]
    assert len(counts) == 1
    assert "ORDER BY" not in counts[0]
    # ... while the query that actually returns rows is fully ordered.
    assert len(windows) == 1
    assert "ORDER BY roles.created_at DESC, roles.id DESC" in windows[0]


async def test_total_is_unchanged_by_the_tiebreaker(
    session: AsyncSession, company: Any, tied_roles: list[Role]
) -> None:
    """Appending a key changes the order of rows, never which rows there are."""
    ordered = _scoped(company.id).order_by(Role.name.asc())
    unordered = _scoped(company.id)

    _, with_sort = await paginate(
        session, ordered, PageParams(page=1, limit=PAGE_SIZE, with_total=True)
    )
    _, without_sort = await paginate(
        session, unordered, PageParams(page=1, limit=PAGE_SIZE, with_total=True)
    )

    assert with_sort.total == without_sort.total == ROW_COUNT
