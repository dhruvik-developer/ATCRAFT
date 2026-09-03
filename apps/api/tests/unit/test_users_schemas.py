"""Edge validation for the user and role contracts.

No database. These assert the normalisations the rest of the module relies on:
an e-mail that reaches the duplicate check already lower-cased, a role code that
cannot vary by punctuation, a permission list that cannot contain a typo, and
update models that can distinguish "not supplied" from "set to null".
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.core.permissions import Permission
from src.modules.users.schemas import (
    MAX_ROLES_PER_USER,
    PasswordResetRequest,
    RoleAssignment,
    RoleCreate,
    RoleFilters,
    RoleUpdate,
    UserCreate,
    UserUpdate,
)

pytestmark = pytest.mark.unit


def _user(**overrides: object) -> UserCreate:
    payload: dict[str, object] = {
        "email": "Person@Example.COM",
        "full_name": "  A   Person ",
        "password": "Sufficiently-Long!7",
    }
    payload.update(overrides)
    return UserCreate(**payload)  # type: ignore[arg-type]


class TestUserCreate:
    def test_email_is_lower_cased_and_trimmed(self) -> None:
        # The duplicate check, the audit summary and the password-policy check
        # all read this string before it reaches the CIText column.
        assert _user().email == "person@example.com"

    def test_name_whitespace_is_collapsed(self) -> None:
        assert _user().full_name == "A Person"

    def test_blank_name_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _user(full_name="   ")

    def test_empty_optional_text_becomes_null(self) -> None:
        # An empty string and NULL must not both mean "not supplied", or a
        # report filtering on employee_code IS NULL misses half the staff.
        created = _user(phone="  ", employee_code="")
        assert created.phone is None
        assert created.employee_code is None

    def test_superuser_cannot_be_requested(self) -> None:
        # The flag bypasses every permission check in the system; it is not part
        # of this contract at all, so a client asking for it is simply ignored.
        assert not hasattr(_user(), "is_superuser")

    def test_unknown_permission_is_refused_with_the_offending_value(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _user(extra_permissions=["purchase_order.aprove"])
        assert "purchase_order.aprove" in str(excinfo.value)

    def test_permissions_are_sorted_and_de_duplicated(self) -> None:
        created = _user(
            extra_permissions=[
                str(Permission.PAYMENT_APPROVE),
                str(Permission.ITEM_VIEW),
                str(Permission.PAYMENT_APPROVE),
            ]
        )
        assert created.extra_permissions == sorted(
            [str(Permission.ITEM_VIEW), str(Permission.PAYMENT_APPROVE)]
        )

    def test_repeated_role_ids_are_collapsed(self) -> None:
        # A duplicate would become a second user_roles row and fail on the
        # composite primary key with a 500 rather than a clear message.
        role_id = uuid.uuid4()
        assert _user(role_ids=[role_id, role_id]).role_ids == [role_id]

    def test_role_list_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            _user(role_ids=[uuid.uuid4() for _ in range(MAX_ROLES_PER_USER + 1)])

    def test_first_password_must_be_changed_by_default(self) -> None:
        # An administrator-chosen first password has been read out loud.
        assert _user().must_change_password is True


class TestUserUpdate:
    def test_unset_fields_are_absent_from_the_dump(self) -> None:
        # This is what makes a PATCH that omits branch_id leave the branch alone.
        payload = UserUpdate(full_name="New Name")
        assert set(payload.model_dump(exclude_unset=True)) == {"full_name"}

    def test_clear_flags_are_distinct_from_null(self) -> None:
        payload = UserUpdate(clear_branch=True)
        dumped = payload.model_dump(exclude_unset=True)
        assert dumped == {"clear_branch": True}
        assert "branch_id" not in dumped

    def test_unknown_fields_are_refused(self) -> None:
        # extra="forbid": a client sending is_superuser or is_active must get an
        # error, not a silently ignored field it believes took effect.
        with pytest.raises(ValidationError):
            UserUpdate(is_superuser=True)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            UserUpdate(is_active=False)  # type: ignore[call-arg]

    def test_an_explicit_empty_permission_list_survives(self) -> None:
        # "Remove every direct grant" must be expressible, and must not be
        # confused with "no change".
        payload = UserUpdate(extra_permissions=[])
        assert payload.model_dump(exclude_unset=True) == {"extra_permissions": []}


class TestRoleContracts:
    @pytest.mark.parametrize(
        "raw", ["purchase manager", "purchase-manager", "  Purchase_Manager  "]
    )
    def test_role_codes_normalise_to_one_form(self, raw: str) -> None:
        assert RoleCreate(code=raw, name="Purchase Manager").code == "PURCHASE_MANAGER"

    @pytest.mark.parametrize("raw", ["1BUYER", "BUY/ER", "BUY.ER", ""])
    def test_malformed_role_codes_are_refused(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            RoleCreate(code=raw, name="Anything")

    def test_system_flag_is_not_part_of_the_contract(self) -> None:
        # System roles are seeded; a client-created role claiming the flag would
        # be undeletable for a reason nobody could trace.
        assert not hasattr(RoleCreate(code="X_ROLE", name="X"), "is_system")

    def test_code_cannot_be_patched(self) -> None:
        with pytest.raises(ValidationError):
            RoleUpdate(code="RENAMED")  # type: ignore[call-arg]

    def test_vendor_flag_cannot_be_patched(self) -> None:
        # Flipping it would, in one statement, either strip the portal boundary
        # from every holder or make every holder illegal.
        with pytest.raises(ValidationError):
            RoleUpdate(is_vendor_role=True)  # type: ignore[call-arg]

    def test_role_filter_rejects_an_unknown_permission(self) -> None:
        with pytest.raises(ValidationError):
            RoleFilters(grants_permission="payment.aprove")
        assert (
            RoleFilters(grants_permission=str(Permission.PAYMENT_APPROVE)).grants_permission
            == str(Permission.PAYMENT_APPROVE)
        )


class TestReasonCapture:
    def test_role_assignment_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError):
            RoleAssignment(role_ids=[])  # type: ignore[call-arg]

    def test_an_empty_role_set_is_a_legal_assignment(self) -> None:
        # "This account holds no roles" must be expressible.
        assert RoleAssignment(role_ids=[], reason="Left the team").role_ids == []

    def test_password_reset_reason_is_mandatory_but_the_password_is_not(self) -> None:
        request = PasswordResetRequest(reason="Forgot it")
        assert request.new_password is None
        with pytest.raises(ValidationError):
            PasswordResetRequest()  # type: ignore[call-arg]
