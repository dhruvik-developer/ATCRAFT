"""Cancelling a packing order removes its cartons in two statements, not 2N.

The order's row lock is held for the whole of ``cancel_order``. Deleting each
carton's contents and then the carton, one statement each inside a loop, made a
fifty-carton order a hundred round-trips under that lock - and CLAUDE.md's rule
is that no database call is awaited inside a loop. The read side was already
batched; this pins the write side to one DELETE for the contents and one for
the cartons, and pins the content DELETE to a tenant-scoped ``IN`` list that
deletes nothing for an empty set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext
from src.modules.audit.models import AuditAction
from src.modules.packing import service as packing_service
from src.modules.packing.models import PackingOrderStatus
from src.modules.packing.repository import CartonContentRepository, CartonRepository

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _no_transaction(session: Any) -> AsyncIterator[Any]:
    yield session


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


LINE_A = uuid.uuid4()
LINE_B = uuid.uuid4()


def _content(line_id: uuid.UUID, quantity: str) -> Any:
    return SimpleNamespace(packing_order_line_id=line_id, quantity=Decimal(quantity))


class TestCancelOrder:
    async def test_the_cartons_and_their_contents_go_in_one_statement_each(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = SimpleNamespace(
            id=uuid.uuid4(), code="PK/2026-27/00003", status=PackingOrderStatus.IN_PROGRESS
        )
        cartons = [SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
        contents = {
            cartons[0].id: [_content(LINE_A, "2"), _content(LINE_B, "1")],
            cartons[1].id: [_content(LINE_A, "3")],
        }
        calls: dict[str, list[Any]] = {
            "delete_for_cartons": [],
            "bulk_delete": [],
            "released": [],
            "transitions": [],
        }

        async def _lock_order(repo: Any, order_id: uuid.UUID) -> Any:
            return order

        async def _totals(self: Any, order_id: uuid.UUID) -> Any:
            return SimpleNamespace(sealed_count=0)

        async def _list_for_order(self: Any, order_id: uuid.UUID) -> list[Any]:
            return cartons

        async def _list_for_cartons(self: Any, carton_ids: Sequence[uuid.UUID]) -> dict[Any, Any]:
            return contents

        async def _delete_for_cartons(self: Any, carton_ids: Sequence[uuid.UUID]) -> int:
            calls["delete_for_cartons"].append(list(carton_ids))
            return 3

        async def _bulk_delete(self: Any, ids: Sequence[uuid.UUID]) -> int:
            calls["bulk_delete"].append(list(ids))
            return len(ids)

        async def _never(self: Any, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("a per-carton delete must not be issued")

        async def _apply(
            session: Any, tenant: Any, order_: Any, *, released: Any, added: Any
        ) -> None:
            calls["released"].append(dict(released))

        async def _transition(
            session: Any, tenant: Any, order_: Any, target: Any, **kwargs: Any
        ) -> None:
            calls["transitions"].append((target, kwargs.get("action")))

        async def _withdraw(session: Any, tenant: Any, order_id: uuid.UUID, **kwargs: Any) -> None:
            assert order_id == order.id
            assert kwargs["reason"] == "Customer withdrew the order"

        monkeypatch.setattr(packing_service, "_lock_order", _lock_order)
        monkeypatch.setattr(packing_service, "_apply_packed_quantities", _apply)
        monkeypatch.setattr(packing_service, "_transition", _transition)
        monkeypatch.setattr(packing_service, "_withdraw_order_inspections", _withdraw)
        monkeypatch.setattr(packing_service, "transaction", _no_transaction)
        monkeypatch.setattr(CartonRepository, "totals_for_order", _totals)
        monkeypatch.setattr(CartonRepository, "list_for_order", _list_for_order)
        monkeypatch.setattr(CartonRepository, "bulk_delete", _bulk_delete)
        monkeypatch.setattr(CartonRepository, "hard_delete", _never)
        monkeypatch.setattr(CartonContentRepository, "list_for_cartons", _list_for_cartons)
        monkeypatch.setattr(CartonContentRepository, "delete_for_cartons", _delete_for_cartons)
        monkeypatch.setattr(CartonContentRepository, "delete_for_carton", _never)

        result = await packing_service.cancel_order(
            None,  # type: ignore[arg-type]
            _tenant(),
            order.id,
            reason="Customer withdrew the order",
        )

        assert result is order
        carton_ids = [carton.id for carton in cartons]
        assert calls["delete_for_cartons"] == [carton_ids]
        assert calls["bulk_delete"] == [carton_ids]
        # The lines still get back everything the cartons held.
        assert calls["released"] == [{LINE_A: Decimal("5"), LINE_B: Decimal("1")}]
        assert calls["transitions"] == [(PackingOrderStatus.CANCELLED, AuditAction.CANCEL)]


class _Capture:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=2)


class TestDeleteForCartons:
    async def test_one_tenant_scoped_delete_over_the_whole_set(self) -> None:
        session = _Capture()
        tenant = _tenant()
        ids = [uuid.uuid4(), uuid.uuid4()]

        removed = await CartonContentRepository(session, tenant).delete_for_cartons(  # type: ignore[arg-type]
            ids
        )

        assert removed == 2
        [statement] = session.statements
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert sql.startswith("DELETE FROM carton_contents")
        assert "carton_contents.company_id = " in sql
        assert "carton_contents.carton_id IN " in sql

    async def test_an_empty_set_deletes_nothing_and_asks_nothing(self) -> None:
        # ``IN ()`` would be dropped from the statement and take every content
        # row the tenant owns with it; the guard is that no statement is sent.
        session = _Capture()

        removed = await CartonContentRepository(session, _tenant()).delete_for_cartons(  # type: ignore[arg-type]
            []
        )

        assert removed == 0
        assert session.statements == []
