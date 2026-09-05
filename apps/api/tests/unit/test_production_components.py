"""The release-time component snapshot, and the planning that goes with it.

The snapshot is the load-bearing idea of the module: what a released job needs is
decided once, from the structure as it stood at that moment, and never re-derived
afterwards. These tests pin the two halves of that - what gets written, and how
much of it can be held - without a database, so the rules can be read as rules.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.modules.bom.engine import BomExplosion, ExplodedComponent
from src.modules.production.models import ProductionOrderComponent
from src.modules.production.repository import ProductionItem
from src.modules.production.service import (
    mandatory_components_covered,
    net_issued,
    outstanding_requirement,
    plan_components,
    plan_reservation,
    plan_work_orders,
)

pytestmark = pytest.mark.unit

ROOT_ITEM = uuid.uuid4()
SCREW = uuid.uuid4()
BODY = uuid.uuid4()
UOM = uuid.uuid4()
OUTPUT_WAREHOUSE = uuid.uuid4()
STORE_WAREHOUSE = uuid.uuid4()
ITEM_DEFAULT_WAREHOUSE = uuid.uuid4()


def _exploded(
    item_id: uuid.UUID,
    code: str,
    *,
    total: str,
    scrap_adjusted: str,
    level: int = 1,
) -> ExplodedComponent:
    return ExplodedComponent(
        item_id=item_id,
        item_code=code,
        item_name=f"Item {code}",
        total_quantity=Decimal(total),
        scrap_adjusted_quantity=Decimal(scrap_adjusted),
        uom_id=UOM,
        level=level,
        is_purchased=True,
        is_manufactured=False,
        source_paths=(("KIT", code),),
        source_path_count=1,
    )


def _explosion(*components: ExplodedComponent) -> BomExplosion:
    return BomExplosion(
        root_item_id=ROOT_ITEM,
        root_item_code="KIT",
        bom_id=uuid.uuid4(),
        bom_code="BOM/00001",
        root_bom_status="APPROVED",
        quantity=Decimal("100"),
        as_of=datetime(2026, 4, 1, tzinfo=UTC).date(),
        max_level_reached=2,
        phantom_item_codes=(),
        components=components,
    )


def _item(
    item_id: uuid.UUID,
    code: str,
    *,
    default_warehouse_id: uuid.UUID | None = None,
    batch_tracked: bool = False,
    serial_tracked: bool = False,
) -> ProductionItem:
    return ProductionItem(
        id=item_id,
        code=code,
        name=f"Item {code}",
        base_uom_id=UOM,
        can_manufacture=False,
        is_active=True,
        is_batch_tracked=batch_tracked,
        is_serial_tracked=serial_tracked,
        standard_cost=Decimal("40"),
        default_warehouse_id=default_warehouse_id,
    )


def _component(
    *,
    required: str,
    issued: str = "0",
    returned: str = "0",
    backflushed: bool = False,
) -> ProductionOrderComponent:
    return ProductionOrderComponent(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        item_code="RM-1",
        required_quantity=Decimal(required),
        issued_quantity=Decimal(issued),
        returned_quantity=Decimal(returned),
        consumed_quantity=Decimal("0"),
        is_backflushed=backflushed,
    )


# -----------------------------------------------------------------------------
# The snapshot
# -----------------------------------------------------------------------------


def test_the_snapshot_takes_the_scrap_adjusted_quantity_not_the_net_one():
    """The allowance is consumed by the process.

    Planning against the un-inflated figure is how a job runs short on its last
    piece, so the snapshot stores the number to actually pick.
    """
    explosion = _explosion(
        _exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="630"),
    )
    plans = plan_components(
        explosion,
        items={SCREW: _item(SCREW, "SCREW-M4")},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset(),
    )
    assert [p.required_quantity for p in plans] == [Decimal("630.0000")]


def test_the_snapshot_records_the_item_code_and_level_it_was_exploded_at():
    """A code corrected in the master later must not rewrite the picked kit list."""
    explosion = _explosion(
        _exploded(BODY, "BODY-1", total="100", scrap_adjusted="100", level=1),
        _exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600", level=2),
    )
    plans = plan_components(
        explosion,
        items={BODY: _item(BODY, "BODY-1"), SCREW: _item(SCREW, "SCREW-M4")},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset(),
    )
    assert [(p.item_code, p.bom_level) for p in plans] == [("BODY-1", 1), ("SCREW-M4", 2)]
    # Line numbers follow the explosion's own ordering, so the kit list reads as
    # an indented structure rather than in insertion order.
    assert [p.line_number for p in plans] == [1, 2]


def test_a_common_part_arrives_as_one_line_because_the_explosion_consolidated_it():
    """Consolidation is the BOM engine's job; the snapshot must not undo it.

    Asserted here because a snapshot that split a consolidated requirement back
    into per-branch rows would reserve, issue and cost the same screw twice.
    """
    explosion = _explosion(_exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"))
    plans = plan_components(
        explosion,
        items={SCREW: _item(SCREW, "SCREW-M4")},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset(),
    )
    assert len(plans) == 1
    assert plans[0].required_quantity == Decimal("600.0000")


def test_the_release_named_store_wins_over_the_items_own_default():
    explosion = _explosion(_exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"))
    plans = plan_components(
        explosion,
        items={SCREW: _item(SCREW, "SCREW-M4", default_warehouse_id=ITEM_DEFAULT_WAREHOUSE)},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=STORE_WAREHOUSE,
        backflush_item_ids=frozenset(),
    )
    assert plans[0].warehouse_id == STORE_WAREHOUSE


def test_an_items_default_store_is_used_when_the_release_names_none():
    explosion = _explosion(_exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"))
    plans = plan_components(
        explosion,
        items={SCREW: _item(SCREW, "SCREW-M4", default_warehouse_id=ITEM_DEFAULT_WAREHOUSE)},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset(),
    )
    assert plans[0].warehouse_id == ITEM_DEFAULT_WAREHOUSE


def test_a_component_with_no_home_falls_back_to_the_jobs_own_warehouse():
    explosion = _explosion(_exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"))
    plans = plan_components(
        explosion,
        items={SCREW: _item(SCREW, "SCREW-M4")},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset(),
    )
    assert plans[0].warehouse_id == OUTPUT_WAREHOUSE


def test_only_the_named_items_are_flagged_for_backflush():
    explosion = _explosion(
        _exploded(BODY, "BODY-1", total="100", scrap_adjusted="100"),
        _exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"),
    )
    plans = plan_components(
        explosion,
        items={BODY: _item(BODY, "BODY-1"), SCREW: _item(SCREW, "SCREW-M4")},
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset({SCREW}),
    )
    assert {p.item_code: p.is_backflushed for p in plans} == {
        "BODY-1": False,
        "SCREW-M4": True,
    }


@pytest.mark.parametrize(("batch_tracked", "serial_tracked"), [(True, False), (False, True)])
def test_a_traced_item_is_never_backflushed_even_when_it_is_named(batch_tracked, serial_tracked):
    """A backflush posts a movement nobody is watching.

    Every movement of a traced item needs a lot or a serial, and there is nothing
    to pick one automatically from - so the request is refused silently at the
    plan rather than exploding later inside the inventory engine.
    """
    explosion = _explosion(_exploded(SCREW, "SCREW-M4", total="600", scrap_adjusted="600"))
    plans = plan_components(
        explosion,
        items={
            SCREW: _item(
                SCREW,
                "SCREW-M4",
                batch_tracked=batch_tracked,
                serial_tracked=serial_tracked,
            )
        },
        fallback_warehouse_id=OUTPUT_WAREHOUSE,
        component_warehouse_id=None,
        backflush_item_ids=frozenset({SCREW}),
    )
    assert plans[0].is_backflushed is False


# -----------------------------------------------------------------------------
# Reservation planning
# -----------------------------------------------------------------------------


def test_a_fully_covered_component_is_held_in_full_and_reports_no_shortage():
    assert plan_reservation(Decimal("100"), Decimal("250")) == (
        Decimal("100.0000"),
        Decimal("0"),
    )


def test_a_partly_covered_component_holds_what_is_there_and_reports_the_rest():
    """Holding the available part is what stops somebody else taking it."""
    reserve, shortage = plan_reservation(Decimal("100"), Decimal("40"))
    assert reserve == Decimal("40.0000")
    assert shortage == Decimal("60.0000")


def test_a_component_with_nothing_free_reserves_nothing_and_is_wholly_short():
    assert plan_reservation(Decimal("100"), Decimal("0")) == (
        Decimal("0"),
        Decimal("100.0000"),
    )


def test_negative_availability_is_treated_as_nothing_free_not_as_a_credit():
    """``available_quantity`` can go negative where the warehouse allows it."""
    reserve, shortage = plan_reservation(Decimal("100"), Decimal("-25"))
    assert reserve == Decimal("0")
    assert shortage == Decimal("100.0000")


# -----------------------------------------------------------------------------
# Issue coverage
# -----------------------------------------------------------------------------


def test_net_issued_is_what_the_job_is_holding_not_what_it_was_given():
    component = _component(required="100", issued="100", returned="30")
    assert net_issued(component) == Decimal("70.0000")
    assert outstanding_requirement(component) == Decimal("30.0000")


def test_an_over_issued_component_has_no_negative_outstanding_requirement():
    component = _component(required="100", issued="120")
    assert outstanding_requirement(component) == Decimal("0")


def test_the_order_only_advances_when_every_mandatory_component_is_covered():
    covered = _component(required="100", issued="100")
    short = _component(required="50", issued="20")
    assert mandatory_components_covered([covered]) is True
    assert mandatory_components_covered([covered, short]) is False


def test_backflushed_components_do_not_hold_the_order_back():
    """They are issued automatically at completion.

    Waiting for them would leave a job that can never reach MATERIAL_ISSUED.
    """
    picked = _component(required="100", issued="100")
    automatic = _component(required="600", issued="0", backflushed=True)
    assert mandatory_components_covered([picked, automatic]) is True


def test_a_returned_component_stops_counting_as_covered():
    """Material sent back to the store is no longer with the job."""
    component = _component(required="100", issued="100", returned="1")
    assert mandatory_components_covered([component]) is False


# -----------------------------------------------------------------------------
# Work-order scheduling
# -----------------------------------------------------------------------------


class _Step:
    """Minimal stand-in for a routing operation.

    A plain object rather than the ORM row: the scheduling function is pure, and
    it can only be proved pure by exercising it without one.
    """

    def __init__(
        self,
        sequence: int,
        *,
        setup: str = "30",
        run_per_unit: str = "1",
        queue: str = "0",
        move: str = "0",
        process_cost: str = "0",
        machine_id: uuid.UUID | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.sequence = sequence
        self.operation_code = f"OP{sequence}"
        self.operation_name = f"Operation {sequence}"
        self.work_center_id = uuid.uuid4()
        self.machine_id = machine_id
        self.setup_time_minutes = Decimal(setup)
        self.run_time_per_unit_minutes = Decimal(run_per_unit)
        self.queue_time_minutes = Decimal(queue)
        self.move_time_minutes = Decimal(move)
        self.process_cost = Decimal(process_cost)


def test_run_time_is_extended_by_the_order_quantity_but_setup_is_not():
    """The whole reason a bigger batch costs less per piece."""
    plans = plan_work_orders(
        [_Step(10, setup="30", run_per_unit="2")],
        planned_quantity=Decimal("50"),
        start_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
    )
    assert plans[0].planned_setup_minutes == Decimal("30.0000")
    assert plans[0].planned_run_minutes == Decimal("100.0000")


def test_operations_are_scheduled_back_to_back_in_sequence_order():
    plans = plan_work_orders(
        [_Step(20, setup="0", run_per_unit="1"), _Step(10, setup="60", run_per_unit="1")],
        planned_quantity=Decimal("60"),
        start_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
    )
    assert [p.sequence for p in plans] == [10, 20]
    # 60 setup + 60 run = 2 hours, then the second step starts.
    assert plans[0].planned_end_at == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    assert plans[1].planned_start_at == plans[0].planned_end_at
    assert plans[1].planned_end_at == datetime(2026, 4, 1, 11, 0, tzinfo=UTC)


def test_queue_and_move_time_delay_the_next_step_without_being_charged():
    """Elapsed but not chargeable - the routing module's own split."""
    plans = plan_work_orders(
        [_Step(10, setup="0", run_per_unit="1", queue="30", move="30")],
        planned_quantity=Decimal("60"),
        start_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
    )
    assert plans[0].planned_run_minutes == Decimal("60.0000")
    assert plans[0].planned_setup_minutes == Decimal("0")
    # 60 run + 30 queue + 30 move of elapsed time.
    assert plans[0].planned_end_at == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)


def test_the_routing_operation_and_its_machine_are_carried_onto_the_work_order():
    machine = uuid.uuid4()
    step = _Step(10, machine_id=machine)
    plans = plan_work_orders(
        [step], planned_quantity=Decimal("10"), start_at=datetime(2026, 4, 1, tzinfo=UTC)
    )
    assert plans[0].routing_operation_id == step.id
    assert plans[0].machine_id == machine
    assert plans[0].work_center_id == step.work_center_id


def test_a_job_with_no_routing_gets_no_work_orders():
    assert plan_work_orders([], planned_quantity=Decimal("10"), start_at=datetime.now(UTC)) == []
