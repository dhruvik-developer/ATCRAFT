"""The vendor rating engine - pure arithmetic, no database.

The rating decides which vendor procurement approaches first, so the tests here
pin the numbers themselves against hand-computed values rather than asserting
that "a score comes back". Three properties matter and each has its own class:

* the weighted formula produces the exact expected score;
* too little evidence produces *no* rating, which is not the same as a low one;
* the input dataclass refuses impossible measurements outright, so a broken
  upstream query cannot quietly produce a plausible-looking score.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.vendors.models import MAX_VENDOR_RATING
from src.modules.vendors.service import (
    DELIVERY_WEIGHT,
    MIN_RECEIPTS_FOR_RATING,
    QUALITY_WEIGHT,
    VendorPerformanceWindow,
    compute_vendor_rating,
    insufficient_result,
)

pytestmark = pytest.mark.unit

VENDOR_ID = uuid.UUID("0192f3a4-0000-7000-8000-000000000001")


def _window(
    *,
    receipts: int = 10,
    on_time: int = 10,
    inspected: str = "0",
    rejected: str = "0",
    window_days: int = 180,
) -> VendorPerformanceWindow:
    return VendorPerformanceWindow(
        vendor_id=VENDOR_ID,
        window_days=window_days,
        receipt_count=receipts,
        on_time_receipt_count=on_time,
        inspected_quantity=Decimal(inspected),
        rejected_quantity=Decimal(rejected),
    )


class TestWeighting:
    def test_weights_sum_to_one(self) -> None:
        # The scale conversion divides by 100 and multiplies by 5, which is only
        # correct while the weights are a partition of the whole.
        assert Decimal("1.00") == DELIVERY_WEIGHT + QUALITY_WEIGHT

    def test_perfect_delivery_and_quality_scores_the_maximum(self) -> None:
        result = compute_vendor_rating(
            _window(receipts=10, on_time=10, inspected="1000", rejected="0")
        )
        assert result.rating == Decimal("5.00")
        assert result.on_time_percentage == Decimal("100.00")
        assert result.rejection_percentage == Decimal("0.00")
        assert result.has_quality_evidence is True

    def test_total_failure_scores_zero(self) -> None:
        result = compute_vendor_rating(
            _window(receipts=10, on_time=0, inspected="1000", rejected="1000")
        )
        assert result.rating == Decimal("0.00")

    def test_weighted_score_matches_hand_calculation(self) -> None:
        # 8 of 10 on time = 80.00%; 50 rejected of 1000 inspected = 5.00%.
        # 0.60 * 80 + 0.40 * 95 = 48 + 38 = 86.00 -> 86/100 * 5 = 4.30
        result = compute_vendor_rating(
            _window(receipts=10, on_time=8, inspected="1000", rejected="50")
        )
        assert result.on_time_percentage == Decimal("80.00")
        assert result.rejection_percentage == Decimal("5.00")
        assert result.rating == Decimal("4.30")

    def test_delivery_is_weighted_more_heavily_than_quality(self) -> None:
        """A late-but-perfect vendor must outscore an on-time-but-rejecting one.

        This is the whole point of the 60/40 split: a schedule slip costs
        planning time, a rejection stops a line.
        """
        late_but_good = compute_vendor_rating(
            _window(receipts=10, on_time=5, inspected="100", rejected="0")
        )
        prompt_but_bad = compute_vendor_rating(
            _window(receipts=10, on_time=10, inspected="100", rejected="50")
        )
        # 0.6*50 + 0.4*100 = 70 -> 3.50 ; 0.6*100 + 0.4*50 = 80 -> 4.00
        assert late_but_good.rating == Decimal("3.50")
        assert prompt_but_bad.rating == Decimal("4.00")
        assert prompt_but_bad.rating > late_but_good.rating

    def test_rating_never_exceeds_the_scale(self) -> None:
        result = compute_vendor_rating(
            _window(receipts=50, on_time=50, inspected="1", rejected="0")
        )
        assert result.rating is not None
        assert Decimal("0") <= result.rating <= Decimal(MAX_VENDOR_RATING)


class TestNoQualityEvidence:
    def test_missing_inspections_fall_back_to_delivery_only(self) -> None:
        """No inspections must not be read as perfect quality.

        With the quality term dropped the score is the delivery percentage on
        the 0-5 scale: 90% -> 4.50, not the 4.80 that crediting an unearned
        100% quality would produce.
        """
        result = compute_vendor_rating(_window(receipts=10, on_time=9, inspected="0"))
        assert result.has_quality_evidence is False
        assert result.rejection_percentage is None
        assert result.rating == Decimal("4.50")
        assert "delivery only" in result.reason

    def test_delivery_only_and_full_evidence_differ_when_quality_is_imperfect(self) -> None:
        delivery_only = compute_vendor_rating(_window(receipts=10, on_time=9, inspected="0"))
        with_quality = compute_vendor_rating(
            _window(receipts=10, on_time=9, inspected="100", rejected="20")
        )
        assert delivery_only.rating != with_quality.rating


class TestInsufficientEvidence:
    @pytest.mark.parametrize("receipts", [0, 1, MIN_RECEIPTS_FOR_RATING - 1])
    def test_too_few_receipts_yields_no_rating_at_all(self, receipts: int) -> None:
        result = compute_vendor_rating(_window(receipts=receipts, on_time=0))
        # None, not zero: an unmeasured vendor is unknown, not bad, and a caller
        # that stored 0.00 here would exclude a new supplier from sourcing.
        assert result.rating is None
        assert result.is_sufficient is False
        assert result.on_time_percentage is None
        assert str(MIN_RECEIPTS_FOR_RATING) in result.reason

    def test_exactly_the_minimum_is_enough(self) -> None:
        result = compute_vendor_rating(
            _window(receipts=MIN_RECEIPTS_FOR_RATING, on_time=MIN_RECEIPTS_FOR_RATING)
        )
        assert result.is_sufficient is True
        assert result.rating == Decimal("5.00")

    def test_insufficient_result_helper_is_shaped_like_a_real_result(self) -> None:
        result = insufficient_result(VENDOR_ID, 90, "Goods receipts are not available yet.")
        assert result.vendor_id == VENDOR_ID
        assert result.window_days == 90
        assert result.rating is None
        assert result.is_sufficient is False
        assert result.has_quality_evidence is False


class TestInputValidation:
    """A window that cannot physically have happened must be rejected.

    These are the failures a wrong join in the adapter produces, and they must
    surface as an error rather than as a confident, wrong score.
    """

    def test_more_on_time_than_total_receipts_is_refused(self) -> None:
        with pytest.raises(ValueError, match="on-time receipts cannot exceed"):
            _window(receipts=3, on_time=5)

    def test_more_rejected_than_inspected_is_refused(self) -> None:
        with pytest.raises(ValueError, match="rejected quantity cannot exceed"):
            _window(inspected="10", rejected="11")

    def test_negative_quantities_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            _window(inspected="-1")

    def test_non_positive_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="window_days must be positive"):
            _window(window_days=0)

    def test_window_is_immutable(self) -> None:
        """Frozen on purpose: a measurement must not be edited after the fact."""
        window = _window()
        with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
            window.receipt_count = 99  # type: ignore[misc]


class TestDecimalDiscipline:
    def test_percentages_and_rating_are_decimal_not_float(self) -> None:
        result = compute_vendor_rating(
            _window(receipts=7, on_time=5, inspected="300", rejected="7")
        )
        assert isinstance(result.rating, Decimal)
        assert isinstance(result.on_time_percentage, Decimal)
        assert isinstance(result.rejection_percentage, Decimal)

    def test_repeating_fraction_is_rounded_half_up_not_truncated(self) -> None:
        # 2 of 3 on time is 66.666...%, which must round to 66.67, not 66.66.
        result = compute_vendor_rating(_window(receipts=6, on_time=4))
        assert result.on_time_percentage == Decimal("66.67")

    def test_same_window_always_produces_the_same_rating(self) -> None:
        first = compute_vendor_rating(_window(receipts=9, on_time=7, inspected="17", rejected="3"))
        second = compute_vendor_rating(_window(receipts=9, on_time=7, inspected="17", rejected="3"))
        assert first == second
