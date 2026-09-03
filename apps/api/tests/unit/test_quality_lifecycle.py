"""The QC inspection state machine.

The rules under test are the ones that keep the stock ledger and the quality
record in agreement: an inspection cannot be closed before it has been decided,
a decision whose stock has already left QC hold cannot be revisited, and one
whose quantity is still held can be - because otherwise material sent for rework
would sit in the QC pool forever with no legal way out.

The graph is ``PENDING -> IN_PROGRESS -> (ACCEPTED | REWORK | REJECTED | HOLD |
SCRAP) -> CLOSED`` (database.md section 7).
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.quality.models import (
    DECIDED_STATUSES,
    OPEN_STATUSES,
    REOPENABLE_STATUSES,
    InspectionResult,
    InspectionStatus,
)
from src.modules.quality.service import QC_INSPECTION_LIFECYCLE as FLOW

HAPPY_PATH = (
    InspectionStatus.PENDING,
    InspectionStatus.IN_PROGRESS,
    InspectionStatus.ACCEPTED,
    InspectionStatus.CLOSED,
)


# -----------------------------------------------------------------------------
# The state graph
# -----------------------------------------------------------------------------


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    FLOW.validate()


def test_the_happy_path_is_reachable_step_by_step() -> None:
    for current, target in pairwise(HAPPY_PATH):
        assert FLOW.can_transition(current, target), f"{current} -> {target} is missing"


def test_closed_is_the_only_terminal_state() -> None:
    for state in InspectionStatus:
        has_exit = bool(FLOW.allowed_targets(state))
        assert has_exit is not FLOW.is_terminal(state), state
    assert FLOW.is_terminal(InspectionStatus.CLOSED)


@pytest.mark.parametrize("verdict", list(DECIDED_STATUSES))
def test_every_verdict_is_reachable_directly_and_after_measuring(
    verdict: InspectionStatus,
) -> None:
    # Directly, because an inspection raised for an item with no check sheet is
    # decided on the inspector's judgement without any readings being taken.
    assert FLOW.can_transition(InspectionStatus.PENDING, verdict)
    assert FLOW.can_transition(InspectionStatus.IN_PROGRESS, verdict)


def test_every_verdict_name_exists_in_both_vocabularies() -> None:
    # `result` and `status` answer different questions but share the five
    # decision names, and a check constraint keeps them in step. A member added
    # to one and not the other would make that constraint unsatisfiable.
    for verdict in DECIDED_STATUSES:
        assert InspectionResult(verdict.value) is not InspectionResult.PENDING


# -----------------------------------------------------------------------------
# Steps that may not be skipped
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Closing an undecided inspection would leave stock on QC hold under a
        # closed document, and the status/result check constraint forbids it too.
        (InspectionStatus.PENDING, InspectionStatus.CLOSED),
        (InspectionStatus.IN_PROGRESS, InspectionStatus.CLOSED),
        # Measurements are recorded on an open inspection, never on a decided one.
        (InspectionStatus.ACCEPTED, InspectionStatus.IN_PROGRESS),
        (InspectionStatus.REJECTED, InspectionStatus.IN_PROGRESS),
        # A closed inspection is closed.
        (InspectionStatus.CLOSED, InspectionStatus.IN_PROGRESS),
        (InspectionStatus.CLOSED, InspectionStatus.ACCEPTED),
    ],
)
def test_skipping_a_step_is_refused(
    current: InspectionStatus, target: InspectionStatus
) -> None:
    assert not FLOW.can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(current, target)


@pytest.mark.parametrize(
    ("decided", "target"),
    [
        (InspectionStatus.ACCEPTED, InspectionStatus.REJECTED),
        (InspectionStatus.REJECTED, InspectionStatus.ACCEPTED),
        (InspectionStatus.SCRAP, InspectionStatus.ACCEPTED),
        (InspectionStatus.SCRAP, InspectionStatus.REWORK),
    ],
)
def test_a_verdict_whose_stock_has_left_qc_hold_cannot_be_revisited(
    decided: InspectionStatus, target: InspectionStatus
) -> None:
    # Accepted stock may already have been issued or sold, rejected stock is in
    # the damaged pool awaiting a vendor return, and scrapped stock is gone.
    # There is nothing to move back.
    assert not FLOW.can_transition(decided, target)


@pytest.mark.parametrize("held", list(REOPENABLE_STATUSES))
@pytest.mark.parametrize("target", list(DECIDED_STATUSES))
def test_a_verdict_whose_stock_is_still_held_can_be_re_decided(
    held: InspectionStatus, target: InspectionStatus
) -> None:
    # REWORK and HOLD leave the quantity in the QC pool. Without these edges the
    # material would have no legal way out, which would be modelling the bug
    # rather than the process. The self-edge covers a partial re-decision.
    assert FLOW.can_transition(held, target)


# -----------------------------------------------------------------------------
# Authority
# -----------------------------------------------------------------------------


def test_measuring_and_deciding_need_different_permissions() -> None:
    # The person who measures a part is usually not the person authorised to
    # release a consignment on the strength of it (section 38).
    assert (
        FLOW.required_permission(InspectionStatus.PENDING, InspectionStatus.IN_PROGRESS)
        is Permission.QC_INSPECT
    )
    for verdict in DECIDED_STATUSES:
        assert (
            FLOW.required_permission(InspectionStatus.IN_PROGRESS, verdict)
            is Permission.QC_DECIDE
        )


def test_every_decision_is_flagged_irreversible() -> None:
    # Drives the UI's confirmation prompt: each of these moves stock.
    for transition in FLOW.transitions:
        if transition.target in DECIDED_STATUSES:
            assert transition.is_irreversible, transition


def test_an_inspector_without_the_decide_right_sees_only_the_start_action() -> None:
    actions = FLOW.available_actions(InspectionStatus.PENDING, [Permission.QC_INSPECT])
    assert [t.target for t in actions] == [InspectionStatus.IN_PROGRESS]


def test_the_rejection_names_what_would_have_been_allowed() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        FLOW.assert_transition(InspectionStatus.PENDING, InspectionStatus.CLOSED)

    details = caught.value.details or {}
    assert set(details.get("allowed", [])) == {
        InspectionStatus.IN_PROGRESS.value,
        *(status.value for status in DECIDED_STATUSES),
    }


def test_the_open_statuses_are_exactly_the_undecided_ones() -> None:
    # The pending queue, the partial index behind it and the record-results guard
    # all read this tuple; a member drifting out of it would silently change all
    # three at once.
    assert set(OPEN_STATUSES) == {InspectionStatus.PENDING, InspectionStatus.IN_PROGRESS}
    assert set(OPEN_STATUSES) | set(DECIDED_STATUSES) | {InspectionStatus.CLOSED} == set(
        InspectionStatus
    )
