"""Request and response contracts for user and role administration.

Conventions applied throughout, each for a reason:

* **No request model carries ``company_id`` or ``is_superuser``.** The tenant
  comes from the authenticated principal (section 40), and the superuser flag is
  a platform-level escape hatch that must not be reachable through an ordinary
  administrative API - see the ASSUMPTION note in ``service.py``.
* **No response model carries ``password_hash``, ``mfa_secret`` or a session
  token.** They are not merely omitted from a serialiser; they are absent from
  the models, so a future field addition cannot leak one by accident.
* **Passwords are bounded but not policed here.** Only length limits live on the
  schema; the real rule is
  :func:`src.core.security.validate_password_policy`, called by the service, so
  a policy change cannot leave the edge and the service disagreeing. A pydantic
  error also cannot say *which* rule failed, and someone resetting a password
  needs exactly that.
* **Permission strings are validated against the compiled registry.** A typo
  such as ``"purchase_order.aprove"`` would otherwise be stored in a role and
  silently grant nothing, which reads as "the permission system is broken".
* **Updates carry explicit "unset" semantics.** Every field on an update schema
  is optional and the service applies only the keys the client actually sent
  (``model_dump(exclude_unset=True)``); ``clear_*`` flags disambiguate "leave it
  alone" from "set it to NULL", which both arrive as ``None``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.common.codes import clean_name, strip_optional
from src.core.permissions import Permission

#: Bounds only - the policy itself lives in ``src.core.security``.
PasswordField = Field(min_length=1, max_length=128)

#: A role code is referenced from seed data, from configuration and from
#: :attr:`src.core.deps.Principal.role_codes`; punctuation would make those
#: comparisons ambiguous.
ROLE_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"

#: Upper bound on a single role assignment. Not a business limit - a guard so a
#: malformed client cannot post ten thousand ids and turn one request into a
#: ten-thousand-row write.
MAX_ROLES_PER_USER = 50

#: Per-user direct grants and denials are an exception mechanism, not a second
#: role system. A list approaching the size of the registry means a role should
#: have been created instead.
MAX_DIRECT_PERMISSIONS = 100

#: Vendor-category and warehouse scope lists. Same reasoning as above.
MAX_SCOPE_IDS = 200

_VALID_PERMISSIONS: frozenset[str] = frozenset(str(p) for p in Permission)

Name = Annotated[str, Field(min_length=1, max_length=255)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]


def _normalise_email(value: str) -> str:
    """Lower-case and trim.

    ``users.email`` is ``CIText`` so the column already normalises on write, but
    the duplicate check, the audit summary and the password-policy check all
    read this string before it ever reaches the column.
    """
    return value.strip().lower()


def _normalise_role_code(value: str) -> str:
    cleaned = value.strip().upper().replace("-", "_").replace(" ", "_")
    if not re.match(ROLE_CODE_PATTERN, cleaned):
        raise ValueError(
            "Role code must start with a letter and contain only capital letters, "
            "digits and underscores, for example PURCHASE_MANAGER."
        )
    return cleaned


def _validate_permissions(values: list[str] | None) -> list[str]:
    """Reject unknown permission strings; return a sorted, de-duplicated list.

    Sorted because the stored array is what audit diffs compare: an otherwise
    identical set in a different order would register as a change and fill the
    one log an access review depends on with edits nobody made.
    """
    if not values:
        return []
    cleaned = {value.strip() for value in values if value and value.strip()}
    unknown = sorted(value for value in cleaned if value not in _VALID_PERMISSIONS)
    if unknown:
        raise ValueError(
            "Unknown permission(s): "
            + ", ".join(unknown)
            + ". Permissions come from the compiled registry - see "
            "GET /roles/permissions/catalogue."
        )
    return sorted(cleaned)


def _unique_ids(values: list[uuid.UUID] | None) -> list[uuid.UUID]:
    """De-duplicate while preserving order.

    A repeated role id would otherwise become a duplicate ``user_roles`` row and
    fail on the composite primary key with a 500 rather than a clear message.
    """
    if not values:
        return []
    seen: dict[uuid.UUID, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


# -----------------------------------------------------------------------------
# Users
# -----------------------------------------------------------------------------


class UserCreate(_Base):
    """Payload for creating an internal staff or vendor-portal account.

    ``is_superuser`` is deliberately absent - see the ASSUMPTION note in the
    service. ``must_change_password`` defaults to True because an administrator
    choosing someone else's first password means that password has been typed
    into a chat window or read out over a telephone.
    """

    email: EmailStr
    full_name: Name
    password: str = PasswordField
    phone: str | None = Field(default=None, max_length=32)
    employee_code: str | None = Field(default=None, max_length=32)

    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None

    #: Set to bind this account to the vendor portal. A user with a vendor may
    #: hold only vendor roles, and a vendor role may be held only by such a
    #: user - enforced in both directions by the service (section 16).
    vendor_id: uuid.UUID | None = None
    allowed_category_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_SCOPE_IDS)] = Field(
        default_factory=list
    )
    allowed_warehouse_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_SCOPE_IDS)] = Field(
        default_factory=list
    )

    role_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_ROLES_PER_USER)] = Field(
        default_factory=list
    )
    extra_permissions: Annotated[list[str], Field(max_length=MAX_DIRECT_PERMISSIONS)] = Field(
        default_factory=list
    )
    denied_permissions: Annotated[list[str], Field(max_length=MAX_DIRECT_PERMISSIONS)] = Field(
        default_factory=list
    )

    is_active: bool = True
    must_change_password: bool = True
    preferences: dict[str, Any] | None = None

    _email = field_validator("email")(_normalise_email)
    _name = field_validator("full_name")(clean_name)
    _optional = field_validator("phone", "employee_code")(strip_optional)
    _permissions = field_validator("extra_permissions", "denied_permissions")(_validate_permissions)
    _ids = field_validator("role_ids", "allowed_category_ids", "allowed_warehouse_ids")(_unique_ids)


class UserUpdate(BaseModel):
    """Partial update of a user.

    Changing the login/recovery email additionally requires password-reset
    authority and the protected-administrator target check. Unchanged email
    submissions remain ordinary profile updates.

    Absent on purpose:

    * ``is_active`` - activation is a lifecycle change with a mandatory reason
      that also revokes sessions, so it goes through the activate/deactivate
      endpoints rather than being flippable as a side effect of an edit.
    * ``role_ids`` - the role set is replaced wholesale through
      ``POST /users/{id}/roles``, which is gated on ``permission.assign`` rather
      than on the much more widely granted ``user.update``.
    * ``is_superuser`` - see the service.
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="forbid")

    email: EmailStr | None = None
    full_name: Name | None = None
    #: An administrator setting a password directly. Validated against the same
    #: policy as a self-service change, and it revokes the account's sessions.
    password: str | None = Field(default=None, min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    employee_code: str | None = Field(default=None, max_length=32)

    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None

    allowed_category_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_SCOPE_IDS)] | None = None
    allowed_warehouse_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_SCOPE_IDS)] | None = None
    extra_permissions: Annotated[list[str], Field(max_length=MAX_DIRECT_PERMISSIONS)] | None = None
    denied_permissions: Annotated[list[str], Field(max_length=MAX_DIRECT_PERMISSIONS)] | None = None

    must_change_password: bool | None = None
    preferences: dict[str, Any] | None = None

    #: "Field absent" and "field null" both arrive as None and must not mean the
    #: same thing - a PATCH that omits ``branch_id`` must not unassign the
    #: branch.
    clear_branch: bool = False
    clear_plant: bool = False
    clear_department: bool = False
    #: Detaching an account from the vendor portal. Refused while the user still
    #: holds vendor roles, because that would break the isolation rule in the
    #: other direction.
    clear_vendor: bool = False

    _email = field_validator("email")(_normalise_email)
    _name = field_validator("full_name")(clean_name)
    _optional = field_validator("phone", "employee_code")(strip_optional)

    @field_validator("extra_permissions", "denied_permissions")
    @classmethod
    def _permissions(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_permissions(value)

    @field_validator("allowed_category_ids", "allowed_warehouse_ids")
    @classmethod
    def _ids(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return None if value is None else _unique_ids(value)


class RoleAssignment(BaseModel):
    """Replace a user's entire role set.

    Replace rather than add/remove: a UI that shows a checklist and posts the
    result cannot express "remove this one" as an add, and two administrators
    editing the same user concurrently with add/remove deltas silently merge
    into a set neither of them chose.

    An empty list is legal and means "this account holds no roles"; it keeps
    only whatever direct grants it has.
    """

    model_config = ConfigDict(extra="forbid")

    role_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_ROLES_PER_USER)] = Field(
        default_factory=list
    )
    reason: Reason

    _ids = field_validator("role_ids")(_unique_ids)


class StatusChangeRequest(BaseModel):
    """Reason capture for a lifecycle change.

    Required, not optional: section 83 is explicit that a state change with no
    recorded reason is what makes an audit trail useless six months later, when
    nobody remembers why an account was switched off.
    """

    reason: Reason


class PasswordResetRequest(BaseModel):
    """Administrator-triggered password reset.

    ``new_password`` is optional. Omitting it makes the server generate a
    single-use temporary password and return it once, which is the safer
    default: an administrator who invents passwords tends to reuse the same one
    for every account they touch.
    """

    model_config = ConfigDict(extra="forbid")

    new_password: str | None = Field(default=None, min_length=1, max_length=128)
    reason: Reason


class PasswordResetResult(BaseModel):
    """Outcome of a reset.

    ``temporary_password`` is populated only when the server generated one, is
    returned exactly once, and is never stored in plaintext nor written to the
    audit log (``src.modules.audit.service`` strips it in any case).
    """

    user_id: uuid.UUID
    must_change_password: bool
    sessions_revoked: int
    temporary_password: str | None = None


class UserSummary(_Base):
    """List row.

    Narrower than the detail model on purpose: a user grid renders none of the
    scope arrays or the preferences blob, and loading them would mean selecting
    two ARRAY columns and a JSONB column for every row on every page.
    """

    id: uuid.UUID
    email: str
    full_name: str
    employee_code: str | None
    is_active: bool
    is_superuser: bool
    must_change_password: bool
    vendor_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    department_id: uuid.UUID | None
    last_login_at: datetime | None
    locked_until: datetime | None
    created_at: datetime
    role_codes: list[str] = Field(default_factory=list)


class RoleSummary(_Base):
    """Role list row.

    ``permission_count`` rather than the array itself: a permission matrix is a
    detail view, and a grid that carried 200 strings per row would be an order
    of magnitude larger than the data anyone is reading.
    """

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    is_system: bool
    is_vendor_role: bool
    is_active: bool
    permission_count: int
    created_at: datetime


class UserDetail(UserSummary):
    """Single-user view, including the roles it holds."""

    phone: str | None
    mfa_enabled: bool
    failed_login_attempts: int
    password_changed_at: datetime | None
    allowed_category_ids: list[uuid.UUID] = Field(default_factory=list)
    allowed_warehouse_ids: list[uuid.UUID] = Field(default_factory=list)
    extra_permissions: list[str] = Field(default_factory=list)
    denied_permissions: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] | None = None
    is_vendor_user: bool = False
    updated_at: datetime
    roles: list[RoleSummary] = Field(default_factory=list)


class UserFilters(BaseModel):
    """List filters, validated here so the repository never sees free text."""

    search: str | None = None
    is_active: bool | None = None
    is_superuser: bool | None = None
    #: True: vendor-portal accounts only. False: internal staff only.
    is_vendor_user: bool | None = None
    vendor_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    #: "Who holds this role?" - answered in SQL, not by filtering a page that
    #: has already been cut to fifty rows.
    role_id: uuid.UUID | None = None
    #: Accounts currently locked out by repeated failed sign-ins.
    is_locked: bool | None = None
    include_deleted: bool = False


class EffectivePermissions(_Base):
    """The resolved authority of one user, with its provenance.

    The breakdown matters as much as the total: "why can this person approve
    payments?" is the question an access review actually asks, and a flat list
    cannot answer it.
    """

    user_id: uuid.UUID
    email: str
    is_superuser: bool
    is_active: bool
    #: Final answer: role permissions plus direct grants, minus direct denials.
    permissions: list[str] = Field(default_factory=list)
    permission_count: int = 0
    #: Role code -> the permissions that role contributes. Inactive and retired
    #: roles are excluded, exactly as ``src.core.deps`` excludes them when a
    #: request is authenticated.
    role_permissions: dict[str, list[str]] = Field(default_factory=dict)
    extra_permissions: list[str] = Field(default_factory=list)
    #: Denials win over every grant, whatever its source.
    denied_permissions: list[str] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Roles
# -----------------------------------------------------------------------------


class RoleCreate(_Base):
    """Payload for creating a role.

    ``is_system`` is absent: system roles are seeded from
    :data:`src.core.permissions.ROLE_PRESETS`. A client-created role that
    claimed to be one would be undeletable for a reason nobody could trace.
    """

    code: Annotated[str, Field(min_length=2, max_length=48)]
    name: Name
    description: str | None = Field(default=None, max_length=2000)
    permissions: Annotated[list[str], Field(max_length=len(_VALID_PERMISSIONS))] = Field(
        default_factory=list
    )
    #: Vendor roles are assignable only to accounts bound to a vendor.
    is_vendor_role: bool = False
    is_active: bool = True

    _code = field_validator("code")(_normalise_role_code)
    _name = field_validator("name")(clean_name)
    _description = field_validator("description")(strip_optional)
    _permissions = field_validator("permissions")(_validate_permissions)


class RoleUpdate(BaseModel):
    """Partial update of a role.

    ``code`` is not updatable: it appears in seed data, in
    :attr:`src.core.deps.Principal.role_codes` and in historical audit rows, so
    renaming it in place would make that history ambiguous.

    ``is_vendor_role`` is not updatable either - flipping it would, in a single
    statement, either strip the portal boundary from every holder or make every
    holder illegal. Create the role you actually want and reassign.
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="forbid")

    name: Name | None = None
    description: str | None = Field(default=None, max_length=2000)
    permissions: Annotated[list[str], Field(max_length=len(_VALID_PERMISSIONS))] | None = None
    is_active: bool | None = None

    _name = field_validator("name")(clean_name)
    _description = field_validator("description")(strip_optional)

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_permissions(value)


class RoleDetail(RoleSummary):
    """Single-role view: the permission matrix and how many people hold it."""

    permissions: list[str] = Field(default_factory=list)
    #: Live, non-deleted holders. Drives the "12 users will be affected" warning
    #: the UI shows before a permission edit is saved.
    user_count: int = 0
    updated_at: datetime


class RoleFilters(BaseModel):
    search: str | None = None
    is_system: bool | None = None
    is_vendor_role: bool | None = None
    is_active: bool | None = None
    #: "Which roles grant payment.approve?" - the question asked during an
    #: access review, answered by the database rather than by the caller
    #: downloading every role and filtering client-side.
    grants_permission: str | None = None
    include_deleted: bool = False

    @field_validator("grants_permission")
    @classmethod
    def _known_permission(cls, value: str | None) -> str | None:
        cleaned = strip_optional(value)
        if cleaned is not None and cleaned not in _VALID_PERMISSIONS:
            raise ValueError(f"Unknown permission: {cleaned}")
        return cleaned


# -----------------------------------------------------------------------------
# Permission catalogue
# -----------------------------------------------------------------------------


class PermissionEntry(BaseModel):
    """One cell of the permission matrix."""

    value: str
    resource: str
    action: str
    group: str


class PermissionGroup(BaseModel):
    """One section of the matrix, in the order the registry declares it."""

    group: str
    resources: list[str] = Field(default_factory=list)
    permissions: list[PermissionEntry] = Field(default_factory=list)


class PermissionCatalogue(BaseModel):
    """The complete registry, grouped for the permission-matrix UI.

    Not paginated, deliberately: this is a compiled-in vocabulary of a fixed
    size, not a query over tenant data that can grow. Paginating it would force
    the matrix screen to make several round trips to render one fixed table.
    """

    groups: list[PermissionGroup] = Field(default_factory=list)
    total: int = 0


__all__ = [
    "MAX_DIRECT_PERMISSIONS",
    "MAX_ROLES_PER_USER",
    "MAX_SCOPE_IDS",
    "ROLE_CODE_PATTERN",
    "EffectivePermissions",
    "PasswordResetRequest",
    "PasswordResetResult",
    "PermissionCatalogue",
    "PermissionEntry",
    "PermissionGroup",
    "RoleAssignment",
    "RoleCreate",
    "RoleDetail",
    "RoleFilters",
    "RoleSummary",
    "RoleUpdate",
    "StatusChangeRequest",
    "UserCreate",
    "UserDetail",
    "UserFilters",
    "UserSummary",
    "UserUpdate",
]
