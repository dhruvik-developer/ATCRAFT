"""Where every piece of an inspected lot ends up.

A disposition has to account for the whole lot. Quantity that belongs to no
bucket is stock left sitting on QC hold underneath a decided document - the
single most common way material becomes permanently unusable without anybody
deciding that it should be. These tests pin the arithmetic that prevents it,
plus two rules that follow from it:

* the ``result`` label derived from a split lot, and
* the delta posted when a REWORK or HOLD decision is revisited, which must never
  release the same pieces twice.

No database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.quality.models import InspectionResult, InspectionStatus
from src.modules.quality.repository import QualityTotals
from src.modules.quality.service import (
    SEVERITY_ORDER,
    Disposition,
    derive_result,
    disposition_delta,
    rejection_rate,
    status_for,
    validate_disposition,
)


def d(value: str) -> Decimal:
    return Decimal(value)


# -----------------------------------------------------------------------------
# The balance rule
# -----------------------------------------------------------------------------


def test_a_disposition_accounting_for_the_whole_lot_is_accepted() -> None:
    validate_disposition(
        d("100"),
        Disposition(
            accepted=d("70"), rejected=d("10"), rework=d("12"), hold=d("5"), scrap=d("3")
        ),
    )


@pytest.mark.parametrize(
    "disposition",
    [
        # Short: 5 pieces would be left on QC hold with nothing explaining them.
        Disposition(accepted=d("95")),
        # Over: 5 pieces dispositioned that were never inspected.
        Disposition(accepted=d("95"), rejected=d("10")),
        Disposition(),
    ],
)
def test_a_disposition_that_does_not_balance_is_refused(disposition: Disposition) -> None:
    with pytest.raises(BusinessRuleError) as caught:
        validate_disposition(d("100"), disposition)

    details = caught.value.details or {}
    assert details["inspected_quantity"] == "100.0000"
    # The error names both sides, so the operator can see which number to fix
    # rather than being told a constraint failed.
    assert "dispositioned_quantity" in details


def test_a_negative_disposition_is_refused() -> None:
    with pytest.raises(BusinessRuleError):
        validate_disposition(d("100"), Disposition(accepted=d("110"), rejected=d("-10")))


def test_the_balance_rule_compares_by_value_not_by_scale() -> None:
    # Decimal("100") and Decimal("100.0000") are the same quantity; a scale
    # difference is not a business difference.
    validate_disposition(d("100.0000"), Disposition(accepted=d("100")))


def test_fractional_quantities_balance_exactly() -> None:
    # Four decimal places is the quantity scale, and a kilogram split three ways
    # has to still add up.
    validate_disposition(
        d("10.5000"),
        Disposition(accepted=d("7.2500"), rejected=d("2.1250"), scrap=d("1.1250")),
    )


# -----------------------------------------------------------------------------
# The result label
# -----------------------------------------------------------------------------


def test_the_dominant_disposition_names_the_result() -> None:
    # 95 good out of 100 is an accepted delivery with a small rejection, not a
    # rejected one. The rejection *rate* is computed from the quantities, never
    # from this label.
    assert (
        derive_result(Disposition(accepted=d("95"), rejected=d("5")))
        is InspectionResult.ACCEPTED
    )
    assert (
        derive_result(Disposition(accepted=d("5"), rejected=d("95")))
        is InspectionResult.REJECTED
    )


def test_an_even_split_goes_to_the_more_severe_disposition() -> None:
    # The optimistic reading of an even split is the one that puts doubtful
    # material into a customer's hands.
    assert (
        derive_result(Disposition(accepted=d("50"), rejected=d("50")))
        is InspectionResult.REJECTED
    )
    assert (
        derive_result(Disposition(accepted=d("50"), rework=d("50"))) is InspectionResult.REWORK
    )
    assert (
        derive_result(Disposition(rejected=d("50"), scrap=d("50"))) is InspectionResult.SCRAP
    )


@pytest.mark.parametrize("verdict", list(SEVERITY_ORDER))
def test_a_lot_going_wholly_to_one_bucket_reads_as_that_bucket(
    verdict: InspectionResult,
) -> None:
    field = {
        InspectionResult.ACCEPTED: "accepted",
        InspectionResult.REWORK: "rework",
        InspectionResult.HOLD: "hold",
        InspectionResult.REJECTED: "rejected",
        InspectionResult.SCRAP: "scrap",
    }[verdict]
    assert derive_result(Disposition(**{field: d("100")})) is verdict


def test_a_disposition_of_nothing_has_no_result() -> None:
    with pytest.raises(BusinessRuleError):
        derive_result(Disposition())


# -----------------------------------------------------------------------------
# The verdict on the goods is not the state of the document
# -----------------------------------------------------------------------------


def test_a_fully_dispositioned_lot_takes_its_verdict_as_its_status() -> None:
    for verdict, field in (
        (InspectionResult.ACCEPTED, "accepted"),
        (InspectionResult.REJECTED, "rejected"),
        (InspectionResult.SCRAP, "scrap"),
    ):
        disposition = Disposition(**{field: d("100")})
        assert status_for(disposition, verdict) is InspectionStatus(verdict.value)


def test_quantity_left_on_hold_keeps_the_document_open_whatever_the_verdict() -> None:
    # 60 released and 40 sent for rework: the verdict on the goods is ACCEPTED,
    # because most of the lot was good. But the document is not finished - those
    # 40 pieces are still in the QC hold pool owing a second decision, and
    # stamping the document ACCEPTED would close their only way out.
    split = Disposition(accepted=d("60"), rework=d("40"))
    assert derive_result(split) is InspectionResult.ACCEPTED
    assert status_for(split, InspectionResult.ACCEPTED) is InspectionStatus.REWORK


def test_a_held_remainder_keeps_the_document_open_too() -> None:
    split = Disposition(accepted=d("80"), hold=d("20"))
    assert status_for(split, InspectionResult.ACCEPTED) is InspectionStatus.HOLD


def test_rework_outranks_hold_when_both_carry_quantity() -> None:
    # Rework is work somebody has been asked to do; hold is a question somebody
    # has been asked to answer. The work list is the more actionable of the two.
    split = Disposition(accepted=d("50"), rework=d("30"), hold=d("20"))
    assert status_for(split, InspectionResult.ACCEPTED) is InspectionStatus.REWORK


def test_a_rejected_lot_with_a_held_remainder_is_still_open() -> None:
    split = Disposition(rejected=d("70"), hold=d("30"))
    assert derive_result(split) is InspectionResult.REJECTED
    assert status_for(split, InspectionResult.REJECTED) is InspectionStatus.HOLD


def test_every_status_a_disposition_can_produce_is_a_real_lifecycle_state() -> None:
    # The status column is constrained to the InspectionStatus vocabulary, and a
    # verdict name that had no matching status would be unwritable.
    for verdict in SEVERITY_ORDER:
        produced = status_for(Disposition(accepted=d("1")), verdict)
        assert produced in set(InspectionStatus)


# -----------------------------------------------------------------------------
# Re-deciding: only the delta may be posted
# -----------------------------------------------------------------------------


def test_the_first_decision_posts_its_whole_disposition() -> None:
    delta = disposition_delta(
        Disposition(), Disposition(accepted=d("80"), rejected=d("20"))
    )
    assert delta.accepted == d("80")
    assert delta.rejected == d("20")


def test_reworked_material_that_now_passes_posts_only_the_new_release() -> None:
    # 100 inspected: 60 released, 40 sent for rework and still on QC hold. The
    # rework succeeds and all 40 pass. Only those 40 may leave the hold pool -
    # posting 100 would release the first 60 a second time.
    applied = Disposition(accepted=d("60"), rework=d("40"))
    delta = disposition_delta(applied, Disposition(accepted=d("100")))

    assert delta.accepted == d("40")
    assert delta.rejected == d("0")
    assert delta.scrap == d("0")
    assert delta.rework == d("0")


def test_a_partial_rework_leaves_the_remainder_on_hold() -> None:
    applied = Disposition(accepted=d("60"), rework=d("40"))
    delta = disposition_delta(
        applied, Disposition(accepted=d("85"), rework=d("10"), rejected=d("5"))
    )

    assert delta.accepted == d("25")
    assert delta.rejected == d("5")
    # Rework never moved, so it is restated rather than differenced.
    assert delta.rework == d("10")


def test_quantity_moved_out_of_hold_can_never_be_taken_back() -> None:
    # The released pieces may already have been issued, sold or consumed; there
    # is nothing to reverse them against.
    applied = Disposition(accepted=d("60"), rework=d("40"))

    with pytest.raises(BusinessRuleError) as caught:
        disposition_delta(applied, Disposition(accepted=d("50"), rework=d("50")))

    assert (caught.value.details or {}).get("bucket") == "accepted"


@pytest.mark.parametrize(
    ("applied", "requested", "bucket"),
    [
        (Disposition(rejected=d("30"), hold=d("70")), Disposition(rejected=d("10")), "rejected"),
        (Disposition(scrap=d("30"), hold=d("70")), Disposition(scrap=d("0")), "scrapped"),
    ],
)
def test_neither_a_rejection_nor_a_scrapping_can_be_reduced(
    applied: Disposition, requested: Disposition, bucket: str
) -> None:
    with pytest.raises(BusinessRuleError) as caught:
        disposition_delta(applied, requested)
    assert (caught.value.details or {}).get("bucket") == bucket


def test_held_quantity_may_be_moved_between_rework_and_hold_freely() -> None:
    # Neither ever left the QC pool, so restating them costs no movement at all.
    delta = disposition_delta(
        Disposition(hold=d("100")), Disposition(rework=d("100"))
    )
    assert delta.accepted == d("0")
    assert delta.rejected == d("0")
    assert delta.scrap == d("0")
    assert delta.rework == d("100")
    assert delta.hold == d("0")


def test_still_held_is_the_quantity_a_disposition_leaves_in_the_pool() -> None:
    disposition = Disposition(accepted=d("50"), rework=d("30"), hold=d("20"))
    assert disposition.still_held == d("50")
    assert Disposition(accepted=d("100")).still_held == d("0")


# -----------------------------------------------------------------------------
# The rate every report quotes
# -----------------------------------------------------------------------------


def totals(*, inspected: str, rejected: str = "0", scrap: str = "0") -> QualityTotals:
    return QualityTotals(
        inspection_count=1,
        inspected_quantity=d(inspected),
        accepted_quantity=d(inspected) - d(rejected) - d(scrap),
        rejected_quantity=d(rejected),
        rework_quantity=d("0"),
        scrap_quantity=d(scrap),
    )


def test_the_rejection_rate_counts_scrapped_material_as_rejected() -> None:
    # Otherwise a supplier's record improves by having their defects destroyed
    # rather than returned.
    assert rejection_rate(totals(inspected="100", rejected="5", scrap="5")) == d("10.0000")


def test_a_clean_record_rates_zero_and_a_total_loss_rates_one_hundred() -> None:
    assert rejection_rate(totals(inspected="1000")) == d("0.0000")
    assert rejection_rate(totals(inspected="1000", rejected="1000")) == d("100.0000")


def test_a_vendor_with_no_inspected_quantity_rates_zero_rather_than_dividing_by_it() -> None:
    assert rejection_rate(totals(inspected="0")) == d("0")


def test_the_rate_keeps_four_decimal_places() -> None:
    # 1 in 3000 is 0.0333%. Rounding that to a whole percent would report a
    # genuinely troublesome supplier as flawless.
    assert rejection_rate(totals(inspected="3000", rejected="1")) == d("0.0333")
