"""Warehouse and storage-location lifecycle rules - no database involved.

The lifecycle is derived from ``is_active`` and ``deleted_at`` rather than
stored in a status column, so these tests pin down two things: that the
derivation is right, and that the state machine refuses the transitions that
would otherwise be reachable by calling the endpoints in the wrong order.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.clock import utcnow
from src.core.errors import ErrorCode, InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.warehouses.service import (
    LOCATION_LIFECYCLE,
    WAREHOUSE_LIFECYCLE,
    MasterLifecycleState,
    lifecycle_state,
    referenced_in_use,
)

pytestmark = pytest.mark.unit


def _row(*, is_active: bool = True, deleted: bool = False) -> SimpleNamespace:
    """Minimal stand-in for a persisted master row.

    Deliberately not an ORM instance: the derivation reads two attributes and
    nothing else, and a plain object keeps the test free of a mapper.
    """
    return SimpleNamespace(is_active=is_active, deleted_at=utcnow() if deleted else None)


class TestLifecycleDerivation:
    def test_active_row_is_active(self) -> None:
        assert lifecycle_state(_row()) is MasterLifecycleState.ACTIVE

    def test_deactivated_row_is_inactive(self) -> None:
        assert lifecycle_state(_row(is_active=False)) is MasterLifecycleState.INACTIVE

    def test_deleted_row_is_archived_even_when_flagged_active(self) -> None:
        # deleted_at wins: a retired row that still carries is_active=True from
        # before the retirement must not read as usable.
        assert lifecycle_state(_row(is_active=True, deleted=True)) is MasterLifecycleState.ARCHIVED


class TestWarehouseStateMachine:
    def test_graph_is_structurally_sound(self) -> None:
        # No unreachable state, no non-terminal state without an exit.
        WAREHOUSE_LIFECYCLE.validate()
        LOCATION_LIFECYCLE.validate()

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (MasterLifecycleState.ACTIVE, MasterLifecycleState.INACTIVE),
            (MasterLifecycleState.INACTIVE, MasterLifecycleState.ACTIVE),
            (MasterLifecycleState.ACTIVE, MasterLifecycleState.ARCHIVED),
            (MasterLifecycleState.INACTIVE, MasterLifecycleState.ARCHIVED),
            (MasterLifecycleState.ARCHIVED, MasterLifecycleState.INACTIVE),
        ],
    )
    def test_legal_transitions(
        self, source: MasterLifecycleState, target: MasterLifecycleState
    ) -> None:
        assert WAREHOUSE_LIFECYCLE.can_transition(source, target)

    def test_restore_never_lands_on_active(self) -> None:
        """A restored warehouse comes back inactive and must be switched on."""
        assert not WAREHOUSE_LIFECYCLE.can_transition(
            MasterLifecycleState.ARCHIVED, MasterLifecycleState.ACTIVE
        )
        assert WAREHOUSE_LIFECYCLE.allowed_targets(MasterLifecycleState.ARCHIVED) == [
            MasterLifecycleState.INACTIVE
        ]

    def test_retiring_twice_is_rejected(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc:
            WAREHOUSE_LIFECYCLE.assert_transition(
                MasterLifecycleState.ARCHIVED, MasterLifecycleState.ARCHIVED
            )
        assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert exc.value.details["current_status"] == "ARCHIVED"
        assert exc.value.details["allowed"] == ["INACTIVE"]

    def test_activating_an_already_active_warehouse_is_rejected(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            WAREHOUSE_LIFECYCLE.assert_transition(
                MasterLifecycleState.ACTIVE, MasterLifecycleState.ACTIVE
            )

    def test_retirement_is_flagged_irreversible_for_the_ui(self) -> None:
        transition = WAREHOUSE_LIFECYCLE.find(
            MasterLifecycleState.ACTIVE, MasterLifecycleState.ARCHIVED
        )
        assert transition is not None
        assert transition.is_irreversible is True


class TestTransitionPermissions:
    def test_every_transition_demands_the_manage_permission(self) -> None:
        assert all(
            t.permission is Permission.WAREHOUSE_MANAGE for t in WAREHOUSE_LIFECYCLE.transitions
        )
        assert all(
            t.permission is Permission.LOCATION_MANAGE for t in LOCATION_LIFECYCLE.transitions
        )

    def test_viewer_is_offered_no_actions(self) -> None:
        actions = WAREHOUSE_LIFECYCLE.available_actions(
            MasterLifecycleState.ACTIVE, [Permission.WAREHOUSE_VIEW]
        )
        assert actions == []

    def test_manager_is_offered_deactivate_and_retire(self) -> None:
        actions = WAREHOUSE_LIFECYCLE.available_actions(
            MasterLifecycleState.ACTIVE, [Permission.WAREHOUSE_MANAGE]
        )
        assert {str(a.target) for a in actions} == {"INACTIVE", "ARCHIVED"}


class TestReferencedInUseError:
    def test_carries_the_contract_error_code(self) -> None:
        error = referenced_in_use("Still holding stock.", warehouse_code="RM-01")
        assert error.code is ErrorCode.REFERENCED_RECORD_IN_USE
        # A business-rule refusal, not a server fault: the client can act on it.
        assert error.status_code == 422
        assert error.details == {"warehouse_code": "RM-01"}

    def test_details_are_omitted_when_empty(self) -> None:
        assert referenced_in_use("Still in use.").details == {}
