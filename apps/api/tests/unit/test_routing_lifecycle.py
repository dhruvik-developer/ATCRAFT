"""Routing, machine and work-centre state machines, and the controls around them.

The rules under test are the ones that keep an approved process trustworthy: you
cannot edit what production is already building against, you cannot approve your
own work, and a scrapped machine never comes back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from src.core.deps import Principal
from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.routing.models import MachineStatus, RoutingStatus
from src.modules.routing.service import (
    MACHINE_AVAILABILITY,
    ROUTING_LIFECYCLE,
    SUPER_ADMIN_ROLE_CODE,
    WORK_CENTER_LIFECYCLE,
    MasterLifecycleState,
    can_approve,
    effectivity_contains,
    is_editable,
    lifecycle_state,
)

AUTHOR = uuid.uuid4()
REVIEWER = uuid.uuid4()


def principal(
    user_id: uuid.UUID,
    *,
    is_superuser: bool = False,
    roles: tuple[str, ...] = ("DESIGN_ENGINEER",),
) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=uuid.uuid4(),
        email="tester@atcraft.test",
        full_name="Tester",
        is_superuser=is_superuser,
        permissions=frozenset({str(Permission.ROUTING_MANAGE)}),
        role_codes=roles,
    )


@dataclass
class FakeRouting:
    """Just the two effectivity columns - no session, no ORM."""

    effective_from: date
    effective_to: date | None = None


@dataclass
class FakeWorkCenter:
    is_active: bool
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Routing lifecycle
# -----------------------------------------------------------------------------


def test_the_routing_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    ROUTING_LIFECYCLE.validate()


def test_a_draft_can_be_approved_or_abandoned() -> None:
    assert set(ROUTING_LIFECYCLE.allowed_targets(RoutingStatus.DRAFT)) == {
        RoutingStatus.APPROVED,
        RoutingStatus.OBSOLETE,
    }


def test_an_obsolete_routing_is_the_end_of_the_line() -> None:
    """A superseded process is never resurrected.

    Orders raised against it still reference it, so reviving it would silently
    change what those orders resolve to.
    """
    assert ROUTING_LIFECYCLE.is_terminal(RoutingStatus.OBSOLETE)
    assert ROUTING_LIFECYCLE.allowed_targets(RoutingStatus.OBSOLETE) == []

    with pytest.raises(InvalidStateTransitionError):
        ROUTING_LIFECYCLE.assert_transition(RoutingStatus.OBSOLETE, RoutingStatus.DRAFT)


def test_an_approved_routing_cannot_go_back_to_draft() -> None:
    """Editing an approved process is a new version, never a reopen."""
    with pytest.raises(InvalidStateTransitionError) as caught:
        ROUTING_LIFECYCLE.assert_transition(RoutingStatus.APPROVED, RoutingStatus.DRAFT)

    assert caught.value.details["current_status"] == "APPROVED"
    assert caught.value.details["allowed"] == ["OBSOLETE"]


def test_approval_and_obsoleting_are_gated_on_routing_manage() -> None:
    for target in (RoutingStatus.APPROVED, RoutingStatus.OBSOLETE):
        transition = ROUTING_LIFECYCLE.find(RoutingStatus.DRAFT, target)
        assert transition is not None
        assert transition.permission is Permission.ROUTING_MANAGE


def test_approval_is_marked_irreversible_so_the_ui_confirms_it() -> None:
    transition = ROUTING_LIFECYCLE.find(RoutingStatus.DRAFT, RoutingStatus.APPROVED)
    assert transition is not None and transition.is_irreversible


def test_only_a_draft_is_editable() -> None:
    assert is_editable(RoutingStatus.DRAFT)
    assert not is_editable(RoutingStatus.APPROVED)
    assert not is_editable(RoutingStatus.OBSOLETE)


# -----------------------------------------------------------------------------
# Approval controls
# -----------------------------------------------------------------------------


def test_an_author_cannot_approve_their_own_routing() -> None:
    assert not can_approve(principal(AUTHOR), AUTHOR)


def test_a_second_person_can_approve_it() -> None:
    assert can_approve(principal(REVIEWER), AUTHOR)


def test_a_super_admin_may_approve_their_own_work() -> None:
    """The documented escape hatch for a two-person engineering team."""
    assert can_approve(principal(AUTHOR, roles=(SUPER_ADMIN_ROLE_CODE,)), AUTHOR)
    assert can_approve(principal(AUTHOR, is_superuser=True), AUTHOR)


def test_a_routing_with_no_recorded_author_can_still_be_approved() -> None:
    # Seeded or imported data has no created_by_id; refusing it outright would
    # make an imported routing permanently unapprovable.
    assert can_approve(principal(REVIEWER), None)


# -----------------------------------------------------------------------------
# Effectivity
# -----------------------------------------------------------------------------


def test_effectivity_is_inclusive_at_both_ends() -> None:
    routing = cast(Any, FakeRouting(date(2026, 4, 1), date(2026, 6, 30)))

    assert effectivity_contains(routing, date(2026, 4, 1))
    assert effectivity_contains(routing, date(2026, 6, 30))
    assert not effectivity_contains(routing, date(2026, 3, 31))
    assert not effectivity_contains(routing, date(2026, 7, 1))


def test_an_open_ended_routing_stays_effective() -> None:
    routing = cast(Any, FakeRouting(date(2026, 4, 1)))

    assert effectivity_contains(routing, date(2030, 1, 1))


# -----------------------------------------------------------------------------
# Machine availability
# -----------------------------------------------------------------------------


def test_the_machine_state_graph_is_structurally_sound() -> None:
    MACHINE_AVAILABILITY.validate()


def test_a_retired_machine_never_comes_back() -> None:
    assert MACHINE_AVAILABILITY.is_terminal(MachineStatus.RETIRED)

    with pytest.raises(InvalidStateTransitionError):
        MACHINE_AVAILABILITY.assert_transition(MachineStatus.RETIRED, MachineStatus.AVAILABLE)


def test_a_running_machine_is_stopped_before_planned_maintenance() -> None:
    """No RUNNING -> MAINTENANCE edge: the job's end must be recorded first,
    otherwise the downtime swallows a job that was never reported finished."""
    with pytest.raises(InvalidStateTransitionError):
        MACHINE_AVAILABILITY.assert_transition(MachineStatus.RUNNING, MachineStatus.MAINTENANCE)

    assert MACHINE_AVAILABILITY.can_transition(MachineStatus.RUNNING, MachineStatus.AVAILABLE)
    assert MACHINE_AVAILABILITY.can_transition(MachineStatus.AVAILABLE, MachineStatus.MAINTENANCE)


def test_a_breakdown_can_be_reported_from_either_working_state() -> None:
    for source in (MachineStatus.AVAILABLE, MachineStatus.RUNNING):
        assert MACHINE_AVAILABILITY.can_transition(source, MachineStatus.BREAKDOWN)


def test_a_broken_machine_is_repaired_or_scrapped_but_not_run() -> None:
    assert set(MACHINE_AVAILABILITY.allowed_targets(MachineStatus.BREAKDOWN)) == {
        MachineStatus.MAINTENANCE,
        MachineStatus.RETIRED,
    }


def test_a_machine_under_maintenance_cannot_be_handed_a_job() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        MACHINE_AVAILABILITY.assert_transition(MachineStatus.MAINTENANCE, MachineStatus.RUNNING)

    assert "AVAILABLE" in caught.value.details["allowed"]


def test_available_actions_are_filtered_by_the_permissions_held() -> None:
    without = MACHINE_AVAILABILITY.available_actions(MachineStatus.AVAILABLE, [])
    with_manage = MACHINE_AVAILABILITY.available_actions(
        MachineStatus.AVAILABLE, [Permission.MACHINE_MANAGE]
    )

    assert without == []
    assert {t.target for t in with_manage} == {
        MachineStatus.RUNNING,
        MachineStatus.MAINTENANCE,
        MachineStatus.BREAKDOWN,
        MachineStatus.RETIRED,
    }


# -----------------------------------------------------------------------------
# Work-centre lifecycle
# -----------------------------------------------------------------------------


def test_the_work_centre_state_graph_is_structurally_sound() -> None:
    WORK_CENTER_LIFECYCLE.validate()


def test_lifecycle_state_is_derived_from_the_flags_not_a_column() -> None:
    assert lifecycle_state(cast(Any, FakeWorkCenter(True))) is MasterLifecycleState.ACTIVE
    assert lifecycle_state(cast(Any, FakeWorkCenter(False))) is MasterLifecycleState.INACTIVE
    # deleted_at wins: a retired centre is archived whatever is_active says.
    archived = FakeWorkCenter(True, datetime(2026, 4, 1, tzinfo=UTC))
    assert lifecycle_state(cast(Any, archived)) is MasterLifecycleState.ARCHIVED


def test_a_restored_work_centre_comes_back_inactive() -> None:
    """Someone must look at its rate and capacity before routings plan work
    there again, so restore never lands directly on ACTIVE."""
    assert WORK_CENTER_LIFECYCLE.allowed_targets(MasterLifecycleState.ARCHIVED) == [
        MasterLifecycleState.INACTIVE
    ]

    with pytest.raises(InvalidStateTransitionError):
        WORK_CENTER_LIFECYCLE.assert_transition(
            MasterLifecycleState.ARCHIVED, MasterLifecycleState.ACTIVE
        )


def test_retiring_is_reachable_from_both_live_states() -> None:
    for source in (MasterLifecycleState.ACTIVE, MasterLifecycleState.INACTIVE):
        assert WORK_CENTER_LIFECYCLE.can_transition(source, MasterLifecycleState.ARCHIVED)
