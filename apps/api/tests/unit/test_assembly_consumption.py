"""Assembly consumption arithmetic and serial traceability.

The Lock Set from master rules section 19 is the worked example throughout: a
Handle, a Cylinder, a Lock body, a set of Keys and a Screw kit go in, one Lock
Set comes out. What is under test is what the plant actually ate, and whether
the units it ate can still be named afterwards.

No database: every function here is pure by construction, which is the point of
keeping the arithmetic out of the service's transaction block.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ValidationError
from src.modules.assembly.models import AssemblyOrder, AssemblyOrderComponent
from src.modules.assembly.service import (
    build_movements,
    consumption_for,
    is_whole_units,
    plan_consumption,
    requirement_for,
)
from src.modules.inventory.models import StockState, TransactionType

COMPONENT_WAREHOUSE = uuid.uuid4()
OUTPUT_WAREHOUSE = uuid.uuid4()
LOCK_SET = uuid.uuid4()


def component(
    line_number: int,
    *,
    required: str,
    issued: str | None = None,
    serialised: bool = False,
    item_id: uuid.UUID | None = None,
) -> AssemblyOrderComponent:
    """An in-memory component row. Never flushed, so defaults are set here."""
    return AssemblyOrderComponent(
        id=uuid.uuid4(),
        assembly_order_id=uuid.uuid4(),
        line_number=line_number,
        item_id=item_id or uuid.uuid4(),
        uom_id=uuid.uuid4(),
        required_quantity=Decimal(required),
        issued_quantity=Decimal(issued if issued is not None else required),
        consumed_quantity=Decimal("0"),
        is_serial_tracked_component=serialised,
        consumed_serial_ids=[],
    )


def order(*, assembled: str, requires_qc: bool = False) -> AssemblyOrder:
    return AssemblyOrder(
        id=uuid.uuid4(),
        code="AO/2026-27/00001",
        item_id=LOCK_SET,
        bom_id=uuid.uuid4(),
        planned_quantity=Decimal("100"),
        assembled_quantity=Decimal(assembled),
        rejected_quantity=Decimal("0"),
        warehouse_id=COMPONENT_WAREHOUSE,
        output_warehouse_id=OUTPUT_WAREHOUSE,
        requires_qc=requires_qc,
    )


# -----------------------------------------------------------------------------
# Requirement: BOM quantity scaled to the order
# -----------------------------------------------------------------------------


def test_a_one_per_component_requires_one_per_planned_unit() -> None:
    assert requirement_for(
        planned_quantity=Decimal("100"),
        quantity_per=Decimal("1"),
        output_quantity=Decimal("1"),
        scrap_percentage=Decimal("0"),
    ) == Decimal("100.0000")


def test_quantity_per_is_stated_per_run_not_per_unit() -> None:
    # A structure that yields 10 lock sets from 2 key blanks needs 20 blanks for
    # 100 sets, not 200.
    assert requirement_for(
        planned_quantity=Decimal("100"),
        quantity_per=Decimal("2"),
        output_quantity=Decimal("10"),
        scrap_percentage=Decimal("0"),
    ) == Decimal("20.0000")


def test_scrap_inflates_the_requirement_because_the_process_eats_it() -> None:
    # 4 screws per set, 100 sets, 5% process loss.
    assert requirement_for(
        planned_quantity=Decimal("100"),
        quantity_per=Decimal("4"),
        output_quantity=Decimal("1"),
        scrap_percentage=Decimal("5"),
    ) == Decimal("420.0000")


# -----------------------------------------------------------------------------
# Consumption: requirement scaled to what was actually built
# -----------------------------------------------------------------------------


def test_a_full_run_consumes_exactly_the_requirement() -> None:
    # The identity that keeps a normal completion free of rounding drift: no
    # multiply-then-divide, so 420 screws for 100 sets consumes 420, not 419.9999.
    assert consumption_for(
        required_quantity=Decimal("420"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("100"),
    ) == Decimal("420.0000")


def test_a_short_run_consumes_proportionally() -> None:
    assert consumption_for(
        required_quantity=Decimal("400"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("25"),
    ) == Decimal("100.0000")


def test_rejected_output_ate_its_components_too() -> None:
    # 90 good + 5 rejected out of 100 planned consumed 95/100 of every part: the
    # rejected lock sets swallowed their cylinders exactly as the good ones did.
    consumed = consumption_for(
        required_quantity=Decimal("100"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("90") + Decimal("5"),
    )
    assert consumed == Decimal("95.0000")


def test_a_run_that_built_nothing_consumes_nothing() -> None:
    assert consumption_for(
        required_quantity=Decimal("100"),
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("0"),
    ) == Decimal("0.0000")


def test_a_zero_planned_quantity_is_refused_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValidationError):
        consumption_for(
            required_quantity=Decimal("10"),
            planned_quantity=Decimal("0"),
            produced_quantity=Decimal("0"),
        )


@pytest.mark.parametrize(
    ("value", "whole"),
    [("3", True), ("3.0000", True), ("3.5", False), ("0", True), ("0.0001", False)],
)
def test_whole_unit_detection(value: str, whole: bool) -> None:
    assert is_whole_units(Decimal(value)) is whole


# -----------------------------------------------------------------------------
# plan_consumption: the three rules it enforces
# -----------------------------------------------------------------------------


def test_the_plan_covers_every_component_of_a_full_run() -> None:
    components = [
        component(1, required="100"),  # Handle
        component(2, required="100"),  # Cylinder
        component(3, required="100"),  # Lock body
        component(4, required="200"),  # Keys, two per set
        component(5, required="420"),  # Screw kit, with scrap
    ]

    plan = plan_consumption(
        components,
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("100"),
        serials_by_line={},
    )

    assert [entry.quantity for entry in plan] == [
        Decimal("100.0000"),
        Decimal("100.0000"),
        Decimal("100.0000"),
        Decimal("200.0000"),
        Decimal("420.0000"),
    ]


def test_consuming_more_than_was_issued_is_refused_not_clamped() -> None:
    # Only 40 handles were kitted, but a full run of 100 would need 100. The
    # difference is either a mis-keyed quantity or parts nobody issued.
    components = [component(1, required="100", issued="40")]

    with pytest.raises(BusinessRuleError) as caught:
        plan_consumption(
            components,
            planned_quantity=Decimal("100"),
            produced_quantity=Decimal("100"),
            serials_by_line={},
        )

    assert caught.value.details["issued"] == "40.0000"
    assert caught.value.details["required"] == "100.0000"


def test_a_short_issue_is_fine_for_the_run_it_actually_covers() -> None:
    components = [component(1, required="100", issued="40")]

    plan = plan_consumption(
        components,
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("40"),
        serials_by_line={},
    )

    assert plan[0].quantity == Decimal("40.0000")


# -----------------------------------------------------------------------------
# Serial traceability
# -----------------------------------------------------------------------------


def test_a_serialised_component_records_the_exact_units_it_ate() -> None:
    cylinders = [uuid.uuid4() for _ in range(3)]
    components = [component(1, required="3", serialised=True)]

    plan = plan_consumption(
        components,
        planned_quantity=Decimal("3"),
        produced_quantity=Decimal("3"),
        serials_by_line={1: cylinders},
    )

    assert plan[0].quantity == Decimal("3.0000")
    assert plan[0].serial_ids == tuple(cylinders)


def test_a_serialised_component_must_name_one_serial_per_unit() -> None:
    components = [component(1, required="3", serialised=True)]

    with pytest.raises(BusinessRuleError) as caught:
        plan_consumption(
            components,
            planned_quantity=Decimal("3"),
            produced_quantity=Decimal("3"),
            serials_by_line={1: [uuid.uuid4(), uuid.uuid4()]},
        )

    assert caught.value.details["expected_serials"] == "3.0000"
    assert caught.value.details["supplied_serials"] == 2


def test_a_serialised_component_may_not_consume_a_part_unit() -> None:
    # Half a cylinder is not a quantity; it is a data error.
    components = [component(1, required="3", serialised=True)]

    with pytest.raises(BusinessRuleError, match="part unit"):
        plan_consumption(
            components,
            planned_quantity=Decimal("2"),
            produced_quantity=Decimal("1"),
            serials_by_line={1: [uuid.uuid4()]},
        )


def test_a_plain_component_cannot_name_serial_numbers() -> None:
    components = [component(1, required="4")]

    with pytest.raises(BusinessRuleError, match="not serial tracked"):
        plan_consumption(
            components,
            planned_quantity=Decimal("4"),
            produced_quantity=Decimal("4"),
            serials_by_line={1: [uuid.uuid4()]},
        )


def test_a_serialised_component_that_consumed_nothing_names_nothing() -> None:
    components = [component(1, required="3", serialised=True)]

    plan = plan_consumption(
        components,
        planned_quantity=Decimal("3"),
        produced_quantity=Decimal("0"),
        serials_by_line={},
    )

    assert plan[0].quantity == Decimal("0.0000")
    assert plan[0].serial_ids == ()


# -----------------------------------------------------------------------------
# build_movements: what actually reaches the ledger
# -----------------------------------------------------------------------------


def test_completion_posts_a_consume_per_component_and_one_receipt() -> None:
    components = [component(1, required="100"), component(2, required="200")]
    plan = plan_consumption(
        components,
        planned_quantity=Decimal("100"),
        produced_quantity=Decimal("100"),
        serials_by_line={},
    )

    movements = build_movements(
        plan, order=order(assembled="100"), reason="Night shift", occurred_at=None
    )

    consumes = [m for m in movements if m.transaction_type is TransactionType.ASSEMBLY_CONSUME]
    receipts = [m for m in movements if m.transaction_type is TransactionType.ASSEMBLY_RECEIPT]

    assert len(consumes) == 2
    assert len(receipts) == 1
    # Components leave the source warehouse; the assembly arrives in the output one.
    assert {m.warehouse_id for m in consumes} == {COMPONENT_WAREHOUSE}
    assert receipts[0].warehouse_id == OUTPUT_WAREHOUSE
    assert receipts[0].item_id == LOCK_SET
    assert receipts[0].quantity == Decimal("100.0000")


def test_a_serialised_component_posts_one_movement_of_one_per_serial() -> None:
    # The inventory engine refuses any other quantity against a serial, because
    # a serial identifies exactly one physical unit.
    cylinders = [uuid.uuid4() for _ in range(3)]
    plan = plan_consumption(
        [component(1, required="3", serialised=True)],
        planned_quantity=Decimal("3"),
        produced_quantity=Decimal("3"),
        serials_by_line={1: cylinders},
    )

    movements = build_movements(
        plan, order=order(assembled="3"), reason="Traced run", occurred_at=None
    )

    consumes = [m for m in movements if m.transaction_type is TransactionType.ASSEMBLY_CONSUME]
    assert len(consumes) == 3
    assert all(m.quantity == Decimal("1") for m in consumes)
    assert [m.serial_id for m in consumes] == cylinders


def test_an_order_that_needs_inspection_receives_its_output_onto_qc_hold() -> None:
    plan = plan_consumption(
        [component(1, required="10")],
        planned_quantity=Decimal("10"),
        produced_quantity=Decimal("10"),
        serials_by_line={},
    )

    movements = build_movements(
        plan,
        order=order(assembled="10", requires_qc=True),
        reason="Held for inspection",
        occurred_at=None,
    )

    receipt = next(m for m in movements if m.transaction_type is TransactionType.ASSEMBLY_RECEIPT)
    assert receipt.stock_state is StockState.QC_HOLD


def test_an_ordinary_order_receives_its_output_as_available() -> None:
    plan = plan_consumption(
        [component(1, required="10")],
        planned_quantity=Decimal("10"),
        produced_quantity=Decimal("10"),
        serials_by_line={},
    )

    movements = build_movements(
        plan, order=order(assembled="10"), reason="Straight through", occurred_at=None
    )

    receipt = next(m for m in movements if m.transaction_type is TransactionType.ASSEMBLY_RECEIPT)
    assert receipt.stock_state is StockState.AVAILABLE


def test_a_wholly_rejected_run_consumes_components_but_receives_no_stock() -> None:
    # The parts are gone; a failed assembly is not a saleable unit and must not
    # land in the finished-goods balance.
    plan = plan_consumption(
        [component(1, required="10")],
        planned_quantity=Decimal("10"),
        produced_quantity=Decimal("10"),
        serials_by_line={},
    )

    movements = build_movements(
        plan, order=order(assembled="0"), reason="All rejected", occurred_at=None
    )

    assert [m.transaction_type for m in movements] == [TransactionType.ASSEMBLY_CONSUME]
    assert movements[0].quantity == Decimal("10.0000")


def test_every_movement_points_back_at_the_assembly_order() -> None:
    built = order(assembled="10")
    plan = plan_consumption(
        [component(1, required="10")],
        planned_quantity=Decimal("10"),
        produced_quantity=Decimal("10"),
        serials_by_line={},
    )

    movements = build_movements(plan, order=built, reason="Traceable", occurred_at=None)

    assert all(m.reference_id == built.id for m in movements)
    assert all(m.reference_code == built.code for m in movements)
    # Section 83: no stock event without a stated reason.
    assert all(m.reason for m in movements)
