"""User and role lifecycle machines, plus temporary-password generation.

No database. Each state machine is asserted to be structurally sound and to
refuse the specific moves that would otherwise be reachable through the API in
the wrong order - reactivating a retired account, deleting a role twice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.clock import utcnow
from src.core.config import settings
from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.core.security import validate_password_policy
from src.modules.users.service import (
    ROLE_LIFECYCLE,
    USER_LIFECYCLE,
    AccountState,
    generate_temporary_password,
    lifecycle_state,
)

pytestmark = pytest.mark.unit


def _row(*, is_active: bool = True, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(is_active=is_active, deleted_at=utcnow() if deleted else None)


class TestLifecycleDerivation:
    def test_active_row_is_active(self) -> None:
        assert lifecycle_state(_row()) is AccountState.ACTIVE

    def test_deactivated_row_is_inactive(self) -> None:
        assert lifecycle_state(_row(is_active=False)) is AccountState.INACTIVE

    def test_retired_row_is_archived_even_when_flagged_active(self) -> None:
        # deleted_at wins: a retired account still carrying is_active=True from
        # before the retirement must not read as usable.
        assert lifecycle_state(_row(is_active=True, deleted=True)) is AccountState.ARCHIVED


class TestUserLifecycle:
    def test_graph_is_structurally_sound(self) -> None:
        # No unreachable state, and no non-terminal state without an exit.
        USER_LIFECYCLE.validate()

    def test_deactivate_and_reactivate_are_both_legal(self) -> None:
        assert USER_LIFECYCLE.can_transition(AccountState.ACTIVE, AccountState.INACTIVE)
        assert USER_LIFECYCLE.can_transition(AccountState.INACTIVE, AccountState.ACTIVE)

    def test_retirement_is_terminal(self) -> None:
        # A returning employee gets a new account rather than inheriting the
        # sessions, grants and lockout state of the old one.
        assert USER_LIFECYCLE.is_terminal(AccountState.ARCHIVED)
        assert USER_LIFECYCLE.allowed_targets(AccountState.ARCHIVED) == []

    def test_reactivating_a_retired_account_is_refused(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            USER_LIFECYCLE.assert_transition(AccountState.ARCHIVED, AccountState.ACTIVE)
        assert excinfo.value.details["current_status"] == str(AccountState.ARCHIVED)
        assert excinfo.value.details["allowed"] == []

    def test_deactivating_an_inactive_account_is_refused(self) -> None:
        # Not a no-op: it would write a second status-change row claiming a
        # transition that did not happen.
        with pytest.raises(InvalidStateTransitionError):
            USER_LIFECYCLE.assert_transition(AccountState.INACTIVE, AccountState.INACTIVE)

    def test_every_transition_is_gated_on_the_deactivate_permission(self) -> None:
        assert {t.permission for t in USER_LIFECYCLE.transitions} == {Permission.USER_DEACTIVATE}

    def test_retirement_is_marked_irreversible(self) -> None:
        retire = USER_LIFECYCLE.find(AccountState.ACTIVE, AccountState.ARCHIVED)
        assert retire is not None
        assert retire.is_irreversible

    def test_available_actions_respect_the_caller(self) -> None:
        assert USER_LIFECYCLE.available_actions(AccountState.ACTIVE, []) == []
        actions = USER_LIFECYCLE.available_actions(
            AccountState.ACTIVE, [Permission.USER_DEACTIVATE]
        )
        assert {t.target for t in actions} == {AccountState.INACTIVE, AccountState.ARCHIVED}


class TestRoleLifecycle:
    def test_graph_is_structurally_sound(self) -> None:
        ROLE_LIFECYCLE.validate()

    def test_a_role_can_be_suspended_and_reinstated(self) -> None:
        assert ROLE_LIFECYCLE.can_transition(AccountState.ACTIVE, AccountState.INACTIVE)
        assert ROLE_LIFECYCLE.can_transition(AccountState.INACTIVE, AccountState.ACTIVE)

    def test_deleting_a_retired_role_is_refused(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            ROLE_LIFECYCLE.assert_transition(AccountState.ARCHIVED, AccountState.ARCHIVED)

    def test_every_transition_is_gated_on_role_manage(self) -> None:
        assert {t.permission for t in ROLE_LIFECYCLE.transitions} == {Permission.ROLE_MANAGE}


class TestTemporaryPassword:
    def test_generated_password_satisfies_the_policy(self) -> None:
        for _ in range(25):
            validate_password_policy(generate_temporary_password("person@example.com"))

    def test_generated_password_is_valid_against_the_owning_address(self) -> None:
        # The policy forbids a password containing the account's e-mail local
        # part, and a one-character local part can appear inside random base64 -
        # which is exactly why the generator retries.
        for _ in range(25):
            candidate = generate_temporary_password("a@example.com")
            validate_password_policy(candidate, email="a@example.com")

    def test_generated_passwords_are_not_reused(self) -> None:
        # An administrator who invents passwords reuses them; a generator that
        # did the same would be no improvement.
        assert len({generate_temporary_password() for _ in range(20)}) == 20

    def test_generated_password_clears_the_configured_minimum_length(self) -> None:
        assert len(generate_temporary_password()) >= settings.password_min_length
