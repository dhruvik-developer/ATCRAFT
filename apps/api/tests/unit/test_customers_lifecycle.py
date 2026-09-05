"""Customer lifecycle, credit standing, channel authority and GST helpers.

No database: everything here is a decision made from a record's flags plus the
caller's permissions, and each one is reachable through the API in the wrong
order if the machine is wrong.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.core.clock import utcnow
from src.core.deps import Principal
from src.core.errors import (
    BusinessRuleError,
    InvalidStateTransitionError,
    PermissionDeniedError,
)
from src.core.permissions import Permission
from src.modules.customers.models import CustomerType
from src.modules.customers.service import (
    CREDIT_STANDING,
    CUSTOMER_LIFECYCLE,
    DEALER_CHANNELS,
    CreditStanding,
    MasterLifecycleState,
    assert_channel_write_allowed,
    assert_gstin_matches_state,
    credit_standing,
    gstin_state_code,
    lifecycle_state,
    requires_dealer_permission,
)

pytestmark = pytest.mark.unit


def _row(*, is_active: bool = True, deleted: bool = False, blocked: bool = False):
    return SimpleNamespace(
        is_active=is_active,
        deleted_at=utcnow() if deleted else None,
        is_blocked=blocked,
    )


def _principal(*permissions: Permission) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="sales@test.local",
        full_name="Sales User",
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
    )


class TestLifecycleDerivation:
    def test_active_row_is_active(self) -> None:
        assert lifecycle_state(_row()) is MasterLifecycleState.ACTIVE

    def test_deactivated_row_is_inactive(self) -> None:
        assert lifecycle_state(_row(is_active=False)) is MasterLifecycleState.INACTIVE

    def test_retired_row_is_archived_even_when_flagged_active(self) -> None:
        # deleted_at wins: a retired account that still carries is_active=True
        # from before the retirement must not read as usable.
        assert lifecycle_state(_row(is_active=True, deleted=True)) is MasterLifecycleState.ARCHIVED

    def test_credit_standing_follows_the_block_flag(self) -> None:
        assert credit_standing(_row()) is CreditStanding.GOOD_STANDING
        assert credit_standing(_row(blocked=True)) is CreditStanding.ON_HOLD


class TestStateMachines:
    def test_graphs_are_structurally_sound(self) -> None:
        # No unreachable state, no non-terminal state without an exit.
        CUSTOMER_LIFECYCLE.validate()
        CREDIT_STANDING.validate()

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
    def test_legal_lifecycle_transitions(
        self, source: MasterLifecycleState, target: MasterLifecycleState
    ) -> None:
        assert CUSTOMER_LIFECYCLE.can_transition(source, target)

    def test_restore_never_lands_on_active(self) -> None:
        """A restored account comes back inactive: someone must confirm its
        credit terms before it can order again."""
        assert CUSTOMER_LIFECYCLE.allowed_targets(MasterLifecycleState.ARCHIVED) == [
            MasterLifecycleState.INACTIVE
        ]

    def test_retired_customer_cannot_be_retired_twice(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc:
            CUSTOMER_LIFECYCLE.assert_transition(
                MasterLifecycleState.ARCHIVED, MasterLifecycleState.ARCHIVED
            )
        assert exc.value.details["current_status"] == "ARCHIVED"

    def test_blocking_an_already_blocked_account_is_refused(self) -> None:
        """Re-blocking would overwrite the original reason and timestamp, which
        are the evidence of why the hold exists."""
        with pytest.raises(InvalidStateTransitionError):
            CREDIT_STANDING.assert_transition(CreditStanding.ON_HOLD, CreditStanding.ON_HOLD)

    def test_hold_can_be_released_and_reimposed(self) -> None:
        assert CREDIT_STANDING.can_transition(CreditStanding.ON_HOLD, CreditStanding.GOOD_STANDING)
        assert CREDIT_STANDING.can_transition(CreditStanding.GOOD_STANDING, CreditStanding.ON_HOLD)

    def test_block_requires_the_customer_update_permission(self) -> None:
        transition = CREDIT_STANDING.find(CreditStanding.GOOD_STANDING, CreditStanding.ON_HOLD)
        assert transition is not None
        assert transition.permission is Permission.CUSTOMER_UPDATE


class TestChannelAuthority:
    def test_dealer_channels_are_dealer_and_distributor(self) -> None:
        assert {CustomerType.DEALER, CustomerType.DISTRIBUTOR} == DEALER_CHANNELS
        assert requires_dealer_permission(CustomerType.DEALER)
        assert requires_dealer_permission(CustomerType.DISTRIBUTOR)
        assert not requires_dealer_permission(CustomerType.CUSTOMER)
        assert not requires_dealer_permission(CustomerType.OEM)

    def test_plain_customer_needs_no_dealer_permission(self) -> None:
        assert_channel_write_allowed(_principal(Permission.CUSTOMER_UPDATE), CustomerType.CUSTOMER)

    def test_dealer_write_without_dealer_manage_is_denied(self) -> None:
        with pytest.raises(PermissionDeniedError) as exc:
            assert_channel_write_allowed(
                _principal(Permission.CUSTOMER_UPDATE), CustomerType.DEALER
            )
        assert exc.value.details["required_permission"] == str(Permission.DEALER_MANAGE)

    def test_dealer_write_with_dealer_manage_is_allowed(self) -> None:
        assert_channel_write_allowed(
            _principal(Permission.CUSTOMER_UPDATE, Permission.DEALER_MANAGE),
            CustomerType.DEALER,
        )

    def test_moving_an_account_out_of_the_dealer_channel_is_also_gated(self) -> None:
        """Both the current and the proposed channel are checked, so a dealer
        cannot be converted to a plain customer to escape the restriction."""
        with pytest.raises(PermissionDeniedError):
            assert_channel_write_allowed(
                _principal(Permission.CUSTOMER_UPDATE),
                CustomerType.DEALER,
                CustomerType.CUSTOMER,
            )

    def test_superuser_passes(self) -> None:
        superuser = Principal(
            user_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            email="root@test.local",
            full_name="Root",
            is_superuser=True,
            permissions=frozenset(),
        )
        assert_channel_write_allowed(superuser, CustomerType.DISTRIBUTOR)


class TestGstinHelpers:
    def test_state_code_is_the_gstin_prefix(self) -> None:
        assert gstin_state_code("27ABCDE1234F1Z0") == "27"

    @pytest.mark.parametrize("value", [None, "", "2"])
    def test_unusable_gstin_yields_no_state_code(self, value: str | None) -> None:
        assert gstin_state_code(value) is None

    def test_matching_state_code_passes(self) -> None:
        assert_gstin_matches_state("27ABCDE1234F1Z0", "27")

    def test_mismatched_state_code_is_refused(self) -> None:
        with pytest.raises(BusinessRuleError) as exc:
            assert_gstin_matches_state("27ABCDE1234F1Z0", "29")
        assert exc.value.details == {"gstin_state_code": "27", "address_state_code": "29"}

    @pytest.mark.parametrize(
        ("gstin", "state_code"),
        [(None, "27"), ("27ABCDE1234F1Z0", None), (None, None)],
    )
    def test_check_is_skipped_when_either_side_is_absent(
        self, gstin: str | None, state_code: str | None
    ) -> None:
        # An unregistered customer has no GSTIN; an export address has no
        # Indian state code. Neither is an error.
        assert_gstin_matches_state(gstin, state_code)
