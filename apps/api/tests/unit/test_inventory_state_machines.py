"""Document lifecycles for transfers, adjustments and stock counts.

Master rules section 73: a status column is only ever changed through a state
machine. These tests pin the graphs down so a transition cannot be quietly added
by an endpoint, and so the transitions that must *not* exist stay absent - the
ones that would let stock leave the system without a matching arrival, or let a
posted document be edited afterwards.
"""

from __future__ import annotations

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.inventory.models import (
    AdjustmentStatus,
    PhysicalCountStatus,
    TransferStatus,
)
from src.modules.inventory.service import ADJUSTMENT_FLOW, COUNT_FLOW, TRANSFER_FLOW

pytestmark = pytest.mark.unit


class TestGraphsAreStructurallySound:
    @pytest.mark.parametrize("machine", [TRANSFER_FLOW, ADJUSTMENT_FLOW, COUNT_FLOW])
    def test_no_unreachable_or_dead_end_states(self, machine: object) -> None:
        machine.validate()  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("machine", "states"),
        [
            (TRANSFER_FLOW, TransferStatus),
            (ADJUSTMENT_FLOW, AdjustmentStatus),
            (COUNT_FLOW, PhysicalCountStatus),
        ],
    )
    def test_every_declared_status_appears_in_the_graph(
        self, machine: object, states: type
    ) -> None:
        """A status the machine has never heard of can only be set by a bug."""
        graph_states = {t.source for t in machine.transitions} | {  # type: ignore[attr-defined]
            t.target
            for t in machine.transitions  # type: ignore[attr-defined]
        }
        assert graph_states == set(states)


class TestTransferFlow:
    def test_the_happy_path(self) -> None:
        assert TRANSFER_FLOW.can_transition(TransferStatus.DRAFT, TransferStatus.APPROVED)
        assert TRANSFER_FLOW.can_transition(TransferStatus.APPROVED, TransferStatus.IN_TRANSIT)
        assert TRANSFER_FLOW.can_transition(TransferStatus.IN_TRANSIT, TransferStatus.RECEIVED)

    def test_a_draft_cannot_be_dispatched_without_approval(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            TRANSFER_FLOW.assert_transition(TransferStatus.DRAFT, TransferStatus.IN_TRANSIT)
        assert excinfo.value.details["allowed"] == ["APPROVED", "CANCELLED"]

    def test_stock_on_a_vehicle_cannot_be_cancelled(self) -> None:
        """Cancelling in transit would leave the quantity owned by nobody."""
        assert not TRANSFER_FLOW.can_transition(
            TransferStatus.IN_TRANSIT, TransferStatus.CANCELLED
        )
        assert TRANSFER_FLOW.allowed_targets(TransferStatus.IN_TRANSIT) == [
            TransferStatus.RECEIVED
        ]

    def test_a_received_transfer_is_terminal(self) -> None:
        assert TRANSFER_FLOW.is_terminal(TransferStatus.RECEIVED)
        assert TRANSFER_FLOW.allowed_targets(TransferStatus.RECEIVED) == []

    def test_dispatch_and_receive_are_marked_irreversible(self) -> None:
        """The UI must confirm before either: both write the stock ledger."""
        for source, target in (
            (TransferStatus.APPROVED, TransferStatus.IN_TRANSIT),
            (TransferStatus.IN_TRANSIT, TransferStatus.RECEIVED),
        ):
            transition = TRANSFER_FLOW.find(source, target)
            assert transition is not None
            assert transition.is_irreversible

    def test_every_transition_needs_the_transfer_permission(self) -> None:
        assert all(
            t.permission is Permission.INVENTORY_TRANSFER for t in TRANSFER_FLOW.transitions
        )


class TestAdjustmentFlow:
    def test_a_small_adjustment_can_post_directly(self) -> None:
        assert ADJUSTMENT_FLOW.can_transition(AdjustmentStatus.DRAFT, AdjustmentStatus.POSTED)

    def test_the_approval_route_exists_and_ends_at_posted(self) -> None:
        assert ADJUSTMENT_FLOW.can_transition(
            AdjustmentStatus.DRAFT, AdjustmentStatus.PENDING_APPROVAL
        )
        assert ADJUSTMENT_FLOW.can_transition(
            AdjustmentStatus.PENDING_APPROVAL, AdjustmentStatus.APPROVED
        )
        assert ADJUSTMENT_FLOW.can_transition(AdjustmentStatus.APPROVED, AdjustmentStatus.POSTED)

    def test_approval_and_rejection_need_the_approve_permission(self) -> None:
        for target in (AdjustmentStatus.APPROVED, AdjustmentStatus.REJECTED):
            transition = ADJUSTMENT_FLOW.find(AdjustmentStatus.PENDING_APPROVAL, target)
            assert transition is not None
            assert transition.permission is Permission.INVENTORY_ADJUST_APPROVE

    def test_a_rejected_adjustment_cannot_be_resurrected(self) -> None:
        """Reworking a rejection means raising a new document, with a new number."""
        assert ADJUSTMENT_FLOW.is_terminal(AdjustmentStatus.REJECTED)
        assert ADJUSTMENT_FLOW.allowed_targets(AdjustmentStatus.REJECTED) == []

    def test_a_posted_adjustment_is_terminal(self) -> None:
        assert ADJUSTMENT_FLOW.is_terminal(AdjustmentStatus.POSTED)
        with pytest.raises(InvalidStateTransitionError):
            ADJUSTMENT_FLOW.assert_transition(
                AdjustmentStatus.POSTED, AdjustmentStatus.CANCELLED
            )

    def test_pending_approval_cannot_skip_straight_to_posted(self) -> None:
        assert not ADJUSTMENT_FLOW.can_transition(
            AdjustmentStatus.PENDING_APPROVAL, AdjustmentStatus.POSTED
        )


class TestPhysicalCountFlow:
    def test_the_happy_path(self) -> None:
        assert COUNT_FLOW.can_transition(
            PhysicalCountStatus.DRAFT, PhysicalCountStatus.IN_PROGRESS
        )
        assert COUNT_FLOW.can_transition(
            PhysicalCountStatus.IN_PROGRESS, PhysicalCountStatus.COUNTED
        )
        assert COUNT_FLOW.can_transition(
            PhysicalCountStatus.COUNTED, PhysicalCountStatus.CONFIRMED
        )

    def test_a_finished_sheet_can_be_reopened_for_a_recount(self) -> None:
        assert COUNT_FLOW.can_transition(
            PhysicalCountStatus.COUNTED, PhysicalCountStatus.IN_PROGRESS
        )

    def test_a_confirmed_count_cannot_be_reopened(self) -> None:
        """Its variance is already in the ledger; a correction is a new document."""
        assert COUNT_FLOW.is_terminal(PhysicalCountStatus.CONFIRMED)
        assert COUNT_FLOW.allowed_targets(PhysicalCountStatus.CONFIRMED) == []

    def test_a_draft_cannot_be_confirmed_without_counting(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            COUNT_FLOW.assert_transition(
                PhysicalCountStatus.DRAFT, PhysicalCountStatus.CONFIRMED
            )

    def test_confirmation_requires_the_reconcile_permission(self) -> None:
        """Counting is a storekeeper's job; writing the variance off is not."""
        transition = COUNT_FLOW.find(
            PhysicalCountStatus.COUNTED, PhysicalCountStatus.CONFIRMED
        )
        assert transition is not None
        assert transition.permission is Permission.INVENTORY_RECONCILE
        assert transition.is_irreversible
