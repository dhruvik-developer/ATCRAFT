"""ORDER BY completion for offset pagination.

``paginate`` may only cut a window out of a *total* order. These tests are about
the statement it builds - what gets appended, in which direction, and which
statements it refuses to touch. The behaviour that needs a real PostgreSQL - a
tie that genuinely repeats and drops rows across pages - lives in
``tests/integration/test_core_pagination_stability.py``.

No database is touched here: every case compiles a statement and reads its
ORDER BY.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Integer, String, select, union_all
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column
from structlog.testing import capture_logs

from src.core.pagination import _stable_order
from src.modules.users.models import Role, user_roles

pytestmark = pytest.mark.unit

WARNING_EVENT = "pagination.offset_order_not_total"


class _ProbeBase(DeclarativeBase):
    """A declarative base of this module's own.

    Deliberately *not* ``src.core.models.Base``: the probe below is a shape no
    module owns, and registering it in the application metadata would put a
    stand-in table in every test schema (CLAUDE.md, "traps"). Its own base means
    its own metadata and its own class registry, and nothing ever creates it -
    the tests here only compile statements.
    """


class _CompositeKeyProbe(_ProbeBase):
    """The composite-primary-key case, which no application model has yet."""

    __tablename__ = "pagination_composite_probe"

    part_a: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_b: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String)


def _order_by(stmt: Any) -> str:
    """The ORDER BY clause of a statement, as SQL."""
    sql = str(stmt)
    if "ORDER BY" not in sql:
        return ""
    return " ".join(sql.split("ORDER BY", 1)[1].split())


# -----------------------------------------------------------------------------
# The tiebreaker is appended
# -----------------------------------------------------------------------------


def test_primary_key_is_appended_to_a_non_unique_sort() -> None:
    stmt = select(Role).order_by(Role.name)

    assert _order_by(_stable_order(stmt)) == "roles.name, roles.id ASC"


def test_unordered_statement_is_given_an_order() -> None:
    """No ORDER BY at all is the same defect with none of the disguise."""
    assert _order_by(_stable_order(select(Role))) == "roles.id ASC"


def test_tiebreaker_follows_a_descending_sort() -> None:
    """A descending page must not flip to ascending inside a tie."""
    stmt = select(Role).order_by(Role.created_at.desc())

    assert _order_by(_stable_order(stmt)) == "roles.created_at DESC, roles.id DESC"


def test_direction_is_read_through_a_nulls_wrapper() -> None:
    """``desc().nullslast()`` wraps the direction; the auth module sorts that way."""
    stmt = select(Role).order_by(Role.name.desc().nullslast())

    assert _order_by(_stable_order(stmt)).endswith("roles.id DESC")


def test_direction_comes_from_the_last_sort_key_only() -> None:
    stmt = select(Role).order_by(Role.created_at.desc(), Role.name.asc())

    assert _order_by(_stable_order(stmt)).endswith("roles.name ASC, roles.id ASC")


def test_every_column_of_a_composite_key_is_appended() -> None:
    stmt = select(_CompositeKeyProbe).order_by(_CompositeKeyProbe.label.desc())

    assert _order_by(_stable_order(stmt)) == (
        "pagination_composite_probe.label DESC, "
        "pagination_composite_probe.part_a DESC, "
        "pagination_composite_probe.part_b DESC"
    )


def test_aliased_entity_is_ordered_by_the_alias() -> None:
    """The base table's id would be a second, unjoined copy of ``roles``."""
    alias = aliased(Role)
    stmt = select(alias).order_by(alias.name)

    ordered = _order_by(_stable_order(stmt))
    assert ordered.endswith("roles_1.id ASC")
    assert "roles.id" not in ordered


def test_distinct_entity_select_still_gets_the_tiebreaker() -> None:
    """SELECT DISTINCT wants ORDER BY in the select list - an entity has its id there."""
    stmt = select(Role).distinct().order_by(Role.name)

    assert _order_by(_stable_order(stmt)).endswith("roles.id ASC")


# -----------------------------------------------------------------------------
# ... but never twice
# -----------------------------------------------------------------------------


def test_trailing_primary_key_is_not_repeated() -> None:
    stmt = select(Role).order_by(Role.name, Role.id.desc())

    assert _stable_order(stmt) is stmt
    assert _order_by(stmt) == "roles.name, roles.id DESC"


def test_primary_key_anywhere_in_the_order_is_enough() -> None:
    """Nothing after a unique term is ever reached, so this is already total."""
    stmt = select(Role).order_by(Role.id.asc(), Role.name)

    assert _stable_order(stmt) is stmt


def test_a_joined_tables_key_does_not_count() -> None:
    """A joined table's id says nothing about ties in the rows being paged."""
    alias = aliased(Role)
    stmt = select(Role).join(alias, alias.company_id == Role.company_id).order_by(alias.id)

    assert _order_by(_stable_order(stmt)).endswith("roles.id ASC")


# -----------------------------------------------------------------------------
# Statements with no derivable key: warn, change nothing
# -----------------------------------------------------------------------------


def _warnings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == WARNING_EVENT]


def test_select_from_a_subquery_warns_and_is_left_alone() -> None:
    inner = select(Role).where(Role.is_active.is_(True)).subquery()
    stmt = select(inner.c.id, inner.c.name).order_by(inner.c.name)

    with capture_logs() as records:
        assert _stable_order(stmt) is stmt

    assert [record["log_level"] for record in _warnings(records)] == ["warning"]


def test_select_from_a_union_warns_and_is_left_alone() -> None:
    combined = union_all(select(Role.id), select(Role.id)).subquery()
    stmt = select(combined)

    with capture_logs() as records:
        assert _stable_order(stmt) is stmt

    assert len(_warnings(records)) == 1


def test_core_table_select_warns_and_is_left_alone() -> None:
    """``user_roles`` is a Table, not a mapped class: there is no mapper to ask."""
    stmt = select(user_roles.c.user_id).order_by(user_roles.c.user_id)

    with capture_logs() as records:
        assert _stable_order(stmt) is stmt

    assert len(_warnings(records)) == 1


def test_grouped_projection_warns_and_is_left_alone() -> None:
    """Ordering by a column that is not grouped is invalid SQL, not a fix."""
    stmt = select(Role.company_id).group_by(Role.company_id).order_by(Role.company_id)

    with capture_logs() as records:
        assert _stable_order(stmt) is stmt

    assert len(_warnings(records)) == 1


def test_distinct_column_select_warns_and_is_left_alone() -> None:
    """PostgreSQL rejects an ORDER BY expression missing from a DISTINCT select list."""
    stmt = select(Role.name).distinct().order_by(Role.name)

    with capture_logs() as records:
        assert _stable_order(stmt) is stmt

    assert len(_warnings(records)) == 1


def test_a_derivable_key_never_warns() -> None:
    """The ordinary case must not fill the log with noise."""
    with capture_logs() as records:
        _stable_order(select(Role).order_by(Role.name))
        _stable_order(select(Role).order_by(Role.name, Role.id))

    assert _warnings(records) == []
