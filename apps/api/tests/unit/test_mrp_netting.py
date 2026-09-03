"""The netting arithmetic, asserted exactly.

Every function here is pure, so these tests carry no database and no fixtures.
They exist because each one of these subtractions, done in the wrong order or
against the wrong pool, produces a plan that is quietly wrong: an under-order
that stops a line, or a double-order that ties up cash.

The worked figures are the ones from the master rules and from
``docs/database.md`` section 6.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.modules.mrp.engine import (
    StockPosition,
    is_late_order,
    net_after_stock,
    net_after_supply,
    order_date_for,
    planning_available,
)

D = Decimal


# -----------------------------------------------------------------------------
# Free stock: reservations, QC holds and damage are not available
# -----------------------------------------------------------------------------


def test_available_excludes_reserved_qc_and_damaged_stock() -> None:
    """The whole point of section 14's "reserved stock is NOT available".

    100 on hand with 40 promised to another order leaves 60 that may be planned
    against, not 100. Treating the reservation as free is how two demands get
    promised the same box.
    """
    position = StockPosition(on_hand=D("100"), reserved=D("40"), qc_hold=D("10"), damaged=D("5"))
    assert position.available == D("45.0000")


def test_available_never_goes_negative() -> None:
    """A corrupted pool must not become negative supply.

    A negative "available" would be *added* to the requirement by the netting
    subtraction and would silently inflate every order raised for the item.
    """
    position = StockPosition(on_hand=D("10"), reserved=D("10"), qc_hold=D("5"))
    assert position.available == D("0.0000")


def test_fully_reserved_stock_is_no_stock_at_all() -> None:
    position = StockPosition(on_hand=D("500"), reserved=D("500"))
    assert position.available == D("0")
    assert net_after_stock(D("120"), position.available) == D("120.0000")


# -----------------------------------------------------------------------------
# Safety stock
# -----------------------------------------------------------------------------


def test_safety_stock_is_held_back_by_default() -> None:
    position = StockPosition(on_hand=D("100"))
    assert planning_available(position, D("30"), respect=True) == D("70.0000")


def test_safety_stock_can_be_planned_away_when_asked() -> None:
    position = StockPosition(on_hand=D("100"))
    assert planning_available(position, D("30"), respect=False) == D("100.0000")


def test_safety_stock_larger_than_stock_leaves_nothing_free() -> None:
    """Not a negative buffer: below the buffer, nothing is available to plan."""
    position = StockPosition(on_hand=D("10"))
    assert planning_available(position, D("40"), respect=True) == D("0.0000")


def test_safety_stock_applies_after_reservations() -> None:
    """Both reductions apply; the buffer is not carved out of promised stock."""
    position = StockPosition(on_hand=D("100"), reserved=D("60"))
    assert planning_available(position, D("25"), respect=True) == D("15.0000")


# -----------------------------------------------------------------------------
# Stage 1: gross against free stock
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gross", "available", "expected"),
    [
        ("200", "60", "140"),  # the sold-and-consumed case from the master rules
        ("100", "0", "100"),
        ("100", "100", "0"),
        ("100", "250", "0"),  # surplus stock never becomes negative demand
        ("0", "50", "0"),
    ],
)
def test_net_after_stock(gross: str, available: str, expected: str) -> None:
    assert net_after_stock(D(gross), D(available)) == D(expected).quantize(D("0.0001"))


def test_net_after_stock_keeps_fractional_quantities_exact() -> None:
    """Quantities are Decimal end to end - 0.1 + 0.2 must not become 0.30000000004."""
    assert net_after_stock(D("10.5"), D("3.25")) == D("7.2500")


# -----------------------------------------------------------------------------
# Stage 2: what is already coming
# -----------------------------------------------------------------------------


def test_incoming_purchase_orders_reduce_the_shortage() -> None:
    """Ordering again against a confirmed purchase order is a duplicate order."""
    assert net_after_supply(D("140"), incoming=D("100"), in_production=D("0")) == D("40.0000")


def test_production_in_progress_reduces_the_shortage() -> None:
    assert net_after_supply(D("140"), incoming=D("0"), in_production=D("140")) == D("0.0000")


def test_both_supply_sources_are_counted() -> None:
    assert net_after_supply(D("140"), incoming=D("60"), in_production=D("50")) == D("30.0000")


def test_supply_beyond_the_requirement_does_not_create_negative_shortage() -> None:
    assert net_after_supply(D("40"), incoming=D("500"), in_production=D("100")) == D("0.0000")


def test_the_full_netting_chain_matches_the_worked_example() -> None:
    """Gross 200, 160 on hand of which 100 reserved, 100 on order.

    Free stock is 60 (not 160), so 140 is net; 100 is already on order, so 40 is
    genuinely short. Each stage in the wrong order gives a different, wrong answer.
    """
    position = StockPosition(on_hand=D("160"), reserved=D("100"))
    free = planning_available(position, D("0"), respect=True)
    net = net_after_stock(D("200"), free)
    shortage = net_after_supply(net, incoming=D("100"), in_production=D("0"))

    assert free == D("60.0000")
    assert net == D("140.0000")
    assert shortage == D("40.0000")


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------


def test_order_date_is_required_date_less_lead_time() -> None:
    assert order_date_for(date(2026, 6, 30), 14) == date(2026, 6, 16)


def test_zero_lead_time_orders_on_the_day() -> None:
    assert order_date_for(date(2026, 6, 30), 0) == date(2026, 6, 30)


def test_order_date_may_land_in_the_past() -> None:
    """That is the answer, not an error - it is how a plan says "already late"."""
    order_date = order_date_for(date(2026, 6, 1), 45)
    assert order_date == date(2026, 4, 17)
    assert is_late_order(order_date, date(2026, 6, 1)) is True


def test_an_order_due_today_is_not_yet_late() -> None:
    assert is_late_order(date(2026, 6, 1), date(2026, 6, 1)) is False


def test_negative_lead_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="lead time"):
        order_date_for(date(2026, 6, 30), -1)
