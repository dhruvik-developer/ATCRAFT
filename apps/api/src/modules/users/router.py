"""HTTP surface for user and role administration.

Route handlers do three things and nothing else: bind parameters, call one
service method, wrap the result in the standard envelope. Every rule, every
query and every audit write lives behind the service (section 74).

Three conventions specific to this module:

* **Two resources, one exported router.** ``/users`` and ``/roles`` are separate
  ``APIRouter`` objects so they get their own OpenAPI tag groups, and are merged
  into the ``router`` that ``src/api/router.py`` mounts. They belong to one
  module because a role assignment touches both and neither can be reasoned
  about without the other.
* **Permission gates are chosen per act, not per resource.** Editing a name is
  ``user.update``; handing out authority is ``permission.assign``; resetting a
  password is ``user.reset_password``. The service re-checks the ones that
  depend on the *stored* row - a direct permission grant, the privilege
  guard - because a route cannot see the record before it is loaded.
* **``/roles/permissions/catalogue`` is declared before ``/roles/{role_id}``.**
  FastAPI matches in declaration order, and although the two differ in depth,
  keeping the literal path first is the habit that stops the next static
  sub-resource from being swallowed by the UUID route.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    require_permission,
)
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.users.schemas import (
    EffectivePermissions,
    PasswordResetRequest,
    PasswordResetResult,
    PermissionCatalogue,
    RoleAssignment,
    RoleCreate,
    RoleDetail,
    RoleFilters,
    RoleSummary,
    RoleUpdate,
    StatusChangeRequest,
    UserCreate,
    UserDetail,
    UserFilters,
    UserSummary,
    UserUpdate,
)
from src.modules.users.service import RoleService, UserService, permission_catalogue

users_router = APIRouter(prefix="/users", tags=["Users"], responses=COMMON_ERROR_RESPONSES)
roles_router = APIRouter(
    prefix="/roles", tags=["Roles & Permissions"], responses=COMMON_ERROR_RESPONSES
)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string rather than a request body:
# a DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being retired")
]


def user_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Email, name, employee code or phone")
    ] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_superuser: Annotated[bool | None, Query()] = None,
    is_vendor_user: Annotated[
        bool | None, Query(description="True: vendor-portal accounts only. False: internal staff")
    ] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    role_id: Annotated[uuid.UUID | None, Query(description="Accounts holding this role")] = None,
    is_locked: Annotated[
        bool | None, Query(description="Accounts currently locked out by failed sign-ins")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired accounts")] = False,
) -> UserFilters:
    return UserFilters(
        search=search,
        is_active=is_active,
        is_superuser=is_superuser,
        is_vendor_user=is_vendor_user,
        vendor_id=vendor_id,
        branch_id=branch_id,
        plant_id=plant_id,
        department_id=department_id,
        role_id=role_id,
        is_locked=is_locked,
        include_deleted=include_deleted,
    )


def role_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code, name or note")] = None,
    is_system: Annotated[bool | None, Query()] = None,
    is_vendor_role: Annotated[bool | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    grants_permission: Annotated[
        str | None, Query(max_length=64, description="Roles granting this permission")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired roles")] = False,
) -> RoleFilters:
    return RoleFilters(
        search=search,
        is_system=is_system,
        is_vendor_role=is_vendor_role,
        is_active=is_active,
        grants_permission=grants_permission,
        include_deleted=include_deleted,
    )


UserFilterDep = Annotated[UserFilters, Depends(user_filters)]
RoleFilterDep = Annotated[RoleFilters, Depends(role_filters)]


# -----------------------------------------------------------------------------
# Users
# -----------------------------------------------------------------------------


@users_router.get(
    "",
    response_model=PaginatedResponse[UserSummary],
    summary="List users",
    dependencies=[require_permission(Permission.USER_VIEW)],
)
async def list_users(
    session: DbSession,
    tenant: Tenant,
    filters: UserFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[UserSummary]:
    items, pagination = await UserService(session, tenant).list_users(filters, params, sort)
    return PaginatedResponse[UserSummary](data=items, pagination=pagination)


@users_router.post(
    "",
    response_model=ApiResponse[UserDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    dependencies=[require_permission(Permission.USER_CREATE)],
)
async def create_user(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    payload: UserCreate,
) -> ApiResponse[UserDetail]:
    """Create an internal or vendor-portal account.

    The initial password is checked against the policy and hashed with Argon2id;
    it is never stored, logged or echoed back.
    """
    created = await UserService(session, tenant).create_user(principal, payload)
    return ApiResponse.ok(created, "User created")


@users_router.get(
    "/{user_id}",
    response_model=ApiResponse[UserDetail],
    summary="Get one user with the roles it holds",
    dependencies=[require_permission(Permission.USER_VIEW)],
)
async def get_user(
    session: DbSession, tenant: Tenant, user_id: uuid.UUID
) -> ApiResponse[UserDetail]:
    return ApiResponse.ok(await UserService(session, tenant).get_user(user_id))


@users_router.patch(
    "/{user_id}",
    response_model=ApiResponse[UserDetail],
    summary="Update a user",
    dependencies=[require_permission(Permission.USER_UPDATE)],
)
async def update_user(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    user_id: uuid.UUID,
    payload: UserUpdate,
) -> ApiResponse[UserDetail]:
    """Update a user's profile, placement or direct permission overrides.

    Direct grants additionally require ``permission.assign`` and pass the
    privilege-escalation guard, both enforced by the service - the route cannot
    see which fields the payload changed relative to the stored row.
    """
    updated = await UserService(session, tenant).update_user(principal, user_id, payload)
    return ApiResponse.ok(updated, "User updated")


@users_router.post(
    "/{user_id}/roles",
    response_model=ApiResponse[UserDetail],
    summary="Replace a user's role set",
    dependencies=[require_permission(Permission.PERMISSION_ASSIGN)],
)
async def assign_roles(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    user_id: uuid.UUID,
    payload: RoleAssignment,
) -> ApiResponse[UserDetail]:
    """Set the user's roles to exactly the submitted list.

    Gated on ``permission.assign`` rather than ``user.update``: assigning a role
    is granting every permission in it, which is a different act from editing a
    telephone number.
    """
    updated = await UserService(session, tenant).assign_roles(principal, user_id, payload)
    return ApiResponse.ok(updated, "Roles updated")


@users_router.post(
    "/{user_id}/activate",
    response_model=ApiResponse[UserDetail],
    summary="Reactivate a user account",
    dependencies=[require_permission(Permission.USER_DEACTIVATE)],
)
async def activate_user(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    user_id: uuid.UUID,
    payload: StatusChangeRequest,
) -> ApiResponse[UserDetail]:
    updated = await UserService(session, tenant).set_active(
        principal, user_id, active=True, reason=payload.reason
    )
    return ApiResponse.ok(updated, "User activated")


@users_router.post(
    "/{user_id}/deactivate",
    response_model=ApiResponse[UserDetail],
    summary="Deactivate a user and revoke its sessions",
    dependencies=[require_permission(Permission.USER_DEACTIVATE)],
)
async def deactivate_user(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    user_id: uuid.UUID,
    payload: StatusChangeRequest,
) -> ApiResponse[UserDetail]:
    """Switch an account off and end every live session it holds."""
    updated = await UserService(session, tenant).set_active(
        principal, user_id, active=False, reason=payload.reason
    )
    return ApiResponse.ok(updated, "User deactivated and sessions revoked")


@users_router.post(
    "/{user_id}/reset-password",
    response_model=ApiResponse[PasswordResetResult],
    summary="Reset a user's password on their behalf",
    dependencies=[require_permission(Permission.USER_RESET_PASSWORD)],
)
async def reset_password(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    user_id: uuid.UUID,
    payload: PasswordResetRequest,
) -> ApiResponse[PasswordResetResult]:
    """Set a new password, force a change at next sign-in and revoke sessions.

    Omit ``new_password`` to have the server generate one; it is returned in this
    response and nowhere else. A super administrator's password can only be set
    by another super administrator, whatever permissions the caller holds.
    """
    result = await UserService(session, tenant).reset_password(principal, user_id, payload)
    message = (
        "Password reset; the temporary password is shown once and is not recoverable"
        if result.temporary_password
        else "Password reset"
    )
    return ApiResponse.ok(result, message)


@users_router.get(
    "/{user_id}/permissions",
    response_model=ApiResponse[EffectivePermissions],
    summary="Effective permissions of a user, with their provenance",
    dependencies=[require_permission(Permission.USER_VIEW)],
)
async def get_user_permissions(
    session: DbSession, tenant: Tenant, user_id: uuid.UUID
) -> ApiResponse[EffectivePermissions]:
    """The resolved set the request path will actually enforce.

    Includes the per-role breakdown, because "why can this person approve
    payments?" is the question an access review asks and a flat list cannot
    answer it.
    """
    return ApiResponse.ok(await UserService(session, tenant).get_effective_permissions(user_id))


@users_router.delete(
    "/{user_id}",
    response_model=ApiResponse[None],
    summary="Retire a user account",
    dependencies=[require_permission(Permission.USER_DEACTIVATE)],
)
async def retire_user(
    session: DbSession,
    tenant: Tenant,
    user_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    """Soft-delete an account and revoke its sessions.

    Gated on ``user.deactivate`` because the registry has no separate delete
    permission and retiring an account is the strongest form of deactivating it;
    there is deliberately no hard delete, since ``users.id`` is referenced by
    every document the person created (section 28).
    """
    await UserService(session, tenant).delete_user(user_id, reason=reason)
    return ApiResponse.ok(None, "User retired and sessions revoked")


# -----------------------------------------------------------------------------
# Roles
# -----------------------------------------------------------------------------


@roles_router.get(
    "/permissions/catalogue",
    response_model=ApiResponse[PermissionCatalogue],
    summary="The full permission registry, grouped for the permission matrix",
    dependencies=[require_permission(Permission.ROLE_VIEW)],
)
async def get_permission_catalogue() -> ApiResponse[PermissionCatalogue]:
    """Every permission the backend can gate on, in matrix order.

    Served from the compiled registry, so it needs neither the database nor the
    tenant; it is a fixed vocabulary of bounded size and is therefore not
    paginated (see :class:`~src.modules.users.schemas.PermissionCatalogue`).
    """
    return ApiResponse.ok(permission_catalogue())


@roles_router.get(
    "",
    response_model=PaginatedResponse[RoleSummary],
    summary="List roles",
    dependencies=[require_permission(Permission.ROLE_VIEW)],
)
async def list_roles(
    session: DbSession,
    tenant: Tenant,
    filters: RoleFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[RoleSummary]:
    items, pagination = await RoleService(session, tenant).list_roles(filters, params, sort)
    return PaginatedResponse[RoleSummary](data=items, pagination=pagination)


@roles_router.post(
    "",
    response_model=ApiResponse[RoleDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a role",
    dependencies=[require_permission(Permission.ROLE_MANAGE)],
)
async def create_role(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    payload: RoleCreate,
) -> ApiResponse[RoleDetail]:
    """Create a role.

    The caller may only put permissions into it that they hold themselves;
    otherwise ``role.manage`` alone would be equivalent to every permission in
    the system.
    """
    created = await RoleService(session, tenant).create_role(principal, payload)
    return ApiResponse.ok(created, "Role created")


@roles_router.get(
    "/{role_id}",
    response_model=ApiResponse[RoleDetail],
    summary="Get one role with its permission set",
    dependencies=[require_permission(Permission.ROLE_VIEW)],
)
async def get_role(
    session: DbSession, tenant: Tenant, role_id: uuid.UUID
) -> ApiResponse[RoleDetail]:
    return ApiResponse.ok(await RoleService(session, tenant).get_role(role_id))


@roles_router.patch(
    "/{role_id}",
    response_model=ApiResponse[RoleDetail],
    summary="Update a role",
    dependencies=[require_permission(Permission.ROLE_MANAGE)],
)
async def update_role(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    role_id: uuid.UUID,
    payload: RoleUpdate,
) -> ApiResponse[RoleDetail]:
    """Edit a role's name, note, permission set or availability.

    A system role's permissions may be edited (section 81 prefers configurable
    behaviour); the platform-administration role may not - see
    ``IMMUTABLE_ROLE_CODES``.
    """
    updated = await RoleService(session, tenant).update_role(principal, role_id, payload)
    return ApiResponse.ok(updated, "Role updated")


@roles_router.delete(
    "/{role_id}",
    response_model=ApiResponse[None],
    summary="Retire a role",
    dependencies=[require_permission(Permission.ROLE_MANAGE)],
)
async def delete_role(
    session: DbSession,
    tenant: Tenant,
    role_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    """Soft-delete a role that nobody holds. System roles cannot be deleted."""
    await RoleService(session, tenant).delete_role(role_id, reason=reason)
    return ApiResponse.ok(None, "Role retired")


# The single router ``src/api/router.py`` mounts. Users first so the identity
# tag group leads the OpenAPI document, matching the mount order there.
router = APIRouter()
router.include_router(users_router)
router.include_router(roles_router)

__all__ = ["roles_router", "router", "users_router"]
