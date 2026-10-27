"""The next return consumes its own original FIFO slice, without repricing."""

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.inventory.service import LayerCost
from src.modules.sales_returns.costing import returned_layer_slice


def costs(*values):
    return tuple(
        LayerCost(quantity=Decimal(qty), unit_cost=Decimal(price)) for qty, price in values
    )


@pytest.mark.parametrize(
    "prior,quantity,expected",
    [
        ("0", "5", costs(("5", "10"))),
        ("5", "15", costs(("5", "10"), ("10", "20"))),
        ("10", "10", costs(("10", "20"))),
        ("19.5", "0.5", costs(("0.5", "20"))),
    ],
)
def test_partial_returns_keep_the_original_fifo_sequence(prior, quantity, expected):
    assert (
        returned_layer_slice(
            costs(("10", "10"), ("10", "20")),
            already_returned=Decimal(prior),
            quantity=Decimal(quantity),
        )
        == expected
    )


def test_a_recorded_zero_cost_is_a_real_cost():
    assert returned_layer_slice(
        costs(("10", "0")), already_returned=Decimal("0"), quantity=Decimal("10")
    ) == costs(("10", "0"))


@pytest.mark.parametrize("prior,quantity", [("-1", "1"), ("0", "0"), ("11", "1"), ("9", "2")])
def test_inconsistent_or_excess_source_quantities_are_refused(prior, quantity):
    with pytest.raises(BusinessRuleError):
        returned_layer_slice(
            costs(("10", "10")), already_returned=Decimal(prior), quantity=Decimal(quantity)
        )
