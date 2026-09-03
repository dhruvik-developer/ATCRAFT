"""GST computation and line arithmetic for sales orders.

These are the numbers printed on a tax invoice, so every assertion here is a
statement about what a customer will be charged. No database: the whole
calculation is pure by construction, which is the point of keeping it in
``compute_line_amounts`` rather than inside the persistence path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.sales.service import (
    compute_line_amounts,
    resolve_tax_treatment,
    sum_order_totals,
)

pytestmark = pytest.mark.unit


# -----------------------------------------------------------------------------
# Which tax applies
# -----------------------------------------------------------------------------


def test_same_state_is_intra_state():
    assert resolve_tax_treatment("27", "27") is False


def test_different_state_is_inter_state():
    assert resolve_tax_treatment("27", "29") is True


def test_unknown_place_of_supply_falls_back_to_igst():
    """An export has no Indian place of supply; IGST is the correct treatment.

    Guessing "same state" from a missing value would understate the tax on every
    export, which is the expensive direction to be wrong in.
    """
    assert resolve_tax_treatment("27", None) is True
    assert resolve_tax_treatment(None, "27") is True
    assert resolve_tax_treatment(None, None) is True


# -----------------------------------------------------------------------------
# Intra-state: CGST + SGST
# -----------------------------------------------------------------------------


def test_intra_state_splits_gst_into_two_equal_halves():
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("18"),
        is_interstate=False,
    )

    assert amounts.gross_amount == Decimal("1000.00")
    assert amounts.taxable_amount == Decimal("1000.00")
    assert amounts.cgst_amount == Decimal("90.00")
    assert amounts.sgst_amount == Decimal("90.00")
    assert amounts.igst_amount == Decimal("0.00")
    assert amounts.tax_amount == Decimal("180.00")
    assert amounts.line_total == Decimal("1180.00")


def test_intra_state_halves_are_rounded_independently_and_stay_equal():
    """Each half is rounded on its own, and the total is their sum.

    5% of 1234.55 is 61.7275. Halving the rounded total (61.73) would give
    30.865 - a value that cannot be printed and that does not add back. Rounding
    each 2.5% component gives 30.86 + 30.86 = 61.72, which is what the invoice
    shows and what the two GST columns must contain.
    """
    amounts = compute_line_amounts(
        quantity=Decimal("1"),
        unit_price=Decimal("1234.55"),
        tax_rate=Decimal("5"),
        is_interstate=False,
    )

    assert amounts.cgst_amount == amounts.sgst_amount == Decimal("30.86")
    assert amounts.tax_amount == amounts.cgst_amount + amounts.sgst_amount == Decimal("61.72")
    assert amounts.line_total == Decimal("1296.27")


def test_intra_state_odd_rate_still_splits_evenly():
    """A 3% rate splits into 1.5% + 1.5% - not into an integer number of paise."""
    amounts = compute_line_amounts(
        quantity=Decimal("7"),
        unit_price=Decimal("99.99"),
        tax_rate=Decimal("3"),
        is_interstate=False,
    )

    assert amounts.gross_amount == Decimal("699.93")
    # 1.5% of 699.93 = 10.49895 -> 10.50 each side.
    assert amounts.cgst_amount == amounts.sgst_amount == Decimal("10.50")
    assert amounts.tax_amount == Decimal("21.00")


# -----------------------------------------------------------------------------
# Inter-state: IGST
# -----------------------------------------------------------------------------


def test_inter_state_charges_igst_only():
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("18"),
        is_interstate=True,
    )

    assert amounts.igst_amount == Decimal("180.00")
    assert amounts.cgst_amount == Decimal("0.00")
    assert amounts.sgst_amount == Decimal("0.00")
    assert amounts.tax_amount == Decimal("180.00")
    assert amounts.line_total == Decimal("1180.00")


def test_igst_and_cgst_sgst_never_coexist():
    """The database constraint says the same thing; this proves the code agrees."""
    for interstate in (True, False):
        amounts = compute_line_amounts(
            quantity=Decimal("3"),
            unit_price=Decimal("77.77"),
            tax_rate=Decimal("12"),
            is_interstate=interstate,
        )
        both_sides = amounts.igst_amount > 0 and (
            amounts.cgst_amount > 0 or amounts.sgst_amount > 0
        )
        assert not both_sides
        assert amounts.tax_amount == (
            amounts.cgst_amount + amounts.sgst_amount + amounts.igst_amount
        )


def test_same_line_costs_the_same_either_way_at_the_round_number():
    """The treatment changes the columns, not the money, when nothing rounds."""
    kwargs = {
        "quantity": Decimal("4"),
        "unit_price": Decimal("250"),
        "tax_rate": Decimal("18"),
    }
    intra = compute_line_amounts(is_interstate=False, **kwargs)
    inter = compute_line_amounts(is_interstate=True, **kwargs)

    assert intra.line_total == inter.line_total == Decimal("1180.00")


# -----------------------------------------------------------------------------
# Discounts
# -----------------------------------------------------------------------------


def test_percentage_discount_reduces_the_taxable_value():
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("18"),
        is_interstate=True,
        discount_percentage=Decimal("10"),
    )

    assert amounts.discount_amount == Decimal("100.00")
    assert amounts.taxable_amount == Decimal("900.00")
    # Tax is charged on the discounted value, never on the list value.
    assert amounts.igst_amount == Decimal("162.00")
    assert amounts.line_total == Decimal("1062.00")


def test_flat_discount_is_used_when_no_percentage_is_given():
    amounts = compute_line_amounts(
        quantity=Decimal("5"),
        unit_price=Decimal("200"),
        tax_rate=Decimal("5"),
        is_interstate=True,
        discount_amount=Decimal("50"),
    )

    assert amounts.taxable_amount == Decimal("950.00")
    assert amounts.igst_amount == Decimal("47.50")


def test_percentage_wins_when_both_are_somehow_supplied():
    """Defence in depth: the schema refuses both, so this can only be reached
    from internal callers rebuilding a payload from stored rows."""
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("0"),
        is_interstate=True,
        discount_percentage=Decimal("10"),
        discount_amount=Decimal("999"),
    )
    assert amounts.discount_amount == Decimal("100.00")


def test_discount_larger_than_the_line_is_refused():
    with pytest.raises(ValidationError):
        compute_line_amounts(
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            is_interstate=True,
            discount_amount=Decimal("150"),
        )


def test_zero_quantity_is_refused():
    with pytest.raises(ValidationError):
        compute_line_amounts(
            quantity=Decimal("0"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            is_interstate=True,
        )


# -----------------------------------------------------------------------------
# Fractional quantities and prices
# -----------------------------------------------------------------------------


def test_fractional_quantity_and_six_place_price_round_once_at_money_scale():
    """Hardware is priced per thousand often enough that six places matter.

    2.5 x 12.345678 = 30.864195, which must land on 30.86 exactly once - not on
    a value that drifted through an intermediate rounding.
    """
    amounts = compute_line_amounts(
        quantity=Decimal("2.5"),
        unit_price=Decimal("12.345678"),
        tax_rate=Decimal("18"),
        is_interstate=True,
    )

    assert amounts.gross_amount == Decimal("30.86")
    assert amounts.igst_amount == Decimal("5.55")
    assert amounts.line_total == Decimal("36.41")


# -----------------------------------------------------------------------------
# Header roll-up
# -----------------------------------------------------------------------------


def test_order_totals_reconcile_subtotal_minus_discount_plus_tax():
    lines = [
        compute_line_amounts(
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            is_interstate=False,
            discount_percentage=Decimal("10"),
        ),
        compute_line_amounts(
            quantity=Decimal("3"),
            unit_price=Decimal("49.50"),
            tax_rate=Decimal("12"),
            is_interstate=False,
        ),
    ]
    totals = sum_order_totals(lines)

    assert totals.subtotal == Decimal("1148.50")
    assert totals.discount_amount == Decimal("100.00")
    assert totals.tax_amount == sum(line.tax_amount for line in lines)
    # The arithmetic a customer checks by hand on the acknowledgement.
    assert totals.total_amount == totals.subtotal - totals.discount_amount + totals.tax_amount
    assert totals.total_amount == sum(line.line_total for line in lines)


def test_empty_order_totals_are_zero_not_an_error():
    totals = sum_order_totals([])
    assert totals.subtotal == totals.tax_amount == totals.total_amount == Decimal("0.00")
