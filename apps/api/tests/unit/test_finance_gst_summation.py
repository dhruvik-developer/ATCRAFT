"""GST line pricing and the summation behind the return.

Two pure pieces: ``price_invoice_line``, which decides the taxable value and
splits the tax, and ``summarise_gst_rows``, which rolls the grouped aggregate
into one side of the return. Both are arithmetic that ends up on a statutory
document, so the tests pin down the rounding rather than trusting it.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.errors import ValidationError
from src.modules.finance.service import price_invoice_line, summarise_gst_rows

pytestmark = pytest.mark.unit


def row(
    *,
    hsn: str | None,
    rate: str,
    taxable: str,
    cgst: str = "0",
    sgst: str = "0",
    igst: str = "0",
    documents: int = 1,
) -> SimpleNamespace:
    """One grouped row as the aggregate query returns it."""
    return SimpleNamespace(
        hsn_code=hsn,
        tax_rate=Decimal(rate),
        taxable_amount=Decimal(taxable),
        cgst_amount=Decimal(cgst),
        sgst_amount=Decimal(sgst),
        igst_amount=Decimal(igst),
        document_count=documents,
    )


class TestLinePricing:
    def test_an_intrastate_line_splits_the_tax_in_half(self) -> None:
        pricing = price_invoice_line(
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            discount_amount=Decimal("0"),
            tax_rate=Decimal("18"),
            is_interstate=False,
        )

        assert pricing.taxable_amount == Decimal("1000.00")
        assert pricing.cgst_amount == Decimal("90.00")
        assert pricing.sgst_amount == Decimal("90.00")
        assert pricing.igst_amount == Decimal("0")
        assert pricing.tax_amount == Decimal("180.00")
        assert pricing.line_total == Decimal("1180.00")

    def test_an_interstate_line_is_all_igst(self) -> None:
        pricing = price_invoice_line(
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            discount_amount=Decimal("0"),
            tax_rate=Decimal("18"),
            is_interstate=True,
        )

        assert pricing.igst_amount == Decimal("180.00")
        assert pricing.cgst_amount == Decimal("0")
        assert pricing.sgst_amount == Decimal("0")

    def test_an_odd_number_of_paise_still_splits_exactly(self) -> None:
        # 18% of 100.05 is 18.009 -> 18.01, which halves to 9.005. The CHECK on
        # the row requires cgst + sgst + igst = tax_amount exactly, so the
        # second half is the remainder rather than a second rounding.
        pricing = price_invoice_line(
            quantity=Decimal("1"),
            unit_price=Decimal("100.05"),
            discount_amount=Decimal("0"),
            tax_rate=Decimal("18"),
            is_interstate=False,
        )

        assert pricing.tax_amount == Decimal("18.01")
        assert pricing.cgst_amount + pricing.sgst_amount == pricing.tax_amount
        assert pricing.cgst_amount == Decimal("9.01")
        assert pricing.sgst_amount == Decimal("9.00")

    def test_the_split_always_reconstitutes_the_tax(self) -> None:
        # Swept across a range of values because the halving is where an
        # invoice's parts most easily stop adding up to its whole.
        for paise in range(1, 200):
            pricing = price_invoice_line(
                quantity=Decimal("1"),
                unit_price=Decimal(paise) / Decimal("100"),
                discount_amount=Decimal("0"),
                tax_rate=Decimal("18"),
                is_interstate=False,
            )
            assert (
                pricing.cgst_amount + pricing.sgst_amount + pricing.igst_amount
                == pricing.tax_amount
            )
            assert pricing.line_total == pricing.taxable_amount + pricing.tax_amount

    def test_the_discount_reduces_the_taxable_value(self) -> None:
        pricing = price_invoice_line(
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            discount_amount=Decimal("150"),
            tax_rate=Decimal("18"),
            is_interstate=False,
        )

        assert pricing.taxable_amount == Decimal("850.00")
        assert pricing.tax_amount == Decimal("153.00")

    def test_a_discount_larger_than_the_line_is_refused(self) -> None:
        # A negative taxable value is not a discount, it is a data-entry error
        # on its way into a return.
        with pytest.raises(ValidationError):
            price_invoice_line(
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                discount_amount=Decimal("150"),
                tax_rate=Decimal("18"),
                is_interstate=False,
            )

    def test_a_zero_rated_line_carries_no_tax(self) -> None:
        pricing = price_invoice_line(
            quantity=Decimal("5"),
            unit_price=Decimal("20"),
            discount_amount=Decimal("0"),
            tax_rate=Decimal("0"),
            is_interstate=False,
        )

        assert pricing.tax_amount == Decimal("0")
        assert pricing.line_total == Decimal("100.00")

    def test_a_six_decimal_unit_price_is_carried_to_money_scale(self) -> None:
        # Fasteners priced per thousand: 0.001250 x 40000 = 50.00 exactly.
        pricing = price_invoice_line(
            quantity=Decimal("40000"),
            unit_price=Decimal("0.001250"),
            discount_amount=Decimal("0"),
            tax_rate=Decimal("18"),
            is_interstate=False,
        )

        assert pricing.taxable_amount == Decimal("50.00")
        assert pricing.tax_amount == Decimal("9.00")


class TestGstSummation:
    def test_an_empty_period_sums_to_zero(self) -> None:
        section = summarise_gst_rows([])

        assert section.rows == []
        assert section.taxable_amount == Decimal("0.00")
        assert section.total_tax == Decimal("0.00")

    def test_slabs_are_added_up_across_rates_and_hsn_codes(self) -> None:
        section = summarise_gst_rows(
            [
                row(hsn="7318", rate="18", taxable="10000", cgst="900", sgst="900"),
                row(hsn="8301", rate="18", taxable="5000", cgst="450", sgst="450"),
                row(hsn="7318", rate="12", taxable="2000", igst="240"),
            ]
        )

        assert len(section.rows) == 3
        assert section.taxable_amount == Decimal("17000.00")
        assert section.cgst_amount == Decimal("1350.00")
        assert section.sgst_amount == Decimal("1350.00")
        assert section.igst_amount == Decimal("240.00")
        assert section.total_tax == Decimal("2940.00")

    def test_each_row_carries_its_own_total(self) -> None:
        section = summarise_gst_rows(
            [row(hsn="7318", rate="18", taxable="10000", cgst="900", sgst="900")]
        )

        assert section.rows[0].total_tax == Decimal("1800.00")
        assert section.rows[0].document_count == 1

    def test_an_unclassified_line_keeps_a_null_hsn_rather_than_being_dropped(self) -> None:
        # A freight or service line has no HSN. Dropping it would understate
        # the return; grouping it under a made-up code would misstate it.
        section = summarise_gst_rows(
            [row(hsn=None, rate="18", taxable="1000", cgst="90", sgst="90")]
        )

        assert section.rows[0].hsn_code is None
        assert section.taxable_amount == Decimal("1000.00")

    def test_fractional_aggregates_are_rounded_once_at_money_scale(self) -> None:
        # The output side apportions a line's tax by dispatched quantity, so the
        # aggregate arrives with more decimals than money has. Rounding happens
        # on the way out, not repeatedly on the way there.
        section = summarise_gst_rows(
            [
                row(
                    hsn="7318",
                    rate="18",
                    taxable="3333.333333",
                    cgst="299.999999",
                    sgst="300.000001",
                )
            ]
        )

        assert section.taxable_amount == Decimal("3333.33")
        assert section.cgst_amount == Decimal("300.00")
        assert section.sgst_amount == Decimal("300.00")
        assert section.total_tax == Decimal("600.00")

    def test_every_total_is_a_decimal(self) -> None:
        section = summarise_gst_rows(
            [row(hsn="7318", rate="18", taxable="10", cgst="0.9", sgst="0.9")]
        )

        for value in (
            section.taxable_amount,
            section.cgst_amount,
            section.sgst_amount,
            section.igst_amount,
            section.total_tax,
        ):
            assert isinstance(value, Decimal)
