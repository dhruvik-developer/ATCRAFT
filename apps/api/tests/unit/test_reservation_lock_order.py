"""Production and sales take reservation balance locks in item order.

Every reserve and release locks a balance row ``FOR UPDATE`` until the
surrounding operation commits. Assembly, job work and a sales cancellation
already walk their lines in ``(item_id, line_number)`` order for exactly that
reason; two operations that walk shared items in different orders can each hold
the lock the other is waiting for, and PostgreSQL aborts one of them.

Production reserved in kit order and released in whatever order the query
returned, and a sales amendment released in payload order. These tests drive
the loops with the allocation calls recorded, so the order is pinned without a
database. Assembly, job work and production now release through one routine,
``allocation.service.release_all_for_demand``, whose order is pinned here too.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import InsufficientStockError
from src.modules.allocation import service as allocation_service
from src.modules.allocation.models import DemandType
from src.modules.production import service as production_service
from src.modules.sales import service as sales_service
from src.modules.sales.repository import LineReservation, SalesOrderLineRepository

pytestmark = pytest.mark.unit

#: Item ids whose sort order is known: LOW < MID < HIGH.
LOW, MID, HIGH = (uuid.UUID(int=value) for value in (1, 2, 3))
WAREHOUSE = uuid.uuid4()


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _component(line_number: int, item_id: uuid.UUID, required: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        line_number=line_number,
        item_id=item_id,
        item_code=f"ITEM-{line_number}",
        warehouse_id=WAREHOUSE,
        required_quantity=Decimal(required),
    )


async def test_a_release_reserves_components_in_item_order_and_reports_in_kit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Kit order deliberately disagrees with item order.
    components = [
        _component(1, HIGH, "4"),
        _component(2, LOW, "4"),
        _component(3, MID, "4"),
        _component(4, LOW, "2"),
    ]
    reserved: list[tuple[uuid.UUID, int]] = []
    by_component = {component.id: component for component in components}

    async def reserve(session: Any, tenant: Any, **kwargs: Any) -> list[Any]:
        component = by_component[kwargs["demand_line_id"]]
        reserved.append((component.item_id, component.line_number))
        if component.line_number == 3:
            raise InsufficientStockError("ITEM-3", "MAIN", Decimal("4"), Decimal("0"))
        return []

    monkeypatch.setattr(production_service.allocation_service, "reserve", reserve)
    order = SimpleNamespace(
        id=uuid.uuid4(), code="PRD/1", priority=5, planned_start_date=date(2026, 11, 10)
    )
    # HIGH is short outright; MID is free but somebody takes it under the lock.
    availability = {(LOW, WAREHOUSE): Decimal("10"), (MID, WAREHOUSE): Decimal("10")}

    shortages = await production_service._reserve_components(
        None,  # type: ignore[arg-type]
        _tenant(),
        order,  # type: ignore[arg-type]
        components,  # type: ignore[arg-type]
        availability,
    )

    assert reserved == [(LOW, 2), (LOW, 4), (MID, 3)]
    assert [shortage.component_id for shortage in shortages] == [
        components[0].id,
        components[2].id,
    ]
    assert [shortage.shortage_quantity for shortage in shortages] == [Decimal("4"), Decimal("4")]


async def test_a_production_order_gives_its_reservations_back_in_item_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = [
        SimpleNamespace(id=uuid.UUID(int=30), item_id=HIGH),
        SimpleNamespace(id=uuid.UUID(int=20), item_id=LOW),
        SimpleNamespace(id=uuid.UUID(int=10), item_id=MID),
        SimpleNamespace(id=uuid.UUID(int=5), item_id=LOW),
    ]
    released: list[uuid.UUID] = []

    async def held_by_demand(
        session: Any, tenant: Any, demand_type: DemandType, demand_id: uuid.UUID
    ) -> list[Any]:
        assert demand_type is DemandType.PRODUCTION_ORDER
        return held

    async def release(session: Any, tenant: Any, *, reservation_id: uuid.UUID, reason: str) -> Any:
        released.append(reservation_id)

    monkeypatch.setattr(production_service.allocation_service, "held_by_demand", held_by_demand)
    monkeypatch.setattr(production_service.allocation_service, "release", release)

    count = await production_service._release_reservations(
        None,  # type: ignore[arg-type]
        _tenant(),
        SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        reason="Cancelled",
    )

    assert count == 4
    assert released == [uuid.UUID(int=5), uuid.UUID(int=20), uuid.UUID(int=10), uuid.UUID(int=30)]


class _Session:
    async def flush(self) -> None:
        return None


class _Port:
    """Records the releases; reserves nothing, so the retake loop is a no-op."""

    def __init__(self) -> None:
        self.released: list[uuid.UUID] = []

    async def release(
        self, session: Any, tenant: Any, *, reservation_id: uuid.UUID, reason: str
    ) -> None:
        self.released.append(reservation_id)

    async def reservable(
        self, session: Any, tenant: Any, *, item_ids: Sequence[uuid.UUID], warehouse_id: Any
    ) -> dict[uuid.UUID, Decimal]:
        return {}

    async def reserve(self, *args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("nothing is reservable")


async def test_a_sales_amendment_releases_changed_lines_in_item_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def line(number: int, item_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            sales_order_id=uuid.uuid4(),
            line_number=number,
            item_id=item_id,
            quantity=Decimal("5"),
            dispatched_quantity=Decimal("0"),
            allocated_quantity=Decimal("5"),
            reservation_id=uuid.uuid4(),
            required_date=None,
            status=None,
        )

    first, second, third = line(1, HIGH), line(2, LOW), line(3, MID)
    holdings = {
        first.id: [LineReservation(id=uuid.UUID(int=101), remaining=Decimal("5"))],
        second.id: [
            LineReservation(id=uuid.UUID(int=201), remaining=Decimal("2")),
            LineReservation(id=uuid.UUID(int=202), remaining=Decimal("3")),
        ],
        third.id: [LineReservation(id=uuid.UUID(int=301), remaining=Decimal("5"))],
    }

    async def open_reservations_by_line(self: Any, lines: Sequence[Any]) -> dict[Any, Any]:
        return holdings

    async def no_lock(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        SalesOrderLineRepository, "open_reservations_by_line", open_reservations_by_line
    )
    monkeypatch.setattr(sales_service, "advisory_xact_lock", no_lock)
    port = _Port()
    order = SimpleNamespace(
        id=uuid.uuid4(),
        code="SO/1",
        warehouse_id=WAREHOUSE,
        priority=5,
        required_date=None,
    )

    # The payload names the lines in an order unrelated to their items.
    await sales_service._rereserve_changed_lines(
        _Session(),  # type: ignore[arg-type]
        _tenant(),
        order=order,  # type: ignore[arg-type]
        changed=[(first, Decimal("4")), (second, Decimal("4")), (third, Decimal("4"))],  # type: ignore[list-item]
        port=port,  # type: ignore[arg-type]
    )

    assert port.released == [
        uuid.UUID(int=201),
        uuid.UUID(int=202),
        uuid.UUID(int=301),
        uuid.UUID(int=101),
    ]


# -----------------------------------------------------------------------------
# The one release routine assembly, job work and production share
# -----------------------------------------------------------------------------


def _held(*rows: tuple[int, uuid.UUID]) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=uuid.UUID(int=key), item_id=item_id) for key, item_id in rows]


async def test_the_shared_release_walks_item_then_line_then_unnamed_then_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item first; inside an item the document's lines in line order, then a grant no line names."""
    granted = uuid.UUID(int=7)  # an approved reallocation's grant: no line points at it
    held = _held((40, LOW), (7, LOW), (30, LOW), (20, HIGH), (10, MID))
    asked: list[tuple[DemandType, uuid.UUID]] = []

    async def held_by_demand(
        session: Any, tenant: Any, demand_type: DemandType, demand_id: uuid.UUID
    ) -> list[Any]:
        asked.append((demand_type, demand_id))
        return held

    monkeypatch.setattr(allocation_service, "held_by_demand", held_by_demand)
    port = _Port()
    order_id = uuid.uuid4()

    count = await allocation_service.release_all_for_demand(
        None,  # type: ignore[arg-type]
        _tenant(),
        demand_type=DemandType.ASSEMBLY_ORDER,
        demand_id=order_id,
        reason="Completed",
        # Line 2 holds 40 and line 1 holds 30: line order, not id order, decides.
        line_numbers={uuid.UUID(int=40): 2, uuid.UUID(int=30): 1},
        release_one=port.release,
    )

    assert count == 5
    assert asked == [(DemandType.ASSEMBLY_ORDER, order_id)]
    assert port.released == [
        uuid.UUID(int=30),
        uuid.UUID(int=40),
        granted,
        uuid.UUID(int=10),
        uuid.UUID(int=20),
    ]


async def test_the_shared_release_defaults_to_allocations_own_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[uuid.UUID] = []

    async def held_by_demand(*args: Any) -> list[Any]:
        return _held((2, HIGH), (1, LOW))

    async def release(session: Any, tenant: Any, *, reservation_id: uuid.UUID, reason: str) -> Any:
        released.append(reservation_id)

    monkeypatch.setattr(allocation_service, "held_by_demand", held_by_demand)
    monkeypatch.setattr(allocation_service, "release", release)

    count = await allocation_service.release_all_for_demand(
        None,  # type: ignore[arg-type]
        _tenant(),
        demand_type=DemandType.JOB_WORK_ORDER,
        demand_id=uuid.uuid4(),
        reason="Cancelled",
    )

    assert count == 2
    assert released == [uuid.UUID(int=1), uuid.UUID(int=2)]


async def test_nothing_held_releases_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def held_by_demand(*args: Any) -> list[Any]:
        return []

    monkeypatch.setattr(allocation_service, "held_by_demand", held_by_demand)
    port = _Port()

    count = await allocation_service.release_all_for_demand(
        None,  # type: ignore[arg-type]
        _tenant(),
        demand_type=DemandType.JOB_WORK_ORDER,
        demand_id=uuid.uuid4(),
        reason="Cancelled",
        release_one=port.release,
    )

    assert count == 0
    assert port.released == []
