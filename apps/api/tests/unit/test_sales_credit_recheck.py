"""Growing a confirmed order re-runs the credit check.

Module rule 4: the credit check runs inside the confirming transaction against
a locked customer row, for the order's total *at that moment*. ``_update_confirmed``
then let any holder of ``sales_order.update`` raise every line's quantity on a
CONFIRMED order, store the larger total and take more reservations with no
check at all - a blocked or over-limit customer confirmed on a small order and
grown afterwards is the double-spend the locked check exists to prevent.

The totals arithmetic is pure and pinned directly. The gate itself is driven
with the two writes it makes replaced (the reservation move and the credit
check), so what is asserted is *when* the check runs, *what* amount it is
given, and that it runs before any reservation moves.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.modules.sales import service
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderLineStatus
from src.modules.sales.schemas import SalesOrderLineIn, SalesOrderUpdate
from src.modules.sales.service import prospective_totals

pytestmark = pytest.mark.unit

D = Decimal


def _line(number: int, quantity: str, unit_price: str, *, tax_rate: str = "18") -> SalesOrderLine:
    return SalesOrderLine(
        id=uuid.uuid4(),
        line_number=number,
        item_id=uuid.uuid4(),
        item_variant_id=None,
        quantity=D(quantity),
        unit_price=D(unit_price),
        tax_rate=D(tax_rate),
        discount_percentage=D("0"),
        discount_amount=D("0"),
        dispatched_quantity=D("0"),
        allocated_quantity=D("0"),
        status=SalesOrderLineStatus.PENDING,
    )


def _order(lines: list[SalesOrderLine]) -> SalesOrder:
    totals = service.sum_order_totals(
        [
            service.compute_line_amounts(
                quantity=D(line.quantity),
                unit_price=D(line.unit_price),
                tax_rate=D(line.tax_rate),
                is_interstate=False,
            )
            for line in lines
        ]
    )
    order = SalesOrder(
        id=uuid.uuid4(),
        code="SO-0001",
        customer_id=uuid.uuid4(),
        warehouse_id=uuid.uuid4(),
        order_date=date(2026, 9, 1),
        required_date=date(2026, 9, 20),
        priority=5,
        is_interstate=False,
    )
    service._apply_totals(order, totals)
    return order


# -----------------------------------------------------------------------------
# The arithmetic
# -----------------------------------------------------------------------------


def test_prospective_totals_substitute_only_the_changed_quantities() -> None:
    lines = [_line(1, "10", "100"), _line(2, "5", "40")]
    before = prospective_totals(lines, changed=[], is_interstate=False)
    assert before.total_amount == D("1000") * D("1.18") + D("200") * D("1.18")

    grown = prospective_totals(lines, changed=[(lines[0], D("12"))], is_interstate=False)
    assert grown.total_amount == D("1200") * D("1.18") + D("200") * D("1.18")
    # Nothing was written: the stored quantity is untouched.
    assert lines[0].quantity == D("10")


# -----------------------------------------------------------------------------
# The gate
# -----------------------------------------------------------------------------


class _LineRepo:
    def __init__(self, lines: list[SalesOrderLine]) -> None:
        self.lines = lines

    async def list_for_order(self, order_id: uuid.UUID) -> list[SalesOrderLine]:
        return self.lines


class _Session:
    async def flush(self) -> None:
        return None


class _Trace:
    """Records the order in which the credit check and the reservation move ran."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> _Trace:
    trace = _Trace()

    async def credit_check(
        session: Any, tenant: Any, customer_id: uuid.UUID, amount: Decimal, *, lock: bool
    ) -> None:
        trace.events.append(("credit", (customer_id, amount, lock)))
        if amount > D("2000"):
            raise BusinessRuleError("credit limit exceeded")

    async def rereserve(session: Any, tenant: Any, *, order: Any, changed: Any, port: Any) -> None:
        trace.events.append(("reserve", [(line.line_number, qty) for line, qty in changed]))
        for line, quantity in changed:
            line.quantity = quantity

    monkeypatch.setattr(service, "assert_customer_can_order", credit_check)
    monkeypatch.setattr(service, "_rereserve_changed_lines", rereserve)
    return trace


def _payload(lines: list[SalesOrderLine], quantities: dict[int, str]) -> SalesOrderUpdate:
    return SalesOrderUpdate(
        lines=[
            SalesOrderLineIn(
                line_number=line.line_number,
                item_id=line.item_id,
                quantity=D(quantities.get(line.line_number, str(line.quantity))),
            )
            for line in lines
        ]
    )


async def _update(order: SalesOrder, lines: list[SalesOrderLine], payload: SalesOrderUpdate) -> Any:
    return await service._update_confirmed(
        _Session(),  # type: ignore[arg-type]
        TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4()),
        order=order,
        payload=payload,
        line_repo=_LineRepo(lines),  # type: ignore[arg-type]
        port=None,  # type: ignore[arg-type]
    )


async def test_raising_a_quantity_checks_credit_for_the_new_total_before_reserving(
    trace: _Trace,
) -> None:
    lines = [_line(1, "10", "100")]
    order = _order(lines)
    assert D(order.total_amount) == D("1180")

    await _update(order, lines, _payload(lines, {1: "15"}))

    assert [event for event, _ in trace.events] == ["credit", "reserve"]
    customer_id, amount, lock = trace.events[0][1]
    assert customer_id == order.customer_id
    # The full prospective total, not the delta: outstanding never includes open orders.
    assert amount == D("1770")
    assert lock is True
    assert D(order.total_amount) == D("1770")


async def test_an_over_limit_increase_is_refused_before_any_reservation_moves(
    trace: _Trace,
) -> None:
    lines = [_line(1, "10", "100")]
    order = _order(lines)

    with pytest.raises(BusinessRuleError, match="credit limit"):
        await _update(order, lines, _payload(lines, {1: "20"}))

    assert [event for event, _ in trace.events] == ["credit"]
    assert lines[0].quantity == D("10")
    assert D(order.total_amount) == D("1180")


async def test_reducing_a_quantity_is_not_credit_checked(trace: _Trace) -> None:
    """A held account can still shrink its exposure (ASSUMPTION in the service)."""
    lines = [_line(1, "10", "100")]
    order = _order(lines)

    await _update(order, lines, _payload(lines, {1: "4"}))

    assert [event for event, _ in trace.events] == ["reserve"]
    assert D(order.total_amount) == D("472")


async def test_a_mixed_change_that_nets_to_no_growth_is_not_credit_checked(
    trace: _Trace,
) -> None:
    lines = [_line(1, "10", "100"), _line(2, "10", "100")]
    order = _order(lines)

    await _update(order, lines, _payload(lines, {1: "15", 2: "5"}))

    assert [event for event, _ in trace.events] == ["reserve"]
    assert D(order.total_amount) == D("2360")
