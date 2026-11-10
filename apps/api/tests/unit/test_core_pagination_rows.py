"""The row-shaped paginator, and the count ceiling it shares with ``paginate``.

``paginate_rows`` is the one offset paginator for statements whose rows are
column tuples - projections, grouped aggregates, unions. Fifteen hand copies of
it lived in the modules, and eight of them counted ``LIMIT COUNT_CEILING`` and
reported the result as the exact total: a set of 25 000 rows answered
``total=10000`` and ``total_pages=400``, and the "last" page offered was not the
last (docs/assumptions.md 15). These tests pin the contract every list endpoint
built on it now shares.

No database is touched: a stand-in session answers the page statement first and
the count second, and records both, so the tests can read what was asked as
well as what came back. The same branches against a real PostgreSQL live in
``tests/integration/test_core_pagination_count_ceiling.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from src.core.pagination import COUNT_CEILING, PageParams, paginate_rows
from src.modules.users.models import Role

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: list[Any] | None = None, scalar: int | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> int:
        assert self._scalar is not None, "the page statement was read as a count"
        return self._scalar


class _Session:
    """Answers the page statement with ``rows`` and a count with ``counted``."""

    def __init__(self, rows: list[Any], counted: int | None = None) -> None:
        self._answers = [_Result(rows=rows)]
        if counted is not None:
            self._answers.append(_Result(scalar=counted))
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        assert self._answers, f"an unexpected extra statement ran: {statement}"
        return self._answers.pop(0)


def _rows(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(code=f"R-{index:03d}") for index in range(count)]


def _stmt() -> Any:
    """A projection - two columns, so ``paginate``'s ``.scalars()`` would lose one."""
    return select(Role.code, func.count().label("members")).group_by(Role.code).order_by(Role.code)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_a_set_exactly_at_the_ceiling_reports_its_exact_total() -> None:
    session = _Session(_rows(3), counted=COUNT_CEILING)

    rows, page = await paginate_rows(session, _stmt(), PageParams(page=1, limit=50))  # type: ignore[arg-type]

    assert len(rows) == 3
    assert page.total == COUNT_CEILING
    assert page.total_pages == -(-COUNT_CEILING // 50)


async def test_one_row_past_the_ceiling_is_unknown_rather_than_capped() -> None:
    """THE regression: the module copies answered ``total=COUNT_CEILING`` here."""
    session = _Session(_rows(51), counted=COUNT_CEILING + 1)

    rows, page = await paginate_rows(session, _stmt(), PageParams(page=1, limit=50))  # type: ignore[arg-type]

    assert len(rows) == 50
    assert page.total is None, f"the ceiling was reported as an exact total ({page.total})"
    assert page.total_pages is None, "a capped total was divided into a page count"
    assert page.has_next is True


async def test_an_empty_set_is_one_empty_page() -> None:
    """The inbound board's copy answered ``total_pages=0`` here; core answers 1."""
    session = _Session([], counted=0)

    rows, page = await paginate_rows(session, _stmt(), PageParams(page=1, limit=50))  # type: ignore[arg-type]

    assert rows == []
    assert page.total == 0
    assert page.total_pages == 1
    assert page.has_next is False
    assert page.has_previous is False


async def test_the_fence_post_row_decides_has_next_and_is_not_returned() -> None:
    session = _Session(_rows(3), counted=7)

    rows, page = await paginate_rows(session, _stmt(), PageParams(page=2, limit=2))  # type: ignore[arg-type]

    assert [row.code for row in rows] == ["R-000", "R-001"]
    assert page.has_next is True
    assert page.has_previous is True
    assert (page.page, page.limit, page.total, page.total_pages) == (2, 2, 7, 4)


async def test_the_last_page_has_no_next() -> None:
    session = _Session(_rows(1), counted=3)

    _, page = await paginate_rows(session, _stmt(), PageParams(page=2, limit=2))  # type: ignore[arg-type]

    assert page.has_next is False
    assert page.has_previous is True


async def test_the_page_statement_fetches_one_extra_row_at_the_offset() -> None:
    session = _Session(_rows(0), counted=0)

    await paginate_rows(session, _stmt(), PageParams(page=3, limit=20))  # type: ignore[arg-type]

    page_sql = session.statements[0].compile(dialect=postgresql.dialect())
    assert page_sql.params["param_1"] == 21
    assert page_sql.params["param_2"] == 40
    assert "ORDER BY roles.code" in str(page_sql)


async def test_the_count_is_bounded_one_past_the_ceiling_and_unsorted() -> None:
    """The bound is what keeps a count of a huge set cheap; the +1 is what makes it honest."""
    session = _Session(_rows(0), counted=0)

    await paginate_rows(session, _stmt(), PageParams(page=1, limit=50))  # type: ignore[arg-type]

    assert len(session.statements) == 2
    count = session.statements[1].compile(dialect=postgresql.dialect())
    assert "count(" in str(count)
    assert "ORDER BY" not in str(count)
    assert COUNT_CEILING + 1 in count.params.values()


async def test_no_total_asked_for_runs_no_count() -> None:
    session = _Session(_rows(2))

    _, page = await paginate_rows(  # type: ignore[arg-type]
        session, _stmt(), PageParams(page=1, limit=5, with_total=False)
    )

    assert len(session.statements) == 1
    assert page.total is None
    assert page.total_pages is None


async def test_a_total_the_caller_already_counted_is_honoured_without_a_second_count() -> None:
    """Reports counts the set to decide whether it can serve it inline, then pages it."""
    session = _Session(_rows(2))

    _, page = await paginate_rows(  # type: ignore[arg-type]
        session, _stmt(), PageParams(page=1, limit=5), total=12
    )

    assert len(session.statements) == 1, "a supplied total was counted again"
    assert page.total == 12
    assert page.total_pages == 3


async def test_a_supplied_total_past_the_ceiling_is_reported_as_unknown() -> None:
    session = _Session(_rows(2))

    _, page = await paginate_rows(  # type: ignore[arg-type]
        session, _stmt(), PageParams(page=1, limit=5), total=COUNT_CEILING + 1
    )

    assert page.total is None
    assert page.total_pages is None


async def test_every_column_of_a_row_survives() -> None:
    """The reason the helper exists: ``paginate`` would hand back only ``code``."""
    row = SimpleNamespace(code="R-001", members=4)
    session = _Session([row], counted=1)

    rows, _ = await paginate_rows(session, _stmt(), PageParams())  # type: ignore[arg-type]

    assert rows == [row]
    assert "members" in _sql(session.statements[0])
