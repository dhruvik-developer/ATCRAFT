"""The dispatch state machine and the movement expansion it guards.

The rules under test are the ones that keep the ledger, the customer and the
paperwork in agreement: a consignment cannot be delivered before it leaves, it
cannot be cancelled after stock has moved, and the receiver's booking-in sequence
can only be walked in the order the receiver actually walks it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

import pytest

from src.core.errors import BusinessRuleError, ErrorCode, InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.dispatch.models import (
    DELIVERED_STATUSES,
    EDITABLE_STATUSES,
    IN_FLIGHT_STATUSES,
    DispatchLine,
    DispatchStatus,
)
from src.modules.dispatch.repository import DispatchItem
from src.modules.dispatch.service import DISPATCH_LIFECYCLE as FLOW
from src.modules.dispatch.service import (
    DISPATCHABLE_ORDER_STATUSES,
    assert_order_dispatchable,
    build_line_movements,
)
from src.modules.inventory.models import TransactionType
from src.modules.sales.models import SalesOrderStatus
from src.modules.sales.service import SALES_ORDER_LIFECYCLE as SALES_FLOW

# The happy path, as a sequence, so the tests below can walk it.
HAPPY_PATH = (
    DispatchStatus.NOT_DISPATCHED,
    DispatchStatus.READY_FOR_DISPATCH,
    DispatchStatus.DISPATCHED,
    DispatchStatus.IN_TRANSIT,
    DispatchStatus.DELIVERED,
    DispatchStatus.QC_PENDING,
    DispatchStatus.RECEIVED,
    DispatchStatus.QC_COMPLETED,
    DispatchStatus.GRN_COMPLETED,
)


# -----------------------------------------------------------------------------
# The state graph
# -----------------------------------------------------------------------------


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    FLOW.validate()


def test_the_whole_database_md_section_7_flow_is_reachable() -> None:
    for current, target in pairwise(HAPPY_PATH):
        assert FLOW.can_transition(current, target), f"{current} -> {target} is missing"


def test_the_terminal_states_are_the_only_ones_with_no_exit() -> None:
    for state in DispatchStatus:
        has_exit = bool(FLOW.allowed_targets(state))
        assert has_exit is not FLOW.is_terminal(state), state


def test_the_initial_state_is_not_dispatched() -> None:
    assert FLOW.initial is DispatchStatus.NOT_DISPATCHED


# -----------------------------------------------------------------------------
# What must not be possible
# -----------------------------------------------------------------------------


def test_a_consignment_cannot_be_delivered_before_it_leaves() -> None:
    for state in (DispatchStatus.NOT_DISPATCHED, DispatchStatus.READY_FOR_DISPATCH):
        with pytest.raises(InvalidStateTransitionError):
            FLOW.assert_transition(state, DispatchStatus.DELIVERED)


def test_a_consignment_cannot_be_dispatched_twice() -> None:
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(DispatchStatus.DISPATCHED, DispatchStatus.DISPATCHED)


def test_a_dispatched_consignment_cannot_be_cancelled() -> None:
    # The ledger records goods that left; a document claiming never to have
    # existed would contradict it.
    for state in DispatchStatus:
        if state in EDITABLE_STATUSES:
            continue
        assert not FLOW.can_transition(state, DispatchStatus.CANCELLED), state


def test_cancellation_is_available_from_exactly_the_editable_statuses() -> None:
    for state in EDITABLE_STATUSES:
        assert FLOW.can_transition(state, DispatchStatus.CANCELLED)


def test_the_receivers_sequence_cannot_be_skipped() -> None:
    # A courier feed reporting "booked in" for something nobody has received is
    # either a bug or a different consignment; either way it must not be applied.
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(DispatchStatus.DELIVERED, DispatchStatus.GRN_COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(DispatchStatus.QC_PENDING, DispatchStatus.GRN_COMPLETED)


def test_nothing_moves_backwards_out_of_delivered() -> None:
    for state in DELIVERED_STATUSES:
        for target in FLOW.allowed_targets(state):
            assert target in DELIVERED_STATUSES, f"{state} -> {target} leaves the delivered set"


def test_a_local_drop_can_be_confirmed_without_an_in_transit_scan() -> None:
    assert FLOW.can_transition(DispatchStatus.DISPATCHED, DispatchStatus.DELIVERED)
    assert FLOW.can_transition(DispatchStatus.IN_TRANSIT, DispatchStatus.DELIVERED)


# -----------------------------------------------------------------------------
# Permissions on the edges
# -----------------------------------------------------------------------------


def test_sending_the_goods_needs_the_dispatch_create_permission() -> None:
    transition = FLOW.find(DispatchStatus.READY_FOR_DISPATCH, DispatchStatus.DISPATCHED)
    assert transition is not None
    assert transition.permission is Permission.DISPATCH_CREATE
    # It consumes reservations and posts stock movements; nothing about it is
    # reversible, and the UI has to be able to say so.
    assert transition.is_irreversible is True


def test_confirming_delivery_needs_its_own_permission() -> None:
    transition = FLOW.find(DispatchStatus.IN_TRANSIT, DispatchStatus.DELIVERED)
    assert transition is not None
    assert transition.permission is Permission.DISPATCH_CONFIRM_DELIVERY


def test_a_viewer_is_offered_no_actions() -> None:
    actions = FLOW.available_actions(DispatchStatus.READY_FOR_DISPATCH, held=[])
    assert actions == []


def test_a_dispatch_clerk_is_offered_only_what_they_may_do() -> None:
    actions = FLOW.available_actions(DispatchStatus.DISPATCHED, held=[Permission.DISPATCH_UPDATE])
    targets = {action.target for action in actions}
    assert DispatchStatus.IN_TRANSIT in targets
    # Confirming delivery is somebody else's authority.
    assert DispatchStatus.DELIVERED not in targets


# -----------------------------------------------------------------------------
# The status sets the queries are built on
# -----------------------------------------------------------------------------


def test_the_in_flight_and_delivered_sets_do_not_overlap() -> None:
    # The partial index and the delay predicate both key on IN_FLIGHT_STATUSES;
    # an overlap would make a delivered consignment eligible to be called late.
    assert set(IN_FLIGHT_STATUSES).isdisjoint(DELIVERED_STATUSES)


def test_every_status_after_dispatch_is_either_in_flight_or_delivered() -> None:
    post_dispatch = set(DispatchStatus) - set(EDITABLE_STATUSES) - {DispatchStatus.CANCELLED}
    assert post_dispatch == set(IN_FLIGHT_STATUSES) | set(DELIVERED_STATUSES)


# -----------------------------------------------------------------------------
# Movement expansion
# -----------------------------------------------------------------------------


def _line(*, quantity: str, serials: int = 0) -> DispatchLine:
    line = DispatchLine()
    line.id = uuid.uuid4()
    line.line_number = 1
    line.item_id = uuid.uuid4()
    line.quantity = Decimal(quantity)
    line.batch_id = None
    line.serial_ids = [uuid.uuid4() for _ in range(serials)]
    line.carton_ids = []
    return line


def _item(*, serial_tracked: bool) -> DispatchItem:
    return DispatchItem(
        id=uuid.uuid4(),
        code="LOCK-01",
        name="Mortice lock",
        base_uom_id=uuid.uuid4(),
        is_batch_tracked=False,
        is_serial_tracked=serial_tracked,
        is_active=True,
    )


def test_an_ordinary_line_posts_one_negative_movement() -> None:
    line = _line(quantity="12")
    movements = build_line_movements(
        line,
        item=_item(serial_tracked=False),
        dispatch_code="DSP/2026-27/00001",
        dispatch_id=uuid.uuid4(),
        warehouse_id=uuid.uuid4(),
        occurred_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )

    assert len(movements) == 1
    assert movements[0].transaction_type is TransactionType.DISPATCH
    assert movements[0].quantity == Decimal("12.0000")
    assert movements[0].serial_id is None


def test_a_serial_tracked_line_posts_one_movement_of_one_unit_per_serial() -> None:
    # The inventory engine refuses any quantity but 1 against a named serial, so
    # a line of 3 has to become three movements or it cannot be posted at all.
    line = _line(quantity="3", serials=3)
    movements = build_line_movements(
        line,
        item=_item(serial_tracked=True),
        dispatch_code="DSP/2026-27/00002",
        dispatch_id=uuid.uuid4(),
        warehouse_id=uuid.uuid4(),
        occurred_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )

    assert len(movements) == 3
    assert {m.quantity for m in movements} == {Decimal("1")}
    assert [m.serial_id for m in movements] == line.serial_ids


def test_every_movement_points_back_at_the_consignment() -> None:
    dispatch_id = uuid.uuid4()
    movements = build_line_movements(
        _line(quantity="2", serials=2),
        item=_item(serial_tracked=True),
        dispatch_code="DSP/2026-27/00003",
        dispatch_id=dispatch_id,
        warehouse_id=uuid.uuid4(),
        occurred_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )

    # The (reference_type, reference_id) pair is how the ledger rows a
    # consignment wrote are found afterwards - see database.md section 4.2.
    for movement in movements:
        assert movement.reference_type == "dispatch"
        assert movement.reference_id == dispatch_id
        assert movement.reference_code == "DSP/2026-27/00003"
        assert movement.reason


# -----------------------------------------------------------------------------
# The sales-order gate: a consignment may only leave an order that is ready
# -----------------------------------------------------------------------------


def test_only_allocated_or_part_dispatched_orders_may_be_dispatched() -> None:
    assert (
        frozenset({SalesOrderStatus.ALLOCATED, SalesOrderStatus.PARTIALLY_DISPATCHED})
        == DISPATCHABLE_ORDER_STATUSES
    )


def test_the_dispatchable_set_is_derived_from_the_sales_lifecycle() -> None:
    # Not a second copy of the rule: if the sales module ever opens a
    # CONFIRMED -> PARTIALLY_DISPATCHED edge, the gate follows it automatically.
    for status in SalesOrderStatus:
        expected = status is SalesOrderStatus.PARTIALLY_DISPATCHED or SALES_FLOW.can_transition(
            status, SalesOrderStatus.PARTIALLY_DISPATCHED
        )
        assert (status in DISPATCHABLE_ORDER_STATUSES) is expected, status


@pytest.mark.parametrize("status", sorted(DISPATCHABLE_ORDER_STATUSES))
def test_a_ready_order_passes_the_gate_silently(status: SalesOrderStatus) -> None:
    assert_order_dispatchable("SO/2026-27/00001", status)


@pytest.mark.parametrize("status", sorted(set(SalesOrderStatus) - DISPATCHABLE_ORDER_STATUSES))
def test_an_order_that_is_not_ready_is_refused_with_the_remedy_named(
    status: SalesOrderStatus,
) -> None:
    with pytest.raises(BusinessRuleError) as raised:
        assert_order_dispatchable("SO/2026-27/00001", status)

    error = raised.value
    # INVALID_STATE_TRANSITION so the frontend headlines it as "Action not
    # allowed in this state"; the message itself says what to do about it.
    assert error.code is ErrorCode.INVALID_STATE_TRANSITION
    assert "SO/2026-27/00001" in error.message
    assert "fully allocated" in error.message
    assert error.details["sales_order_status"] == str(status)
    assert set(error.details["dispatchable_statuses"]) == {
        str(s) for s in DISPATCHABLE_ORDER_STATUSES
    }
