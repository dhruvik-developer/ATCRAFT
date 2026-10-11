"""Business rules for user and role administration.

What this module is responsible for, and why each rule exists:

* **Permission resolution has one definition.**
  :func:`resolve_effective_permissions` computes role permissions plus direct
  grants minus direct denials, skipping inactive and retired roles. It is a pure
  function over already-loaded data, and it and ``src.core.deps._resolve_permissions``
  both call :func:`src.core.permissions.resolve_role_grants`: the permission set
  an administrator is shown must be the same set the request path will enforce a
  moment later, or the screen is lying. Both are still unit-tested against each
  other.
* **Privilege escalation is structurally impossible.**
  :func:`assert_can_grant` refuses to let anybody hand out authority they do not
  themselves hold. It guards role creation, role permission edits, per-user
  direct grants *and* role assignment, because all four are ways of moving a
  permission onto an account. Superusers are exempt - that flag is the platform
  escape hatch and is not settable through this API.
* **The vendor-portal boundary is enforced in both directions.**
  :func:`assert_vendor_role_consistency` refuses a vendor role on an internal
  account and refuses an internal role on a vendor account. Checking only one
  direction leaves the other open, and either hole lets a vendor principal reach
  internal costing (section 16).
* **Deactivating an account ends its sessions.** A refresh token that outlives
  the account it belongs to is the difference between "access revoked" and
  "access revoked in fourteen days".
* **Nobody can lock the tenant out of itself.** The last active holder of
  super-administrator authority cannot be retired, deactivated, or have that
  authority removed by a role reassignment.
* **Every authority change invalidates the affected users' cached permission
  sets - after the transaction commits.** Dropping the cache entry *inside* the
  transaction would let a concurrent request repopulate it from the pre-commit
  state, leaving stale authority behind exactly where section 39 forbids it.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cache import invalidate_user_permissions
from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.messages import NO_FIELDS_TO_UPDATE
from src.core.pagination import PageParams, SortParams
from src.core.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_GROUPS,
    Permission,
    group_for,
    resolve_role_grants,
)
from src.core.responses import OffsetPagination
from src.core.security import hash_password, validate_password_policy
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.users.models import Role, User
from src.modules.users.repository import (
    RoleRepository,
    UserRepository,
    UserSessionRepository,
)
from src.modules.users.schemas import (
    EffectivePermissions,
    PasswordResetRequest,
    PasswordResetResult,
    PermissionCatalogue,
    PermissionEntry,
    PermissionGroup,
    RoleAssignment,
    RoleCreate,
    RoleDetail,
    RoleFilters,
    RoleSummary,
    RoleUpdate,
    UserCreate,
    UserDetail,
    UserFilters,
    UserSummary,
    UserUpdate,
)

log = get_logger(__name__)

AUDIT_USER = "user"
AUDIT_ROLE = "role"

# -----------------------------------------------------------------------------
# Decisions this module makes explicitly
# -----------------------------------------------------------------------------

# ASSUMPTION: "holding SUPER_ADMIN" means either the ``is_superuser`` flag or
# membership of a usable role whose code is in this set. Both routes reach the
# same authority, so a guard that only looked at one of them could be walked
# around by using the other.
SUPER_ADMIN_ROLE_CODES: frozenset[str] = frozenset({"SUPER_ADMIN"})

# ASSUMPTION: the SUPER_ADMIN role is immutable - its name, description,
# permission set and active flag cannot be edited, and it cannot be retired. It
# is the role the "last administrator" guard is defined in terms of, so allowing
# it to be emptied of permissions would defeat that guard while leaving it
# nominally intact. Other system roles remain editable, as section 81 requires.
IMMUTABLE_ROLE_CODES: frozenset[str] = frozenset({"SUPER_ADMIN"})

# ASSUMPTION: ``is_superuser`` is not settable through this API at all. It
# bypasses every permission check in the system, so it is set by the seed and by
# a database-level operation only. The alternative - exposing it behind a
# permission - means one compromised administrative account becomes a platform
# compromise.
SUPERUSER_FLAG_IS_API_SETTABLE = False

# ASSUMPTION: the privilege guard applies symmetrically. Granting a permission
# you do not hold is escalation; *revoking* one you do not hold is tampering
# with an account more privileged than your own, which is how a low-privileged
# administrator locks out the people who could stop them. Both are refused, so
# the check runs over the symmetric difference of the before and after sets.
REQUIRE_AUTHORITY_TO_REVOKE = True

# ASSUMPTION: only a super-administrator may set a super-administrator's
# password, by either route a password can be set (``reset_password`` and
# ``update_user(password=...)``). ``user.reset_password`` is an ordinary
# administrative permission; without this rule a holder of it could reset a
# SUPER_ADMIN's password, read the temporary password out of the response and
# sign in as them - locking out the people who could stop them *and* taking
# over their authority. REQUIRE_AUTHORITY_TO_REVOKE above refuses far weaker
# tampering for the same reason. A caller who holds super-admin by either
# route (the flag or the role) is let through.
PROTECT_SUPER_ADMIN_PASSWORDS = True

SUPER_ADMIN_PASSWORD_PROTECTED = (
    "Only a super administrator may set a super administrator's password."  # noqa: S105
)

# ASSUMPTION: a role that somebody still holds cannot be retired. Soft-deleting
# it would silently strip authority from every holder with no audit entry naming
# them. Reassign the holders first; the refusal says how many there are.
BLOCK_ROLE_DELETION_WHILE_HELD = True

# ASSUMPTION: an administrator cannot retire or deactivate their own account.
# Not a security rule - a correctness one: the request that removes your own
# authority is authenticated with it, and the half-applied result ("deactivated,
# but the sessions were not revoked because the caller lost permission mid-way")
# is not worth supporting. Ask a colleague.
BLOCK_SELF_LIFECYCLE_CHANGE = True

# The generated temporary password is rejected and regenerated if it happens to
# violate the policy - see :func:`generate_temporary_password`.
MAX_TEMP_PASSWORD_ATTEMPTS = 8
TEMP_PASSWORD_LENGTH = 20

#: One pool per class the password policy requires. Visually ambiguous
#: characters are left out (no l/I/0/1): a temporary password is read off a
#: screen and typed by hand, and "was that a one or an ell" turns a reset into a
#: support call.
_PASSWORD_POOLS: tuple[str, ...] = (
    "abcdefghijkmnopqrstuvwxyz",
    "ABCDEFGHJKLMNPQRSTUVWXYZ",
    "23456789",
    "!@#$%^&*-_=+?",
)

#: An e-mail local part at or below this length is excluded character by
#: character from the generated password - see ``_temporary_password_pools``.
SHORT_EMAIL_LOCAL_PART = 4

_USER_AUDITED_FIELDS = [
    "email",
    "full_name",
    "phone",
    "employee_code",
    "branch_id",
    "plant_id",
    "department_id",
    "vendor_id",
    "allowed_category_ids",
    "allowed_warehouse_ids",
    "extra_permissions",
    "denied_permissions",
    "is_active",
    "is_superuser",
    "must_change_password",
    "mfa_enabled",
]

_ROLE_AUDITED_FIELDS = [
    "code",
    "name",
    "description",
    "permissions",
    "is_system",
    "is_vendor_role",
    "is_active",
]


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class AccountState(StrEnum):
    """Lifecycle of a user or role row.

    There is no ``status`` column on either table: the state is derived from
    ``is_active`` and ``deleted_at``, the two flags the rest of the system
    already reads, and a third redundant column could disagree with them.
    Routing the transitions through a :class:`StateMachine` still gives one
    place where the legal moves are declared, tested and rendered (section 18).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


USER_LIFECYCLE: StateMachine[AccountState] = StateMachine(
    entity="User",
    initial=AccountState.ACTIVE,
    transitions=(
        Transition(
            AccountState.ACTIVE,
            AccountState.INACTIVE,
            permission=Permission.USER_DEACTIVATE,
            label="Deactivate",
        ),
        Transition(
            AccountState.INACTIVE,
            AccountState.ACTIVE,
            permission=Permission.USER_DEACTIVATE,
            label="Activate",
        ),
        Transition(
            AccountState.ACTIVE,
            AccountState.ARCHIVED,
            permission=Permission.USER_DEACTIVATE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            AccountState.INACTIVE,
            AccountState.ARCHIVED,
            permission=Permission.USER_DEACTIVATE,
            label="Retire",
            is_irreversible=True,
        ),
    ),
    # Terminal on purpose: an account is retired because the person left. Their
    # e-mail address stays reserved so their history remains attributable, and a
    # returning employee is given a new account rather than inheriting the
    # sessions, grants and lockout state of the old one.
    terminal=frozenset({AccountState.ARCHIVED}),
)

ROLE_LIFECYCLE: StateMachine[AccountState] = StateMachine(
    entity="Role",
    initial=AccountState.ACTIVE,
    transitions=(
        Transition(
            AccountState.ACTIVE,
            AccountState.INACTIVE,
            permission=Permission.ROLE_MANAGE,
            label="Suspend",
        ),
        Transition(
            AccountState.INACTIVE,
            AccountState.ACTIVE,
            permission=Permission.ROLE_MANAGE,
            label="Reinstate",
        ),
        Transition(
            AccountState.ACTIVE,
            AccountState.ARCHIVED,
            permission=Permission.ROLE_MANAGE,
            label="Delete",
            is_irreversible=True,
        ),
        Transition(
            AccountState.INACTIVE,
            AccountState.ARCHIVED,
            permission=Permission.ROLE_MANAGE,
            label="Delete",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({AccountState.ARCHIVED}),
)


def lifecycle_state(record: Any) -> AccountState:
    """Derive the lifecycle state of a user or role row from its flags."""
    if getattr(record, "deleted_at", None) is not None:
        return AccountState.ARCHIVED
    return AccountState.ACTIVE if record.is_active else AccountState.INACTIVE


# -----------------------------------------------------------------------------
# Permission resolution - pure
# -----------------------------------------------------------------------------


class RoleView(Protocol):
    """The shape the resolution rules need.

    A protocol rather than the ORM class so every rule below can be exercised -
    and called - without a database session or a mapped instance.
    """

    code: str
    permissions: list[str]
    is_vendor_role: bool
    is_active: bool
    deleted_at: datetime | None


def usable_roles(roles: Iterable[RoleView]) -> list[RoleView]:
    """The roles that actually contribute authority.

    A suspended or retired role stays *assigned* - the membership row is history
    and the grid should still show it - but it grants nothing. This mirrors the
    filter in ``src.core.deps._resolve_permissions``; if the two ever diverge,
    an administrator sees a permission the request path will refuse.
    """
    return [role for role in roles if role.is_active and role.deleted_at is None]


def role_permission_map(roles: Iterable[RoleView]) -> dict[str, list[str]]:
    """Role code -> the permissions it contributes, for the provenance view."""
    return {role.code: sorted(role.permissions or []) for role in usable_roles(roles)}


def resolve_effective_permissions(
    roles: Iterable[RoleView],
    extra_permissions: Sequence[str] | None,
    denied_permissions: Sequence[str] | None,
) -> frozenset[str]:
    """Union of role permissions plus direct grants, minus direct denials.

    Denials are applied last and unconditionally: an explicit "this person may
    not approve payments" must not be overridable by adding another role, which
    is precisely how such an exception gets quietly undone in practice.

    Superusers are *not* special-cased here. The flag is authority applied at
    the check (``Principal.has``), not a stored permission set, and pretending
    otherwise would make this function disagree with the request path for the
    one account where being wrong matters most.

    The fold is :func:`~src.core.permissions.resolve_role_grants` - the same
    function ``src.core.deps._resolve_permissions`` calls - so the screen and the
    request path share one definition rather than two that are tested against
    each other.
    """
    granted, _ = resolve_role_grants(roles, extra_permissions, denied_permissions)
    return granted


def holds_super_admin(is_superuser: bool, roles: Iterable[RoleView]) -> bool:
    """Does this account hold platform administration by either route?"""
    if is_superuser:
        return True
    return any(role.code in SUPER_ADMIN_ROLE_CODES for role in usable_roles(roles))


def caller_holds_super_admin(principal: Principal) -> bool:
    """The caller's side of :func:`holds_super_admin`.

    A principal carries only the codes of its *usable* roles, so the role
    route needs no liveness check here.
    """
    return principal.is_superuser or any(
        code in SUPER_ADMIN_ROLE_CODES for code in principal.role_codes
    )


def assert_may_set_password_for(
    principal: Principal, *, target_is_superuser: bool, target_roles: Iterable[RoleView]
) -> None:
    """Refuse to let a non-super-administrator set a super-administrator's
    password. See :data:`PROTECT_SUPER_ADMIN_PASSWORDS`."""
    if not PROTECT_SUPER_ADMIN_PASSWORDS:
        return
    if holds_super_admin(target_is_superuser, target_roles) and not caller_holds_super_admin(
        principal
    ):
        raise AuthorizationError(
            SUPER_ADMIN_PASSWORD_PROTECTED,
            code=ErrorCode.PERMISSION_DENIED,
            details={
                "required_permission": str(Permission.USER_RESET_PASSWORD),
                "reason": SUPER_ADMIN_PASSWORD_PROTECTED,
            },
        )


# -----------------------------------------------------------------------------
# Authority guards - pure
# -----------------------------------------------------------------------------


def assert_can_grant(principal: Principal, permissions: Iterable[str], *, what: str) -> None:
    """Refuse to move authority the caller does not itself hold.

    The privilege-escalation guard. Without it, ``role.manage`` alone is
    equivalent to every permission in the system: create a role granting
    ``payment.approve``, assign it to yourself, approve the payment.

    Superusers are exempt because the flag already bypasses every check; making
    them pass this one would be theatre.
    """
    if principal.is_superuser:
        return

    missing = sorted(set(permissions) - set(principal.permissions))
    if not missing:
        return

    log.info(
        "authz.escalation_blocked",
        user_id=str(principal.user_id),
        subject=what,
        missing=missing,
    )
    # Built and then augmented rather than constructed with ``details=``:
    # PermissionDeniedError populates that key itself, so passing it through the
    # constructor is a TypeError at the moment the guard is supposed to fire.
    denied = PermissionDeniedError(", ".join(missing))
    denied.details["missing_permissions"] = missing
    denied.details["reason"] = (
        f"You cannot change {what} to include permissions you do not hold yourself."
    )
    raise denied


def assert_grant_change_allowed(
    principal: Principal,
    before: Iterable[str],
    after: Iterable[str],
    *,
    what: str,
) -> frozenset[str]:
    """Guard a change to a permission set, in both directions.

    Returns the symmetric difference, which is also what the caller records in
    the audit entry - the set that was checked and the set that changed are by
    construction the same set.
    """
    before_set = set(before)
    after_set = set(after)
    delta = after_set - before_set
    if REQUIRE_AUTHORITY_TO_REVOKE:
        delta |= before_set - after_set
    assert_can_grant(principal, delta, what=what)
    return frozenset(delta)


def assert_vendor_role_consistency(vendor_id: uuid.UUID | None, roles: Iterable[RoleView]) -> None:
    """Enforce the vendor-portal isolation boundary in both directions.

    A vendor role may be held only by an account bound to a vendor, and an
    account bound to a vendor may hold only vendor roles (section 16).

    The second half is the one that is easy to forget and expensive to get
    wrong: a vendor user who also holds ``PURCHASE_MANAGER`` sees the tenant's
    costing, its other suppliers' prices and its whole order book, because the
    vendor filter in the repositories is applied to the *vendor* views, not to
    every internal screen.
    """
    role_list = list(roles)
    if vendor_id is None:
        offenders = sorted(role.code for role in role_list if role.is_vendor_role)
        if offenders:
            raise BusinessRuleError(
                "Vendor roles can only be assigned to an account bound to a vendor: "
                + ", ".join(offenders),
                details={"vendor_roles": offenders, "vendor_id": None},
            )
        return

    offenders = sorted(role.code for role in role_list if not role.is_vendor_role)
    if offenders:
        raise BusinessRuleError(
            "A vendor-portal account may hold vendor roles only; these are internal roles: "
            + ", ".join(offenders),
            details={"internal_roles": offenders, "vendor_id": str(vendor_id)},
        )


def assert_role_assignment_allowed(
    principal: Principal,
    added: Sequence[RoleView],
    removed: Sequence[RoleView],
) -> frozenset[str]:
    """Privilege guard for a role reassignment.

    A role is a bundle of permissions, so assigning one is granting all of them
    and the same rule applies. Removals are checked too - see
    :data:`REQUIRE_AUTHORITY_TO_REVOKE`.
    """
    moved: set[str] = set()
    for role in list(added) + (list(removed) if REQUIRE_AUTHORITY_TO_REVOKE else []):
        moved.update(role.permissions or [])
    assert_can_grant(principal, moved, what="role membership")
    return frozenset(moved)


def _temporary_password_pools(email: str | None) -> tuple[str, ...]:
    """Character pools for a generated password, one per policy class.

    When the account's e-mail local part is short, its characters are removed
    from every pool. The policy forbids a password *containing* that local part,
    and for a one-character one ("a@example.com") the rule collides with the
    requirement to include a lowercase letter often enough that retrying a
    random draw is not a reliable strategy - excluding the character outright
    makes the collision impossible instead of unlikely.

    A pool that would empty out falls back to its full form; the final policy
    check still runs, so the worst case is a retry rather than a bad password.
    """
    local = (email or "").split("@")[0].lower()
    if not local or len(local) > SHORT_EMAIL_LOCAL_PART:
        return _PASSWORD_POOLS

    banned = set(local) | set(local.upper())
    return tuple("".join(c for c in pool if c not in banned) or pool for pool in _PASSWORD_POOLS)


def generate_temporary_password(email: str | None = None) -> str:
    """A random password that satisfies the configured policy.

    Generated rather than chosen: an administrator who invents passwords reuses
    the same one for every account they touch. One character is drawn from each
    policy class first, so the result cannot fail the "needs a symbol" rule by
    chance, and the whole string is then shuffled so the classes are not in a
    predictable order.

    The result is validated against the real policy before it is returned - this
    function must never hand back a password the very next login would reject.
    """
    rng = secrets.SystemRandom()
    pools = _temporary_password_pools(email)
    alphabet = "".join(pools)
    # Comfortably above the configured minimum, and bounded by what the policy
    # accepts at the top end.
    length = min(128, max(TEMP_PASSWORD_LENGTH, settings.password_min_length + 4))

    for _ in range(MAX_TEMP_PASSWORD_ATTEMPTS):
        characters = [rng.choice(pool) for pool in pools]
        characters += [rng.choice(alphabet) for _ in range(length - len(pools))]
        rng.shuffle(characters)
        candidate = "".join(characters)
        try:
            validate_password_policy(candidate, email=email)
        except ValidationError:
            continue
        return candidate

    raise BusinessRuleError(
        "Could not generate a temporary password that satisfies the password policy. "
        "Supply one explicitly.",
        code=ErrorCode.PASSWORD_POLICY,
    )


# -----------------------------------------------------------------------------
# Permission catalogue - pure
# -----------------------------------------------------------------------------


def permission_catalogue() -> PermissionCatalogue:
    """The whole registry, grouped for the permission-matrix UI.

    Group order follows :data:`src.core.permissions.PERMISSION_GROUPS` rather
    than being sorted alphabetically: the matrix reads top-down as the business
    flow, from organisation through procurement to dispatch, and re-sorting it
    would scatter related resources.
    """
    grouped: dict[str, list[PermissionEntry]] = {}
    for permission in ALL_PERMISSIONS:
        resource, _, action = str(permission).partition(".")
        group = group_for(permission)
        grouped.setdefault(group, []).append(
            PermissionEntry(value=str(permission), resource=resource, action=action, group=group)
        )

    # "Other" catches any permission whose resource is not listed in a group -
    # it should stay empty, and the unit tests assert that it does.
    ordered_names = [*PERMISSION_GROUPS, "Other"]
    groups = [
        PermissionGroup(
            group=name,
            resources=list(PERMISSION_GROUPS.get(name, ())),
            permissions=sorted(grouped[name], key=lambda entry: (entry.resource, entry.action)),
        )
        for name in ordered_names
        if name in grouped
    ]
    return PermissionCatalogue(groups=groups, total=len(ALL_PERMISSIONS))


# -----------------------------------------------------------------------------
# Presentation helpers
# -----------------------------------------------------------------------------


def _role_summary(role: Role, permission_count: int) -> RoleSummary:
    return RoleSummary(
        id=role.id,
        code=role.code,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        is_vendor_role=role.is_vendor_role,
        is_active=role.is_active,
        permission_count=permission_count,
        created_at=role.created_at,
    )


def _user_detail(user: User, roles: Sequence[Role]) -> UserDetail:
    """Assemble the detail response from explicitly named attributes.

    Built field by field rather than by attribute-mode validation so that the
    password hash and MFA secret cannot be picked up by a future edit, and so
    the ``lazy="raise"`` relationships are never touched.
    """
    return UserDetail(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        employee_code=user.employee_code,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        must_change_password=user.must_change_password,
        vendor_id=user.vendor_id,
        branch_id=user.branch_id,
        plant_id=user.plant_id,
        department_id=user.department_id,
        last_login_at=user.last_login_at,
        locked_until=user.locked_until,
        created_at=user.created_at,
        role_codes=sorted(role.code for role in roles),
        phone=user.phone,
        mfa_enabled=user.mfa_enabled,
        failed_login_attempts=user.failed_login_attempts,
        password_changed_at=user.password_changed_at,
        allowed_category_ids=list(user.allowed_category_ids or []),
        allowed_warehouse_ids=list(user.allowed_warehouse_ids or []),
        extra_permissions=sorted(user.extra_permissions or []),
        denied_permissions=sorted(user.denied_permissions or []),
        preferences=user.preferences,
        is_vendor_user=user.vendor_id is not None,
        updated_at=user.updated_at,
        roles=[_role_summary(role, len(role.permissions or [])) for role in roles],
    )


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot remove this, something still depends on it" refusal.

    One constructor so every such refusal carries the same machine-readable code
    and the frontend can react to all of them uniformly.
    """
    return BusinessRuleError(
        message, code=ErrorCode.REFERENCED_RECORD_IN_USE, details=details or None
    )


# -----------------------------------------------------------------------------
# User service
# -----------------------------------------------------------------------------


class UserService:
    """User administration. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = UserRepository(session, tenant)
        self.roles = RoleRepository(session, tenant)
        self.sessions = UserSessionRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_users(
        self, filters: UserFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[UserSummary], OffsetPagination]:
        """A page of users, with each row's role codes.

        Two statements for the whole page, never one per row: the users, then
        their role codes in a single join (section 32).
        """
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)

        codes = await self.repo.role_codes_for_users([row.id for row in rows])
        items = [
            UserSummary(
                id=row.id,
                email=row.email,
                full_name=row.full_name,
                employee_code=row.employee_code,
                is_active=row.is_active,
                is_superuser=row.is_superuser,
                must_change_password=row.must_change_password,
                vendor_id=row.vendor_id,
                branch_id=row.branch_id,
                plant_id=row.plant_id,
                department_id=row.department_id,
                last_login_at=row.last_login_at,
                locked_until=row.locked_until,
                created_at=row.created_at,
                role_codes=codes.get(row.id, []),
            )
            for row in rows
        ]
        return items, pagination

    async def get_user(self, user_id: uuid.UUID) -> UserDetail:
        user = await self._require_user(user_id)
        return _user_detail(user, list(user.roles))

    async def get_effective_permissions(self, user_id: uuid.UUID) -> EffectivePermissions:
        """The resolved authority of one account, with its provenance.

        A superuser is reported as holding the entire registry, because that is
        what ``Principal.has`` will answer for every check - showing the stored
        set instead would understate the account's real reach.
        """
        user = await self._require_user(user_id)
        roles = list(user.roles)

        if user.is_superuser:
            effective: frozenset[str] = frozenset(str(p) for p in ALL_PERMISSIONS)
        else:
            effective = resolve_effective_permissions(
                roles, user.extra_permissions, user.denied_permissions
            )

        return EffectivePermissions(
            user_id=user.id,
            email=user.email,
            is_superuser=user.is_superuser,
            is_active=user.is_active,
            permissions=sorted(effective),
            permission_count=len(effective),
            role_permissions=role_permission_map(roles),
            extra_permissions=sorted(user.extra_permissions or []),
            denied_permissions=sorted(user.denied_permissions or []),
        )

    # --- writes -------------------------------------------------------------

    async def create_user(self, principal: Principal, payload: UserCreate) -> UserDetail:
        """Create an account, optionally with its initial role set."""
        self._assert_may_change_direct_grants(
            principal,
            before_extra=(),
            after_extra=payload.extra_permissions,
            before_denied=(),
            after_denied=payload.denied_permissions,
        )

        async with transaction(self.session):
            if await self.repo.email_taken(payload.email):
                raise DuplicateError(
                    f"An account with the address {payload.email} already exists in this company.",
                    details={"email": payload.email},
                )
            if payload.employee_code and await self.repo.employee_code_taken(payload.employee_code):
                raise DuplicateError(
                    f"Employee code '{payload.employee_code}' is already in use.",
                    details={"employee_code": payload.employee_code},
                )

            validate_password_policy(payload.password, email=payload.email, field="password")
            await self._assert_references(
                branch_id=payload.branch_id,
                plant_id=payload.plant_id,
                department_id=payload.department_id,
                vendor_id=payload.vendor_id,
            )
            self._assert_scope_is_meaningful(payload.vendor_id, payload.allowed_category_ids)
            await self._assert_category_scope(payload.vendor_id, payload.allowed_category_ids)

            roles = await self._load_assignable_roles(payload.role_ids)
            assert_vendor_role_consistency(payload.vendor_id, roles)
            assert_role_assignment_allowed(principal, roles, [])

            user = User(
                email=payload.email,
                full_name=payload.full_name,
                phone=payload.phone,
                employee_code=payload.employee_code,
                password_hash=hash_password(payload.password),
                password_changed_at=utcnow(),
                must_change_password=payload.must_change_password,
                is_active=payload.is_active,
                # Never from the payload - see SUPERUSER_FLAG_IS_API_SETTABLE.
                is_superuser=False,
                branch_id=payload.branch_id,
                plant_id=payload.plant_id,
                department_id=payload.department_id,
                vendor_id=payload.vendor_id,
                # NULL rather than an empty array: "no restriction" and "an empty
                # restriction list" must not be two different encodings of the
                # same state, or a later query has to test for both.
                allowed_category_ids=payload.allowed_category_ids or None,
                allowed_warehouse_ids=payload.allowed_warehouse_ids or None,
                extra_permissions=payload.extra_permissions or None,
                denied_permissions=payload.denied_permissions or None,
                preferences=payload.preferences,
            )
            self.repo.add(user)
            await self.session.flush()

            if payload.role_ids:
                await self.repo.replace_roles(
                    user.id, payload.role_ids, assigned_by_id=self.tenant.user_id
                )

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_USER,
                entity_id=user.id,
                entity_code=user.email,
                summary=f"Created user {user.email} ({user.full_name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(user, _USER_AUDITED_FIELDS),
            )
            if roles:
                await self._record_role_change(
                    user, before=[], after=roles, reason="Initial role set on account creation"
                )

        # After the commit - see the module docstring.
        await self._invalidate([user.id])
        return await self.get_user(user.id)

    async def update_user(
        self, principal: Principal, user_id: uuid.UUID, payload: UserUpdate
    ) -> UserDetail:
        changes = payload.model_dump(exclude_unset=True)
        password = changes.pop("password", None)
        clear_branch = bool(changes.pop("clear_branch", False))
        clear_plant = bool(changes.pop("clear_plant", False))
        clear_department = bool(changes.pop("clear_department", False))
        clear_vendor = bool(changes.pop("clear_vendor", False))
        clears = clear_branch or clear_plant or clear_department or clear_vendor

        if not changes and not clears and password is None:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        grants_changed = False

        async with transaction(self.session):
            user = await self._require_user(user_id)
            before = snapshot(user, _USER_AUDITED_FIELDS)
            roles = list(user.roles)

            # Before anything is changed: an authority check that runs part-way
            # through an update is one an early `return` can skip.
            self._assert_may_set_a_password(principal, password)
            if password is not None:
                self._assert_may_set_password_for(principal, user)

            new_email = changes.get("email")
            if (
                new_email
                and new_email != user.email
                and await self.repo.email_taken(new_email, exclude_id=user.id)
            ):
                raise DuplicateError(
                    f"An account with the address {new_email} already exists in this company.",
                    details={"email": new_email},
                )

            new_code = changes.get("employee_code")
            if (
                new_code
                and new_code != user.employee_code
                and await self.repo.employee_code_taken(new_code, exclude_id=user.id)
            ):
                raise DuplicateError(
                    f"Employee code '{new_code}' is already in use.",
                    details={"employee_code": new_code},
                )

            if "extra_permissions" in changes or "denied_permissions" in changes:
                grants_changed = True
                self._assert_may_change_direct_grants(
                    principal,
                    before_extra=user.extra_permissions or (),
                    after_extra=changes.get("extra_permissions", user.extra_permissions) or (),
                    before_denied=user.denied_permissions or (),
                    after_denied=changes.get("denied_permissions", user.denied_permissions) or (),
                )

            await self._assert_references(
                branch_id=changes.get("branch_id"),
                plant_id=changes.get("plant_id"),
                department_id=changes.get("department_id"),
                vendor_id=changes.get("vendor_id"),
            )

            for field, value in changes.items():
                setattr(user, field, value)

            # Explicit clears, because "field absent" and "field null" both
            # arrive as None and must not mean the same thing.
            if clear_branch:
                user.branch_id = None
            if clear_plant:
                user.plant_id = None
            if clear_department:
                user.department_id = None
            if clear_vendor:
                user.vendor_id = None

            # Re-checked after the assignment because moving an account into or
            # out of the vendor portal changes which of its existing roles are
            # legal - in both directions.
            assert_vendor_role_consistency(user.vendor_id, roles)
            self._assert_scope_is_meaningful(user.vendor_id, user.allowed_category_ids)
            # Only when this edit touches the scope or the vendor: a stored scope
            # that has since drifted (a qualification revoked) must not block an
            # unrelated edit to the person's phone number.
            if "allowed_category_ids" in changes or "vendor_id" in changes:
                await self._assert_category_scope(user.vendor_id, user.allowed_category_ids)

            # An empty list means "no restriction" and is stored as NULL, so the
            # two encodings never coexist.
            if not user.allowed_category_ids:
                user.allowed_category_ids = None
            if not user.allowed_warehouse_ids:
                user.allowed_warehouse_ids = None
            if not user.extra_permissions:
                user.extra_permissions = None
            if not user.denied_permissions:
                user.denied_permissions = None

            revoked = 0
            if password is not None:
                validate_password_policy(password, email=user.email, field="password")
                user.password_hash = hash_password(password)
                user.password_changed_at = utcnow()
                # An administrator-chosen password has been read aloud or typed
                # into a chat window unless the caller says otherwise.
                if "must_change_password" not in changes:
                    user.must_change_password = True
                user.failed_login_attempts = 0
                user.locked_until = None
                revoked = await self.sessions.revoke_all_for_user(
                    user.id, reason="password_changed_by_admin"
                )

            self.repo.touch(user)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_USER,
                entity_id=user.id,
                entity_code=user.email,
                summary=f"Updated user {user.email}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(user, _USER_AUDITED_FIELDS),
            )

            if grants_changed:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.PERMISSION_CHANGE,
                    entity_type=AUDIT_USER,
                    entity_id=user.id,
                    entity_code=user.email,
                    summary=f"Changed direct permission grants for {user.email}",
                    user_id=self.tenant.user_id,
                    old_values={
                        "extra_permissions": before.get("extra_permissions"),
                        "denied_permissions": before.get("denied_permissions"),
                    },
                    new_values={
                        "extra_permissions": user.extra_permissions,
                        "denied_permissions": user.denied_permissions,
                    },
                )

            if password is not None:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.PASSWORD_CHANGED,
                    entity_type=AUDIT_USER,
                    entity_id=user.id,
                    entity_code=user.email,
                    summary=f"Password set by an administrator for {user.email}",
                    user_id=self.tenant.user_id,
                    context={"sessions_revoked": revoked, "initiated_by": "administrator"},
                )

        if grants_changed or password is not None:
            await self._invalidate([user_id])
        return await self.get_user(user_id)

    async def assign_roles(
        self, principal: Principal, user_id: uuid.UUID, payload: RoleAssignment
    ) -> UserDetail:
        """Replace a user's role set.

        Every guard that matters runs inside the transaction, against the rows
        as they are at that moment: the roles must belong to this tenant, the
        vendor boundary must hold in both directions, the caller must already
        hold everything being moved, and the tenant must be left with at least
        one platform administrator.
        """
        async with transaction(self.session):
            user = await self._require_user(user_id)
            current = list(user.roles)
            target = await self._load_assignable_roles(payload.role_ids)

            current_ids = {role.id for role in current}
            target_ids = {role.id for role in target}
            if current_ids == target_ids:
                raise BusinessRuleError(
                    "The submitted role set is identical to the current one.",
                    details={"role_codes": sorted(role.code for role in current)},
                )

            added = [role for role in target if role.id not in current_ids]
            removed = [role for role in current if role.id not in target_ids]

            assert_vendor_role_consistency(user.vendor_id, target)
            assert_role_assignment_allowed(principal, added, removed)

            if holds_super_admin(user.is_superuser, current) and not holds_super_admin(
                user.is_superuser, target
            ):
                await self._assert_not_last_super_admin(
                    user, action="removing super-administrator authority from"
                )

            await self.repo.replace_roles(
                user.id, payload.role_ids, assigned_by_id=self.tenant.user_id
            )
            await self._record_role_change(
                user, before=current, after=target, reason=payload.reason
            )

        await self._invalidate([user_id])
        return await self.get_user(user_id)

    async def set_active(
        self, principal: Principal, user_id: uuid.UUID, *, active: bool, reason: str
    ) -> UserDetail:
        """Activate or deactivate an account, validated by the lifecycle machine.

        Deactivation revokes every live session in the same transaction. A
        refresh token that outlives the account it belongs to turns "access
        revoked" into "access revoked in fourteen days".
        """
        target = AccountState.ACTIVE if active else AccountState.INACTIVE

        async with transaction(self.session):
            user = await self._require_user(user_id)
            current = lifecycle_state(user)
            USER_LIFECYCLE.assert_transition(current, target)

            if not active:
                self._assert_not_self(user, "deactivate")
                if holds_super_admin(user.is_superuser, list(user.roles)):
                    await self._assert_not_last_super_admin(user, action="deactivating")

            user.is_active = active
            if active:
                # Reinstating an account clears the lockout counters: the person
                # is being let back in, and leaving a stale lockout behind means
                # their first sign-in attempt fails for no visible reason.
                user.failed_login_attempts = 0
                user.locked_until = None

            self.repo.touch(user)
            await self.session.flush()

            revoked = 0
            if not active:
                revoked = await self.sessions.revoke_all_for_user(
                    user.id, reason="user_deactivated"
                )

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_USER,
                entity_id=user.id,
                entity_code=user.email,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=f"{reason} (sessions revoked: {revoked})" if revoked else reason,
            )

        # The account's authority has effectively changed even though no role
        # did, so the cached permission set must not survive the change.
        await self._invalidate([user_id])
        return await self.get_user(user_id)

    async def reset_password(
        self, principal: Principal, user_id: uuid.UUID, payload: PasswordResetRequest
    ) -> PasswordResetResult:
        """Administrator-triggered password reset.

        Always sets ``must_change_password`` and always revokes the account's
        sessions: a reset is requested either because the password was forgotten
        or because it was exposed, and both cases require the old sessions to
        stop working.

        The route gates on ``user.reset_password``; the target's authority is
        checked here, before anything is changed, because that permission alone
        must not be enough to take over a super-administrator's account.
        """
        generated: str | None = None

        async with transaction(self.session):
            user = await self._require_user(user_id)
            self._assert_may_set_password_for(principal, user)

            if payload.new_password is not None:
                validate_password_policy(payload.new_password, email=user.email)
                new_password = payload.new_password
            else:
                generated = generate_temporary_password(user.email)
                new_password = generated

            user.password_hash = hash_password(new_password)
            user.password_changed_at = utcnow()
            user.must_change_password = True
            user.failed_login_attempts = 0
            user.locked_until = None
            self.repo.touch(user)
            await self.session.flush()

            revoked = await self.sessions.revoke_all_for_user(user.id, reason="password_reset")

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.PASSWORD_CHANGED,
                entity_type=AUDIT_USER,
                entity_id=user.id,
                entity_code=user.email,
                summary=f"Password reset by an administrator for {user.email}",
                user_id=self.tenant.user_id,
                # The password itself is never recorded, generated or not; the
                # audit service would strip it, and it must not get that far.
                context={
                    "reason": payload.reason,
                    "sessions_revoked": revoked,
                    "password_generated": generated is not None,
                },
            )

        await self._invalidate([user_id])
        return PasswordResetResult(
            user_id=user_id,
            must_change_password=True,
            sessions_revoked=revoked,
            temporary_password=generated,
        )

    async def delete_user(self, user_id: uuid.UUID, *, reason: str) -> None:
        """Retire an account.

        Soft, because ``users.id`` is referenced by every document the person
        ever created and by every audit row describing what they did (section
        28). The row is hidden from the application, never removed.
        """
        async with transaction(self.session):
            user = await self._require_user(user_id)
            self._assert_not_self(user, "retire")

            current = lifecycle_state(user)
            USER_LIFECYCLE.assert_transition(current, AccountState.ARCHIVED)

            if holds_super_admin(user.is_superuser, list(user.roles)):
                await self._assert_not_last_super_admin(user, action="retiring")

            await self.repo.soft_delete(user)
            revoked = await self.sessions.revoke_all_for_user(user.id, reason="user_retired")

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_USER,
                entity_id=user.id,
                entity_code=user.email,
                from_status=str(current),
                to_status=str(AccountState.ARCHIVED),
                user_id=self.tenant.user_id,
                action=AuditAction.DELETE,
                reason=f"{reason} (sessions revoked: {revoked})",
            )

        await self._invalidate([user_id])

    # --- internals ----------------------------------------------------------

    async def _require_user(self, user_id: uuid.UUID) -> User:
        user = await self.repo.get_with_roles(user_id)
        if user is None:
            # Deliberately identical to "exists in another tenant" - confirming
            # existence elsewhere is itself a disclosure.
            raise NotFoundError("User", user_id)
        return user

    async def _load_assignable_roles(self, role_ids: Sequence[uuid.UUID]) -> list[Role]:
        """Resolve role ids to this tenant's roles, refusing anything else.

        This is where "a user may only be assigned roles from their own company"
        is enforced: the repository query is tenant-scoped, so another tenant's
        role comes back missing and is reported as not found rather than
        silently dropped from the assignment.
        """
        if not role_ids:
            return []

        roles = await self.roles.get_assignable(role_ids)
        found = {role.id for role in roles}
        missing = [str(role_id) for role_id in role_ids if role_id not in found]
        if missing:
            raise NotFoundError("Role", ", ".join(missing))

        inactive = sorted(role.code for role in roles if not role.is_active)
        if inactive:
            raise BusinessRuleError(
                "These roles are suspended and cannot be assigned: " + ", ".join(inactive),
                details={"suspended_roles": inactive},
            )
        return roles

    def _assert_may_set_a_password(self, principal: Principal, password: str | None) -> None:
        """Setting somebody's password needs ``user.reset_password``.

        Exactly the same reasoning as :meth:`_assert_may_change_direct_grants`
        above, applied to the other authority this payload can carry. The route
        gates on ``user.update``, which is the right permission for editing a
        name or a phone number; choosing an account's password is a different
        act, and the codebase already says so - there is a dedicated
        ``POST /users/{id}/reset-password`` gated on ``user.reset_password``,
        whose own docstring reads "resetting a password is user.reset_password".

        Without this, that gate was a formality: a holder of ``user.update``
        alone could PATCH ``{"password": "..."}`` onto any account in the
        company - including a SUPER_ADMIN - and then sign in as them. The
        forced ``must_change_password`` does not help, because it is not
        enforced on an already-issued token.
        """
        if password is None:
            return
        if not principal.has(Permission.USER_RESET_PASSWORD):
            raise PermissionDeniedError(str(Permission.USER_RESET_PASSWORD))

    def _assert_may_set_password_for(self, principal: Principal, user: User) -> None:
        """Holding ``user.reset_password`` is not enough against a super-admin.

        Applied at both places a password can be set. ``user.roles`` is loaded
        by :meth:`_require_user`, so this reads what is already in memory.
        """
        assert_may_set_password_for(
            principal, target_is_superuser=user.is_superuser, target_roles=list(user.roles)
        )

    def _assert_may_change_direct_grants(
        self,
        principal: Principal,
        *,
        before_extra: Iterable[str],
        after_extra: Iterable[str],
        before_denied: Iterable[str],
        after_denied: Iterable[str],
    ) -> None:
        """Per-user overrides need ``permission.assign`` *and* the authority itself.

        The route gates on ``user.create``/``user.update``, which is the right
        permission for editing a name or a phone number. Handing somebody a
        direct grant is a different act, so it needs the permission that governs
        authority - and, on top of that, the escalation guard.
        """
        # The two lists are compared separately, not as one union: moving a
        # permission from the grant list to the denial list leaves the union
        # unchanged while reversing what the account can do.
        delta = (set(after_extra) ^ set(before_extra)) | (set(after_denied) ^ set(before_denied))
        if not delta:
            return

        if not principal.has(Permission.PERMISSION_ASSIGN):
            raise PermissionDeniedError(str(Permission.PERMISSION_ASSIGN))

        assert_can_grant(principal, delta, what="direct permission grants")

    async def _assert_references(
        self,
        *,
        branch_id: uuid.UUID | None,
        plant_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
        vendor_id: uuid.UUID | None,
    ) -> None:
        """Every organisational reference must be a live record of this tenant.

        The foreign keys alone are not enough: ``branches.id`` is globally
        unique, so a valid id from another company would satisfy the constraint
        while breaking tenant isolation (section 40).
        """
        if branch_id is not None and not await self.repo.branch_exists(branch_id):
            raise NotFoundError("Branch", branch_id)
        if plant_id is not None and not await self.repo.plant_exists(plant_id):
            raise NotFoundError("Plant", plant_id)
        if department_id is not None and not await self.repo.department_exists(department_id):
            raise NotFoundError("Department", department_id)
        if vendor_id is not None and not await self.repo.vendor_exists(vendor_id):
            raise NotFoundError("Vendor", vendor_id)

    @staticmethod
    def _assert_scope_is_meaningful(
        vendor_id: uuid.UUID | None, allowed_category_ids: Sequence[uuid.UUID] | None
    ) -> None:
        """Category restrictions only mean something on a vendor account.

        Refused rather than ignored on an internal account: a silently discarded
        restriction reads, on the screen that set it, as a restriction that is in
        force (section 16).
        """
        if allowed_category_ids and vendor_id is None:
            raise BusinessRuleError(
                "Category restrictions apply to vendor-portal accounts only. "
                "Set a vendor on this account, or clear the category list.",
                details={"allowed_category_ids": [str(c) for c in allowed_category_ids]},
            )

    async def _assert_category_scope(
        self, vendor_id: uuid.UUID | None, allowed_category_ids: Sequence[uuid.UUID] | None
    ) -> None:
        """A vendor user's categories: live categories of this tenant the vendor supplies.

        A UUID foreign key is not a tenant check (an array column carries no
        foreign key at all), and a scope naming a category the vendor is not
        qualified for promises the manager orders that will never be raised on
        that vendor. ``vendor_categories`` is the scoping table its own model
        describes; this is where that subset rule is enforced.
        """
        if not allowed_category_ids or vendor_id is None:
            return
        requested = list(dict.fromkeys(allowed_category_ids))
        live = await self.repo.live_category_ids(requested)
        missing = [str(c) for c in requested if c not in live]
        if missing:
            raise NotFoundError("Item category", ", ".join(missing))
        qualified = await self.repo.vendor_category_ids(vendor_id)
        outside = [str(c) for c in requested if c not in qualified]
        if outside:
            raise BusinessRuleError(
                "A vendor user can only be limited to item categories the vendor is qualified to "
                "supply. Qualify the vendor for these categories first, or leave them out.",
                details={"allowed_category_ids": outside},
            )

    def _assert_not_self(self, user: User, action: str) -> None:
        if BLOCK_SELF_LIFECYCLE_CHANGE and user.id == self.tenant.user_id:
            raise BusinessRuleError(
                f"You cannot {action} your own account. Ask another administrator.",
                details={"user_id": str(user.id)},
            )

    async def _assert_not_last_super_admin(self, user: User, *, action: str) -> None:
        """Refuse to leave the tenant with no platform administrator.

        Evaluated inside the caller's transaction so two concurrent requests
        cannot each observe "there is one other administrator" and both proceed.
        """
        remaining = await self.repo.count_active_super_admins(
            sorted(SUPER_ADMIN_ROLE_CODES), exclude_user_id=user.id
        )
        if remaining == 0:
            raise BusinessRuleError(
                f"{user.email} is the only active super administrator in this company; "
                f"{action} it would leave nobody able to administer it. "
                "Grant the role to someone else first.",
                details={
                    "user_id": str(user.id),
                    "super_admin_roles": sorted(SUPER_ADMIN_ROLE_CODES),
                },
            )

    async def _record_role_change(
        self,
        user: User,
        *,
        before: Sequence[Role],
        after: Sequence[Role],
        reason: str,
    ) -> None:
        before_codes = sorted(role.code for role in before)
        after_codes = sorted(role.code for role in after)
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            action=AuditAction.ROLE_CHANGE,
            entity_type=AUDIT_USER,
            entity_id=user.id,
            entity_code=user.email,
            summary=(
                f"Roles for {user.email}: "
                f"{', '.join(before_codes) or 'none'} -> {', '.join(after_codes) or 'none'}"
            ),
            user_id=self.tenant.user_id,
            old_values={"roles": before_codes},
            new_values={"roles": after_codes},
            context={"reason": reason},
        )

    async def _invalidate(self, user_ids: Iterable[uuid.UUID]) -> None:
        """Drop the cached permission set of every affected user.

        Called after the surrounding transaction has committed. Dropping the key
        while the change is still uncommitted would let a concurrent
        authenticated request repopulate it from the old state, which is the
        stale-authority failure section 39 exists to prevent.
        """
        for user_id in dict.fromkeys(user_ids):
            await invalidate_user_permissions(user_id, self.tenant.company_id)


# -----------------------------------------------------------------------------
# Role service
# -----------------------------------------------------------------------------


class RoleService:
    """Role and permission-matrix administration. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = RoleRepository(session, tenant)
        self.users = UserRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_roles(
        self, filters: RoleFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[RoleSummary], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)

        # The permission arrays are not loaded for list rows; their sizes come
        # back for the whole page in one statement.
        counts = await self.repo.permission_counts([row.id for row in rows])
        return [_role_summary(row, counts.get(row.id, 0)) for row in rows], pagination

    async def get_role(self, role_id: uuid.UUID) -> RoleDetail:
        role = await self.repo.get_or_404(role_id)
        holders = await self.users.count_users_with_role(role.id)
        return self._to_detail(role, holders)

    # --- writes -------------------------------------------------------------

    async def create_role(self, principal: Principal, payload: RoleCreate) -> RoleDetail:
        """Create a role.

        Audited as a CREATE rather than a PERMISSION_CHANGE: at this moment
        nobody holds the role, so no account's authority has changed. The
        permission set is captured in ``new_values`` either way.
        """
        assert_can_grant(principal, payload.permissions, what="a role's permissions")

        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Role code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )

            role = Role(
                code=payload.code,
                name=payload.name,
                description=payload.description,
                permissions=payload.permissions,
                # Never from the payload: system roles are seeded, and one
                # claiming the flag would be undeletable for no traceable reason.
                is_system=False,
                is_vendor_role=payload.is_vendor_role,
                is_active=payload.is_active,
            )
            self.repo.add(role)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ROLE,
                entity_id=role.id,
                entity_code=role.code,
                summary=f"Created role {role.code} ({role.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(role, _ROLE_AUDITED_FIELDS),
            )

        return self._to_detail(role, 0)

    async def update_role(
        self, principal: Principal, role_id: uuid.UUID, payload: RoleUpdate
    ) -> RoleDetail:
        """Edit a role.

        A system role's permissions *can* be edited - section 81 prefers
        configurable behaviour - with the single exception listed in
        :data:`IMMUTABLE_ROLE_CODES`.
        """
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        affected: list[uuid.UUID] = []

        async with transaction(self.session):
            role = await self.repo.get_or_404(role_id)
            self._assert_mutable(role)

            before = snapshot(role, _ROLE_AUDITED_FIELDS)
            new_permissions = changes.get("permissions")
            permissions_changed = new_permissions is not None and sorted(new_permissions) != sorted(
                role.permissions or []
            )
            if permissions_changed:
                assert_grant_change_allowed(
                    principal,
                    role.permissions or [],
                    new_permissions or [],
                    what=f"the permissions of role {role.code}",
                )

            new_active = changes.get("is_active")
            state_from = lifecycle_state(role)
            state_to: AccountState | None = None
            if new_active is not None and new_active != role.is_active:
                state_to = AccountState.ACTIVE if new_active else AccountState.INACTIVE
                ROLE_LIFECYCLE.assert_transition(state_from, state_to)

            for field, value in changes.items():
                setattr(role, field, value)

            self.repo.touch(role)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ROLE,
                entity_id=role.id,
                entity_code=role.code,
                summary=f"Updated role {role.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(role, _ROLE_AUDITED_FIELDS),
            )

            if permissions_changed or state_to is not None:
                # Read once and reused for both the audit context and the cache
                # invalidation - the same set, by construction.
                affected = await self.users.user_ids_with_role(role.id)

            if permissions_changed:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.PERMISSION_CHANGE,
                    entity_type=AUDIT_ROLE,
                    entity_id=role.id,
                    entity_code=role.code,
                    summary=(
                        f"Permission set of role {role.code} changed; "
                        f"{len(affected)} account(s) affected"
                    ),
                    user_id=self.tenant.user_id,
                    old_values={"permissions": before.get("permissions")},
                    new_values={"permissions": role.permissions},
                    context={"affected_user_count": len(affected)},
                )

            if state_to is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ROLE,
                    entity_id=role.id,
                    entity_code=role.code,
                    from_status=str(state_from),
                    to_status=str(state_to),
                    user_id=self.tenant.user_id,
                    action=AuditAction.ROLE_CHANGE,
                    reason=f"{len(affected)} account(s) affected",
                )

        await self._invalidate(affected)
        return await self.get_role(role_id)

    async def delete_role(self, role_id: uuid.UUID, *, reason: str) -> None:
        """Retire a role.

        System roles cannot be retired at all: they are recreated by the seed on
        the next deployment, so "deleting" one would produce a role that keeps
        coming back with its original permissions.
        """
        async with transaction(self.session):
            role = await self.repo.get_or_404(role_id)

            if role.is_system:
                raise BusinessRuleError(
                    f"Role {role.code} is a system role and cannot be deleted. "
                    "Suspend it instead if it should not be assignable.",
                    details={"code": role.code, "is_system": True},
                )

            holders = await self.users.count_users_with_role(role.id)
            if BLOCK_ROLE_DELETION_WHILE_HELD and holders:
                raise referenced_in_use(
                    f"Role {role.code} is still held by {holders} account(s). "
                    "Reassign them before deleting it.",
                    code=role.code,
                    user_count=holders,
                )

            current = lifecycle_state(role)
            ROLE_LIFECYCLE.assert_transition(current, AccountState.ARCHIVED)

            await self.repo.soft_delete(role)

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ROLE,
                entity_id=role.id,
                entity_code=role.code,
                from_status=str(current),
                to_status=str(AccountState.ARCHIVED),
                user_id=self.tenant.user_id,
                action=AuditAction.ROLE_CHANGE,
                reason=reason,
            )

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _assert_mutable(role: Role) -> None:
        if role.code in IMMUTABLE_ROLE_CODES:
            raise BusinessRuleError(
                f"Role {role.code} is immutable: it defines the platform-administration "
                "authority the last-administrator guard is written in terms of.",
                details={"code": role.code},
            )

    def _to_detail(self, role: Role, user_count: int) -> RoleDetail:
        return RoleDetail(
            id=role.id,
            code=role.code,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            is_vendor_role=role.is_vendor_role,
            is_active=role.is_active,
            permission_count=len(role.permissions or []),
            created_at=role.created_at,
            permissions=sorted(role.permissions or []),
            user_count=user_count,
            updated_at=role.updated_at,
        )

    async def _invalidate(self, user_ids: Iterable[uuid.UUID]) -> None:
        """Drop each affected user's cached permission set, after the commit."""
        for user_id in dict.fromkeys(user_ids):
            await invalidate_user_permissions(user_id, self.tenant.company_id)


__all__ = [
    "AUDIT_ROLE",
    "AUDIT_USER",
    "BLOCK_ROLE_DELETION_WHILE_HELD",
    "BLOCK_SELF_LIFECYCLE_CHANGE",
    "IMMUTABLE_ROLE_CODES",
    "PROTECT_SUPER_ADMIN_PASSWORDS",
    "REQUIRE_AUTHORITY_TO_REVOKE",
    "ROLE_LIFECYCLE",
    "SUPERUSER_FLAG_IS_API_SETTABLE",
    "SUPER_ADMIN_PASSWORD_PROTECTED",
    "SUPER_ADMIN_ROLE_CODES",
    "USER_LIFECYCLE",
    "AccountState",
    "RoleService",
    "RoleView",
    "UserService",
    "assert_can_grant",
    "assert_grant_change_allowed",
    "assert_may_set_password_for",
    "assert_role_assignment_allowed",
    "assert_vendor_role_consistency",
    "caller_holds_super_admin",
    "generate_temporary_password",
    "holds_super_admin",
    "lifecycle_state",
    "permission_catalogue",
    "referenced_in_use",
    "resolve_effective_permissions",
    "role_permission_map",
    "usable_roles",
]
