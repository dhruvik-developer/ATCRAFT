"""Where a reservation is held when its caller names no bin or lot.

No database. :func:`plan_positions` is the pure choice behind
``allocation.reserve_from_stock``: given the positions that hold reservable
stock, which ones a promise is pinned to and how much at each
(docs/assumptions.md §E2-2). What is being defended:

* stock that sits in a bin or a lot is promised *there* - the defect was a
  sales reservation pinned to the unbinned, lot-less position, which held
  nothing and failed the whole confirmation;
* as few positions as possible: one lot that covers the line, then one bin;
* lots in the movement engine's FEFO order, so the lot a reservation holds is
  the lot an unnamed issue would pick (§E8b-1);
* a split only when no single lot or position can cover the quantity, in that
  same fixed order, and never more than the positions hold.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.modules.allocation.repository import ReservablePosition
from src.modules.allocation.service import plan_positions

pytestmark = pytest.mark.unit

ITEM = uuid.uuid4()
TODAY = date(2026, 10, 28)


def _position(
    available: str,
    *,
    location: uuid.UUID | None = None,
    code: str | None = None,
    lot: uuid.UUID | None = None,
    expires_in: int | None = None,
    received_ago: int | None = 0,
) -> ReservablePosition:
    return ReservablePosition(
        item_id=ITEM,
        location_id=location,
        batch_id=lot,
        available=Decimal(available),
        expires_on=None if expires_in is None else TODAY + timedelta(days=expires_in),
        received_on=None if received_ago is None else TODAY - timedelta(days=received_ago),
        location_code=code,
    )


def _drawn(plan: list[tuple[ReservablePosition, Decimal]]) -> list[tuple[object, Decimal]]:
    return [(position.location_code or position.batch_id, quantity) for position, quantity in plan]


def test_stock_in_one_bin_is_held_in_that_bin() -> None:
    rack = _position("100", location=uuid.uuid4(), code="A1")

    plan = plan_positions([rack], Decimal("100"))

    assert plan == [(rack, Decimal("100.0000"))]


def test_one_bin_that_covers_the_quantity_wins_over_bin_order() -> None:
    small = _position("5", location=uuid.uuid4(), code="A1")
    large = _position("100", location=uuid.uuid4(), code="B7")

    assert _drawn(plan_positions([small, large], Decimal("50"))) == [("B7", Decimal("50"))]


def test_no_bin_covering_splits_in_bin_order_unbinned_first() -> None:
    unbinned = _position("10")
    rack_b = _position("50", location=uuid.uuid4(), code="B7")
    rack_a = _position("60", location=uuid.uuid4(), code="A1")

    plan = plan_positions([rack_b, rack_a, unbinned], Decimal("100"))

    assert [(position.location_code, quantity) for position, quantity in plan] == [
        (None, Decimal("10.0000")),
        ("A1", Decimal("60.0000")),
        ("B7", Decimal("30.0000")),
    ]


def test_the_earliest_expiring_lot_that_covers_the_line_is_held_whole() -> None:
    early, late, undated = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    positions = [
        _position("30", lot=early, expires_in=30),
        _position("100", lot=undated),
        _position("100", lot=late, expires_in=200),
    ]

    # The early lot cannot cover 50; the dated late lot comes before the
    # undated one - exactly the lot the engine's own FEFO pick chooses.
    assert _drawn(plan_positions(positions, Decimal("50"))) == [(late, Decimal("50"))]


def test_no_lot_covering_splits_in_fefo_order() -> None:
    early, late = uuid.uuid4(), uuid.uuid4()
    positions = [
        _position("50", lot=late, expires_in=200),
        _position("30", lot=early, expires_in=30),
    ]

    assert _drawn(plan_positions(positions, Decimal("70"))) == [
        (early, Decimal("30")),
        (late, Decimal("40")),
    ]


def test_equal_expiry_falls_back_to_receipt_date_then_lot_id() -> None:
    older, newer = uuid.uuid4(), uuid.uuid4()
    positions = [
        _position("20", lot=newer, expires_in=90, received_ago=1),
        _position("20", lot=older, expires_in=90, received_ago=40),
    ]

    assert _drawn(plan_positions(positions, Decimal("30"))) == [
        (older, Decimal("20")),
        (newer, Decimal("10")),
    ]


def test_one_lot_spread_over_two_bins_is_preferred_over_a_later_lot() -> None:
    early, late = uuid.uuid4(), uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    positions = [
        _position("100", lot=late, expires_in=200, location=uuid.uuid4(), code="C1"),
        _position("30", lot=early, expires_in=30, location=second, code="B7"),
        _position("30", lot=early, expires_in=30, location=first, code="A1"),
    ]

    plan = plan_positions(positions, Decimal("50"))

    # One lot keeps the line on one dispatch line; within it, bin order.
    assert [(position.batch_id, position.location_id, qty) for position, qty in plan] == [
        (early, first, Decimal("30.0000")),
        (early, second, Decimal("20.0000")),
    ]


def test_never_plans_more_than_the_positions_hold() -> None:
    positions = [_position("30", location=uuid.uuid4(), code="A1"), _position("20")]

    plan = plan_positions(positions, Decimal("80"))

    assert sum(quantity for _, quantity in plan) == Decimal("50")


@pytest.mark.parametrize("quantity", ["0", "-5"])
def test_nothing_is_planned_for_a_non_positive_quantity(quantity: str) -> None:
    assert plan_positions([_position("10")], Decimal(quantity)) == []


def test_empty_positions_are_ignored() -> None:
    empty = _position("0", location=uuid.uuid4(), code="A1")
    full = _position("10", location=uuid.uuid4(), code="B7")

    assert _drawn(plan_positions([empty, full], Decimal("10"))) == [("B7", Decimal("10"))]
