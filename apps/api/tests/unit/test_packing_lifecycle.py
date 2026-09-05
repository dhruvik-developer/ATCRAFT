"""The packing order state machine.

The rules under test are the ones that keep the bench, the stock ledger and the
dispatch dock in agreement: you cannot pack an order nobody started, you cannot
cancel one whose boxes are already sealed and paid for, and dispatch cannot take
an order that has not been packed.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.packing.models import (
    CANCELLABLE_STATUSES,
    CARTON_EDITABLE_STATUSES,
    OPEN_STATUSES,
    PackingOrderStatus,
)
from src.modules.packing.service import PACKING_ORDER_LIFECYCLE as FLOW

# The happy path, as a sequence, so the tests below can walk it.
HAPPY_PATH = (
    PackingOrderStatus.DRAFT,
    PackingOrderStatus.IN_PROGRESS,
    PackingOrderStatus.PACKED,
    PackingOrderStatus.HANDED_OVER,
    PackingOrderStatus.CLOSED,
)


# -----------------------------------------------------------------------------
# The state graph
# -----------------------------------------------------------------------------


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    FLOW.validate()


def test_every_step_of_the_happy_path_is_reachable() -> None:
    for current, target in pairwise(HAPPY_PATH):
        assert FLOW.can_transition(current, target), f"{current} -> {target} is missing"


def test_the_terminal_states_are_the_only_ones_with_no_exit() -> None:
    for state in PackingOrderStatus:
        has_exit = bool(FLOW.allowed_targets(state))
        assert has_exit is not FLOW.is_terminal(state), state


def test_the_initial_state_is_draft() -> None:
    assert FLOW.initial is PackingOrderStatus.DRAFT


# -----------------------------------------------------------------------------
# Steps that may not be skipped
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Packing cannot be completed on an order nobody started: there are no
        # cartons, so there is nothing to freeze.
        (PackingOrderStatus.DRAFT, PackingOrderStatus.PACKED),
        # Dispatch cannot take a draft, or an order still being packed.
        (PackingOrderStatus.DRAFT, PackingOrderStatus.HANDED_OVER),
        (PackingOrderStatus.IN_PROGRESS, PackingOrderStatus.HANDED_OVER),
        # Closing skips the handover, which is what records who took the goods.
        (PackingOrderStatus.PACKED, PackingOrderStatus.CLOSED),
        (PackingOrderStatus.IN_PROGRESS, PackingOrderStatus.CLOSED),
        # There is no way back: unpacking is a new order, not a reverse step.
        (PackingOrderStatus.PACKED, PackingOrderStatus.IN_PROGRESS),
        (PackingOrderStatus.HANDED_OVER, PackingOrderStatus.PACKED),
    ],
)
def test_a_skipped_or_reversed_step_is_refused(
    current: PackingOrderStatus, target: PackingOrderStatus
) -> None:
    assert not FLOW.can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(current, target)


def test_the_refusal_names_what_was_actually_allowed() -> None:
    # The error is what the frontend renders, so it has to say where the document
    # could have gone rather than only that it could not go here.
    with pytest.raises(InvalidStateTransitionError) as exc:
        FLOW.assert_transition(PackingOrderStatus.DRAFT, PackingOrderStatus.PACKED)
    assert exc.value.details["allowed"] == ["IN_PROGRESS", "CANCELLED"]


# -----------------------------------------------------------------------------
# Cancellation
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("state", CANCELLABLE_STATUSES)
def test_an_order_may_be_cancelled_before_it_is_packed(state: PackingOrderStatus) -> None:
    assert FLOW.can_transition(state, PackingOrderStatus.CANCELLED)


@pytest.mark.parametrize(
    "state",
    [
        PackingOrderStatus.PACKED,
        PackingOrderStatus.HANDED_OVER,
        PackingOrderStatus.CLOSED,
        PackingOrderStatus.CANCELLED,
    ],
)
def test_a_packed_order_can_never_be_cancelled(state: PackingOrderStatus) -> None:
    # By then cartons are sealed and packing material has been consumed against
    # the order. A cancelled order that ate boxes is a ledger nobody can explain.
    assert not FLOW.can_transition(state, PackingOrderStatus.CANCELLED)


def test_the_cancellable_set_and_the_graph_agree() -> None:
    # Two declarations of one rule: the tuple the service checks and the edges
    # the machine holds. They have to be the same set or one of them is a lie.
    from_graph = {
        transition.source
        for transition in FLOW.transitions
        if transition.target is PackingOrderStatus.CANCELLED
    }
    assert from_graph == set(CANCELLABLE_STATUSES)


# -----------------------------------------------------------------------------
# Authority and irreversibility
# -----------------------------------------------------------------------------


def test_every_transition_names_the_permission_it_needs() -> None:
    for transition in FLOW.transitions:
        assert transition.permission is not None, transition


def test_the_dispatch_handover_belongs_to_dispatch_not_packing() -> None:
    # It is the moment a dispatch takes responsibility for the boxes, so a packer
    # holding only packing.manage must not be able to perform it.
    handover = FLOW.find(PackingOrderStatus.PACKED, PackingOrderStatus.HANDED_OVER)
    assert handover is not None
    assert handover.permission is Permission.DISPATCH_CREATE


def test_a_packer_is_offered_exactly_the_actions_they_can_perform() -> None:
    held = [Permission.PACKING_MANAGE]
    offered = {
        transition.target for transition in FLOW.available_actions(PackingOrderStatus.PACKED, held)
    }
    # PACKED -> HANDED_OVER needs dispatch.create, so a packer is offered nothing
    # from here at all.
    assert offered == set()

    offered_from_progress = {
        transition.target
        for transition in FLOW.available_actions(PackingOrderStatus.IN_PROGRESS, held)
    }
    assert offered_from_progress == {PackingOrderStatus.PACKED, PackingOrderStatus.CANCELLED}


def test_the_transitions_that_freeze_or_consume_are_marked_irreversible() -> None:
    # Used by the UI to demand confirmation. Completion freezes the totals a
    # transporter is quoted against; the handover passes responsibility on.
    for source, target in (
        (PackingOrderStatus.IN_PROGRESS, PackingOrderStatus.PACKED),
        (PackingOrderStatus.PACKED, PackingOrderStatus.HANDED_OVER),
    ):
        transition = FLOW.find(source, target)
        assert transition is not None
        assert transition.is_irreversible, (source, target)


# -----------------------------------------------------------------------------
# The status vocabularies the repository and the indexes are built from
# -----------------------------------------------------------------------------


def test_cartons_are_only_editable_while_packing_is_in_progress() -> None:
    # A draft has no cartons; a packed order's are sealed and paid for.
    assert CARTON_EDITABLE_STATUSES == (PackingOrderStatus.IN_PROGRESS,)


def test_the_open_set_stops_at_the_dispatch_handover() -> None:
    # "Open" means the packing bench still owes somebody boxes. Once a dispatch
    # has taken the order the packing work is finished even though the order
    # itself has not been closed, so HANDED_OVER is deliberately outside the set.
    assert set(OPEN_STATUSES) == {
        PackingOrderStatus.DRAFT,
        PackingOrderStatus.IN_PROGRESS,
        PackingOrderStatus.PACKED,
    }
    assert PackingOrderStatus.HANDED_OVER not in OPEN_STATUSES


def test_no_terminal_state_is_treated_as_open() -> None:
    for state in OPEN_STATUSES:
        assert not FLOW.is_terminal(state)
