"""``labels_by_id``: one tenant-scoped IN-query labels a whole page of ids.

A read names what it references by id; the additive ``*_code`` / ``*_name``
fields beside those ids are filled through this one helper, so the three rules a
hand-written lookup tends to get wrong are written once:

* tenant scope - a foreign key to a UUID is satisfied by another company's row;
* archived rows still label - a retired item is still what a posted document
  names, so there is no ``deleted_at`` filter;
* an empty or all-``None`` set reads nothing, never an unrestricted ``IN ()``.

The database behaviour is pinned by the module read tests that use it; these pin
the statement it builds and the short-circuits, with no database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.common.repository import labels_by_id
from src.core.deps import TenantContext
from src.modules.customers.models import Customer
from src.modules.products.models import Item

pytestmark = pytest.mark.unit


class _RecordingSession:
    """Answers ``execute`` with canned rows and keeps every statement it saw."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        rows = self.rows

        class _Result:
            def all(self) -> list[Any]:
                return rows

        return _Result()


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


class TestNothingAskedNothingRead:
    @pytest.mark.parametrize("ids", [[], [None, None]], ids=["empty", "all-none"])
    async def test_an_empty_set_answers_empty_without_a_query(self, ids: list[Any]) -> None:
        session = _RecordingSession()

        labels = await labels_by_id(session, _tenant(), ids, Item.code, Item.name)  # type: ignore[arg-type]

        assert labels == {}
        assert session.statements == []


class TestTheStatement:
    async def test_one_tenant_scoped_in_query_with_no_soft_delete_filter(self) -> None:
        tenant = _tenant()
        first, second = uuid.uuid4(), uuid.uuid4()
        session = _RecordingSession()

        await labels_by_id(
            session,  # type: ignore[arg-type]
            tenant,
            [first, None, second, first],
            Item.code,
            Item.name,
        )

        (statement,) = session.statements
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert sql.startswith("SELECT items.id, items.code, items.name")
        assert "items.company_id = " in sql
        assert "items.id IN " in sql
        # Archived rows still label: a posted document keeps naming them.
        assert "deleted_at" not in sql
        params = list(compiled.params.values())
        assert tenant.company_id in params
        # Duplicates and None are dropped before the IN list is bound.
        assert [first, second] in params

    async def test_rows_come_back_keyed_by_id_and_read_by_column_name(self) -> None:
        item_id = uuid.uuid4()
        session = _RecordingSession([SimpleNamespace(id=item_id, code="HNG-01", name="Hinge")])

        labels = await labels_by_id(session, _tenant(), [item_id], Item.code, Item.name)  # type: ignore[arg-type]

        assert labels[item_id].code == "HNG-01"
        assert labels[item_id].name == "Hinge"


class TestMisuseIsRefused:
    async def test_no_column_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one column"):
            await labels_by_id(_RecordingSession(), _tenant(), [uuid.uuid4()])  # type: ignore[arg-type]

    async def test_columns_from_two_tables_are_refused(self) -> None:
        """Two tables in one select is a cross join, not a label."""
        session = _RecordingSession()

        with pytest.raises(ValueError, match="same table"):
            await labels_by_id(
                session,  # type: ignore[arg-type]
                _tenant(),
                [uuid.uuid4()],
                Item.code,
                Customer.name,
            )
        assert session.statements == []
