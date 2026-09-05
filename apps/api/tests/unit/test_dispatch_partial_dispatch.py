"""Partial-dispatch arithmetic (master rules section 72).

The sentence these tests defend: a sales-order line for 100 that ships 40 leaves
a balance of 60 open, the order is ``PARTIALLY_DISPATCHED`` rather than done, and
the next consignment picks up exactly where this one stopped. Getting it wrong
means either shipping twice or telling a customer their order is complete when it
is not, and neither is visible until they complain.

Everything here is pure: no database, no session, no fixtures.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.dispatch.service import dispatch_balance, sales_order_status_path
from src.modules.sales.models import SalesOrderStatus


def q(value: str) -> Decimal:
    return Decimal(value)


# -----------------------------------------------------------------------------
# One line's balance
# -----------------------------------------------------------------------------


def test_a_partial_dispatch_leaves_the_balance_open() -> None:
    balance = dispatch_balance(ordered=q("100"), dispatched_before=q("0"), dispatching_now=q("40"))

    assert balance.dispatched_after == q("40")
    assert balance.balance == q("60")
    assert balance.is_partial is True
    assert balance.is_complete is False


def test_a_second_consignment_picks_up_where_the_first_stopped() -> None:
    first = dispatch_balance(ordered=q("100"), dispatched_before=q("0"), dispatching_now=q("40"))
    second = dispatch_balance(
        ordered=q("100"), dispatched_before=first.dispatched_after, dispatching_now=q("35")
    )

    assert second.dispatched_after == q("75")
    assert second.balance == q("25")
    assert second.is_complete is False


def test_the_final_consignment_closes_the_line_exactly() -> None:
    balance = dispatch_balance(ordered=q("100"), dispatched_before=q("75"), dispatching_now=q("25"))

    assert balance.dispatched_after == q("100")
    assert balance.balance == q("0")
    assert balance.is_complete is True
    assert balance.is_partial is False


def test_shipping_the_whole_line_in_one_go_is_complete_immediately() -> None:
    balance = dispatch_balance(ordered=q("100"), dispatched_before=q("0"), dispatching_now=q("100"))

    assert balance.is_complete is True
    assert balance.balance == q("0")


def test_over_dispatch_is_refused_rather_than_clamped() -> None:
    # Clamping would silently ship 25 when somebody typed 30, and the difference
    # is either a mis-key or goods nobody ordered. Both need a person.
    with pytest.raises(BusinessRuleError) as excinfo:
        dispatch_balance(ordered=q("100"), dispatched_before=q("75"), dispatching_now=q("30"))

    details = excinfo.value.details
    assert details["ordered"] == "100.0000"
    assert details["already_dispatched"] == "75.0000"
    assert details["requested"] == "30.0000"
    assert details["remaining"] == "25.0000"


def test_a_fully_dispatched_line_cannot_take_another_consignment() -> None:
    with pytest.raises(BusinessRuleError):
        dispatch_balance(ordered=q("100"), dispatched_before=q("100"), dispatching_now=q("1"))


def test_a_zero_or_negative_quantity_is_refused() -> None:
    for quantity in (q("0"), q("-5")):
        with pytest.raises(BusinessRuleError):
            dispatch_balance(ordered=q("100"), dispatched_before=q("0"), dispatching_now=quantity)


def test_fractional_quantities_do_not_drift() -> None:
    # Four decimal places is the ``Quantity`` scale; three consignments of a
    # third each must close the line exactly rather than leaving 0.0001 behind.
    running = q("0")
    for _ in range(3):
        balance = dispatch_balance(
            ordered=q("10"), dispatched_before=running, dispatching_now=q("3.3333")
        )
        running = balance.dispatched_after

    assert running == q("9.9999")
    final = dispatch_balance(
        ordered=q("10"), dispatched_before=running, dispatching_now=q("0.0001")
    )
    assert final.is_complete is True
    assert final.balance == q("0")


# -----------------------------------------------------------------------------
# What the order's status becomes
# -----------------------------------------------------------------------------


def test_the_first_partial_dispatch_moves_an_allocated_order_to_partially_dispatched() -> None:
    path = sales_order_status_path(SalesOrderStatus.ALLOCATED, fully_dispatched=False)
    assert path == (SalesOrderStatus.PARTIALLY_DISPATCHED,)


def test_a_second_partial_dispatch_changes_nothing() -> None:
    # The quantities moved; the status did not. Re-writing it would produce an
    # audit row asserting a transition that never happened.
    path = sales_order_status_path(SalesOrderStatus.PARTIALLY_DISPATCHED, fully_dispatched=False)
    assert path == ()


def test_the_closing_dispatch_moves_a_partially_dispatched_order_to_dispatched() -> None:
    path = sales_order_status_path(SalesOrderStatus.PARTIALLY_DISPATCHED, fully_dispatched=True)
    assert path == (SalesOrderStatus.DISPATCHED,)


def test_an_order_shipped_complete_in_one_go_is_walked_through_partially_dispatched() -> None:
    # The sales lifecycle has no ALLOCATED -> DISPATCHED edge. Jumping would be
    # a status the sales module's own state machine rejects, so the path has to
    # contain both hops.
    path = sales_order_status_path(SalesOrderStatus.ALLOCATED, fully_dispatched=True)
    assert path == (SalesOrderStatus.PARTIALLY_DISPATCHED, SalesOrderStatus.DISPATCHED)


def test_every_step_of_the_path_is_a_legal_sales_order_transition() -> None:
    from src.modules.sales.service import SALES_ORDER_LIFECYCLE

    for start, fully in (
        (SalesOrderStatus.ALLOCATED, False),
        (SalesOrderStatus.ALLOCATED, True),
        (SalesOrderStatus.PARTIALLY_DISPATCHED, True),
    ):
        current = start
        for target in sales_order_status_path(current, fully_dispatched=fully):
            assert SALES_ORDER_LIFECYCLE.can_transition(current, target), f"{current}->{target}"
            current = target


def test_an_already_dispatched_order_is_not_moved_again() -> None:
    assert sales_order_status_path(SalesOrderStatus.DISPATCHED, fully_dispatched=True) == ()
