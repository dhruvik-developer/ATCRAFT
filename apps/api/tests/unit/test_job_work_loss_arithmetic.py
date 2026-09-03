"""Job work receipt arithmetic: yield, loss, tolerance and charges.

These are the numbers that decide how much company material stops being
traceable. Every case here is one an operator actually keys: a plating shop that
returns 98 of the 100 handles it was sent, one that returns 103, one that keeps
ten for rework, one that hands the raw material back untouched.

Pure functions, no database - the whole point of ``plan_line_receipt`` is that
the rules can be exercised without one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ErrorCode, ValidationError
from src.modules.job_work.models import RateBasis, RejectionAction
from src.modules.job_work.service import (
    EXCESS_RECEIPT_TOLERANCE_PERCENT,
    estimate_charges,
    input_equivalent,
    loss_percentage,
    plan_line_receipt,
    receipt_charge,
    yield_ratio,
)


def plan(**overrides):
    """One line of a receipt against 100 units sent out at a 1:1 yield."""
    kwargs = {
        "line_number": 1,
        "outstanding_input": Decimal("100"),
        "ratio": Decimal("1"),
        "expected_output_quantity": Decimal("100"),
        "output_received_so_far": Decimal("0"),
        "output_quantity": Decimal("0"),
        "rejected_quantity": Decimal("0"),
        "rejection_action": RejectionAction.RETURN_TO_STOCK,
        "returned_unprocessed_quantity": Decimal("0"),
        "is_final": False,
        "expected_loss_percentage": Decimal("0"),
    }
    kwargs.update(overrides)
    return plan_line_receipt(**kwargs)


# -----------------------------------------------------------------------------
# Yield
# -----------------------------------------------------------------------------


def test_a_one_to_one_process_has_a_ratio_of_one() -> None:
    assert yield_ratio(Decimal("100"), Decimal("100")) == Decimal("1")


def test_a_process_that_yields_fewer_pieces_has_a_ratio_below_one() -> None:
    # 100 kg of bar in, 40 machined blanks out.
    assert yield_ratio(Decimal("100"), Decimal("40")) == Decimal("0.4")


@pytest.mark.parametrize(
    ("issued", "expected"),
    [(Decimal("0"), Decimal("10")), (Decimal("-5"), Decimal("10")), (Decimal("10"), Decimal("0"))],
)
def test_a_non_positive_quantity_has_no_yield(issued: Decimal, expected: Decimal) -> None:
    with pytest.raises(ValidationError):
        yield_ratio(issued, expected)


def test_input_equivalent_inverts_the_yield() -> None:
    ratio = yield_ratio(Decimal("100"), Decimal("40"))
    # 40 blanks came back, so all 100 kg was consumed producing them.
    assert input_equivalent(Decimal("40"), ratio) == Decimal("100.0000")


# -----------------------------------------------------------------------------
# The identity that keeps the job-work pool reconcilable
# -----------------------------------------------------------------------------


def test_a_full_good_return_consumes_exactly_what_it_produced() -> None:
    result = plan(output_quantity=Decimal("100"), is_final=True)

    assert result.input_consumed_quantity == Decimal("100.0000")
    assert result.input_for_output == Decimal("100.0000")
    assert result.scrap_quantity == Decimal("0.0000")


def test_the_drawdown_identity_holds_for_a_mixed_return() -> None:
    # 70 good, 10 rejected and sent back, 5 handed back raw, and the line closed
    # so the remaining 15 is written off.
    result = plan(
        output_quantity=Decimal("70"),
        rejected_quantity=Decimal("10"),
        returned_unprocessed_quantity=Decimal("5"),
        is_final=True,
    )

    assert (
        result.input_for_output
        + result.returned_unprocessed_quantity
        + result.scrap_quantity
        == result.input_consumed_quantity
    )
    assert result.input_consumed_quantity == Decimal("100.0000")
    assert result.scrap_quantity == Decimal("15.0000")


def test_a_partial_receipt_consumes_only_what_it_accounts_for() -> None:
    result = plan(output_quantity=Decimal("60"))

    # Not final, so the other 40 is still at the vendor rather than scrapped.
    assert result.input_consumed_quantity == Decimal("60.0000")
    assert result.scrap_quantity == Decimal("0.0000")


def test_closing_a_line_writes_the_shortfall_off_rather_than_leaving_it_out() -> None:
    result = plan(output_quantity=Decimal("98"), is_final=True)

    assert result.input_consumed_quantity == Decimal("100.0000")
    assert result.scrap_quantity == Decimal("2.0000")


# -----------------------------------------------------------------------------
# Loss allowance
# -----------------------------------------------------------------------------


def test_loss_inside_the_allowance_raises_no_excess() -> None:
    result = plan(
        output_quantity=Decimal("98"), is_final=True, expected_loss_percentage=Decimal("5")
    )

    assert result.scrap_quantity == Decimal("2.0000")
    assert result.excess_loss_quantity == Decimal("0.0000")


def test_loss_beyond_the_allowance_reports_only_the_excess() -> None:
    # 6 lost against a 2% allowance on the 100 consumed: 2 is agreed, 4 is not.
    result = plan(
        output_quantity=Decimal("94"), is_final=True, expected_loss_percentage=Decimal("2")
    )

    assert result.scrap_quantity == Decimal("6.0000")
    assert result.excess_loss_quantity == Decimal("4.0000")


def test_loss_exactly_at_the_allowance_is_not_an_excess() -> None:
    result = plan(
        output_quantity=Decimal("95"), is_final=True, expected_loss_percentage=Decimal("5")
    )

    assert result.scrap_quantity == Decimal("5.0000")
    assert result.excess_loss_quantity == Decimal("0.0000")


def test_the_allowance_is_measured_against_what_the_lot_consumed() -> None:
    # A half-sized lot gets a half-sized allowance: 50 consumed at 10% is 5.
    result = plan(
        outstanding_input=Decimal("50"),
        expected_output_quantity=Decimal("50"),
        output_quantity=Decimal("44"),
        is_final=True,
        expected_loss_percentage=Decimal("10"),
    )

    assert result.scrap_quantity == Decimal("6.0000")
    assert result.excess_loss_quantity == Decimal("1.0000")


def test_loss_percentage_reports_against_the_input_consumed() -> None:
    assert loss_percentage(
        scrap_quantity=Decimal("2"), input_consumed=Decimal("100")
    ) == Decimal("2.0000")


def test_a_line_that_consumed_nothing_has_lost_nothing() -> None:
    # Not undefined: a line nobody has received against cannot have a loss rate,
    # and the reconciliation report has to be sortable.
    assert loss_percentage(scrap_quantity=Decimal("0"), input_consumed=Decimal("0")) == 0


# -----------------------------------------------------------------------------
# Over-receipt
# -----------------------------------------------------------------------------


def test_output_beyond_the_tolerance_is_refused_not_clamped() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        plan(output_quantity=Decimal("103"))

    assert "tolerance" in str(exc.value)
    assert exc.value.details["cumulative_output"] == "103.0000"


def test_the_tolerance_counts_output_across_every_earlier_lot() -> None:
    # 98 already delivered; 5 more takes the cumulative to 103, past 102.
    with pytest.raises(BusinessRuleError):
        plan(
            outstanding_input=Decimal("2"),
            output_received_so_far=Decimal("98"),
            output_quantity=Decimal("5"),
        )


def test_an_over_delivery_inside_the_tolerance_draws_only_the_input_that_exists() -> None:
    # The vendor returned half a piece more than was sent. The surplus output is
    # received; the job-work pool gives up what it actually holds and no more.
    result = plan(
        outstanding_input=Decimal("40"),
        output_received_so_far=Decimal("60"),
        output_quantity=Decimal("40.5"),
    )

    assert result.output_quantity == Decimal("40.5000")
    assert result.input_for_output == Decimal("40.0000")
    assert result.input_consumed_quantity == Decimal("40.0000")


def test_a_lot_that_implies_far_more_input_than_is_out_is_refused() -> None:
    # Only 10 left at the vendor, but 50 pieces of output claimed against it.
    with pytest.raises(BusinessRuleError) as exc:
        plan(outstanding_input=Decimal("10"), output_quantity=Decimal("50"))

    assert exc.value.code is ErrorCode.INSUFFICIENT_STOCK


def test_more_unprocessed_material_than_was_sent_cannot_come_back() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        plan(
            outstanding_input=Decimal("10"), returned_unprocessed_quantity=Decimal("11")
        )

    assert exc.value.code is ErrorCode.INSUFFICIENT_STOCK


def test_nothing_can_be_received_against_a_line_with_no_material_at_the_vendor() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        plan(outstanding_input=Decimal("0"), output_quantity=Decimal("1"))

    assert "no material at the vendor" in str(exc.value)


def test_the_tolerance_constant_is_the_one_the_error_reports() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        plan(output_quantity=Decimal("100") + EXCESS_RECEIPT_TOLERANCE_PERCENT + 1)

    assert exc.value.details["tolerance_percent"] == str(EXCESS_RECEIPT_TOLERANCE_PERCENT)


# -----------------------------------------------------------------------------
# Rework stays at the vendor
# -----------------------------------------------------------------------------


def test_rework_consumes_no_input_and_leaves_the_line_open() -> None:
    result = plan(
        output_quantity=Decimal("90"),
        rejected_quantity=Decimal("10"),
        rejection_action=RejectionAction.REWORK_AT_VENDOR,
    )

    assert result.rework_quantity == Decimal("10.0000")
    # The rejected pieces are not counted as an outcome - they have not been
    # delivered and their material is still in the job-work pool.
    assert result.rejected_quantity == 0
    assert result.input_consumed_quantity == Decimal("90.0000")


def test_rework_pieces_do_not_burn_the_over_receipt_tolerance_twice() -> None:
    # 90 good and 10 left for rework. When the 10 come back good later, the
    # cumulative is 100 - not 110 - so the second lot is not refused.
    first = plan(
        output_quantity=Decimal("90"),
        rejected_quantity=Decimal("10"),
        rejection_action=RejectionAction.REWORK_AT_VENDOR,
    )
    second = plan(
        outstanding_input=Decimal("100") - first.input_consumed_quantity,
        output_received_so_far=first.output_quantity,
        output_quantity=Decimal("10"),
        is_final=True,
    )

    assert second.output_quantity == Decimal("10.0000")
    assert second.input_consumed_quantity == Decimal("10.0000")


def test_returned_rejects_are_an_outcome_and_do_consume_input() -> None:
    result = plan(
        output_quantity=Decimal("90"),
        rejected_quantity=Decimal("10"),
        rejection_action=RejectionAction.RETURN_TO_STOCK,
    )

    assert result.rejected_quantity == Decimal("10.0000")
    assert result.rework_quantity == 0
    assert result.input_for_output == Decimal("100.0000")


def test_a_report_that_accounts_for_no_material_is_refused() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        plan(
            rejected_quantity=Decimal("5"),
            rejection_action=RejectionAction.REWORK_AT_VENDOR,
        )

    assert "accounts for no material" in str(exc.value)


# -----------------------------------------------------------------------------
# A yield that is not 1:1
# -----------------------------------------------------------------------------


def test_a_fractional_yield_converts_output_back_to_the_input_it_ate() -> None:
    # 100 kg of bar was sent out to be machined into 40 blanks; 20 came back.
    result = plan(
        ratio=yield_ratio(Decimal("100"), Decimal("40")),
        expected_output_quantity=Decimal("40"),
        output_quantity=Decimal("20"),
    )

    assert result.input_for_output == Decimal("50.0000")
    assert result.input_consumed_quantity == Decimal("50.0000")


def test_closing_a_fractional_yield_line_scraps_the_uncut_balance() -> None:
    result = plan(
        ratio=yield_ratio(Decimal("100"), Decimal("40")),
        expected_output_quantity=Decimal("40"),
        output_quantity=Decimal("38"),
        is_final=True,
    )

    assert result.input_for_output == Decimal("95.0000")
    assert result.scrap_quantity == Decimal("5.0000")


# -----------------------------------------------------------------------------
# Charges
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("basis", [RateBasis.PER_PIECE, RateBasis.PER_KG])
def test_a_unit_rate_is_charged_on_good_output_only(basis: RateBasis) -> None:
    charge = receipt_charge(
        rate_per_unit=Decimal("12.50"),
        rate_basis=basis,
        output_quantity=Decimal("98"),
        lot_already_charged=False,
    )

    assert charge == Decimal("1225.00")


def test_a_lot_rate_is_earned_once() -> None:
    first = receipt_charge(
        rate_per_unit=Decimal("5000"),
        rate_basis=RateBasis.LOT,
        output_quantity=Decimal("60"),
        lot_already_charged=False,
    )
    second = receipt_charge(
        rate_per_unit=Decimal("5000"),
        rate_basis=RateBasis.LOT,
        output_quantity=Decimal("40"),
        lot_already_charged=True,
    )

    assert first == Decimal("5000.00")
    assert second == 0


def test_a_lot_rate_is_not_earned_by_a_delivery_that_returned_nothing() -> None:
    charge = receipt_charge(
        rate_per_unit=Decimal("5000"),
        rate_basis=RateBasis.LOT,
        output_quantity=Decimal("0"),
        lot_already_charged=False,
    )

    assert charge == 0


def test_a_short_delivery_costs_less_without_a_credit_note() -> None:
    full = receipt_charge(
        rate_per_unit=Decimal("10"),
        rate_basis=RateBasis.PER_PIECE,
        output_quantity=Decimal("100"),
        lot_already_charged=False,
    )
    short = receipt_charge(
        rate_per_unit=Decimal("10"),
        rate_basis=RateBasis.PER_PIECE,
        output_quantity=Decimal("98"),
        lot_already_charged=False,
    )

    assert full - short == Decimal("20.00")


def test_the_estimate_uses_the_expected_output_not_the_quantity_sent() -> None:
    estimate = estimate_charges(
        rate_per_unit=Decimal("12.50"),
        rate_basis=RateBasis.PER_PIECE,
        expected_output_quantity=Decimal("40"),
    )

    assert estimate == Decimal("500.00")


def test_a_lot_estimate_ignores_the_quantity() -> None:
    assert estimate_charges(
        rate_per_unit=Decimal("5000"),
        rate_basis=RateBasis.LOT,
        expected_output_quantity=Decimal("9999"),
    ) == Decimal("5000.00")


def test_charges_never_become_floats() -> None:
    charge = receipt_charge(
        rate_per_unit=Decimal("0.1"),
        rate_basis=RateBasis.PER_PIECE,
        output_quantity=Decimal("3"),
        lot_already_charged=False,
    )

    assert isinstance(charge, Decimal)
    # 0.1 * 3 is 0.30000000000000004 in binary floating point.
    assert charge == Decimal("0.30")
