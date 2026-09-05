"""A unit-conversion ratio is rounded to its own scale, not a price's."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from src.core.types import FACTOR_QUANTUM, to_factor, to_unit_price

pytestmark = pytest.mark.unit


def test_factor_keeps_eight_places() -> None:
    with localcontext() as ctx:
        ctx.prec = 28
        twelfth = Decimal(1) / Decimal(12)

    assert to_factor(twelfth) == Decimal("0.08333333")
    assert Decimal("0.00000001") == FACTOR_QUANTUM


def test_the_unit_price_scale_would_lose_a_whole_unit_at_scale() -> None:
    """The defect: 1 BOX = 12 NOS reported as 0.083333 makes input * factor
    disagree with the converted quantity by a unit at ~10^5 quantities."""
    with localcontext() as ctx:
        ctx.prec = 28
        twelfth = Decimal(1) / Decimal(12)
    quantity = Decimal("120000")

    exact = quantity * twelfth
    unit_price_error = abs(quantity * to_unit_price(twelfth) - exact)
    factor_error = abs(quantity * to_factor(twelfth) - exact)
    assert unit_price_error > Decimal("0.03")
    assert factor_error <= Decimal("0.0004")
    assert unit_price_error >= 100 * factor_error


def test_half_up_like_every_other_quantum() -> None:
    assert to_factor("0.000000005") == Decimal("0.00000001")
    assert to_factor(12) == Decimal("12.00000000")
