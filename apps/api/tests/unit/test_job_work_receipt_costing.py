"""Receipt cost capitalisation: pure costing arithmetic (E7b/P13.M5)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.job_work.models import JobWorkChallanLine
from src.modules.job_work.service import capitalised_output_cost, issue_unit_basis


def _challan_line(*, quantity: str, unit_value: str) -> JobWorkChallanLine:
    return JobWorkChallanLine(
        company_id=uuid.uuid4(),
        challan_id=uuid.uuid4(),
        job_work_order_line_id=uuid.uuid4(),
        line_number=1,
        item_id=uuid.uuid4(),
        uom_id=uuid.uuid4(),
        quantity=Decimal(quantity),
        unit_value=Decimal(unit_value),
        total_value=Decimal(quantity) * Decimal(unit_value),
    )


def test_issue_basis_is_the_quantity_weighted_challan_average() -> None:
    lines = [
        _challan_line(quantity="60", unit_value="100"),
        _challan_line(quantity="40", unit_value="110"),
    ]
    assert issue_unit_basis(lines) == Decimal("104")


def test_issue_basis_is_none_without_challan_history() -> None:
    assert issue_unit_basis([]) is None


def test_capitalised_cost_adds_input_value_and_charge_per_good_unit() -> None:
    cost = capitalised_output_cost(
        input_for_output=Decimal("100"),
        challan_lines=[_challan_line(quantity="100", unit_value="100")],
        line_charge=Decimal("1250"),
        output_quantity=Decimal("100"),
    )
    assert cost is not None
    assert cost.unit_cost == Decimal("112.500000")
    assert cost.input_value == Decimal("10000")
    assert cost.rounding_residual == Decimal("0")


def test_capitalised_cost_records_the_rounding_residual() -> None:
    cost = capitalised_output_cost(
        input_for_output=Decimal("10"),
        challan_lines=[_challan_line(quantity="10", unit_value="1")],
        line_charge=Decimal("0"),
        output_quantity=Decimal("3"),
    )
    assert cost is not None
    # 10 / 3 at six places leaves dust behind, and the dust is named.
    assert cost.unit_cost == Decimal("3.333333")
    assert cost.rounding_residual == Decimal("10") - cost.unit_cost * Decimal("3")
    assert cost.rounding_residual == Decimal("0.000001")


@pytest.mark.parametrize(
    ("input_quantity", "good", "rejected", "material_unit"),
    [("100", "90", "10", "100"), ("10", "80", "20", "10"), ("100", "8", "2", "1000")],
)
def test_good_and_returned_rejects_share_input_value_without_duplicate_material(
    input_quantity: str, good: str, rejected: str, material_unit: str
) -> None:
    lines = [_challan_line(quantity=input_quantity, unit_value="100")]
    output = Decimal(good)
    rejected_output = Decimal(rejected)
    delivered = output + rejected_output
    good_cost = capitalised_output_cost(
        input_for_output=Decimal(input_quantity),
        challan_lines=lines,
        line_charge=output * Decimal("12.50"),
        output_quantity=output,
        delivered_output_quantity=delivered,
    )
    rejected_cost = capitalised_output_cost(
        input_for_output=Decimal(input_quantity),
        challan_lines=lines,
        line_charge=Decimal("0"),
        output_quantity=rejected_output,
        delivered_output_quantity=delivered,
    )
    assert good_cost is not None and rejected_cost is not None
    assert good_cost.unit_cost == Decimal(material_unit) + Decimal("12.50")
    assert rejected_cost.unit_cost == Decimal(material_unit)
    assert good_cost.input_value + rejected_cost.input_value == Decimal(input_quantity) * 100


def test_fractional_raw_shares_are_not_rounded_before_valuing_returned_output() -> None:
    lines = [_challan_line(quantity="1", unit_value="10000")]
    good = capitalised_output_cost(
        input_for_output=Decimal("1"),
        challan_lines=lines,
        line_charge=Decimal("0"),
        output_quantity=Decimal("2"),
        delivered_output_quantity=Decimal("3"),
    )
    rejected = capitalised_output_cost(
        input_for_output=Decimal("1"),
        challan_lines=lines,
        line_charge=Decimal("0"),
        output_quantity=Decimal("1"),
        delivered_output_quantity=Decimal("3"),
    )
    assert good is not None and rejected is not None
    assert good.unit_cost == rejected.unit_cost == Decimal("3333.333333")
    assert good.input_value + rejected.input_value == Decimal("10000")
    assert good.rounding_residual + rejected.rounding_residual == Decimal("0.000001")


def test_capitalised_cost_is_none_without_good_output_or_basis() -> None:
    lines = [_challan_line(quantity="100", unit_value="100")]
    assert (
        capitalised_output_cost(
            input_for_output=Decimal("0"),
            challan_lines=lines,
            line_charge=Decimal("10"),
            output_quantity=Decimal("0"),
        )
        is None
    )
    assert (
        capitalised_output_cost(
            input_for_output=Decimal("100"),
            challan_lines=[],
            line_charge=Decimal("10"),
            output_quantity=Decimal("100"),
        )
        is None
    )
