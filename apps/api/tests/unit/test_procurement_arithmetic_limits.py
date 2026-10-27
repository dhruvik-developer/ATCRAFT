"""Valid individual fields must not overflow calculated document columns."""

from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.procurement.service_rules import compute_line_amounts, sum_document_totals


@pytest.mark.parametrize(
    "quantity,price,tax",
    [
        ("1000000000000", "100000000000", "0"),
        ("99999999999999.9999", "1", "0"),
        ("90000000000000", "1", "18"),
    ],
)
def test_calculated_line_overflow_is_a_controlled_input_refusal(quantity, price, tax):
    with pytest.raises(ValidationError, match="amount is too large"):
        compute_line_amounts(
            quantity=Decimal(quantity), unit_price=Decimal(price), tax_rate=Decimal(tax)
        )


@pytest.mark.parametrize(
    "field", ["quantity", "unit_price", "tax_rate", "discount_percentage", "discount_amount"]
)
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_arithmetic_is_refused_before_decimal_comparison(field, bad):
    values = {"quantity": Decimal("1"), "unit_price": Decimal("1"), "tax_rate": Decimal("0")}
    values[field] = Decimal(bad)
    with pytest.raises(ValidationError, match="finite decimal"):
        compute_line_amounts(**values)


def test_largest_presentable_money_value_still_prices_exactly():
    line = compute_line_amounts(
        quantity=Decimal("99999999999999.99"), unit_price=Decimal("1"), tax_rate=Decimal("0")
    )
    assert line.line_total == Decimal("99999999999999.99")
    assert sum_document_totals([line]).total_amount == line.line_total


def test_document_sum_and_freight_cannot_overflow_when_each_line_fits():
    line = compute_line_amounts(
        quantity=Decimal("60000000000000"), unit_price=Decimal("1"), tax_rate=Decimal("0")
    )
    with pytest.raises(ValidationError, match="amount is too large"):
        sum_document_totals([line, line])
    with pytest.raises(ValidationError, match="amount is too large"):
        sum_document_totals([line], freight_amount=Decimal("50000000000000"))
