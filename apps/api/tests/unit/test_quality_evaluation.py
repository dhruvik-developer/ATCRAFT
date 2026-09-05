"""Judging a measurement, and judging a whole check sheet.

Two rules are under test here, and both decide whether defective hardware
reaches a customer.

The first is the **critical override**: a critical parameter out of
specification rejects the entire lot regardless of every other reading. Master
rules section 21 states it, and the consequence is that a thread that does not
engage cannot be argued away by twenty dimensions that passed.

The second is **spec-range evaluation**: the boundary of a tolerance band is
inclusive, a numeric parameter with no band is informational rather than a
failure, and a reading in the wrong form for its parameter is refused instead of
being compared as a string - which is how "10" comes out below "9".

No database. Everything here is a pure function over dataclasses.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ValidationError
from src.modules.quality.models import InspectionResult, ParameterType
from src.modules.quality.service import (
    Disposition,
    ParameterSpec,
    Reading,
    assert_critical_override,
    evaluate_inspection,
    evaluate_reading,
)


def spec(
    *,
    name: str = "Bore diameter",
    parameter_type: ParameterType = ParameterType.NUMERIC,
    minimum: str | None = None,
    maximum: str | None = None,
    allowed: tuple[str, ...] = (),
    critical: bool = False,
    mandatory: bool = True,
) -> ParameterSpec:
    return ParameterSpec(
        id=uuid.uuid4(),
        name=name,
        parameter_type=parameter_type,
        min_value=None if minimum is None else Decimal(minimum),
        max_value=None if maximum is None else Decimal(maximum),
        allowed_values=allowed,
        is_critical=critical,
        is_mandatory=mandatory,
    )


# -----------------------------------------------------------------------------
# Numeric tolerance bands
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("measured", "expected"),
    [
        # The band is inclusive at both ends: a part machined exactly to the
        # stated limit conforms. Treating the limit as exclusive would reject
        # nominal work and quietly tighten every tolerance in the system.
        ("9.950", True),
        ("10.050", True),
        ("10.000", True),
        ("9.9499", False),
        ("10.0501", False),
    ],
)
def test_a_numeric_reading_is_judged_against_an_inclusive_band(
    measured: str, expected: bool
) -> None:
    parameter = spec(minimum="9.95", maximum="10.05")
    assert evaluate_reading(parameter, Reading(numeric=Decimal(measured))) is expected


def test_a_one_sided_band_only_constrains_the_side_it_states() -> None:
    minimum_only = spec(name="Coating thickness", minimum="2.5")
    assert evaluate_reading(minimum_only, Reading(numeric=Decimal("2.5"))) is True
    assert evaluate_reading(minimum_only, Reading(numeric=Decimal("40"))) is True
    assert evaluate_reading(minimum_only, Reading(numeric=Decimal("2.4999"))) is False

    maximum_only = spec(name="Surface roughness", maximum="1.6")
    assert evaluate_reading(maximum_only, Reading(numeric=Decimal("0"))) is True
    assert evaluate_reading(maximum_only, Reading(numeric=Decimal("1.61"))) is False


def test_a_numeric_parameter_with_no_band_is_informational() -> None:
    # A target with no tolerance cannot be met by a real measurement, so judging
    # against it would reject every lot. The reading is recorded, not failed.
    parameter = ParameterSpec(
        id=uuid.uuid4(),
        name="Weight",
        parameter_type=ParameterType.NUMERIC,
        target_value=Decimal("125"),
    )
    assert evaluate_reading(parameter, Reading(numeric=Decimal("118.4"))) is True


def test_six_decimal_places_of_a_reading_are_not_rounded_away() -> None:
    # Micron-level tolerances are ordinary hardware specifications. A reading
    # that misses by 0.0000005 mm is inside the band; one that misses by
    # 0.000001 mm is not, and the column's scale has to be able to say so.
    parameter = spec(minimum="2.500000", maximum="2.500001")
    assert evaluate_reading(parameter, Reading(numeric=Decimal("2.500001"))) is True
    assert evaluate_reading(parameter, Reading(numeric=Decimal("2.500002"))) is False


# -----------------------------------------------------------------------------
# The other three parameter types
# -----------------------------------------------------------------------------


def test_a_boolean_parameter_passes_on_true() -> None:
    parameter = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN)
    assert evaluate_reading(parameter, Reading(boolean=True)) is True
    assert evaluate_reading(parameter, Reading(boolean=False)) is False


def test_a_select_parameter_must_match_its_list() -> None:
    parameter = spec(
        name="Finish",
        parameter_type=ParameterType.SELECT,
        allowed=("SATIN", "MATT", "MIRROR"),
    )
    assert evaluate_reading(parameter, Reading(text="MATT")) is True
    # Surrounding whitespace is a typing artefact, not a different finish.
    assert evaluate_reading(parameter, Reading(text="  MIRROR  ")) is True
    assert evaluate_reading(parameter, Reading(text="BRUSHED")) is False
    # Deliberately not case-folded: folding could merge two grades that a
    # specification distinguishes.
    assert evaluate_reading(parameter, Reading(text="matt")) is False


def test_free_text_is_informational_until_a_list_constrains_it() -> None:
    free = spec(name="Batch marking", parameter_type=ParameterType.TEXT)
    assert evaluate_reading(free, Reading(text="anything at all")) is True

    constrained = spec(
        name="Packing", parameter_type=ParameterType.TEXT, allowed=("BOX", "BLISTER")
    )
    assert evaluate_reading(constrained, Reading(text="BOX")) is True
    assert evaluate_reading(constrained, Reading(text="LOOSE")) is False


@pytest.mark.parametrize(
    ("parameter", "reading"),
    [
        (spec(), Reading(text="10.0")),
        (spec(), Reading(boolean=True)),
        (spec(parameter_type=ParameterType.BOOLEAN), Reading(numeric=Decimal("1"))),
        (
            spec(parameter_type=ParameterType.SELECT, allowed=("A",)),
            Reading(numeric=Decimal("1")),
        ),
    ],
)
def test_a_reading_in_the_wrong_form_is_refused_not_coerced(
    parameter: ParameterSpec, reading: Reading
) -> None:
    # A numeric measurement typed into the text field would be compared as a
    # string, which is how "10" comes out below "9".
    with pytest.raises(ValidationError):
        evaluate_reading(parameter, reading)


# -----------------------------------------------------------------------------
# The critical override
# -----------------------------------------------------------------------------


def test_one_critical_failure_rejects_the_lot_however_much_else_passed() -> None:
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    others = [spec(name=f"Dimension {n}", minimum="0", maximum="100") for n in range(20)]

    evaluation = evaluate_inspection(
        [thread, *others],
        {thread.id: False, **{parameter.id: True for parameter in others}},
    )

    assert evaluation.recommended_result is InspectionResult.REJECTED
    assert evaluation.has_critical_failure
    assert evaluation.critical_failures == ("Thread engages",)
    assert evaluation.in_spec_count == 20


def test_a_critical_failure_settles_the_verdict_even_with_readings_outstanding() -> None:
    # Once the characteristic that can reject on its own has failed, no further
    # measurement can change the answer, so the decision is not held up waiting
    # for the rest of the sheet.
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    unmeasured = spec(name="Plating thickness", minimum="2.5", maximum="3.5")

    evaluation = evaluate_inspection([thread, unmeasured], {thread.id: False})

    assert evaluation.recommended_result is InspectionResult.REJECTED
    assert evaluation.is_conclusive
    assert evaluation.missing_mandatory == ("Plating thickness",)


def test_a_critical_parameter_that_passed_does_not_override_anything() -> None:
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    finish = spec(name="Finish", minimum="0", maximum="1")

    evaluation = evaluate_inspection([thread, finish], {thread.id: True, finish.id: False})

    assert not evaluation.has_critical_failure
    assert evaluation.recommended_result is InspectionResult.REWORK
    assert evaluation.failures == ("Finish",)


def test_a_non_critical_deviation_recommends_rework_rather_than_acceptance() -> None:
    # Defaulting a deviating lot towards acceptance would make the whole check
    # sheet decorative. The inspector still decides; the recommendation does not
    # nudge them the wrong way.
    finish = spec(name="Finish")
    evaluation = evaluate_inspection([finish], {finish.id: False})
    assert evaluation.recommended_result is InspectionResult.REWORK
    assert evaluation.is_conclusive


def test_a_missing_mandatory_reading_leaves_the_verdict_inconclusive() -> None:
    measured = spec(name="Length", minimum="0", maximum="10")
    missing = spec(name="Hardness", minimum="0", maximum="10")

    evaluation = evaluate_inspection([measured, missing], {measured.id: True})

    assert evaluation.recommended_result is InspectionResult.PENDING
    assert not evaluation.is_conclusive
    assert evaluation.missing_mandatory == ("Hardness",)


def test_an_optional_parameter_may_be_skipped() -> None:
    required = spec(name="Length", minimum="0", maximum="10")
    optional = spec(name="Destructive pull test", mandatory=False)

    evaluation = evaluate_inspection([required, optional], {required.id: True})

    assert evaluation.recommended_result is InspectionResult.ACCEPTED
    assert evaluation.is_conclusive
    assert evaluation.missing_mandatory == ()


def test_readings_for_parameters_outside_the_sheet_are_ignored() -> None:
    # A template forked into a new version leaves old readings behind. They must
    # not silently count towards - or against - the current specification.
    current = spec(name="Length", minimum="0", maximum="10")
    stale = uuid.uuid4()

    evaluation = evaluate_inspection([current], {current.id: True, stale: False})

    assert evaluation.recommended_result is InspectionResult.ACCEPTED
    assert evaluation.measured_count == 1
    assert evaluation.failures == ()


def test_an_inspection_with_no_check_sheet_is_decidable_on_judgement() -> None:
    # An item nobody has written a specification for still gets inspected; the
    # alternative is stock that can never leave QC hold.
    evaluation = evaluate_inspection([], {})
    assert evaluation.recommended_result is InspectionResult.ACCEPTED
    assert evaluation.is_conclusive


# -----------------------------------------------------------------------------
# The override is binding on the decision, not merely advisory
# -----------------------------------------------------------------------------


def test_a_critical_failure_forbids_accepting_any_of_the_lot() -> None:
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    evaluation = evaluate_inspection([thread], {thread.id: False})

    with pytest.raises(BusinessRuleError) as caught:
        assert_critical_override(
            evaluation, Disposition(accepted=Decimal("90"), rejected=Decimal("10"))
        )
    assert "Thread engages" in str(caught.value)


def test_a_critical_failure_forbids_sending_any_of_the_lot_for_rework() -> None:
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    evaluation = evaluate_inspection([thread], {thread.id: False})

    with pytest.raises(BusinessRuleError):
        assert_critical_override(evaluation, Disposition(rework=Decimal("100")))


def test_a_critical_failure_still_allows_rejecting_and_scrapping() -> None:
    # Scrap is a strictly harsher disposition than rejection, never a softer one,
    # so destroying critically defective material is permitted.
    thread = spec(name="Thread engages", parameter_type=ParameterType.BOOLEAN, critical=True)
    evaluation = evaluate_inspection([thread], {thread.id: False})

    assert_critical_override(evaluation, Disposition(rejected=Decimal("60"), scrap=Decimal("40")))


def test_without_a_critical_failure_any_disposition_is_permitted() -> None:
    finish = spec(name="Finish")
    evaluation = evaluate_inspection([finish], {finish.id: False})
    assert_critical_override(evaluation, Disposition(accepted=Decimal("100")))
