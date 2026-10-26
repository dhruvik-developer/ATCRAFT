"""Allocate remaining invoice value and rounded tax without losing the last paisa."""

from dataclasses import dataclass
from decimal import Decimal

from src.core.errors import BusinessRuleError
from src.core.types import ZERO, to_money


@dataclass(frozen=True, slots=True)
class CreditedLineTotals:
    quantity: Decimal = ZERO
    taxable: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO


def quantity_credit(
    quantity: Decimal,
    taxable: Decimal,
    prior: CreditedLineTotals,
    requested: Decimal,
) -> Decimal:
    remaining_quantity = quantity - prior.quantity
    remaining_value = taxable - prior.taxable
    if remaining_quantity <= ZERO or requested > remaining_quantity:
        raise BusinessRuleError("Credit quantity exceeds the invoice line balance.")
    if remaining_value < ZERO:
        raise BusinessRuleError("Existing credit history exceeds the invoice line value.")
    # Amount-only price credits reduce what is still owed for the remaining
    # units. The final quantity credit takes the exact remainder, including any
    # paisa carried by earlier rounded partial credits or cancelled/reissued notes.
    return to_money(remaining_value * requested / remaining_quantity)


def credit_tax_parts(
    taxable: Decimal,
    original_taxable: Decimal,
    original_parts: tuple[Decimal, Decimal, Decimal],
    prior: CreditedLineTotals,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if original_taxable <= ZERO:
        return ZERO, ZERO, ZERO, ZERO
    cumulative = prior.taxable + taxable
    if cumulative > original_taxable:
        raise BusinessRuleError("Credit amount exceeds the invoice line balance.")
    parts = []
    for original, credited in zip(
        original_parts, (prior.cgst, prior.sgst, prior.igst), strict=True
    ):
        if credited > original:
            raise BusinessRuleError("Existing credit history exceeds the invoice tax component.")
        # Older notes may have rounded a component up sooner. Never take tax
        # back from an issued note; carry that difference until the next target.
        parts.append(max(ZERO, to_money(original * cumulative / original_taxable) - credited))
    cgst, sgst, igst = parts
    return cgst, sgst, igst, to_money(cgst + sgst + igst)


def credit_round_off(
    original_raw_total: Decimal,
    original_round_off: Decimal,
    issued_total: Decimal,
    issued_round_off: Decimal,
    raw_credit: Decimal,
) -> Decimal:
    if original_raw_total <= ZERO:
        return ZERO
    cumulative_raw = issued_total - issued_round_off + raw_credit
    return to_money(original_round_off * cumulative_raw / original_raw_total) - issued_round_off
