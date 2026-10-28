"""Physical cartons must agree with shipped items, lots, demands and serials."""

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.dispatch.repository import CartonContentFacts
from src.modules.dispatch.schemas import DispatchLineIn
from src.modules.dispatch.service import _assert_carton_contents_match, assign_cartons


def _content(carton_id, item_id, quantity="10", *, batch=None, demand=None, serials=(), number=1):
    return CartonContentFacts(
        carton_id, item_id, batch, demand, Decimal(quantity), tuple(serials), number
    )


def _line(cartons, item_id, quantity="10", *, batch=None, demand=None, serials=()):
    return DispatchLineIn(
        item_id=item_id,
        quantity=Decimal(quantity),
        batch_id=batch,
        sales_order_line_id=demand,
        serial_ids=list(serials),
        carton_ids=cartons,
    )


def test_one_mixed_carton_may_appear_on_distinct_item_rows() -> None:
    carton, first, second = (uuid.uuid4() for _ in range(3))
    _assert_carton_contents_match(
        [_line([carton], first), _line([carton], second, "5")],
        [_content(carton, first), _content(carton, second, "5")],
    )


def test_same_item_for_distinct_sales_lines_in_one_box_remains_distinct() -> None:
    carton, item, first_demand, second_demand = (uuid.uuid4() for _ in range(4))
    _assert_carton_contents_match(
        [_line([carton], item, demand=first_demand), _line([carton], item, demand=second_demand)],
        [_content(carton, item, demand=first_demand), _content(carton, item, demand=second_demand)],
    )


def test_two_cartons_consolidate_without_counting_contents_twice() -> None:
    first, second, item = (uuid.uuid4() for _ in range(3))
    _assert_carton_contents_match(
        [_line([first, second], item, "15")], [_content(first, item), _content(second, item, "5")]
    )


@pytest.mark.parametrize("quantity", ["9", "11"])
def test_under_and_overstated_quantity_are_refused(quantity) -> None:
    carton, item = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(BusinessRuleError, match="quantity does not match"):
        _assert_carton_contents_match([_line([carton], item, quantity)], [_content(carton, item)])


@pytest.mark.parametrize("mismatch", ["item", "batch", "demand"])
def test_carton_content_identity_must_match(mismatch) -> None:
    carton, item, batch, demand = (uuid.uuid4() for _ in range(4))
    options = {"batch": batch, "demand": demand}
    if mismatch == "item":
        item_on_line = uuid.uuid4()
    else:
        item_on_line = item
        options[mismatch] = uuid.uuid4()
    with pytest.raises(BusinessRuleError, match="no matching item"):
        _assert_carton_contents_match(
            [_line([carton], item_on_line, **options)],
            [_content(carton, item, batch=batch, demand=demand)],
        )


def test_a_mixed_carton_cannot_ship_only_one_of_its_contents() -> None:
    carton, first, second = (uuid.uuid4() for _ in range(3))
    with pytest.raises(BusinessRuleError, match="some contents are missing"):
        _assert_carton_contents_match(
            [_line([carton], first)], [_content(carton, first), _content(carton, second)]
        )


def test_bulk_goods_cannot_cover_unreferenced_contents_of_a_mixed_carton() -> None:
    carton, first, second = (uuid.uuid4() for _ in range(3))
    with pytest.raises(BusinessRuleError, match="some contents are missing"):
        _assert_carton_contents_match(
            [_line([carton], first), _line([], second)],
            [_content(carton, first), _content(carton, second)],
        )


def test_a_cartons_same_content_cannot_be_repeated_on_two_rows() -> None:
    carton, item = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(BusinessRuleError, match="already loaded"):
        _assert_carton_contents_match(
            [_line([carton], item), _line([carton], item)], [_content(carton, item)]
        )


def test_serial_identity_must_match_even_when_the_count_matches() -> None:
    carton, item, packed, other = (uuid.uuid4() for _ in range(4))
    with pytest.raises(BusinessRuleError, match="serials do not match"):
        _assert_carton_contents_match(
            [_line([carton], item, "1", serials=[other])],
            [_content(carton, item, "1", serials=[packed])],
        )


def test_matching_serials_in_multiple_cartons_are_accepted() -> None:
    first, second, item, serial_a, serial_b = (uuid.uuid4() for _ in range(5))
    _assert_carton_contents_match(
        [_line([first, second], item, "2", serials=[serial_b, serial_a])],
        [
            _content(first, item, "1", serials=[serial_a]),
            _content(second, item, "1", serials=[serial_b]),
        ],
    )


# -----------------------------------------------------------------------------
# Matching a packing order's cartons to a consignment that names none
# -----------------------------------------------------------------------------
#
# A consignment raised on screen names no carton; the packing hand-over then
# loads the whole packing order onto it. The cartons are assigned to the lines
# by the same item / lot / demand key the check above verifies.


def test_each_line_is_given_the_cartons_holding_its_goods() -> None:
    first, second, item, other, demand = (uuid.uuid4() for _ in range(5))
    lines = [_line([], item, "15", demand=demand), _line([], other, "5")]
    contents = [
        _content(first, item, "10", demand=demand, number=1),
        _content(second, item, "5", demand=demand, number=2),
        _content(second, other, "5", number=2),
    ]

    assign_cartons(lines, contents)

    assert lines[0].carton_ids == sorted([first, second], key=str)
    assert lines[1].carton_ids == [second]
    # The assignment is exactly what the reconciliation then accepts.
    _assert_carton_contents_match(lines, contents)


def test_a_line_whose_goods_were_not_packed_stays_loose() -> None:
    carton, packed, loose = (uuid.uuid4() for _ in range(3))
    lines = [_line([], packed), _line([], loose, "3")]

    assign_cartons(lines, [_content(carton, packed)])

    assert lines[0].carton_ids == [carton]
    assert lines[1].carton_ids == []


def test_packed_goods_no_line_carries_are_refused_by_carton_number() -> None:
    carried, left_behind, item, other = (uuid.uuid4() for _ in range(4))
    lines = [_line([], item)]

    with pytest.raises(BusinessRuleError, match=r"Carton\(s\) 7 of the packing order") as excinfo:
        assign_cartons(
            lines,
            [_content(carried, item, number=1), _content(left_behind, other, "4", number=7)],
        )

    assert excinfo.value.details["carton_ids"] == [str(left_behind)]


def test_a_different_lot_or_demand_is_not_the_same_goods() -> None:
    carton, item, lot, demand = (uuid.uuid4() for _ in range(4))

    with pytest.raises(BusinessRuleError, match="no line of this consignment carries"):
        assign_cartons([_line([], item, batch=lot)], [_content(carton, item, demand=demand)])


def test_two_lines_the_boxes_cannot_tell_apart_are_refused() -> None:
    carton, item = uuid.uuid4(), uuid.uuid4()

    with pytest.raises(BusinessRuleError, match="Lines 1 and 2") as excinfo:
        assign_cartons([_line([], item, "4"), _line([], item, "6")], [_content(carton, item)])

    assert excinfo.value.details["line_numbers"] == [1, 2]
