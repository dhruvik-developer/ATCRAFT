"""Physical cartons must agree with shipped items, lots, demands and serials."""

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.dispatch.repository import CartonContentFacts
from src.modules.dispatch.schemas import DispatchLineIn
from src.modules.dispatch.service import _assert_carton_contents_match


def _content(carton_id, item_id, quantity="10", *, batch=None, demand=None, serials=()):
    return CartonContentFacts(carton_id, item_id, batch, demand, Decimal(quantity), tuple(serials))


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
