"""The job work order state machine.

The rules under test are the ones that keep company material at a vendor
accounted for: you cannot send stock to a processor nobody approved, you cannot
cancel an order whose material has already left the building, and an order with
goods still outside cannot be quietly closed.

The lifecycle is fixed by ``docs/database.md`` section 7; this file is what stops
it drifting.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.job_work.models import (
    CANCELLABLE_STATUSES,
    MATERIAL_AT_VENDOR_STATUSES,
    OPEN_SUPPLY_STATUSES,
    JobWorkOrderStatus,
)
from src.modules.job_work.service import JOB_WORK_ORDER_LIFECYCLE as FLOW

# The path docs/database.md section 7 fixes, as a sequence, so the tests below
# can walk it.
CONTRACT_PATH = (
    JobWorkOrderStatus.DRAFT,
    JobWorkOrderStatus.APPROVED,
    JobWorkOrderStatus.ISSUED,
    JobWorkOrderStatus.PARTIALLY_RECEIVED,
    JobWorkOrderStatus.RECEIVED,
    JobWorkOrderStatus.QC_PENDING,
    JobWorkOrderStatus.COMPLETED,
    JobWorkOrderStatus.CLOSED,
)


# -----------------------------------------------------------------------------
# The state graph
# -----------------------------------------------------------------------------


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    FLOW.validate()


def test_every_step_of_the_contract_path_exists() -> None:
    for current, target in pairwise(CONTRACT_PATH):
        assert FLOW.can_transition(current, target), f"{current} -> {target} is missing"


def test_the_terminal_states_are_the_only_ones_with_no_exit() -> None:
    for state in JobWorkOrderStatus:
        has_exit = bool(FLOW.allowed_targets(state))
        assert has_exit is not FLOW.is_terminal(state), state


def test_a_vendor_who_returns_everything_at_once_skips_partial_receipt() -> None:
    assert FLOW.can_transition(JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.RECEIVED)


def test_an_order_that_needs_no_inspection_skips_the_qc_hold() -> None:
    assert FLOW.can_transition(JobWorkOrderStatus.RECEIVED, JobWorkOrderStatus.COMPLETED)


# -----------------------------------------------------------------------------
# Steps that may not be skipped
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Material may not leave without an approval behind it.
        (JobWorkOrderStatus.DRAFT, JobWorkOrderStatus.ISSUED),
        # Nothing can be received before anything was sent.
        (JobWorkOrderStatus.APPROVED, JobWorkOrderStatus.RECEIVED),
        (JobWorkOrderStatus.APPROVED, JobWorkOrderStatus.PARTIALLY_RECEIVED),
        # An order cannot be finished while the goods are still out.
        (JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.COMPLETED),
        (JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.CLOSED),
        # Closing before completion would skip the inspection verdict.
        (JobWorkOrderStatus.RECEIVED, JobWorkOrderStatus.CLOSED),
        (JobWorkOrderStatus.QC_PENDING, JobWorkOrderStatus.CLOSED),
    ],
)
def test_the_lifecycle_cannot_be_short_circuited(
    current: JobWorkOrderStatus, target: JobWorkOrderStatus
) -> None:
    assert not FLOW.can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        FLOW.assert_transition(current, target)


def test_a_partially_received_order_can_only_go_on_to_be_received() -> None:
    # THE rule that stops material being written off silently: the only way out
    # of PARTIALLY_RECEIVED is a final receipt, which posts the shortfall as
    # scrap with a reason. There is no edge to CLOSED or CANCELLED.
    assert FLOW.allowed_targets(JobWorkOrderStatus.PARTIALLY_RECEIVED) == [
        JobWorkOrderStatus.RECEIVED
    ]


# -----------------------------------------------------------------------------
# Cancellation stops at the gate
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("state", CANCELLABLE_STATUSES)
def test_an_order_may_be_cancelled_while_its_material_is_still_here(
    state: JobWorkOrderStatus,
) -> None:
    assert FLOW.can_transition(state, JobWorkOrderStatus.CANCELLED)


@pytest.mark.parametrize(
    "state",
    [
        JobWorkOrderStatus.ISSUED,
        JobWorkOrderStatus.PARTIALLY_RECEIVED,
        JobWorkOrderStatus.RECEIVED,
        JobWorkOrderStatus.QC_PENDING,
        JobWorkOrderStatus.COMPLETED,
    ],
)
def test_an_order_cannot_be_cancelled_once_material_has_left(
    state: JobWorkOrderStatus,
) -> None:
    # Cancelling would orphan a quantity that is still on the company's books in
    # the JOB_WORK pool with nothing left to explain where it went.
    assert not FLOW.can_transition(state, JobWorkOrderStatus.CANCELLED)


def test_the_cancellable_set_matches_the_graph() -> None:
    from_graph = {
        state
        for state in JobWorkOrderStatus
        if FLOW.can_transition(state, JobWorkOrderStatus.CANCELLED)
    }
    assert from_graph == set(CANCELLABLE_STATUSES)


# -----------------------------------------------------------------------------
# Status vocabularies the indexes and feeds are built from
# -----------------------------------------------------------------------------


def test_material_at_vendor_is_exactly_the_issued_but_unfinished_states() -> None:
    # The partial index ix_job_work_orders_material_at_vendor is built for this
    # tuple, and pending-at-vendor filters on it. They have to move together.
    assert set(MATERIAL_AT_VENDOR_STATUSES) == {
        JobWorkOrderStatus.ISSUED,
        JobWorkOrderStatus.PARTIALLY_RECEIVED,
    }


def test_every_at_vendor_status_is_also_an_open_supply_status() -> None:
    assert set(MATERIAL_AT_VENDOR_STATUSES) <= set(OPEN_SUPPLY_STATUSES)


def test_no_terminal_status_is_treated_as_open_supply() -> None:
    for state in OPEN_SUPPLY_STATUSES:
        assert not FLOW.is_terminal(state), state


# -----------------------------------------------------------------------------
# Authority
# -----------------------------------------------------------------------------


def test_issuing_and_receiving_need_their_own_permissions() -> None:
    issue = FLOW.find(JobWorkOrderStatus.APPROVED, JobWorkOrderStatus.ISSUED)
    receive = FLOW.find(JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.RECEIVED)

    assert issue is not None and issue.permission is Permission.JOB_WORK_ISSUE
    assert receive is not None and receive.permission is Permission.JOB_WORK_RECEIVE


def test_approving_is_not_something_the_person_who_raised_the_order_can_do() -> None:
    # There is no JOB_WORK_APPROVE in the vocabulary. Approval is gated on
    # JOB_WORK_CLOSE, which the production planner - who holds CREATE and ISSUE -
    # deliberately does not have, so the two duties stay separate.
    approve = FLOW.find(JobWorkOrderStatus.DRAFT, JobWorkOrderStatus.APPROVED)

    assert approve is not None
    assert approve.permission is Permission.JOB_WORK_CLOSE
    assert approve.permission is not Permission.JOB_WORK_CREATE


def test_the_inspection_verdict_belongs_to_quality() -> None:
    verdict = FLOW.find(JobWorkOrderStatus.QC_PENDING, JobWorkOrderStatus.COMPLETED)

    assert verdict is not None and verdict.permission is Permission.QC_DECIDE


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobWorkOrderStatus.DRAFT, JobWorkOrderStatus.APPROVED),
        (JobWorkOrderStatus.APPROVED, JobWorkOrderStatus.ISSUED),
        (JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.RECEIVED),
        (JobWorkOrderStatus.ISSUED, JobWorkOrderStatus.PARTIALLY_RECEIVED),
        (JobWorkOrderStatus.PARTIALLY_RECEIVED, JobWorkOrderStatus.RECEIVED),
        (JobWorkOrderStatus.QC_PENDING, JobWorkOrderStatus.COMPLETED),
        (JobWorkOrderStatus.APPROVED, JobWorkOrderStatus.CANCELLED),
    ],
)
def test_every_transition_that_moves_stock_or_reservations_is_marked_irreversible(
    current: JobWorkOrderStatus, target: JobWorkOrderStatus
) -> None:
    # The flag drives the confirmation the UI demands. A transition that writes
    # the ledger or hands reservations back and is not marked would be performed
    # by a single misplaced click.
    transition = FLOW.find(current, target)

    assert transition is not None
    assert transition.is_irreversible, f"{current} -> {target}"


def test_a_planner_sees_only_the_actions_they_may_perform() -> None:
    held = {Permission.JOB_WORK_CREATE, Permission.JOB_WORK_ISSUE, Permission.JOB_WORK_RECEIVE}
    targets = {t.target for t in FLOW.available_actions(JobWorkOrderStatus.DRAFT, held)}

    assert targets == {JobWorkOrderStatus.CANCELLED}
