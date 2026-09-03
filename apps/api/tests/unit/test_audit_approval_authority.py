"""Segregation of duties on the approval queue.

The rule these tests pin: the person who raised a request may not decide it, and
the person who decides it must hold the permission the underlying action needs.
An approval given by its own author is not a second judgement, and an approval
given by someone without the authority to perform the action is not an approval
at all - both would make the evidence master rules section 42 asks for worthless.

Pure logic - the authority decision is a function of a principal and two fields
off the request row, so none of this needs a database.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.deps import Principal
from src.core.errors import (
    AuthorizationError,
    InvalidStateTransitionError,
    PermissionDeniedError,
)
from src.core.permissions import Permission
from src.modules.audit.query_service import (
    APPROVAL_DECISION_MACHINE,
    DECIDABLE_PERMISSIONS,
    SELF_APPROVAL_BLOCKED,
    assert_may_decide,
    decidable_actions_for,
    evaluate_decision_authority,
    permission_for_action,
)
from src.modules.audit.schemas import ApprovalStatus

pytestmark = pytest.mark.unit

REQUESTER = uuid.uuid4()
APPROVER = uuid.uuid4()
PO_APPROVE = str(Permission.PURCHASE_ORDER_APPROVE)


def make_principal(
    user_id: uuid.UUID,
    *permissions: Permission,
    is_superuser: bool = False,
) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=uuid.uuid4(),
        email="approver@test.local",
        full_name="Approver",
        is_superuser=is_superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


# --- the segregation-of-duties block -----------------------------------------


def test_a_qualified_approver_may_decide_someone_elses_request():
    principal = make_principal(APPROVER, Permission.PURCHASE_ORDER_APPROVE)

    authority = evaluate_decision_authority(
        principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE
    )

    assert authority.allowed is True
    assert authority.permission is Permission.PURCHASE_ORDER_APPROVE
    assert authority.reason is None
    assert authority.is_self_request is False


def test_the_requester_may_not_approve_their_own_request():
    principal = make_principal(REQUESTER, Permission.PURCHASE_ORDER_APPROVE)

    authority = evaluate_decision_authority(
        principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE
    )

    assert authority.allowed is False
    assert authority.is_self_request is True
    assert authority.reason == SELF_APPROVAL_BLOCKED


def test_self_approval_raises_authorization_not_permission_denied():
    """The caller *has* the permission, so telling them to go and get it is wrong."""
    principal = make_principal(REQUESTER, Permission.PURCHASE_ORDER_APPROVE)

    with pytest.raises(AuthorizationError) as excinfo:
        assert_may_decide(principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE)

    assert not isinstance(excinfo.value, PermissionDeniedError)
    assert excinfo.value.status_code == 403


def test_a_superuser_may_decide_their_own_request():
    # The escape hatch: a superuser can already perform the underlying action
    # directly, so blocking them here would only move the work somewhere that
    # leaves less evidence. The audit entry is the control instead.
    principal = make_principal(REQUESTER, is_superuser=True)

    authority = assert_may_decide(principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE)

    assert authority.allowed is True
    assert authority.is_self_request is True


def test_an_anonymous_request_is_not_treated_as_self_raised():
    # created_by_id is nullable (a request raised by a scheduled job). NULL must
    # not compare equal to the approver.
    principal = make_principal(APPROVER, Permission.PURCHASE_ORDER_APPROVE)

    authority = evaluate_decision_authority(
        principal, requested_by_id=None, requested_action=PO_APPROVE
    )

    assert authority.allowed is True
    assert authority.is_self_request is False


# --- the permission the underlying action requires ---------------------------


def test_holding_a_different_approval_permission_is_not_enough():
    principal = make_principal(APPROVER, Permission.PAYMENT_APPROVE)

    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_may_decide(principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE)

    assert excinfo.value.details["required_permission"] == PO_APPROVE


def test_missing_permission_is_reported_before_self_approval():
    """A caller with no authority over the request learns nothing about who raised it."""
    principal = make_principal(REQUESTER, Permission.PAYMENT_APPROVE)

    authority = evaluate_decision_authority(
        principal, requested_by_id=REQUESTER, requested_action=PO_APPROVE
    )

    assert authority.reason is not None
    assert SELF_APPROVAL_BLOCKED not in authority.reason


def test_an_unknown_action_is_superuser_only():
    ordinary = make_principal(APPROVER, *DECIDABLE_PERMISSIONS)
    superuser = make_principal(APPROVER, is_superuser=True)

    assert (
        evaluate_decision_authority(
            ordinary, requested_by_id=REQUESTER, requested_action="something.invented"
        ).allowed
        is False
    )
    assert (
        evaluate_decision_authority(
            superuser, requested_by_id=REQUESTER, requested_action="something.invented"
        ).allowed
        is True
    )


def test_an_unknown_action_raises_authorization_error_for_an_ordinary_user():
    principal = make_principal(APPROVER, *DECIDABLE_PERMISSIONS)

    with pytest.raises(AuthorizationError):
        assert_may_decide(
            principal, requested_by_id=REQUESTER, requested_action="something.invented"
        )


def test_requested_action_maps_onto_the_permission_registry():
    assert permission_for_action("  Purchase_Order.Approve ") is (Permission.PURCHASE_ORDER_APPROVE)
    assert permission_for_action("not.a.permission") is None


def test_the_decidable_permission_set_is_derived_from_the_registry():
    # Derived rather than hand-listed: a hand-listed set that missed a new
    # permission would make its requests invisible to everyone who holds it.
    assert Permission.PURCHASE_ORDER_APPROVE in DECIDABLE_PERMISSIONS
    assert Permission.PAYMENT_APPROVE in DECIDABLE_PERMISSIONS
    assert Permission.INVENTORY_ADJUST_APPROVE in DECIDABLE_PERMISSIONS
    assert Permission.PURCHASE_ORDER_VIEW not in DECIDABLE_PERMISSIONS


# --- what reaches the queue ---------------------------------------------------


def test_the_queue_is_narrowed_to_the_permissions_actually_held():
    principal = make_principal(APPROVER, Permission.BOM_APPROVE, Permission.ITEM_VIEW)

    assert decidable_actions_for(principal) == frozenset({str(Permission.BOM_APPROVE)})


def test_a_principal_with_no_approval_rights_gets_an_empty_set_not_none():
    # The distinction is load-bearing: None means "no filter" in the repository,
    # so returning it here would show every pending request to a user with no
    # approval authority at all.
    principal = make_principal(APPROVER, Permission.ITEM_VIEW)

    assert decidable_actions_for(principal) == frozenset()


def test_a_superuser_queue_is_unrestricted():
    principal = make_principal(APPROVER, is_superuser=True)

    assert decidable_actions_for(principal) is None


# --- the decision state machine ----------------------------------------------


def test_the_decision_machine_is_structurally_sound():
    APPROVAL_DECISION_MACHINE.validate()


def test_pending_is_the_only_state_a_decision_can_be_made_from():
    assert set(APPROVAL_DECISION_MACHINE.allowed_targets(ApprovalStatus.PENDING)) == {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
    }
    assert APPROVAL_DECISION_MACHINE.allowed_targets(ApprovalStatus.APPROVED) == []


def test_a_decided_request_cannot_be_decided_again():
    with pytest.raises(InvalidStateTransitionError) as excinfo:
        APPROVAL_DECISION_MACHINE.assert_transition(
            ApprovalStatus.APPROVED, ApprovalStatus.REJECTED
        )

    assert excinfo.value.details["current_status"] == "APPROVED"
    assert excinfo.value.details["allowed"] == []


def test_both_decisions_are_terminal_and_irreversible():
    assert APPROVAL_DECISION_MACHINE.is_terminal(ApprovalStatus.APPROVED)
    assert APPROVAL_DECISION_MACHINE.is_terminal(ApprovalStatus.REJECTED)
    assert all(t.is_irreversible for t in APPROVAL_DECISION_MACHINE.transitions)
