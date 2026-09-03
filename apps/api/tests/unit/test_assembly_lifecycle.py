"""The assembly order state machine.

The rules under test are the ones that keep the shop floor and the stock ledger
in agreement: you cannot release a job nobody approved, you cannot cancel one
whose parts are already on the bench, and completion cannot skip the inspection
branch when the output has to be inspected.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.assembly.models import AssemblyOrderStatus
from src.modules.assembly.service import ASSEMBLY_ORDER_LIFECYCLE as FLOW

# The happy path, as a sequence, so the tests below can walk it.
HAPPY_PATH = (
    AssemblyOrderStatus.DRAFT,
    AssemblyOrderStatus.APPROVED,
    AssemblyOrderStatus.RELEASED,
    AssemblyOrderStatus.COMPONENTS_ISSUED,
    AssemblyOrderStatus.IN_PROGRESS,
    AssemblyOrderStatus.COMPLETED,
    AssemblyOrderStatus.CLOSED,
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
    for state in AssemblyOrderStatus:
        has_exit = bool(FLOW.allowed_targets(state))
        assert has_exit is not FLOW.is_terminal(state), state


# -----------------------------------------------------------------------------
# Steps that may not be skipped
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Releasing reserves stock; an unapproved order must not be able to.
        (AssemblyOrderStatus.DRAFT, AssemblyOrderStatus.RELEASED),
        # Components cannot be issued against a job nobody released.
        (AssemblyOrderStatus.APPROVED, AssemblyOrderStatus.COMPONENTS_ISSUED),
        # Nothing can be completed before it was started.
        (AssemblyOrderStatus.RELEASED, AssemblyOrderStatus.COMPLETED),
        (AssemblyOrderStatus.COMPONENTS_ISSUED, AssemblyOrderStatus.COMPLETED),
        # Held output has to be inspected before it is complete.
        (AssemblyOrderStatus.QC_PENDING, AssemblyOrderStatus.CLOSED),
        # A closed order is closed.
        (AssemblyOrderStatus.CLOSED, AssemblyOrderStatus.IN_PROGRESS),
        (AssemblyOrderStatus.CANCELLED, AssemblyOrderStatus.APPROVED),
    ],
)
def test_skipping_a_step_is_refused(
    current: AssemblyOrderStatus, target: AssemblyOrderStatus
) -> None:
    assert not FLOW.can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(current, target)


def test_the_rejection_names_what_would_have_been_allowed() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        FLOW.assert_transition(AssemblyOrderStatus.DRAFT, AssemblyOrderStatus.COMPLETED)

    details = caught.value.details or {}
    assert set(details.get("allowed", [])) == {
        AssemblyOrderStatus.APPROVED.value,
        AssemblyOrderStatus.CANCELLED.value,
    }


# -----------------------------------------------------------------------------
# Cancellation stops where the parts start moving
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current",
    [
        AssemblyOrderStatus.DRAFT,
        AssemblyOrderStatus.APPROVED,
        AssemblyOrderStatus.RELEASED,
    ],
)
def test_an_order_may_be_cancelled_until_its_components_are_issued(
    current: AssemblyOrderStatus,
) -> None:
    assert FLOW.can_transition(current, AssemblyOrderStatus.CANCELLED)


@pytest.mark.parametrize(
    "current",
    [
        AssemblyOrderStatus.COMPONENTS_ISSUED,
        AssemblyOrderStatus.IN_PROGRESS,
        AssemblyOrderStatus.QC_PENDING,
        AssemblyOrderStatus.COMPLETED,
    ],
)
def test_an_order_whose_parts_are_on_the_bench_can_no_longer_be_cancelled(
    current: AssemblyOrderStatus,
) -> None:
    # Cancelling here would leave reservations - and, past completion, ledger
    # rows - belonging to a document claiming never to have existed.
    assert not FLOW.can_transition(current, AssemblyOrderStatus.CANCELLED)


# -----------------------------------------------------------------------------
# The completion branch
# -----------------------------------------------------------------------------


def test_completion_can_go_straight_through_or_via_inspection() -> None:
    targets = set(FLOW.allowed_targets(AssemblyOrderStatus.IN_PROGRESS))
    assert targets == {AssemblyOrderStatus.QC_PENDING, AssemblyOrderStatus.COMPLETED}


def test_completing_requires_the_assembly_completion_right() -> None:
    for target in (AssemblyOrderStatus.COMPLETED, AssemblyOrderStatus.QC_PENDING):
        transition = FLOW.find(AssemblyOrderStatus.IN_PROGRESS, target)
        assert transition is not None
        assert transition.permission is Permission.ASSEMBLY_COMPLETE


def test_clearing_an_inspection_requires_the_quality_decision_right() -> None:
    # Deliberately not ASSEMBLY_COMPLETE: the person who built it must not be
    # able to pass it themselves purely by holding the assembly rights.
    transition = FLOW.find(AssemblyOrderStatus.QC_PENDING, AssemblyOrderStatus.COMPLETED)
    assert transition is not None
    assert transition.permission is Permission.QC_DECIDE


# -----------------------------------------------------------------------------
# Irreversibility marks the transitions that touch stock
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Takes hard reservations away from every other document.
        (AssemblyOrderStatus.APPROVED, AssemblyOrderStatus.RELEASED),
        # Hands the parts over.
        (AssemblyOrderStatus.RELEASED, AssemblyOrderStatus.COMPONENTS_ISSUED),
        # Posts the consume and receipt movements.
        (AssemblyOrderStatus.IN_PROGRESS, AssemblyOrderStatus.COMPLETED),
        (AssemblyOrderStatus.IN_PROGRESS, AssemblyOrderStatus.QC_PENDING),
        # Releases or damages held stock.
        (AssemblyOrderStatus.QC_PENDING, AssemblyOrderStatus.COMPLETED),
        # Gives reservations back.
        (AssemblyOrderStatus.RELEASED, AssemblyOrderStatus.CANCELLED),
    ],
)
def test_transitions_that_move_stock_are_flagged_irreversible(
    current: AssemblyOrderStatus, target: AssemblyOrderStatus
) -> None:
    transition = FLOW.find(current, target)
    assert transition is not None
    assert transition.is_irreversible


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AssemblyOrderStatus.DRAFT, AssemblyOrderStatus.APPROVED),
        (AssemblyOrderStatus.COMPONENTS_ISSUED, AssemblyOrderStatus.IN_PROGRESS),
        (AssemblyOrderStatus.COMPLETED, AssemblyOrderStatus.CLOSED),
        (AssemblyOrderStatus.DRAFT, AssemblyOrderStatus.CANCELLED),
    ],
)
def test_paperwork_only_transitions_are_not_flagged_irreversible(
    current: AssemblyOrderStatus, target: AssemblyOrderStatus
) -> None:
    transition = FLOW.find(current, target)
    assert transition is not None
    assert not transition.is_irreversible


# -----------------------------------------------------------------------------
# What the UI is allowed to offer
# -----------------------------------------------------------------------------


def test_available_actions_are_filtered_by_what_the_actor_holds() -> None:
    held = [Permission.ASSEMBLY_ORDER_MANAGE]
    offered = FLOW.available_actions(AssemblyOrderStatus.IN_PROGRESS, held)
    # Completion needs assembly.complete, which this actor does not hold.
    assert offered == []

    offered = FLOW.available_actions(
        AssemblyOrderStatus.IN_PROGRESS, [Permission.ASSEMBLY_COMPLETE]
    )
    assert {transition.target for transition in offered} == {
        AssemblyOrderStatus.COMPLETED,
        AssemblyOrderStatus.QC_PENDING,
    }
