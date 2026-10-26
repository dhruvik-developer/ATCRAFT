"""Paisa allocation and legacy rounded-credit boundaries without database state."""

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.finance.credit_note_rounding import (
    CreditedLineTotals,
    credit_round_off,
    credit_tax_parts,
    quantity_credit,
)


@pytest.mark.parametrize("quantity", [3, 7, 11])
def test_every_paisa_is_allocated_when_all_units_are_credited(quantity):
    prior = CreditedLineTotals()
    amounts = []
    for _ in range(quantity):
        value = quantity_credit(Decimal(quantity), Decimal("1.00"), prior, Decimal(1))
        amounts.append(value)
        prior = CreditedLineTotals(quantity=prior.quantity + 1, taxable=prior.taxable + value)
    assert sum(amounts) == Decimal("1.00")
    assert all(value >= 0 for value in amounts)


def test_amount_credit_is_shared_over_the_remaining_units():
    prior = CreditedLineTotals(quantity=Decimal(1), taxable=Decimal("0.43"))
    assert quantity_credit(Decimal(3), Decimal(1), prior, Decimal(1)) == Decimal("0.29")
    assert quantity_credit(Decimal(3), Decimal(1), prior, Decimal(2)) == Decimal("0.57")


@pytest.mark.parametrize(
    "prior,requested",
    [
        (CreditedLineTotals(quantity=Decimal(3)), Decimal(1)),
        (CreditedLineTotals(quantity=Decimal(2)), Decimal(2)),
        (CreditedLineTotals(taxable=Decimal("1.01")), Decimal(1)),
    ],
)
def test_credits_cannot_exceed_remaining_quantity_or_issued_value(prior, requested):
    with pytest.raises(BusinessRuleError):
        quantity_credit(Decimal(3), Decimal(1), prior, requested)


def test_gst_components_take_the_rounding_remainder_instead_of_rounding_every_note():
    original = (Decimal("0.01"), Decimal("0.01"), Decimal(0))
    first = credit_tax_parts(Decimal("0.05"), Decimal("0.10"), original, CreditedLineTotals())
    assert first == (Decimal("0.01"), Decimal("0.01"), Decimal(0), Decimal("0.02"))
    second = credit_tax_parts(
        Decimal("0.05"),
        Decimal("0.10"),
        original,
        CreditedLineTotals(taxable=Decimal("0.05"), cgst=first[0], sgst=first[1]),
    )
    assert second == (Decimal(0), Decimal(0), Decimal(0), Decimal(0))


def test_a_legacy_early_rounded_tax_credit_is_never_taken_back():
    assert credit_tax_parts(
        Decimal("0.01"),
        Decimal("1.00"),
        (Decimal("0.01"), Decimal(0), Decimal(0)),
        CreditedLineTotals(taxable=Decimal("0.01"), cgst=Decimal("0.01")),
    ) == (Decimal(0), Decimal(0), Decimal(0), Decimal(0))


@pytest.mark.parametrize(
    "prior,amount",
    [
        (CreditedLineTotals(taxable=Decimal("1.00")), Decimal("0.01")),
        (CreditedLineTotals(cgst=Decimal("0.10")), Decimal("0.01")),
    ],
)
def test_inconsistent_legacy_tax_history_fails_closed(prior, amount):
    with pytest.raises(BusinessRuleError):
        credit_tax_parts(amount, Decimal(1), (Decimal("0.09"), Decimal("0.09"), Decimal(0)), prior)


def test_zero_taxable_snapshot_has_no_tax_allocation():
    assert credit_tax_parts(
        Decimal(0), Decimal(0), (Decimal(0), Decimal(0), Decimal(0)), CreditedLineTotals()
    ) == (
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
    )
    assert credit_round_off(Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0)) == 0


@pytest.mark.parametrize("rounding", [Decimal("0.20"), Decimal("-0.18")])
def test_invoice_round_off_is_fully_reversed_across_partial_credit_notes(rounding):
    original_raw = Decimal("1.18")
    issued_total, issued_rounding = Decimal(0), Decimal(0)
    for raw in (Decimal("0.39"), Decimal("0.40"), Decimal("0.39")):
        allocated = credit_round_off(original_raw, rounding, issued_total, issued_rounding, raw)
        issued_total += raw + allocated
        issued_rounding += allocated
    assert issued_rounding == rounding
    assert issued_total == original_raw + rounding
