"""Purchase line arithmetic and the quotation recommendation.

Two things are asserted here. The first is what the company will owe a vendor:
the line and header totals are reconciled against the vendor's invoice by
finance, so a total that cannot be reproduced from the columns beside it is a
three-way-match failure. The second is which vendor the system recommends and
why, because a recommendation nobody can explain is one buyers learn to ignore.

No database: both are pure by construction, which is the point of keeping them
out of the persistence path.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.procurement.service_rules import (
    DEFAULT_SCORE_WEIGHTS,
    UNRATED_VENDOR_SCORE,
    OfferInput,
    ScoreWeights,
    compute_line_amounts,
    effective_unit_price,
    rank_offers,
    recommendation_reason,
    score_offer,
    sum_document_totals,
)
from src.modules.vendors.models import MAX_VENDOR_RATING

pytestmark = pytest.mark.unit


# =============================================================================
# Line arithmetic
# =============================================================================


def test_a_plain_line_is_quantity_times_price_plus_tax():
    amounts = compute_line_amounts(
        quantity=Decimal("10"), unit_price=Decimal("100"), tax_rate=Decimal("18")
    )

    assert amounts.gross_amount == Decimal("1000.00")
    assert amounts.discount_amount == Decimal("0.00")
    assert amounts.taxable_amount == Decimal("1000.00")
    assert amounts.tax_amount == Decimal("180.00")
    assert amounts.line_total == Decimal("1180.00")


def test_tax_is_charged_on_the_discounted_value_not_the_gross():
    """Charging GST on the pre-discount value overstates the tax on every order."""
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("18"),
        discount_percentage=Decimal("10"),
    )

    assert amounts.discount_amount == Decimal("100.00")
    assert amounts.taxable_amount == Decimal("900.00")
    assert amounts.tax_amount == Decimal("162.00")
    assert amounts.line_total == Decimal("1062.00")


def test_a_flat_discount_is_taken_as_given():
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("18"),
        discount_amount=Decimal("250"),
    )
    assert amounts.taxable_amount == Decimal("750.00")
    assert amounts.tax_amount == Decimal("135.00")


def test_a_percentage_discount_wins_when_both_forms_arrive():
    """The schema rejects both together; this pins the behaviour if one slips past."""
    amounts = compute_line_amounts(
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        tax_rate=Decimal("0"),
        discount_percentage=Decimal("10"),
        discount_amount=Decimal("999"),
    )
    assert amounts.discount_amount == Decimal("100.00")


def test_a_discount_larger_than_the_line_is_refused():
    """A negative taxable value would produce negative tax on a statutory document."""
    with pytest.raises(ValidationError):
        compute_line_amounts(
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            discount_amount=Decimal("150"),
        )


def test_amounts_are_rounded_at_money_scale_not_left_at_full_precision():
    """Every one of these numbers is written to a NUMERIC column and printed."""
    amounts = compute_line_amounts(
        quantity=Decimal("3"), unit_price=Decimal("33.333333"), tax_rate=Decimal("18")
    )
    assert amounts.gross_amount == Decimal("100.00")
    assert amounts.tax_amount == Decimal("18.00")
    assert amounts.line_total == Decimal("118.00")


def test_a_fractional_tax_rate_rounds_half_up():
    """12.5% of 1234.55 is 154.31875, and Indian invoicing rounds half up."""
    amounts = compute_line_amounts(
        quantity=Decimal("1"), unit_price=Decimal("1234.55"), tax_rate=Decimal("12.5")
    )
    assert amounts.tax_amount == Decimal("154.32")
    assert amounts.line_total == Decimal("1388.87")


def test_a_zero_rated_line_carries_no_tax():
    amounts = compute_line_amounts(
        quantity=Decimal("5"), unit_price=Decimal("20"), tax_rate=Decimal("0")
    )
    assert amounts.tax_amount == Decimal("0.00")
    assert amounts.line_total == Decimal("100.00")


def test_a_zero_quantity_line_is_refused():
    with pytest.raises(ValidationError):
        compute_line_amounts(
            quantity=Decimal("0"), unit_price=Decimal("10"), tax_rate=Decimal("18")
        )


def test_a_negative_price_is_refused():
    with pytest.raises(ValidationError):
        compute_line_amounts(
            quantity=Decimal("1"), unit_price=Decimal("-1"), tax_rate=Decimal("18")
        )


# =============================================================================
# Document totals
# =============================================================================


def test_the_header_reads_subtotal_minus_discount_plus_tax_plus_freight():
    """The arithmetic a buyer checks by hand against the vendor's proforma."""
    lines = [
        compute_line_amounts(
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            discount_percentage=Decimal("10"),
        ),
        compute_line_amounts(
            quantity=Decimal("5"), unit_price=Decimal("40"), tax_rate=Decimal("12")
        ),
    ]
    totals = sum_document_totals(lines, freight_amount=Decimal("250"))

    assert totals.subtotal == Decimal("1200.00")
    assert totals.discount == Decimal("100.00")
    assert totals.tax_amount == Decimal("186.00")
    assert totals.freight_amount == Decimal("250.00")
    assert totals.total_amount == Decimal("1536.00")
    assert (
        totals.total_amount
        == totals.subtotal - totals.discount + totals.tax_amount + totals.freight_amount
    )


def test_freight_defaults_to_zero_and_does_not_vanish_from_the_total():
    lines = [
        compute_line_amounts(
            quantity=Decimal("1"), unit_price=Decimal("100"), tax_rate=Decimal("0")
        )
    ]
    assert sum_document_totals(lines).total_amount == Decimal("100.00")
    assert sum_document_totals(lines, freight_amount=Decimal("15.55")).total_amount == Decimal(
        "115.55"
    )


def test_an_empty_document_totals_to_zero_rather_than_failing():
    totals = sum_document_totals([])
    assert totals.subtotal == Decimal("0.00")
    assert totals.total_amount == Decimal("0.00")


# =============================================================================
# Comparable prices
# =============================================================================


def test_the_effective_price_is_what_will_actually_be_paid_per_unit():
    """List price alone cannot compare two vendors with different discounts."""
    assert effective_unit_price(
        unit_price=Decimal("100"), discount_percentage=Decimal("10")
    ) == Decimal("90.000000")
    assert effective_unit_price(unit_price=Decimal("100")) == Decimal("100")


# =============================================================================
# Quotation ranking and the recommendation
# =============================================================================

CHEAP_SLOW = uuid.uuid4()
DEAR_FAST = uuid.uuid4()
MIDDLE = uuid.uuid4()


def offer(
    vendor_id: uuid.UUID,
    price: str,
    lead_time: int,
    rating: str | None = None,
    *,
    expired: bool = False,
) -> OfferInput:
    return OfferInput(
        vendor_id=vendor_id,
        quotation_id=uuid.uuid4(),
        quotation_line_id=uuid.uuid4(),
        effective_unit_price=Decimal(price),
        lead_time_days=lead_time,
        vendor_rating=Decimal(rating) if rating is not None else None,
        is_expired=expired,
    )


def test_the_cheapest_offer_wins_when_everything_else_is_equal():
    ranked = rank_offers([offer(DEAR_FAST, "120", 7, "4.0"), offer(CHEAP_SLOW, "100", 7, "4.0")])
    assert ranked[0].offer.vendor_id == CHEAP_SLOW
    assert ranked[0].rank == 1
    assert ranked[0].is_lowest_price is True


def test_a_far_shorter_lead_time_can_beat_a_slightly_higher_price():
    """A part that arrives four weeks late stops a line; the weights say so."""
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 60, "4.0"), offer(DEAR_FAST, "103", 3, "4.0")])
    assert ranked[0].offer.vendor_id == DEAR_FAST
    assert ranked[0].is_fastest is True
    assert ranked[0].is_lowest_price is False


def test_a_small_lead_time_advantage_does_not_beat_a_large_price_gap():
    """Price carries 60%; delivery must not quietly become the deciding factor."""
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 14, "4.0"), offer(DEAR_FAST, "180", 10, "4.0")])
    assert ranked[0].offer.vendor_id == CHEAP_SLOW


def test_a_better_rated_vendor_wins_a_tie_on_price_and_delivery():
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 7, "2.0"), offer(MIDDLE, "100", 7, "5.0")])
    assert ranked[0].offer.vendor_id == MIDDLE


def test_an_unrated_vendor_is_scored_neutrally_not_badly():
    """A vendor with no history is unknown, not poor.

    Scoring them zero would make it impossible for a new vendor ever to win a
    first order, which is not a procurement policy anyone would choose.
    """
    unrated = score_offer(
        offer(MIDDLE, "100", 7, None), best_price=Decimal("100"), best_lead_time=7
    )
    poor = score_offer(offer(MIDDLE, "100", 7, "0.5"), best_price=Decimal("100"), best_lead_time=7)
    excellent = score_offer(
        offer(MIDDLE, "100", 7, "5.0"), best_price=Decimal("100"), best_lead_time=7
    )

    assert poor < unrated < excellent
    assert Decimal("0.5") == UNRATED_VENDOR_SCORE


def test_the_best_offer_on_every_factor_scores_one():
    best = score_offer(
        offer(MIDDLE, "100", 0, str(MAX_VENDOR_RATING)),
        best_price=Decimal("100"),
        best_lead_time=0,
    )
    assert best == Decimal("1.0000")


def test_a_zero_day_lead_time_does_not_divide_by_zero():
    """Ex-stock quotes are common and must not break the ranking."""
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 0, "4.0"), offer(DEAR_FAST, "100", 5, "4.0")])
    assert ranked[0].offer.vendor_id == CHEAP_SLOW
    assert all(entry.score > 0 for entry in ranked)


def test_a_free_offer_does_not_break_the_price_ratio():
    """A no-charge sample is legitimate and must not produce a division by zero."""
    ranked = rank_offers([offer(CHEAP_SLOW, "0", 7, "4.0"), offer(DEAR_FAST, "100", 7, "4.0")])
    assert ranked[0].offer.vendor_id == CHEAP_SLOW


def test_an_expired_quotation_ranks_last_but_is_still_shown():
    """The buyer needs to see that the cheapest price on the table has lapsed."""
    ranked = rank_offers(
        [offer(CHEAP_SLOW, "50", 7, "5.0", expired=True), offer(DEAR_FAST, "100", 7, "3.0")]
    )

    assert ranked[0].offer.vendor_id == DEAR_FAST
    assert ranked[-1].offer.vendor_id == CHEAP_SLOW
    assert ranked[-1].score == Decimal("0")
    assert len(ranked) == 2


def test_an_expired_offer_is_not_treated_as_the_lowest_price():
    """Otherwise a lapsed quote would set the benchmark every live one is scored against."""
    ranked = rank_offers(
        [offer(CHEAP_SLOW, "50", 7, "5.0", expired=True), offer(DEAR_FAST, "100", 7, "3.0")]
    )
    live = next(entry for entry in ranked if not entry.offer.is_expired)
    assert live.is_lowest_price is True


def test_ranking_is_deterministic_when_two_offers_tie_exactly():
    """Two identical requests must not rank differently."""
    offers = [offer(CHEAP_SLOW, "100", 7, "4.0"), offer(DEAR_FAST, "100", 7, "4.0")]
    first = [entry.offer.quotation_line_id for entry in rank_offers(offers)]
    second = [entry.offer.quotation_line_id for entry in rank_offers(list(reversed(offers)))]
    assert first == second


def test_weights_can_be_retuned_without_touching_the_ranker():
    """Price-only weighting must pick the cheapest, whatever the delivery promise."""
    price_only = ScoreWeights(price=Decimal("1"), lead_time=Decimal("0"), rating=Decimal("0"))
    ranked = rank_offers(
        [offer(CHEAP_SLOW, "100", 90, "1.0"), offer(DEAR_FAST, "101", 1, "5.0")],
        weights=price_only,
    )
    assert ranked[0].offer.vendor_id == CHEAP_SLOW


def test_weights_that_sum_to_zero_are_refused_rather_than_dividing_by_zero():
    with pytest.raises(ValidationError):
        score_offer(
            offer(MIDDLE, "100", 7, "4.0"),
            best_price=Decimal("100"),
            best_lead_time=7,
            weights=ScoreWeights(price=Decimal("0"), lead_time=Decimal("0"), rating=Decimal("0")),
        )


def test_the_default_weights_favour_price_without_ignoring_the_rest():
    assert DEFAULT_SCORE_WEIGHTS.price == Decimal("0.60")
    assert DEFAULT_SCORE_WEIGHTS.total() == Decimal("1.00")


# --- the stated reason --------------------------------------------------------


def test_the_reason_names_the_winner_and_the_facts_that_decided_it():
    names = {CHEAP_SLOW: "Acme Fasteners", DEAR_FAST: "Bolt Works"}
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 7, "4.5"), offer(DEAR_FAST, "120", 9, "3.0")])
    reason = recommendation_reason(ranked, vendor_names=names)

    assert "Acme Fasteners" in reason
    assert "lowest effective unit price" in reason
    assert "7-day lead time" in reason
    assert "4.5" in reason


def test_the_reason_admits_when_the_winner_is_not_the_cheapest():
    """A buyer overriding the recommendation should be arguing with real criteria."""
    names = {CHEAP_SLOW: "Acme Fasteners", DEAR_FAST: "Bolt Works"}
    ranked = rank_offers([offer(CHEAP_SLOW, "100", 90, "1.0"), offer(DEAR_FAST, "104", 2, "5.0")])
    reason = recommendation_reason(ranked, vendor_names=names)

    assert reason.startswith("Bolt Works")
    assert "above Acme Fasteners" in reason


def test_the_reason_says_so_when_a_vendor_has_no_delivery_history():
    ranked = rank_offers([offer(MIDDLE, "100", 7, None)])
    reason = recommendation_reason(ranked, vendor_names={MIDDLE: "New Supplier"})
    assert "only live quotation" in reason
    assert "no delivery history yet" in reason


def test_the_reason_says_so_when_everything_has_lapsed():
    ranked = rank_offers([offer(MIDDLE, "100", 7, "4.0", expired=True)])
    reason = recommendation_reason(ranked, vendor_names={MIDDLE: "Lapsed Supplier"})
    assert "lapsed" in reason


def test_the_reason_says_so_when_nothing_was_quoted():
    assert "No quotations" in recommendation_reason([], vendor_names={})
