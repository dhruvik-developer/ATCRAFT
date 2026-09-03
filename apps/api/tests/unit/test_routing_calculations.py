"""Routing time, cost and lead-time arithmetic.

These are the numbers MRP schedules from and production costs against, so the
tests pin the behaviour that is easy to get subtly wrong: setup charged once
rather than per unit, queue and move time counted as elapsed but not charged,
money kept in Decimal, and the lead time rounded **up** to whole days.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.routing.models import DEFAULT_WORKING_HOURS_PER_DAY
from src.modules.routing.repository import CostedOperation, CostedRouting
from src.modules.routing.service import (
    calculate_operation_cost,
    calculate_operation_time,
    calculate_routing_cost,
    chargeable_operation_time,
    cost_breakdown,
    resolve_working_hours_per_day,
    total_lead_time_days,
    total_routing_minutes,
)


def operation(
    *,
    sequence: int = 10,
    setup: str = "30",
    run: str = "1.5",
    queue: str = "20",
    move: str = "10",
    process_cost: str = "0",
    rate: str = "600",
    hours_per_day: str = "8",
    outsourced: bool = False,
) -> CostedOperation:
    return CostedOperation(
        sequence=sequence,
        operation_code=f"OP{sequence}",
        operation_name=f"Operation {sequence}",
        work_center_id=uuid.uuid4(),
        work_center_code=f"WC{sequence}",
        work_center_cost_per_hour=Decimal(rate),
        work_center_working_hours_per_day=Decimal(hours_per_day),
        setup_time_minutes=Decimal(setup),
        run_time_per_unit_minutes=Decimal(run),
        queue_time_minutes=Decimal(queue),
        move_time_minutes=Decimal(move),
        process_cost=Decimal(process_cost),
        is_outsourced=outsourced,
    )


def routing(*operations: CostedOperation) -> CostedRouting:
    return CostedRouting(
        id=uuid.uuid4(),
        code="RT-001",
        item_id=uuid.uuid4(),
        operations=tuple(operations),
    )


# -----------------------------------------------------------------------------
# Operation time
# -----------------------------------------------------------------------------


def test_operation_time_is_setup_plus_run_times_quantity_plus_queue_and_move() -> None:
    # 30 setup + 1.5 x 100 run + 20 queue + 10 move
    assert calculate_operation_time(operation(), Decimal("100")) == Decimal("210.0000")


def test_setup_queue_and_move_do_not_scale_with_quantity() -> None:
    """Doubling the batch must only double the *run* component.

    This is the whole reason a bigger batch costs less per piece; if setup were
    multiplied too, the cost per unit would be flat and batching would look
    pointless.
    """
    one = calculate_operation_time(operation(), Decimal("1"))
    two = calculate_operation_time(operation(), Decimal("2"))

    assert two - one == Decimal("1.5")


def test_a_zero_quantity_still_costs_the_fixed_time() -> None:
    # Setting up and moving a job happens whether or not a piece is made.
    assert calculate_operation_time(operation(), Decimal("0")) == Decimal("60.0000")


def test_negative_quantity_is_rejected_rather_than_producing_negative_time() -> None:
    with pytest.raises(ValueError, match="negative"):
        calculate_operation_time(operation(), Decimal("-1"))


def test_fractional_quantities_stay_exact() -> None:
    """Decimal, never float: 0.1 x 3 must be 0.3, not 0.30000000000000004."""
    step = operation(setup="0", run="0.1", queue="0", move="0")

    assert calculate_operation_time(step, Decimal("3")) == Decimal("0.3000")


# -----------------------------------------------------------------------------
# Charged time and cost
# -----------------------------------------------------------------------------


def test_queue_and_move_time_are_elapsed_but_not_charged() -> None:
    step = operation()
    quantity = Decimal("100")

    elapsed = calculate_operation_time(step, quantity)
    charged = chargeable_operation_time(step, quantity)

    assert elapsed == Decimal("210.0000")
    assert charged == Decimal("180.0000")
    # The 30 minutes of waiting is real time, but nobody is working on the part.
    assert elapsed - charged == step.queue_time_minutes + step.move_time_minutes


def test_operation_cost_is_rate_times_charged_hours_plus_process_cost() -> None:
    step = operation(process_cost="250")

    # 180 charged minutes = 3 hours at 600/hour = 1800, plus a flat 250.
    assert calculate_operation_cost(step, Decimal("100")) == Decimal("2050.00")


def test_routing_cost_sums_every_operation() -> None:
    plan = routing(
        operation(sequence=10, process_cost="250"),
        operation(sequence=20, setup="15", run="0.5", rate="300", process_cost="0"),
    )

    # 2050 for the first step; second: (15 + 50) / 60 x 300 = 325.
    assert calculate_routing_cost(plan, Decimal("100")) == Decimal("2375.00")


def test_a_routing_with_no_operations_costs_nothing() -> None:
    assert calculate_routing_cost(routing(), Decimal("100")) == Decimal("0")


def test_cost_per_unit_falls_as_setup_is_amortised() -> None:
    plan = routing(operation(process_cost="0"))

    ten = cost_breakdown(plan, Decimal("10")).cost_per_unit
    thousand = cost_breakdown(plan, Decimal("1000")).cost_per_unit

    assert ten > thousand


def test_the_cost_breakdown_lines_add_up_to_the_total() -> None:
    """Per-line rounding, then a sum - never the reverse.

    A total rounded independently of its lines is the classic invoice defect:
    the printed rows add up to a different number than the printed total.
    """
    plan = routing(
        operation(sequence=10, run="0.333", process_cost="10.005"),
        operation(sequence=20, run="0.777", rate="455.55"),
        operation(sequence=30, setup="7", run="0.1", rate="123.45", process_cost="0.005"),
    )

    result = cost_breakdown(plan, Decimal("7"))

    assert sum(line.total_cost for line in result.operations) == result.total_cost
    assert result.time_cost + result.process_cost == result.total_cost


def test_an_outsourced_step_is_flagged_in_the_breakdown() -> None:
    plan = routing(operation(sequence=10), operation(sequence=20, outsourced=True))

    result = cost_breakdown(plan, Decimal("1"))

    assert [line.is_outsourced for line in result.operations] == [False, True]


# -----------------------------------------------------------------------------
# Lead time
# -----------------------------------------------------------------------------


def test_total_minutes_sums_elapsed_time_across_operations() -> None:
    plan = routing(operation(sequence=10), operation(sequence=20, setup="0", run="0.5"))

    # 210 for the first step, plus 0 + 50 + 20 + 10 for the second.
    assert total_routing_minutes(plan, Decimal("100")) == Decimal("290.0000")


def test_a_part_day_of_work_still_occupies_a_whole_day() -> None:
    """The rounding rule the whole schedule depends on.

    8.1 hours of work does not fit in an 8-hour day, so the job runs into a
    second one. Rounding to nearest would call this "one day" and release the
    order too late, every time.
    """
    plan = routing(operation(setup="486", run="0", queue="0", move="0"))

    assert total_lead_time_days(plan, Decimal("1"), Decimal("8")) == 2


def test_an_exact_day_is_not_rounded_up_to_two() -> None:
    plan = routing(operation(setup="480", run="0", queue="0", move="0"))

    assert total_lead_time_days(plan, Decimal("1"), Decimal("8")) == 1


def test_even_a_single_minute_of_work_takes_a_day() -> None:
    plan = routing(operation(setup="1", run="0", queue="0", move="0"))

    assert total_lead_time_days(plan, Decimal("1"), Decimal("8")) == 1


def test_a_routing_with_no_work_takes_no_days() -> None:
    # Not one day: a routing that describes nothing must not add a day to every
    # plan that touches it.
    assert total_lead_time_days(routing(), Decimal("100")) == 0


def test_lead_time_uses_the_shortest_shift_among_the_work_centres() -> None:
    """The bottleneck shift, not an average and not the longest.

    Grinding may run 16 hours a day, but if plating only runs 8 the job waits
    for plating. Taking the longer shift would promise a date the plant misses.
    """
    plan = routing(
        operation(sequence=10, setup="480", run="0", queue="0", move="0", hours_per_day="16"),
        operation(sequence=20, setup="0", run="0", queue="0", move="0", hours_per_day="8"),
    )

    assert resolve_working_hours_per_day(plan) == Decimal("8")
    assert total_lead_time_days(plan, Decimal("1")) == 1
    # The same 8 hours against the 16-hour shift would have been half a day.
    assert total_lead_time_days(plan, Decimal("1"), Decimal("16")) == 1


def test_an_unrated_routing_falls_back_to_the_default_shift() -> None:
    assert resolve_working_hours_per_day(routing()) == DEFAULT_WORKING_HOURS_PER_DAY


def test_a_zero_length_working_day_is_rejected_rather_than_dividing_by_zero() -> None:
    plan = routing(operation())

    with pytest.raises(ValueError, match="greater than zero"):
        total_lead_time_days(plan, Decimal("1"), Decimal("0"))


def test_lead_time_grows_with_quantity() -> None:
    plan = routing(operation(setup="0", run="60", queue="0", move="0"))

    assert total_lead_time_days(plan, Decimal("8"), Decimal("8")) == 1
    assert total_lead_time_days(plan, Decimal("9"), Decimal("8")) == 2


def test_the_breakdown_reports_the_same_lead_time_as_the_direct_call() -> None:
    plan = routing(operation(sequence=10), operation(sequence=20, hours_per_day="6"))
    quantity = Decimal("250")

    result = cost_breakdown(plan, quantity)

    assert result.working_hours_per_day == Decimal("6")
    assert result.lead_time_days == total_lead_time_days(plan, quantity, Decimal("6"))
    assert result.total_minutes == total_routing_minutes(plan, quantity)
