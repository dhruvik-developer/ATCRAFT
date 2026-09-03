"""Three-way match variance - the control this module exists for.

``evaluate_match`` decides whether a vendor's bill agrees with what was ordered
(price) and what was accepted at the gate (quantity). It is pure, so every
tolerance boundary can be pinned down exactly rather than inferred from a
passing integration run - which matters, because the difference between 2.00%
and 2.01% is the difference between paying an invoice and stopping it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.finance.models import (
    PRICE_VARIANCE_TOLERANCE,
    QUANTITY_VARIANCE_TOLERANCE,
    MatchStatus,
)
from src.modules.finance.service import MatchLineInput, evaluate_match, variance_ratio

pytestmark = pytest.mark.unit

INVOICE_ID = uuid.uuid4()
VENDOR_ID = uuid.uuid4()
ORDER_ID = uuid.uuid4()
RECEIPT_ID = uuid.uuid4()


def line(
    *,
    invoiced_quantity: str,
    invoiced_unit_price: str,
    ordered_unit_price: str | None = None,
    accepted_quantity: str | None = None,
    ordered_quantity: str | None = None,
    order_line: bool = True,
    line_number: int = 1,
) -> MatchLineInput:
    quantity = Decimal(invoiced_quantity)
    price = Decimal(invoiced_unit_price)
    return MatchLineInput(
        line_number=line_number,
        invoice_line_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        item_code=f"RM-{line_number:03d}",
        invoiced_quantity=quantity,
        invoiced_unit_price=price,
        invoiced_amount=quantity * price,
        purchase_order_line_id=uuid.uuid4() if order_line else None,
        ordered_quantity=Decimal(ordered_quantity) if ordered_quantity else None,
        ordered_unit_price=Decimal(ordered_unit_price) if ordered_unit_price else None,
        accepted_quantity=Decimal(accepted_quantity) if accepted_quantity else None,
    )


def run(*lines: MatchLineInput, **overrides: object):
    kwargs: dict = {
        "invoice_id": INVOICE_ID,
        "invoice_code": "VINV/2026-27/00001",
        "vendor_id": VENDOR_ID,
        "purchase_order_id": ORDER_ID,
        "goods_receipt_id": RECEIPT_ID,
    }
    kwargs.update(overrides)
    return evaluate_match(list(lines), **kwargs)


class TestExactAgreement:
    def test_a_bill_matching_the_order_and_the_receipt_passes(self) -> None:
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                ordered_quantity="100",
                accepted_quantity="100",
            )
        )

        assert result.matched is True
        assert result.match_status is MatchStatus.MATCHED
        assert result.blocking_reasons == []
        assert result.total_invoiced == Decimal("2500.00")
        assert result.total_expected == Decimal("2500.00")
        assert result.total_variance == Decimal("0.00")
        assert result.lines[0].within_tolerance is True

    def test_billing_only_what_was_accepted_passes_on_a_partial_receipt(self) -> None:
        # 100 ordered, 60 accepted, 60 billed: correct, not a shortfall.
        result = run(
            line(
                invoiced_quantity="60",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                ordered_quantity="100",
                accepted_quantity="60",
            )
        )

        assert result.matched is True
        assert result.total_expected == Decimal("1500.00")


class TestPriceTolerance:
    def test_a_price_within_two_percent_passes(self) -> None:
        # 25 -> 25.50 is exactly 2%.
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25.50",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is True
        assert result.lines[0].price_variance == Decimal("0.500000")
        assert result.lines[0].price_variance_ratio == Decimal("0.02")

    def test_a_price_just_beyond_two_percent_blocks(self) -> None:
        # 25 -> 25.51 is 2.04%.
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25.51",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is False
        assert result.match_status is MatchStatus.VARIANCE
        assert len(result.blocking_reasons) == 1
        assert "price" in result.blocking_reasons[0].lower()
        assert result.lines[0].within_tolerance is False

    def test_a_price_below_the_order_is_also_a_variance_when_it_is_large(self) -> None:
        # Undercharging is still a disagreement with the agreed price, and the
        # tolerance is symmetric: a credit somebody forgot to invoice is as
        # much a reconciliation problem as an overcharge.
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="20",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is False
        assert result.lines[0].price_variance_ratio == Decimal("-0.2")
        assert result.total_variance == Decimal("-500.00")

    def test_a_small_undercharge_within_tolerance_passes(self) -> None:
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="24.50",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is True

    def test_billing_against_a_zero_ordered_price_blocks(self) -> None:
        # A free-of-charge line the vendor decided to charge for. The
        # proportional variance is undefined, and reporting it as 0% would let
        # it straight through.
        result = run(
            line(
                invoiced_quantity="10",
                invoiced_unit_price="5",
                ordered_unit_price="0",
                accepted_quantity="10",
            )
        )

        assert result.matched is False
        assert result.lines[0].price_variance_ratio is None
        assert "ordered price of zero" in result.blocking_reasons[0]

    def test_the_tolerance_constant_is_the_one_the_matcher_uses(self) -> None:
        # Guards against the constant being changed in the model while the
        # matcher keeps its own copy.
        result = run(
            line(
                invoiced_quantity="1",
                invoiced_unit_price="100",
                ordered_unit_price="100",
                accepted_quantity="1",
            )
        )

        assert result.price_tolerance == PRICE_VARIANCE_TOLERANCE
        assert result.quantity_tolerance == QUANTITY_VARIANCE_TOLERANCE


class TestQuantityTolerance:
    def test_billing_more_than_was_accepted_blocks(self) -> None:
        # The classic overbill: the vendor shipped 100, we accepted 95 and they
        # invoiced 100.
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                ordered_quantity="100",
                accepted_quantity="95",
            )
        )

        assert result.matched is False
        assert result.lines[0].quantity_variance == Decimal("5.0000")
        assert "accepted at the gate" in result.blocking_reasons[0]
        # Expected is what was accepted at the agreed price, so the money
        # variance is the five units nobody took in.
        assert result.total_expected == Decimal("2375.00")
        assert result.total_variance == Decimal("125.00")

    def test_a_quantity_variance_of_a_single_unit_blocks(self) -> None:
        # Zero tolerance means zero: quantity is not a rounding artefact.
        result = run(
            line(
                invoiced_quantity="101",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is False

    def test_a_fractional_quantity_variance_blocks(self) -> None:
        result = run(
            line(
                invoiced_quantity="100.5",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is False
        assert result.lines[0].quantity_variance == Decimal("0.5000")

    def test_billing_less_than_was_accepted_also_blocks(self) -> None:
        result = run(
            line(
                invoiced_quantity="90",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                accepted_quantity="100",
            )
        )

        assert result.matched is False
        assert result.lines[0].quantity_variance == Decimal("-10.0000")

    def test_a_line_with_nothing_accepted_yet_blocks(self) -> None:
        # An invoice that arrives before the goods do must not be approved just
        # because there is nothing to contradict it.
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                accepted_quantity=None,
            )
        )

        assert result.matched is False
        assert "no accepted goods-receipt quantity" in result.blocking_reasons[0]


class TestUnmatchedLines:
    def test_a_line_with_no_order_line_blocks(self) -> None:
        # A handling charge the vendor added after the fact.
        result = run(
            line(
                invoiced_quantity="1",
                invoiced_unit_price="750",
                order_line=False,
                line_number=2,
            )
        )

        assert result.matched is False
        assert "not billed against any purchase order line" in result.blocking_reasons[0]
        assert result.lines[0].expected_amount is None
        assert result.lines[0].amount_variance is None

    def test_an_invoice_with_no_purchase_order_at_all_blocks(self) -> None:
        result = run(
            line(invoiced_quantity="1", invoiced_unit_price="500", order_line=False),
            purchase_order_id=None,
            goods_receipt_id=None,
        )

        assert result.matched is False
        assert any("not linked to a purchase order" in reason for reason in result.blocking_reasons)


class TestMultiLineInvoices:
    def test_one_bad_line_blocks_the_whole_invoice(self) -> None:
        result = run(
            line(
                invoiced_quantity="100",
                invoiced_unit_price="25",
                ordered_unit_price="25",
                accepted_quantity="100",
                line_number=1,
            ),
            line(
                invoiced_quantity="50",
                invoiced_unit_price="40",
                ordered_unit_price="30",
                accepted_quantity="50",
                line_number=2,
            ),
        )

        assert result.matched is False
        assert result.lines[0].within_tolerance is True
        assert result.lines[1].within_tolerance is False
        assert len(result.blocking_reasons) == 1

    def test_the_worst_variance_is_the_largest_single_line_difference(self) -> None:
        result = run(
            line(
                invoiced_quantity="10",
                invoiced_unit_price="11",
                ordered_unit_price="10",
                accepted_quantity="10",
                line_number=1,
            ),
            line(
                invoiced_quantity="100",
                invoiced_unit_price="12",
                ordered_unit_price="10",
                accepted_quantity="100",
                line_number=2,
            ),
        )

        # Line 1 is out by 10, line 2 by 200 - the payables queue sorts on the
        # larger of the two.
        assert result.worst_variance == Decimal("200.00")
        assert result.total_invoiced == Decimal("1310.00")
        assert result.total_expected == Decimal("1100.00")
        assert result.total_variance == Decimal("210.00")

    def test_totals_stay_decimal_throughout(self) -> None:
        result = run(
            line(
                invoiced_quantity="3",
                invoiced_unit_price="0.1",
                ordered_unit_price="0.1",
                accepted_quantity="3",
            )
        )

        assert isinstance(result.total_invoiced, Decimal)
        assert result.total_invoiced == Decimal("0.30")


class TestVarianceRatio:
    def test_a_zero_basis_has_no_ratio(self) -> None:
        assert variance_ratio(Decimal("5"), Decimal("0")) is None

    def test_the_ratio_is_signed(self) -> None:
        assert variance_ratio(Decimal("110"), Decimal("100")) == Decimal("0.1")
        assert variance_ratio(Decimal("90"), Decimal("100")) == Decimal("-0.1")
