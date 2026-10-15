"""Permission resolution, the privilege-escalation guard and vendor isolation.

No database: every rule here is a decision made from an already-loaded role set
plus the caller's own authority, and each one is reachable through the API by
somebody who should not be able to reach it if the rule is wrong.

The most important test in this file is
``TestResolutionMatchesTheRequestPath``: the set an administrator is shown must
be the set the request path enforces, and the two are computed by different
functions in different modules.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.core.clock import utcnow
from src.core.deps import Principal, _resolve_permissions
from src.core.errors import AuthorizationError, BusinessRuleError, ErrorCode, PermissionDeniedError
from src.core.permissions import ALL_PERMISSIONS, PERMISSION_GROUPS, Permission
from src.modules.users.service import (
    PROTECT_SUPER_ADMIN_PASSWORDS,
    SUPER_ADMIN_PASSWORD_PROTECTED,
    SUPER_ADMIN_ROLE_CODES,
    assert_can_grant,
    assert_grant_change_allowed,
    assert_may_set_password_for,
    assert_role_assignment_allowed,
    assert_vendor_role_consistency,
    caller_holds_super_admin,
    holds_super_admin,
    permission_catalogue,
    resolve_effective_permissions,
    role_permission_map,
    usable_roles,
)

pytestmark = pytest.mark.unit


def _role(
    code: str,
    *permissions: Permission | str,
    is_vendor_role: bool = False,
    is_dealer_role: bool = False,
    is_active: bool = True,
    deleted: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        permissions=[str(p) for p in permissions],
        is_vendor_role=is_vendor_role,
        is_dealer_role=is_dealer_role,
        is_active=is_active,
        deleted_at=utcnow() if deleted else None,
    )


def _principal(*permissions: Permission | str, superuser: bool = False) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="admin@test.local",
        full_name="Admin User",
        is_superuser=superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------


class TestResolution:
    def test_roles_are_unioned(self) -> None:
        roles = [
            _role("BUYER", Permission.PURCHASE_ORDER_VIEW, Permission.PURCHASE_ORDER_CREATE),
            _role("STORE", Permission.INVENTORY_VIEW, Permission.PURCHASE_ORDER_VIEW),
        ]
        assert resolve_effective_permissions(roles, None, None) == frozenset(
            {
                str(Permission.PURCHASE_ORDER_VIEW),
                str(Permission.PURCHASE_ORDER_CREATE),
                str(Permission.INVENTORY_VIEW),
            }
        )

    def test_direct_grants_are_added(self) -> None:
        roles = [_role("BUYER", Permission.PURCHASE_ORDER_VIEW)]
        resolved = resolve_effective_permissions(
            roles, [str(Permission.PURCHASE_ORDER_APPROVE)], None
        )
        assert str(Permission.PURCHASE_ORDER_APPROVE) in resolved

    def test_denials_beat_a_role_grant(self) -> None:
        # The whole point of the denial list: "this buyer may not approve" must
        # not be undone by giving them another role that happens to include it.
        roles = [
            _role("BUYER", Permission.PURCHASE_ORDER_APPROVE),
            _role("MANAGER", Permission.PURCHASE_ORDER_APPROVE, Permission.PAYMENT_APPROVE),
        ]
        resolved = resolve_effective_permissions(
            roles, None, [str(Permission.PURCHASE_ORDER_APPROVE)]
        )
        assert str(Permission.PURCHASE_ORDER_APPROVE) not in resolved
        assert str(Permission.PAYMENT_APPROVE) in resolved

    def test_denials_beat_a_direct_grant_too(self) -> None:
        resolved = resolve_effective_permissions(
            [], [str(Permission.PAYMENT_APPROVE)], [str(Permission.PAYMENT_APPROVE)]
        )
        assert resolved == frozenset()

    def test_suspended_and_retired_roles_grant_nothing(self) -> None:
        roles = [
            _role("SUSPENDED", Permission.PAYMENT_APPROVE, is_active=False),
            _role("RETIRED", Permission.PAYMENT_RECORD, deleted=True),
            _role("LIVE", Permission.PAYMENT_VIEW),
        ]
        assert resolve_effective_permissions(roles, None, None) == frozenset(
            {str(Permission.PAYMENT_VIEW)}
        )
        assert [role.code for role in usable_roles(roles)] == ["LIVE"]

    def test_provenance_map_excludes_unusable_roles(self) -> None:
        roles = [
            _role("LIVE", Permission.ITEM_VIEW, Permission.BOM_VIEW),
            _role("SUSPENDED", Permission.PAYMENT_APPROVE, is_active=False),
        ]
        assert role_permission_map(roles) == {
            "LIVE": [str(Permission.BOM_VIEW), str(Permission.ITEM_VIEW)]
        }

    def test_superuser_flag_is_not_folded_into_the_stored_set(self) -> None:
        # The flag is authority applied at the check, not a stored permission
        # list. Folding it in here would make this function disagree with
        # Principal.has for the one account where being wrong matters most.
        assert resolve_effective_permissions([], None, None) == frozenset()


class TestResolutionMatchesTheRequestPath:
    """The administration view and the enforcement path must agree exactly."""

    @pytest.mark.parametrize(
        ("extra", "denied"),
        [
            (None, None),
            ([str(Permission.PAYMENT_APPROVE)], None),
            (None, [str(Permission.PURCHASE_ORDER_APPROVE)]),
            ([str(Permission.PAYMENT_APPROVE)], [str(Permission.PAYMENT_APPROVE)]),
        ],
    )
    async def test_agrees_with_core_deps(
        self, extra: list[str] | None, denied: list[str] | None
    ) -> None:
        roles = [
            _role("BUYER", Permission.PURCHASE_ORDER_VIEW, Permission.PURCHASE_ORDER_APPROVE),
            _role("GHOST", Permission.COMPANY_MANAGE, is_active=False),
            _role("GONE", Permission.PAYMENT_RECORD, deleted=True),
        ]
        user = SimpleNamespace(roles=roles, extra_permissions=extra, denied_permissions=denied)

        # _resolve_permissions never touches the session; passing None proves it.
        from_request_path, role_codes = await _resolve_permissions(None, user)  # type: ignore[arg-type]

        assert resolve_effective_permissions(roles, extra, denied) == from_request_path
        assert role_codes == ("BUYER",)


# -----------------------------------------------------------------------------
# Privilege escalation
# -----------------------------------------------------------------------------


class TestPrivilegeEscalationGuard:
    def test_granting_a_held_permission_is_allowed(self) -> None:
        principal = _principal(Permission.PURCHASE_ORDER_APPROVE, Permission.ROLE_MANAGE)
        assert_can_grant(principal, [str(Permission.PURCHASE_ORDER_APPROVE)], what="a role")

    def test_granting_an_unheld_permission_is_refused(self) -> None:
        principal = _principal(Permission.ROLE_MANAGE)
        with pytest.raises(PermissionDeniedError) as excinfo:
            assert_can_grant(principal, [str(Permission.PAYMENT_APPROVE)], what="a role")
        assert excinfo.value.details["missing_permissions"] == [str(Permission.PAYMENT_APPROVE)]

    def test_the_refusal_names_every_missing_permission(self) -> None:
        principal = _principal(Permission.ROLE_MANAGE)
        with pytest.raises(PermissionDeniedError) as excinfo:
            assert_can_grant(
                principal,
                [str(Permission.PAYMENT_APPROVE), str(Permission.COMPANY_MANAGE)],
                what="a role",
            )
        assert excinfo.value.details["missing_permissions"] == sorted(
            [str(Permission.COMPANY_MANAGE), str(Permission.PAYMENT_APPROVE)]
        )

    def test_superusers_are_exempt(self) -> None:
        # The flag already bypasses every check; making it pass this one would
        # be theatre, not defence.
        principal = _principal(superuser=True)
        assert_can_grant(principal, [str(p) for p in ALL_PERMISSIONS], what="a role")

    def test_role_manage_alone_cannot_mint_authority(self) -> None:
        # The attack the guard exists to stop: create a role granting payment
        # approval, assign it to yourself, approve the payment.
        principal = _principal(Permission.ROLE_MANAGE, Permission.PERMISSION_ASSIGN)
        with pytest.raises(PermissionDeniedError):
            assert_can_grant(principal, [str(Permission.PAYMENT_APPROVE)], what="a role")

    def test_an_unchanged_set_needs_no_authority(self) -> None:
        principal = _principal(Permission.ROLE_MANAGE)
        held = [str(Permission.PAYMENT_APPROVE), str(Permission.COMPANY_MANAGE)]
        assert assert_grant_change_allowed(principal, held, list(reversed(held)), what="x") == (
            frozenset()
        )

    def test_revoking_an_unheld_permission_is_refused(self) -> None:
        # Symmetry matters: stripping authority you do not hold is how a
        # low-privileged administrator locks out the people who could stop them.
        principal = _principal(Permission.ROLE_MANAGE)
        with pytest.raises(PermissionDeniedError):
            assert_grant_change_allowed(
                principal, [str(Permission.PAYMENT_APPROVE)], [], what="a role"
            )

    def test_the_delta_is_what_gets_checked(self) -> None:
        principal = _principal(Permission.ITEM_VIEW, Permission.BOM_VIEW)
        delta = assert_grant_change_allowed(
            principal,
            [str(Permission.ITEM_VIEW)],
            [str(Permission.ITEM_VIEW), str(Permission.BOM_VIEW)],
            what="a role",
        )
        assert delta == frozenset({str(Permission.BOM_VIEW)})


class TestRoleAssignmentGuard:
    def test_assigning_a_role_grants_all_of_its_permissions(self) -> None:
        principal = _principal(Permission.PERMISSION_ASSIGN, Permission.ITEM_VIEW)
        added = [_role("BUYER", Permission.ITEM_VIEW, Permission.PAYMENT_APPROVE)]
        with pytest.raises(PermissionDeniedError) as excinfo:
            assert_role_assignment_allowed(principal, added, [])
        assert str(Permission.PAYMENT_APPROVE) in excinfo.value.details["missing_permissions"]

    def test_assigning_a_role_the_caller_fully_holds_is_allowed(self) -> None:
        principal = _principal(
            Permission.PERMISSION_ASSIGN, Permission.ITEM_VIEW, Permission.BOM_VIEW
        )
        added = [_role("ENGINEER", Permission.ITEM_VIEW, Permission.BOM_VIEW)]
        assert assert_role_assignment_allowed(principal, added, []) == frozenset(
            {str(Permission.ITEM_VIEW), str(Permission.BOM_VIEW)}
        )

    def test_removing_a_more_privileged_role_is_refused(self) -> None:
        principal = _principal(Permission.PERMISSION_ASSIGN)
        removed = [_role("SUPER_ADMIN", Permission.COMPANY_MANAGE)]
        with pytest.raises(PermissionDeniedError):
            assert_role_assignment_allowed(principal, [], removed)


# -----------------------------------------------------------------------------
# Vendor-portal isolation
# -----------------------------------------------------------------------------


class TestVendorIsolation:
    def test_internal_account_may_not_hold_a_vendor_role(self) -> None:
        with pytest.raises(BusinessRuleError) as excinfo:
            assert_vendor_role_consistency(
                None, [_role("VENDOR_OWNER", Permission.PURCHASE_ORDER_VIEW, is_vendor_role=True)]
            )
        assert excinfo.value.details["vendor_roles"] == ["VENDOR_OWNER"]

    def test_vendor_account_may_not_hold_an_internal_role(self) -> None:
        # The direction that is easy to forget and expensive to get wrong: a
        # vendor user holding PURCHASE_MANAGER sees the whole order book.
        with pytest.raises(BusinessRuleError) as excinfo:
            assert_vendor_role_consistency(
                uuid.uuid4(),
                [
                    _role("VENDOR_OWNER", is_vendor_role=True),
                    _role("PURCHASE_MANAGER", Permission.PURCHASE_ORDER_APPROVE),
                ],
            )
        assert excinfo.value.details["internal_roles"] == ["PURCHASE_MANAGER"]

    def test_matching_combinations_pass(self) -> None:
        assert_vendor_role_consistency(None, [_role("STORE_KEEPER", Permission.INVENTORY_VIEW)])
        assert_vendor_role_consistency(uuid.uuid4(), [_role("VENDOR_OWNER", is_vendor_role=True)])

    def test_no_roles_is_always_consistent(self) -> None:
        # An account may exist before anybody decides what it is allowed to do.
        assert_vendor_role_consistency(None, [])
        assert_vendor_role_consistency(uuid.uuid4(), [])

    def test_a_suspended_vendor_role_is_still_a_vendor_role(self) -> None:
        # The check is about *membership*, not about what currently grants
        # anything - reinstating the role must not be able to breach the
        # boundary retroactively.
        with pytest.raises(BusinessRuleError):
            assert_vendor_role_consistency(
                None, [_role("VENDOR_OWNER", is_vendor_role=True, is_active=False)]
            )


# -----------------------------------------------------------------------------
# Super-administrator detection
# -----------------------------------------------------------------------------


class TestSuperAdminDetection:
    def test_the_flag_counts(self) -> None:
        assert holds_super_admin(True, [])

    def test_the_role_counts(self) -> None:
        assert holds_super_admin(False, [_role(next(iter(SUPER_ADMIN_ROLE_CODES)))])

    def test_a_suspended_super_admin_role_does_not_count(self) -> None:
        # A suspended role grants nothing, so a tenant whose only "administrator"
        # holds one is already locked out - the guard must not pretend otherwise.
        assert not holds_super_admin(
            False, [_role(next(iter(SUPER_ADMIN_ROLE_CODES)), is_active=False)]
        )

    def test_an_ordinary_role_does_not_count(self) -> None:
        assert not holds_super_admin(False, [_role("ADMIN", Permission.COMPANY_MANAGE)])


# -----------------------------------------------------------------------------
# Permission catalogue
# -----------------------------------------------------------------------------


class TestPermissionCatalogue:
    def test_every_permission_appears_exactly_once(self) -> None:
        catalogue = permission_catalogue()
        seen = [entry.value for group in catalogue.groups for entry in group.permissions]
        assert sorted(seen) == sorted(str(p) for p in ALL_PERMISSIONS)
        assert len(seen) == len(set(seen)) == catalogue.total

    def test_no_permission_falls_into_other(self) -> None:
        # "Other" is a safety net for a resource nobody added to a group. If it
        # is ever non-empty, the matrix UI has an unlabelled section.
        catalogue = permission_catalogue()
        assert [group.group for group in catalogue.groups if group.group == "Other"] == []

    def test_groups_follow_registry_order(self) -> None:
        # The matrix reads top-down as the business flow; re-sorting it
        # alphabetically would scatter related resources.
        catalogue = permission_catalogue()
        present = {group.group for group in catalogue.groups}
        expected = [name for name in PERMISSION_GROUPS if name in present]
        assert [group.group for group in catalogue.groups] == expected

    def test_entries_are_split_into_resource_and_action(self) -> None:
        catalogue = permission_catalogue()
        entry = next(
            entry
            for group in catalogue.groups
            for entry in group.permissions
            if entry.value == str(Permission.PURCHASE_ORDER_APPROVE)
        )
        assert (entry.resource, entry.action) == ("purchase_order", "approve")
        assert entry.group == "Procurement"


# -----------------------------------------------------------------------------
# Setting a super-administrator's password
# -----------------------------------------------------------------------------


class TestSuperAdminPasswordProtection:
    """``user.reset_password`` alone must not be enough to take over a
    super-administrator: the reset returns a temporary password in the
    response, so a holder could sign in as them and lock them out."""

    @staticmethod
    def _principal_with_roles(*codes: str, superuser: bool = False) -> Principal:
        return Principal(
            user_id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            email="admin@test.local",
            full_name="Admin User",
            is_superuser=superuser,
            permissions=frozenset({str(Permission.USER_RESET_PASSWORD)}),
            role_codes=codes,
        )

    def test_the_rule_is_switched_on(self) -> None:
        assert PROTECT_SUPER_ADMIN_PASSWORDS is True

    @pytest.mark.parametrize(
        ("target_is_superuser", "target_roles"),
        [
            (True, []),
            (False, [_role("SUPER_ADMIN", Permission.USER_VIEW)]),
        ],
        ids=["flagged target", "role-holding target"],
    )
    def test_a_permission_holder_cannot_set_a_super_admins_password(
        self, target_is_superuser: bool, target_roles: list[SimpleNamespace]
    ) -> None:
        caller = self._principal_with_roles("USER_ADMIN")

        with pytest.raises(AuthorizationError) as excinfo:
            assert_may_set_password_for(
                caller, target_is_superuser=target_is_superuser, target_roles=target_roles
            )

        assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
        assert excinfo.value.status_code == 403
        assert excinfo.value.message == SUPER_ADMIN_PASSWORD_PROTECTED
        assert excinfo.value.details["reason"] == SUPER_ADMIN_PASSWORD_PROTECTED
        assert excinfo.value.details["required_permission"] == str(Permission.USER_RESET_PASSWORD)

    @pytest.mark.parametrize(
        "caller",
        [
            _principal_with_roles.__func__("USER_ADMIN", superuser=True),  # type: ignore[attr-defined]
            _principal_with_roles.__func__("SUPER_ADMIN"),  # type: ignore[attr-defined]
        ],
        ids=["flagged caller", "role-holding caller"],
    )
    def test_a_super_admin_by_either_route_is_let_through(self, caller: Principal) -> None:
        assert caller_holds_super_admin(caller)
        assert_may_set_password_for(
            caller, target_is_superuser=True, target_roles=[_role("SUPER_ADMIN")]
        )
        assert_may_set_password_for(
            caller, target_is_superuser=False, target_roles=[_role("SUPER_ADMIN")]
        )

    def test_an_ordinary_target_needs_no_super_admin_caller(self) -> None:
        caller = self._principal_with_roles("USER_ADMIN")
        assert not caller_holds_super_admin(caller)
        assert_may_set_password_for(
            caller, target_is_superuser=False, target_roles=[_role("STORES", Permission.USER_VIEW)]
        )

    def test_a_suspended_super_admin_role_on_the_target_does_not_count(self) -> None:
        """Consistent with holds_super_admin: an unusable role grants nothing,
        so it protects nothing either."""
        caller = self._principal_with_roles("USER_ADMIN")
        assert_may_set_password_for(
            caller,
            target_is_superuser=False,
            target_roles=[_role("SUPER_ADMIN", is_active=False)],
        )
