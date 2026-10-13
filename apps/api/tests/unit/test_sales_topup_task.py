"""The scheduled allocation top-up, as an adapter: who it asks, in what order, and
what one bad order does to the rest.

The rule itself - reserve newly free stock for each short line - is
``sales.service.allocate_order`` and is exercised against PostgreSQL in
``tests/integration/test_sales_fulfilment.py``. What is pinned here is the
part a database cannot make interesting: every order gets its own session, a
failure is logged and skipped, a race the lock resolved is not a failure, the
work set is taken most-urgent-first and bounded, and the sweep acts as the
system rather than as any person.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.core.errors import BusinessRuleError
from src.core.ids import SYSTEM_ACTOR_ID, persisted_actor
from src.modules.sales import service as sales_service
from src.workers.tasks import maintenance

pytestmark = pytest.mark.unit


class _Sessions:
    """Counts the sessions the sweep opens; each order must get its own."""

    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def scope(self) -> Any:
        self.opened += 1
        yield object()


def _run() -> dict[str, Any]:
    # The task is the Celery-wrapped coroutine, which runs it on a fresh loop of
    # its own; that loop must not be the test's.
    return asyncio.run(asyncio.to_thread(maintenance.allocate_short_orders))


def test_one_order_failing_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    company = uuid.uuid4()
    orders = [uuid.uuid4() for _ in range(3)]
    sessions = _Sessions()
    seen: list[uuid.UUID] = []

    async def companies(session: Any) -> list[uuid.UUID]:
        return [company]

    async def short_orders(session: Any, company_id: uuid.UUID) -> list[uuid.UUID]:
        assert company_id == company
        return orders

    async def allocate(session: Any, tenant: Any, order_id: uuid.UUID, *, reason: str) -> None:
        seen.append(order_id)
        if order_id == orders[1]:
            raise RuntimeError("a corrupt row")

    monkeypatch.setattr(maintenance, "session_scope", sessions.scope)
    monkeypatch.setattr(maintenance, "_companies_with_short_orders", companies)
    monkeypatch.setattr(maintenance, "_short_orders", short_orders)
    monkeypatch.setattr(sales_service, "allocate_order", allocate)

    outcome = _run()

    assert seen == orders
    assert outcome == {"companies": 1, "examined": 3, "skipped": 0, "failed": 1}
    # One session for the tenant scan, one for the order list, one per order.
    assert sessions.opened == 2 + len(orders)


def test_an_order_that_stopped_being_eligible_is_skipped_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = uuid.uuid4()

    async def companies(session: Any) -> list[uuid.UUID]:
        return [uuid.uuid4()]

    async def short_orders(session: Any, company_id: uuid.UUID) -> list[uuid.UUID]:
        return [order]

    async def allocate(session: Any, tenant: Any, order_id: uuid.UUID, *, reason: str) -> None:
        # Shipped or cancelled between the scan and the lock: the lock did its job.
        raise BusinessRuleError("SO/2026-27/00001 is DISPATCHED")

    monkeypatch.setattr(maintenance, "session_scope", _Sessions().scope)
    monkeypatch.setattr(maintenance, "_companies_with_short_orders", companies)
    monkeypatch.setattr(maintenance, "_short_orders", short_orders)
    monkeypatch.setattr(sales_service, "allocate_order", allocate)

    assert _run() == {"companies": 1, "examined": 1, "skipped": 1, "failed": 0}


def test_the_sweep_acts_as_the_system_never_as_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    company = uuid.uuid4()
    tenants: list[Any] = []

    async def companies(session: Any) -> list[uuid.UUID]:
        return [company]

    async def short_orders(session: Any, company_id: uuid.UUID) -> list[uuid.UUID]:
        return [uuid.uuid4()]

    async def allocate(session: Any, tenant: Any, order_id: uuid.UUID, *, reason: str) -> None:
        tenants.append(tenant)

    monkeypatch.setattr(maintenance, "session_scope", _Sessions().scope)
    monkeypatch.setattr(maintenance, "_companies_with_short_orders", companies)
    monkeypatch.setattr(maintenance, "_short_orders", short_orders)
    monkeypatch.setattr(sales_service, "allocate_order", allocate)

    _run()

    assert [tenant.company_id for tenant in tenants] == [company]
    assert tenants[0].user_id == SYSTEM_ACTOR_ID
    # ...which every actor column stores as nobody, not as a user that cannot exist.
    assert persisted_actor(tenants[0].user_id) is None


def test_the_work_set_is_most_urgent_first_and_bounded() -> None:
    captured: list[str] = []

    class _Rows:
        def scalars(self) -> _Rows:
            return self

        def all(self) -> list[Any]:
            return []

    class _Capture:
        async def execute(self, statement: Any) -> _Rows:
            captured.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
            return _Rows()

    asyncio.run(maintenance._short_orders(_Capture(), uuid.uuid4()))  # type: ignore[arg-type]

    sql = captured[0]
    assert (
        "ORDER BY sales_orders.priority, sales_orders.required_date, "
        "sales_orders.order_date, sales_orders.id" in sql
    )
    assert f"LIMIT {maintenance.MAX_TOPUP_ORDERS}" in sql
    # Only orders the top-up may work on, and only ones actually short.
    assert "EXISTS" in sql
    for status in sales_service.TOP_UP_STATUSES:
        assert f"'{status}'" in sql
    assert "'DISPATCHED'" not in sql


def test_the_bound_is_two_hundred_orders_a_company_a_tick() -> None:
    """docs/assumptions.md section E1-2 states the number; this keeps them equal."""
    assert maintenance.MAX_TOPUP_ORDERS == 200
