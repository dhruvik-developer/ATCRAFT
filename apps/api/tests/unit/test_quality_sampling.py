"""How much of a delivered lot gets measured.

A sampling plan is a statement about risk: inspect 32 of 300 pieces and the
probability of passing a bad lot is a known number. Get the arithmetic wrong -
round a sample down, sample more than arrived, sample nothing at all - and the
number stops meaning anything while the paperwork still claims it does.

The AQL table is transcribed from ISO 2859-1 / ANSI Z1.4 general inspection
level II, single sampling. It is asserted here band by band because an
"approximately right" statistical plan is not a statistical plan.

No database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.quality.models import SamplingPlan
from src.modules.quality.service import AQL_MAX_SAMPLE, aql_sample_size, compute_sample_size


def sample(
    plan: SamplingPlan,
    quantity: str,
    *,
    percentage: str | None = None,
    fixed: int | None = None,
) -> Decimal:
    return compute_sample_size(
        plan=plan,
        received_quantity=Decimal(quantity),
        sample_percentage=None if percentage is None else Decimal(percentage),
        sample_size=fixed,
    )


# -----------------------------------------------------------------------------
# ALL
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", ["1", "250", "100000", "12.5"])
def test_the_all_plan_measures_the_whole_lot(quantity: str) -> None:
    assert sample(SamplingPlan.ALL, quantity) == Decimal(quantity)


# -----------------------------------------------------------------------------
# PERCENTAGE
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "percentage", "expected"),
    [
        ("1000", "10", "100"),
        # 25 * 10% = 2.5. Rounded UP: rounding a sample down is the one direction
        # that weakens the guarantee the plan exists to give.
        ("25", "10", "3"),
        ("1", "10", "1"),
        ("100", "100", "100"),
        ("999", "0.5", "5"),
    ],
)
def test_a_percentage_sample_rounds_up(quantity: str, percentage: str, expected: str) -> None:
    assert sample(SamplingPlan.PERCENTAGE, quantity, percentage=percentage) == Decimal(expected)


def test_a_percentage_sample_never_exceeds_the_lot() -> None:
    # 0.5 kg of powder at 10% rounds up to a whole unit, which is more than
    # arrived. The result is capped back so the sample stays inspectable.
    assert sample(SamplingPlan.PERCENTAGE, "0.5", percentage="10") == Decimal("0.5")


def test_a_percentage_plan_without_a_percentage_is_refused() -> None:
    with pytest.raises(BusinessRuleError):
        sample(SamplingPlan.PERCENTAGE, "100")


# -----------------------------------------------------------------------------
# FIXED_SAMPLE
# -----------------------------------------------------------------------------


def test_a_fixed_sample_is_constant_whatever_the_lot_size() -> None:
    assert sample(SamplingPlan.FIXED_SAMPLE, "100", fixed=10) == Decimal("10")
    assert sample(SamplingPlan.FIXED_SAMPLE, "100000", fixed=10) == Decimal("10")


def test_a_fixed_sample_larger_than_the_lot_collapses_to_the_lot() -> None:
    # Asking for 20 pieces out of a delivery of 5 is not an error worth refusing
    # a receipt over; it means inspect all five.
    assert sample(SamplingPlan.FIXED_SAMPLE, "5", fixed=20) == Decimal("5")


def test_a_fixed_plan_without_a_sample_size_is_refused() -> None:
    with pytest.raises(BusinessRuleError):
        sample(SamplingPlan.FIXED_SAMPLE, "100")


# -----------------------------------------------------------------------------
# AQL - ISO 2859-1 general inspection level II
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lot", "expected"),
    [
        (1, 1),
        (2, 2),
        (8, 2),
        (9, 3),
        (15, 3),
        (16, 5),
        (25, 5),
        (26, 8),
        (50, 8),
        (51, 13),
        (90, 13),
        (91, 20),
        (150, 20),
        (151, 32),
        (280, 32),
        (281, 50),
        (500, 50),
        (501, 80),
        (1200, 80),
        (1201, 125),
        (3200, 125),
        (3201, 200),
        (10_000, 200),
        (10_001, 315),
        (35_000, 315),
        (35_001, 500),
        (150_000, 500),
        (150_001, 800),
        (500_000, 800),
        (500_001, AQL_MAX_SAMPLE),
        (5_000_000, AQL_MAX_SAMPLE),
    ],
)
def test_every_aql_band_boundary_matches_the_standard(lot: int, expected: int) -> None:
    assert aql_sample_size(lot) == expected


def test_the_aql_sample_grows_far_more_slowly_than_a_percentage() -> None:
    # This is the entire reason a statistical plan exists: a hundredfold larger
    # lot does not cost a hundred times the inspection.
    small = sample(SamplingPlan.AQL, "100")
    large = sample(SamplingPlan.AQL, "10000")
    assert large > small
    assert large < small * 20


def test_an_aql_sample_never_exceeds_the_lot() -> None:
    # Band 2-8 calls for 2, and the lot is 2, so it collapses to full inspection
    # rather than asking for pieces that do not exist.
    assert sample(SamplingPlan.AQL, "2") == Decimal("2")
    assert sample(SamplingPlan.AQL, "1") == Decimal("1")


def test_a_fractional_lot_is_rounded_up_before_the_aql_table_is_read() -> None:
    # The standard is defined over whole pieces. 8.4 kg reads the 9-15 band, then
    # the answer is capped back to what actually arrived.
    assert sample(SamplingPlan.AQL, "8.4") == Decimal("3")


# -----------------------------------------------------------------------------
# Invariants that hold for every plan
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "percentage", "fixed"),
    [
        (SamplingPlan.ALL, None, None),
        (SamplingPlan.PERCENTAGE, "5", None),
        (SamplingPlan.FIXED_SAMPLE, None, 40),
        (SamplingPlan.AQL, None, None),
    ],
)
@pytest.mark.parametrize("quantity", ["1", "7", "63", "480", "9999"])
def test_a_sample_is_always_between_one_piece_and_the_whole_lot(
    plan: SamplingPlan, percentage: str | None, fixed: int | None, quantity: str
) -> None:
    # The database enforces `sample_size > 0 AND sample_size <= inspected_quantity`;
    # this is the same invariant, checked before a row is ever built.
    result = sample(plan, quantity, percentage=percentage, fixed=fixed)
    assert Decimal("1") <= result <= Decimal(quantity)


@pytest.mark.parametrize("plan", list(SamplingPlan))
def test_a_lot_with_no_quantity_cannot_be_sampled(plan: SamplingPlan) -> None:
    with pytest.raises(BusinessRuleError):
        compute_sample_size(
            plan=plan,
            received_quantity=Decimal("0"),
            sample_percentage=Decimal("10"),
            sample_size=5,
        )
