"""What ``total`` means once a result set is bigger than the count ceiling.

``docs/assumptions.md`` 15: exact counts up to :data:`COUNT_CEILING` rows, and
past that the API returns ``total: null`` and the UI shows a range without a
total. Counting a large filtered set is a second scan of the same data, and
nobody reads "page 412 of 9 187".

The code capped the count instead. ``LIMIT 10000`` inside the counted subquery
makes a set of a million rows return 10 000 - in the field whose whole meaning
is "this is how many there are". Nothing about that response says it was capped:
10 000 is a plausible total, ``total_pages`` comes back as 200, and a client
paging past 200 finds rows the total said did not exist. A caller cannot tell
the capped answer from the true one, which is what makes it worse than no answer
at all - ``None`` at least says "unknown" in a field that is already optional
and that the web client already reads that way.

The ceiling is monkeypatched down to a handful of rows. Seeding ten thousand of
them would test the same branch for a hundred times the runtime, and the branch
is what is under test - not the number.

Fixtures (``session``, ``company``) come from ``tests/conftest.py``; the outer
transaction is rolled back after every test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Select, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import pagination as pagination_module
from src.core.pagination import PageParams, paginate
from src.modules.users.models import Role

pytestmark = pytest.mark.integration

#: Small enough to seed instantly, large enough to page through.
CEILING = 4
ROW_COUNT = 7
PAGE_SIZE = 2
CODE_PREFIX = "PGCEIL"


@pytest.fixture(autouse=True)
def _low_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Move the ceiling, not the data.

    ``paginate`` reads the module global when it builds the count, so patching
    it here exercises the real branch with the real arithmetic.
    """
    monkeypatch.setattr(pagination_module, "COUNT_CEILING", CEILING)


@pytest.fixture
async def roles(session: AsyncSession, company: Any) -> list[Role]:
    """``ROW_COUNT`` rows, built through the ORM so Python-side defaults apply."""
    rows = [
        Role(
            company_id=company.id,
            code=f"{CODE_PREFIX}-{index:03d}",
            name=f"Ceiling {index:03d}",
            permissions=[],
            is_system=False,
            is_active=True,
        )
        for index in range(ROW_COUNT)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


def _scoped(company_id: uuid.UUID, *, limit: int | None = None) -> Select[tuple[Role]]:
    """Only this test's rows: other fixtures seed roles into the same company."""
    stmt = (
        select(Role)
        .where(Role.company_id == company_id, Role.code.startswith(CODE_PREFIX))
        .order_by(Role.code.asc())
    )
    return stmt if limit is None else stmt.where(Role.code <= f"{CODE_PREFIX}-{limit - 1:03d}")


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
# Under the ceiling: an exact total, as before
# -----------------------------------------------------------------------------


async def test_a_small_result_set_still_reports_its_exact_total(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    stmt = _scoped(company.id, limit=CEILING - 1)

    rows, page = await paginate(session, stmt, PageParams(page=1, limit=PAGE_SIZE))

    assert len(rows) == PAGE_SIZE
    assert page.total == CEILING - 1
    assert page.total_pages == -(-(CEILING - 1) // PAGE_SIZE)


async def test_a_set_exactly_at_the_ceiling_is_still_exact(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    """The boundary is inclusive: ``COUNT_CEILING`` rows are counted, not refused.

    Off by one here and every set of exactly ten thousand - a page size times a
    round number, which is how synthetic data tends to arrive - loses its total
    for no reason.
    """
    stmt = _scoped(company.id, limit=CEILING)

    _, page = await paginate(session, stmt, PageParams(page=1, limit=PAGE_SIZE))

    assert page.total == CEILING
    assert page.total_pages == -(-CEILING // PAGE_SIZE)


# -----------------------------------------------------------------------------
# Past the ceiling: unknown, not capped
# -----------------------------------------------------------------------------


async def test_past_the_ceiling_the_total_is_unknown_rather_than_capped(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    """THE regression. ``ROW_COUNT`` rows, a ceiling of ``CEILING``.

    The capped answer was ``total=CEILING``: a number that is not the count,
    presented in the field that means the count. ``None`` is the only honest
    value the envelope has for "more than I was willing to count".
    """
    stmt = _scoped(company.id)

    rows, page = await paginate(session, stmt, PageParams(page=1, limit=PAGE_SIZE))

    assert len(rows) == PAGE_SIZE
    assert page.total is None, f"the ceiling was reported as an exact total ({page.total})"
    assert page.total_pages is None, "a capped total was divided into a page count"
    assert page.has_next is True


async def test_the_pages_past_a_capped_total_are_still_reachable(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    """Losing the total must not lose the paging.

    ``has_next`` is computed from the extra row this page fetched, not from the
    count, so a caller walks the whole set exactly as before - which is what
    makes ``None`` an acceptable answer rather than a broken one.
    """
    stmt = _scoped(company.id)
    seen: list[uuid.UUID] = []
    page_number = 1
    while True:
        rows, page = await paginate(session, stmt, PageParams(page=page_number, limit=PAGE_SIZE))
        seen.extend(role.id for role in rows)
        assert page.total is None
        if not page.has_next:
            break
        page_number += 1
        assert page_number <= ROW_COUNT, "paging did not terminate"

    assert seen == [role.id for role in roles]


async def test_the_count_is_still_bounded_by_the_ceiling(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    """The fix must not have turned a bounded probe into a full count.

    The point of the ceiling is that the second query does not scan the whole
    table. One row past it is all that is needed to know there is more.
    """
    stmt = _scoped(company.id)

    with _captured_sql(session) as statements:
        _, page = await paginate(session, stmt, PageParams(page=1, limit=PAGE_SIZE))

    assert page.total is None
    counts = [sql for sql in statements if "count(" in sql]
    assert len(counts) == 1
    assert "LIMIT" in counts[0], "the bounded count became an unbounded one"
    assert "ORDER BY" not in counts[0]


async def test_asking_for_no_total_still_runs_no_count(
    session: AsyncSession, company: Any, roles: list[Role]
) -> None:
    """``with_total=False`` is unchanged: no second query at all."""
    stmt = _scoped(company.id)

    with _captured_sql(session) as statements:
        _, page = await paginate(
            session, stmt, PageParams(page=1, limit=PAGE_SIZE, with_total=False)
        )

    assert page.total is None
    assert page.total_pages is None
    assert [sql for sql in statements if "count(" in sql] == []
