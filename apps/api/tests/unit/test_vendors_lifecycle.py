"""Vendor and transporter lifecycle rules, plus the small pure helpers.

The vendor lifecycle is derived from three flags rather than stored in a status
column, so these tests pin down two separate things: that the derivation is
right (including the precedence between the flags), and that the state machine
refuses the moves that would otherwise be reachable by calling the endpoints in
the wrong order - notably "restore straight to approved", which would let a
retirement be used to launder an approval.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.clock import utcnow
from src.core.errors import ErrorCode, InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.vendors.service import (
    ACCOUNT_NUMBER_VISIBLE_DIGITS,
    TRANSPORTER_LIFECYCLE,
    VENDOR_LIFECYCLE,
    TransporterState,
    VendorState,
    build_tracking_url,
    can_receive_purchase_orders,
    mask_account_number,
    referenced_in_use,
    template_has_placeholder,
    transporter_state,
    vendor_state,
)

pytestmark = pytest.mark.unit


def _vendor(
    *, approved: bool = False, active: bool = True, deleted: bool = False
) -> SimpleNamespace:
    """Minimal stand-in for a persisted vendor row.

    Deliberately not an ORM instance: the derivation reads three attributes and
    nothing else, and a plain object keeps the test free of a mapper.
    """
    return SimpleNamespace(
        is_approved=approved, is_active=active, deleted_at=utcnow() if deleted else None
    )


class TestVendorStateDerivation:
    def test_new_vendor_is_pending_approval(self) -> None:
        assert vendor_state(_vendor()) is VendorState.PENDING_APPROVAL

    def test_approved_and_active_vendor_is_approved(self) -> None:
        assert vendor_state(_vendor(approved=True)) is VendorState.APPROVED

    def test_deactivation_wins_over_approval(self) -> None:
        # An approved vendor that has been switched off must not read as usable;
        # procurement checks this derivation before raising an order.
        assert vendor_state(_vendor(approved=True, active=False)) is VendorState.SUSPENDED

    def test_retirement_wins_over_everything(self) -> None:
        retired = _vendor(approved=True, active=True, deleted=True)
        assert vendor_state(retired) is VendorState.ARCHIVED

    @pytest.mark.parametrize(
        ("vendor", "expected"),
        [
            (_vendor(approved=True), True),
            (_vendor(approved=False), False),
            (_vendor(approved=True, active=False), False),
            (_vendor(approved=True, deleted=True), False),
        ],
    )
    def test_purchase_order_eligibility_needs_all_three_flags(
        self, vendor: SimpleNamespace, expected: bool
    ) -> None:
        assert can_receive_purchase_orders(vendor) is expected


class TestVendorStateMachine:
    def test_graph_is_structurally_sound(self) -> None:
        # No unreachable state, no non-terminal state without an exit.
        VENDOR_LIFECYCLE.validate()
        TRANSPORTER_LIFECYCLE.validate()

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (VendorState.PENDING_APPROVAL, VendorState.APPROVED),
            (VendorState.APPROVED, VendorState.PENDING_APPROVAL),
            (VendorState.APPROVED, VendorState.SUSPENDED),
            (VendorState.SUSPENDED, VendorState.APPROVED),
            (VendorState.SUSPENDED, VendorState.ARCHIVED),
            (VendorState.ARCHIVED, VendorState.SUSPENDED),
        ],
    )
    def test_legal_transitions(self, source: VendorState, target: VendorState) -> None:
        assert VENDOR_LIFECYCLE.can_transition(source, target)

    def test_restore_never_lands_on_approved(self) -> None:
        """A retirement must not be usable as a way to re-approve a vendor."""
        assert not VENDOR_LIFECYCLE.can_transition(VendorState.ARCHIVED, VendorState.APPROVED)
        assert VENDOR_LIFECYCLE.allowed_targets(VendorState.ARCHIVED) == [VendorState.SUSPENDED]

    def test_archived_vendor_cannot_be_re_archived(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            VENDOR_LIFECYCLE.assert_transition(VendorState.ARCHIVED, VendorState.ARCHIVED)
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert excinfo.value.details["current_status"] == "ARCHIVED"

    def test_retirement_requires_the_delete_permission_not_merely_update(self) -> None:
        """Retiring is a stronger act than editing, and the graph says so."""
        transition = VENDOR_LIFECYCLE.find(VendorState.APPROVED, VendorState.ARCHIVED)
        assert transition is not None
        assert transition.permission is Permission.VENDOR_DELETE
        assert transition.is_irreversible is True

    def test_an_editor_without_delete_is_offered_no_retire_action(self) -> None:
        actions = VENDOR_LIFECYCLE.available_actions(
            VendorState.APPROVED, held=[Permission.VENDOR_UPDATE]
        )
        targets = {t.target for t in actions}
        assert VendorState.ARCHIVED not in targets
        assert VendorState.SUSPENDED in targets


class TestTransporterStateMachine:
    def test_state_derivation(self) -> None:
        assert (
            transporter_state(SimpleNamespace(is_active=True, deleted_at=None))
            is TransporterState.ACTIVE
        )
        assert (
            transporter_state(SimpleNamespace(is_active=False, deleted_at=None))
            is TransporterState.INACTIVE
        )
        assert (
            transporter_state(SimpleNamespace(is_active=True, deleted_at=utcnow()))
            is TransporterState.ARCHIVED
        )

    def test_restore_returns_inactive(self) -> None:
        assert TRANSPORTER_LIFECYCLE.allowed_targets(TransporterState.ARCHIVED) == [
            TransporterState.INACTIVE
        ]


class TestAccountNumberMasking:
    def test_only_the_last_four_characters_survive(self) -> None:
        masked = mask_account_number("50100123456789")
        assert masked is not None
        assert masked.endswith("6789")
        assert "50100" not in masked
        assert len(masked) == len("50100123456789")

    def test_short_numbers_are_fully_masked(self) -> None:
        # Nothing meaningful can be revealed from four characters or fewer, so
        # none of them are shown at all.
        assert mask_account_number("1234") == "****"

    def test_absent_number_stays_absent(self) -> None:
        assert mask_account_number(None) is None
        assert mask_account_number("") is None

    def test_visible_digit_count_is_the_documented_constant(self) -> None:
        masked = mask_account_number("9" * 20)
        assert masked is not None
        assert masked.count("9") == ACCOUNT_NUMBER_VISIBLE_DIGITS


class TestTrackingUrl:
    def test_placeholder_is_substituted(self) -> None:
        url = build_tracking_url("https://carrier.example/t?awb={tracking_id}", "ABC123")
        assert url == "https://carrier.example/t?awb=ABC123"

    def test_tracking_id_is_percent_encoded(self) -> None:
        """Consignment numbers with slashes are common and must not split the URL."""
        url = build_tracking_url("https://carrier.example/t/{tracking_id}", "MH/2026/0012")
        assert url == "https://carrier.example/t/MH%2F2026%2F0012"

    def test_query_injection_via_the_tracking_id_is_neutralised(self) -> None:
        url = build_tracking_url(
            "https://carrier.example/t?awb={tracking_id}", "X&redirect=evil.example"
        )
        assert url is not None
        assert "&redirect=" not in url

    def test_absent_template_yields_no_link(self) -> None:
        assert build_tracking_url(None, "ABC123") is None

    def test_blank_tracking_id_yields_no_link(self) -> None:
        assert build_tracking_url("https://carrier.example/{tracking_id}", "   ") is None

    def test_placeholder_detection(self) -> None:
        assert template_has_placeholder("https://x.example/{tracking_id}")
        assert not template_has_placeholder("https://x.example/track")


class TestReferencedInUse:
    def test_carries_the_machine_readable_code(self) -> None:
        error = referenced_in_use("Still in use", vendor_code="VEND-001")
        assert error.code is ErrorCode.REFERENCED_RECORD_IN_USE
        assert error.status_code == 422
        assert error.details == {"vendor_code": "VEND-001"}
