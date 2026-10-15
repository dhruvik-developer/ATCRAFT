"""Production costing arithmetic.

Closing a job answers a question the plant will act on - what did this actually
cost? - so the maths is exercised here without a database, at the same level of
detail as the inventory valuation tests.

Two properties matter most and are asserted directly rather than implied:

* every figure stays ``Decimal``, and the total equals the sum of the printed
  lines rather than the rounding of an unrounded sum;
* an operation nobody clocked is costed from its planned time when it certainly
  ran, and at nothing when it certainly did not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.production.models import (
    OVER_ISSUE_MULTIPLIER,
    OVER_PRODUCTION_MULTIPLIER,
    ProductionOrder,
    ProductionOrderComponent,
    ProductionOrderStatus,
    WorkOrderStatus,
)
from src.modules.production.repository import IssuedComponentTotals
from src.modules.production.service import (
    OperationCostInput,
    assert_within_over_issue_tolerance,
    assert_within_over_production_tolerance,
    backflush_quantity,
    charged_minutes,
    issue_ceiling,
    material_cost_line,
    operation_cost_line,
    summarise_cost,
)

pytestmark = pytest.mark.unit


def _operation(
    *,
    status: WorkOrderStatus = WorkOrderStatus.COMPLETED,
    setup: str = "30",
    run: str = "120",
    process_cost: str = "0",
    cost_per_hour: str = "600",
    setup_cost: str = "0",
    is_outsourced: bool = False,
    started: datetime | None = None,
    ended: datetime | None = None,
) -> OperationCostInput:
    return OperationCostInput(
        work_order_id=uuid.uuid4(),
        sequence=10,
        operation_code="CNC",
        work_center_id=uuid.uuid4(),
        status=status,
        planned_setup_minutes=Decimal(setup),
        planned_run_minutes=Decimal(run),
        process_cost=Decimal(process_cost),
        actual_start_at=started,
        actual_end_at=ended,
        cost_per_hour=Decimal(cost_per_hour),
        work_centre_setup_cost=Decimal(setup_cost),
        is_outsourced=is_outsourced,
    )


def _component(*, required: str = "100", item_code: str = "RM-1") -> ProductionOrderComponent:
    return ProductionOrderComponent(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        item_code=item_code,
        required_quantity=Decimal(required),
        issued_quantity=Decimal("0"),
        returned_quantity=Decimal("0"),
        consumed_quantity=Decimal("0"),
        is_backflushed=False,
    )


def _order(*, planned: str = "100", produced: str = "100") -> ProductionOrder:
    return ProductionOrder(
        id=uuid.uuid4(),
        code="MO/2026-27/00001",
        status=ProductionOrderStatus.COMPLETED,
        planned_quantity=Decimal(planned),
        produced_quantity=Decimal(produced),
    )


# -----------------------------------------------------------------------------
# Charged time
# -----------------------------------------------------------------------------


def test_a_clocked_operation_is_charged_its_elapsed_time():
    started = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    ended = datetime(2026, 4, 1, 11, 30, tzinfo=UTC)
    minutes, is_actual = charged_minutes(_operation(started=started, ended=ended))
    assert minutes == Decimal("210.0000")
    assert is_actual is True


def test_elapsed_time_includes_downtime_rather_than_deducting_it():
    """The centre was occupied by this job while it stood idle.

    Expressed here as a property of the calculation: nothing about downtime
    enters ``charged_minutes`` at all, so a three-hour shift with an hour of
    breakdown is still charged three hours.
    """
    started = datetime(2026, 4, 1, 6, 0, tzinfo=UTC)
    ended = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    minutes, _ = charged_minutes(_operation(started=started, ended=ended, setup="0", run="0"))
    assert minutes == Decimal("180.0000")


def test_a_completed_but_unclocked_operation_falls_back_to_planned_time():
    """Costing it at zero would understate every job whose operator forgot."""
    minutes, is_actual = charged_minutes(
        _operation(status=WorkOrderStatus.COMPLETED, setup="30", run="120")
    )
    assert minutes == Decimal("150.0000")
    assert is_actual is False


@pytest.mark.parametrize("status", [WorkOrderStatus.PENDING, WorkOrderStatus.CANCELLED])
def test_an_operation_that_never_ran_is_charged_nothing(status):
    """Charging planned time for work never done inflates every short-closed job."""
    minutes, is_actual = charged_minutes(_operation(status=status))
    assert minutes == Decimal("0")
    assert is_actual is True


def test_only_one_end_of_the_clock_is_not_enough_to_charge_actual_time():
    """A started-but-unfinished operation has no measurable duration yet."""
    minutes, is_actual = charged_minutes(
        _operation(
            status=WorkOrderStatus.IN_PROGRESS,
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        )
    )
    assert minutes == Decimal("0")
    assert is_actual is True


# -----------------------------------------------------------------------------
# Operation cost
# -----------------------------------------------------------------------------


def test_operation_cost_is_rate_times_hours_plus_the_flat_charges():
    line = operation_cost_line(
        _operation(
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            ended=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
            cost_per_hour="600",
            setup_cost="250",
            process_cost="75",
        )
    )
    # 120 minutes at 600/hour = 1200, plus a 250 changeover and a 75 die charge.
    assert line.time_cost == Decimal("1200.00")
    assert line.setup_cost == Decimal("250.00")
    assert line.process_cost == Decimal("75.00")
    assert line.total_cost == Decimal("1525.00")


def test_the_work_centre_changeover_charge_is_applied_once_per_operation():
    """The routing roll-up deliberately omits it; production applies it here.

    Charging it inside the hourly rate as well would count one changeover twice,
    which is exactly what the routing module's comment warns against.
    """
    line = operation_cost_line(
        _operation(
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            ended=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            setup_cost="500",
        )
    )
    assert line.setup_cost == Decimal("500.00")


def test_an_operation_that_never_ran_carries_no_flat_charges_either():
    line = operation_cost_line(
        _operation(status=WorkOrderStatus.CANCELLED, setup_cost="500", process_cost="75")
    )
    assert line.charged_minutes == Decimal("0")
    assert line.setup_cost == Decimal("0")
    assert line.process_cost == Decimal("0")
    assert line.total_cost == Decimal("0.00")


def test_outsourced_work_order_time_is_not_double_counted_as_internal_operation_cost():
    line = operation_cost_line(
        _operation(
            is_outsourced=True,
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            ended=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
            cost_per_hour="600",
            setup_cost="250",
            process_cost="75",
        )
    )
    assert line.is_outsourced is True
    assert line.time_cost == Decimal("0.00")
    assert line.setup_cost == Decimal("0.00")
    assert line.process_cost == Decimal("0.00")
    assert line.total_cost == Decimal("0.00")


def test_a_zero_rate_work_centre_still_costs_its_flat_charges():
    """A rate nobody has set is not a reason to lose the die charge."""
    line = operation_cost_line(
        _operation(
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            ended=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            cost_per_hour="0",
            process_cost="90",
        )
    )
    assert line.time_cost == Decimal("0.00")
    assert line.total_cost == Decimal("90.00")


def test_operation_cost_never_produces_a_float():
    line = operation_cost_line(
        _operation(
            started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            ended=datetime(2026, 4, 1, 8, 7, tzinfo=UTC),
            cost_per_hour="437.50",
        )
    )
    for value in (line.charged_minutes, line.time_cost, line.total_cost, line.cost_per_hour):
        assert isinstance(value, Decimal)


# -----------------------------------------------------------------------------
# Material cost
# -----------------------------------------------------------------------------


def test_material_is_costed_at_what_the_movement_actually_carried():
    """Not at standard cost: closure exists to find out what the job really cost."""
    component = _component(required="100")
    totals = IssuedComponentTotals(
        component_id=component.id,
        issued_quantity=Decimal("100"),
        returned_quantity=Decimal("0"),
        net_quantity=Decimal("100"),
        net_cost=Decimal("4237.50"),
    )
    line = material_cost_line(component, totals)
    assert line.total_cost == Decimal("4237.50")
    assert line.unit_cost == Decimal("42.375000")


def test_a_return_lowers_both_the_quantity_and_the_value():
    component = _component(required="100")
    totals = IssuedComponentTotals(
        component_id=component.id,
        issued_quantity=Decimal("100"),
        returned_quantity=Decimal("30"),
        net_quantity=Decimal("70"),
        net_cost=Decimal("2800.00"),
    )
    line = material_cost_line(component, totals)
    assert line.net_issued_quantity == Decimal("70.0000")
    assert line.total_cost == Decimal("2800.00")
    assert line.unit_cost == Decimal("40.000000")


def test_a_component_that_was_never_issued_costs_nothing_rather_than_standard():
    """A requirement is not a cost. Charging one would invent a spend."""
    line = material_cost_line(_component(required="50"), None)
    assert line.issued_quantity == Decimal("0")
    assert line.total_cost == Decimal("0")
    assert line.unit_cost == Decimal("0")


def test_a_fully_returned_component_reports_no_unit_cost_instead_of_dividing_by_zero():
    component = _component()
    totals = IssuedComponentTotals(
        component_id=component.id,
        issued_quantity=Decimal("40"),
        returned_quantity=Decimal("40"),
        net_quantity=Decimal("0"),
        net_cost=Decimal("0"),
    )
    line = material_cost_line(component, totals)
    assert line.unit_cost == Decimal("0")


# -----------------------------------------------------------------------------
# The total
# -----------------------------------------------------------------------------


def test_the_total_is_the_sum_of_the_printed_lines():
    """A breakdown that does not add up to its own total is a support ticket."""
    order = _order(planned="100", produced="96")
    component = _component()
    materials = [
        material_cost_line(
            component,
            IssuedComponentTotals(
                component_id=component.id,
                issued_quantity=Decimal("100"),
                returned_quantity=Decimal("0"),
                net_quantity=Decimal("100"),
                net_cost=Decimal("3333.33"),
            ),
        )
    ]
    operations = [
        operation_cost_line(
            _operation(
                started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
                ended=datetime(2026, 4, 1, 9, 20, tzinfo=UTC),
                cost_per_hour="450",
            )
        ),
        operation_cost_line(_operation(setup="15", run="45", cost_per_hour="300")),
    ]

    cost = summarise_cost(order=order, materials=materials, operations=operations, is_final=True)
    assert cost.material_cost == sum(line.total_cost for line in materials)
    assert cost.operation_cost == sum(line.total_cost for line in operations)
    assert cost.total_cost == cost.material_cost + cost.operation_cost


def test_cost_per_unit_divides_by_the_good_output_only():
    order = _order(planned="100", produced="80")
    component = _component()
    materials = [
        material_cost_line(
            component,
            IssuedComponentTotals(
                component_id=component.id,
                issued_quantity=Decimal("100"),
                returned_quantity=Decimal("0"),
                net_quantity=Decimal("100"),
                net_cost=Decimal("8000.00"),
            ),
        )
    ]
    cost = summarise_cost(order=order, materials=materials, operations=[], is_final=True)
    # The whole kit was consumed to make 80 good pieces, so each one carries 100.
    assert cost.cost_per_unit == Decimal("100.00")


def test_a_job_with_no_output_reports_no_unit_cost_rather_than_infinity():
    order = _order(planned="100", produced="0")
    cost = summarise_cost(order=order, materials=[], operations=[], is_final=False)
    assert cost.cost_per_unit is None
    assert cost.total_cost == Decimal("0.00")


def test_operations_costed_from_planned_time_are_counted_and_reported():
    """A planner reading a cost needs to know how much of it was estimated."""
    order = _order()
    operations = [
        operation_cost_line(_operation(setup="30", run="90")),
        operation_cost_line(
            _operation(
                started=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
                ended=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            )
        ),
    ]
    cost = summarise_cost(order=order, materials=[], operations=operations, is_final=True)
    assert cost.uncosted_operation_count == 1


def test_total_and_variance_include_subcontract_and_reconcile_to_standard():
    order = _order(planned="100", produced="80")
    order.standard_unit_cost = Decimal("100.0000")
    order.standard_material_unit_cost = Decimal("60.0000")
    order.standard_operation_unit_cost = Decimal("30.0000")
    order.standard_subcontract_unit_cost = Decimal("10.0000")
    component = _component()
    materials = [
        material_cost_line(
            component,
            IssuedComponentTotals(
                component_id=component.id,
                issued_quantity=Decimal("100"),
                returned_quantity=Decimal("0"),
                net_quantity=Decimal("100"),
                net_cost=Decimal("5000.00"),
            ),
        )
    ]
    operations = [operation_cost_line(_operation(setup="0", run="0", process_cost="2000"))]

    cost = summarise_cost(
        order=order,
        materials=materials,
        operations=operations,
        subcontract_cost=Decimal("900"),
        is_final=True,
    )

    assert cost.material_cost == Decimal("5000.00")
    assert cost.operation_cost == Decimal("2000.00")
    assert cost.subcontract_cost == Decimal("900.00")
    assert cost.total_cost == Decimal("7900.00")
    assert cost.actual_unit_cost == Decimal("98.75")
    assert cost.material_variance == Decimal("200.00")
    assert cost.operation_variance == Decimal("-400.00")
    assert cost.subcontract_variance == Decimal("100.00")
    assert cost.total_variance == Decimal("-100.00")
    assert (
        cost.material_variance + cost.operation_variance + cost.subcontract_variance
        == cost.total_variance
    )


# -----------------------------------------------------------------------------
# Backflush
# -----------------------------------------------------------------------------


def test_backflush_scales_to_what_was_actually_produced():
    """A job that made 80 of 100 consumed 80 units' worth of material."""
    assert backflush_quantity(
        required_quantity=Decimal("200"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("80"),
        already_issued=Decimal("0"),
    ) == Decimal("160.0000")


def test_backflush_deducts_what_was_already_issued_by_hand():
    assert backflush_quantity(
        required_quantity=Decimal("200"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("100"),
        already_issued=Decimal("150"),
    ) == Decimal("50.0000")


def test_backflush_never_goes_negative_when_more_was_issued_than_used():
    """Over-issued material comes back as a return, not as a negative issue."""
    assert backflush_quantity(
        required_quantity=Decimal("200"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("50"),
        already_issued=Decimal("180"),
    ) == Decimal("0")


def test_backflush_follows_output_above_plan_too():
    assert backflush_quantity(
        required_quantity=Decimal("100"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("105"),
        already_issued=Decimal("0"),
    ) == Decimal("105.0000")


# -----------------------------------------------------------------------------
# Tolerances
# -----------------------------------------------------------------------------


def test_issuing_up_to_the_tolerance_is_allowed():
    ceiling = issue_ceiling(Decimal("100"))
    assert ceiling == Decimal("100") * OVER_ISSUE_MULTIPLIER
    assert_within_over_issue_tolerance(
        item_code="RM-1",
        line_number=1,
        required_quantity=Decimal("100"),
        already_issued=Decimal("100"),
        incoming_quantity=Decimal("10"),
    )


def test_issuing_past_the_tolerance_is_refused_with_the_numbers_that_caused_it():
    with pytest.raises(BusinessRuleError) as exc:
        assert_within_over_issue_tolerance(
            item_code="RM-1",
            line_number=3,
            required_quantity=Decimal("100"),
            already_issued=Decimal("100"),
            incoming_quantity=Decimal("50"),
        )
    details = exc.value.details
    assert details["line_number"] == 3
    assert details["maximum_issuable"] == "110.0000"
    assert details["item_code"] == "RM-1"


def test_booking_output_within_the_tolerance_is_allowed():
    assert_within_over_production_tolerance(
        order_code="MO/1",
        planned_quantity=Decimal("100"),
        already_produced=Decimal("95"),
        incoming=Decimal("10"),
    )
    assert Decimal("100") * OVER_PRODUCTION_MULTIPLIER == Decimal("110.0")


def test_a_mis_keyed_output_quantity_is_refused_before_it_reaches_the_warehouse():
    with pytest.raises(BusinessRuleError) as exc:
        assert_within_over_production_tolerance(
            order_code="MO/1",
            planned_quantity=Decimal("100"),
            already_produced=Decimal("0"),
            incoming=Decimal("1000"),
        )
    assert exc.value.details["maximum_producible"] == "110.0000"
