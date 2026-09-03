"""Production state machines.

The production order graph is the contract in database.md section 7. These tests
are what stops an edge being added quietly: a transition nobody declared is a job
that can reach a state no downstream module expects - stock reserved for an order
that was cancelled, or output posted against one that was never released.

No database. The machines are declared data, which is the point of encoding them
that way rather than as ``if`` chains through the service.
"""

from __future__ import annotations

import itertools

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.production.models import (
    MaterialIssueStatus,
    ProductionOrderStatus,
    WorkOrderStatus,
)
from src.modules.production.service import (
    MATERIAL_ISSUE_FLOW,
    PRODUCTION_ORDER_FLOW,
    WORK_ORDER_FLOW,
)

pytestmark = pytest.mark.unit

ALL_MACHINES = [PRODUCTION_ORDER_FLOW, WORK_ORDER_FLOW, MATERIAL_ISSUE_FLOW]

HAPPY_PATH = [
    ProductionOrderStatus.DRAFT,
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
    ProductionOrderStatus.COMPLETED,
    ProductionOrderStatus.CLOSED,
]

#: The contract: cancellation is reachable only before the job is under way.
CANCELLABLE = {
    ProductionOrderStatus.DRAFT,
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
}


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.entity)
def test_every_machine_is_structurally_sound(machine):
    """No unreachable state, and no non-terminal state a document cannot leave."""
    machine.validate()


def test_the_documented_happy_path_is_walkable_end_to_end():
    for current, target in itertools.pairwise(HAPPY_PATH):
        PRODUCTION_ORDER_FLOW.assert_transition(current, target)


def test_the_graph_declares_exactly_the_documented_edges():
    """Guards against an edge being added without being argued for.

    The set is written out rather than derived from the machine, because a test
    that reads the graph to check the graph would pass whatever it was given.
    """
    expected = {
        (ProductionOrderStatus.DRAFT, ProductionOrderStatus.APPROVED),
        (ProductionOrderStatus.APPROVED, ProductionOrderStatus.RELEASED),
        (ProductionOrderStatus.RELEASED, ProductionOrderStatus.MATERIAL_ISSUED),
        (ProductionOrderStatus.MATERIAL_ISSUED, ProductionOrderStatus.IN_PROCESS),
        (ProductionOrderStatus.IN_PROCESS, ProductionOrderStatus.COMPLETED),
        (ProductionOrderStatus.COMPLETED, ProductionOrderStatus.CLOSED),
    } | {(state, ProductionOrderStatus.CANCELLED) for state in CANCELLABLE}

    actual = {(t.source, t.target) for t in PRODUCTION_ORDER_FLOW.transitions}
    assert actual == expected


# -----------------------------------------------------------------------------
# Forbidden transitions - one test each, because each is a different accident
# -----------------------------------------------------------------------------


def test_a_draft_cannot_be_released_without_approval():
    """Release explodes a structure and holds stock. It needs a decision first."""
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus.DRAFT, ProductionOrderStatus.RELEASED
        )


def test_an_approved_order_cannot_skip_release_and_take_material():
    """Without a release there is no component snapshot to issue against."""
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus.APPROVED, ProductionOrderStatus.MATERIAL_ISSUED
        )


def test_a_released_order_cannot_start_before_its_material_is_issued():
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus.RELEASED, ProductionOrderStatus.IN_PROCESS
        )


def test_an_order_cannot_be_completed_before_it_starts():
    """Completing from MATERIAL_ISSUED would post output for work never done."""
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus.MATERIAL_ISSUED, ProductionOrderStatus.COMPLETED
        )


def test_an_order_cannot_be_closed_before_it_is_completed():
    """Closure costs the job; costing one still running would freeze a part-cost."""
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus.IN_PROCESS, ProductionOrderStatus.CLOSED
        )


@pytest.mark.parametrize(
    "state",
    [
        ProductionOrderStatus.IN_PROCESS,
        ProductionOrderStatus.COMPLETED,
        ProductionOrderStatus.CLOSED,
        ProductionOrderStatus.CANCELLED,
    ],
)
def test_a_job_under_way_or_finished_cannot_be_cancelled(state):
    """database.md section 7: CANCELLED is reachable before IN_PROCESS only."""
    with pytest.raises(InvalidStateTransitionError):
        PRODUCTION_ORDER_FLOW.assert_transition(state, ProductionOrderStatus.CANCELLED)


@pytest.mark.parametrize("target", list(ProductionOrderStatus))
def test_nothing_leaves_a_terminal_state(target):
    for terminal in (ProductionOrderStatus.CLOSED, ProductionOrderStatus.CANCELLED):
        with pytest.raises(InvalidStateTransitionError):
            PRODUCTION_ORDER_FLOW.assert_transition(terminal, target)


def test_no_status_can_be_reversed():
    """There is no undo edge anywhere in the graph.

    Every backwards step is checked rather than a sample: a single reverse edge
    would let a completed job go back to RELEASED and be issued material twice.
    """
    for later, earlier in itertools.combinations(reversed(HAPPY_PATH), 2):
        with pytest.raises(InvalidStateTransitionError):
            PRODUCTION_ORDER_FLOW.assert_transition(later, earlier)


def test_every_forward_jump_of_more_than_one_step_is_refused():
    for index, source in enumerate(HAPPY_PATH):
        for target in HAPPY_PATH[index + 2 :]:
            with pytest.raises(InvalidStateTransitionError):
                PRODUCTION_ORDER_FLOW.assert_transition(source, target)


# -----------------------------------------------------------------------------
# Permissions carried by the edges
# -----------------------------------------------------------------------------


def test_each_lifecycle_step_demands_its_own_right():
    """Approving, releasing and closing are different jobs and different rights."""
    assert (
        PRODUCTION_ORDER_FLOW.required_permission(
            ProductionOrderStatus.DRAFT, ProductionOrderStatus.APPROVED
        )
        is Permission.PRODUCTION_ORDER_APPROVE
    )
    assert (
        PRODUCTION_ORDER_FLOW.required_permission(
            ProductionOrderStatus.APPROVED, ProductionOrderStatus.RELEASED
        )
        is Permission.PRODUCTION_ORDER_RELEASE
    )
    assert (
        PRODUCTION_ORDER_FLOW.required_permission(
            ProductionOrderStatus.COMPLETED, ProductionOrderStatus.CLOSED
        )
        is Permission.PRODUCTION_ORDER_CLOSE
    )
    assert (
        PRODUCTION_ORDER_FLOW.required_permission(
            ProductionOrderStatus.RELEASED, ProductionOrderStatus.CANCELLED
        )
        is Permission.PRODUCTION_ORDER_CANCEL
    )


def test_a_storekeeper_cannot_see_the_approval_action():
    """``available_actions`` drives the UI; it must not offer what will 403."""
    held = [Permission.MATERIAL_ISSUE_CREATE, Permission.PRODUCTION_ORDER_VIEW]
    offered = PRODUCTION_ORDER_FLOW.available_actions(ProductionOrderStatus.DRAFT, held)
    assert offered == []

    releaser = [Permission.PRODUCTION_ORDER_RELEASE]
    offered = PRODUCTION_ORDER_FLOW.available_actions(ProductionOrderStatus.APPROVED, releaser)
    assert [t.target for t in offered] == [ProductionOrderStatus.RELEASED]


def test_the_irreversible_steps_are_marked_as_such():
    """The UI confirms before these, and the audit tests rely on the flag."""
    irreversible = {
        (t.source, t.target) for t in PRODUCTION_ORDER_FLOW.transitions if t.is_irreversible
    }
    assert (ProductionOrderStatus.APPROVED, ProductionOrderStatus.RELEASED) in irreversible
    assert (
        ProductionOrderStatus.RELEASED,
        ProductionOrderStatus.MATERIAL_ISSUED,
    ) in irreversible
    assert (ProductionOrderStatus.IN_PROCESS, ProductionOrderStatus.COMPLETED) in irreversible
    assert (ProductionOrderStatus.COMPLETED, ProductionOrderStatus.CLOSED) in irreversible


# -----------------------------------------------------------------------------
# Work orders
# -----------------------------------------------------------------------------


def test_an_operation_cannot_be_completed_without_being_started():
    with pytest.raises(InvalidStateTransitionError):
        WORK_ORDER_FLOW.assert_transition(WorkOrderStatus.PENDING, WorkOrderStatus.COMPLETED)


def test_a_finished_operation_cannot_be_reopened_or_cancelled():
    for target in (WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.PENDING, WorkOrderStatus.CANCELLED):
        with pytest.raises(InvalidStateTransitionError):
            WORK_ORDER_FLOW.assert_transition(WorkOrderStatus.COMPLETED, target)


def test_an_operation_may_be_abandoned_from_either_live_state():
    WORK_ORDER_FLOW.assert_transition(WorkOrderStatus.PENDING, WorkOrderStatus.CANCELLED)
    WORK_ORDER_FLOW.assert_transition(WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.CANCELLED)


# -----------------------------------------------------------------------------
# Material issues
# -----------------------------------------------------------------------------


def test_a_posted_issue_is_terminal():
    """Stock has moved. The correction is a return document, not an edit."""
    for target in (
        MaterialIssueStatus.DRAFT,
        MaterialIssueStatus.CANCELLED,
        MaterialIssueStatus.POSTED,
    ):
        with pytest.raises(InvalidStateTransitionError):
            MATERIAL_ISSUE_FLOW.assert_transition(MaterialIssueStatus.POSTED, target)


def test_a_cancelled_issue_cannot_be_posted():
    with pytest.raises(InvalidStateTransitionError):
        MATERIAL_ISSUE_FLOW.assert_transition(
            MaterialIssueStatus.CANCELLED, MaterialIssueStatus.POSTED
        )


def test_posting_is_marked_irreversible():
    transition = MATERIAL_ISSUE_FLOW.find(MaterialIssueStatus.DRAFT, MaterialIssueStatus.POSTED)
    assert transition is not None
    assert transition.is_irreversible
