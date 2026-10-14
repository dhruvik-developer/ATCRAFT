"""FastAPI dependencies: authentication, authorisation and tenant context.

Master rules sections 38/39/40. Three rules are enforced structurally here so no
route can forget them:

1. ``company_id`` comes from the verified token, never from the request. A route
   cannot even *see* a client-supplied tenant id.
2. Permissions are resolved per request from the database (Redis-cached with
   explicit invalidation), not read from token claims - so revoking an approval
   right takes effect immediately rather than at the next token expiry.
3. A vendor principal carries its vendor and category restrictions on the
   :class:`Principal`, and repositories must apply them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from fastapi.params import Depends as DependsParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.actors import SYSTEM_ACTOR_ID
from src.core.cache import cache_get, cache_set, make_key
from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import get_db
from src.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ErrorCode,
    PasswordChangeRequiredError,
    PermissionDeniedError,
)
from src.core.logging import bind_request_context, get_logger
from src.core.permissions import Permission, resolve_role_grants
from src.core.security import TokenPayload, decode_token
from src.modules.users.models import User, UserSession

log = get_logger(__name__)

# auto_error=False so a missing header produces our envelope-shaped 401 rather
# than FastAPI's default body.
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]


def password_change_route_allowed(path: str) -> bool:
    """Whether a temporary-password principal may call ``path``."""
    prefix = settings.api_v1_prefix.rstrip("/")
    allowed = {
        f"{prefix}/auth/me",
        f"{prefix}/auth/change-password",
        f"{prefix}/auth/logout",
        f"{prefix}/auth/logout-all",
    }
    return path.rstrip("/") in allowed


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, fully resolved.

    Immutable on purpose: a route handler must not be able to widen its own
    authority mid-request.
    """

    user_id: uuid.UUID
    company_id: uuid.UUID
    email: str
    full_name: str
    is_superuser: bool
    permissions: frozenset[str]
    must_change_password: bool = False
    session_id: str | None = None
    role_codes: tuple[str, ...] = ()
    vendor_id: uuid.UUID | None = None
    allowed_category_ids: tuple[uuid.UUID, ...] = ()
    allowed_warehouse_ids: tuple[uuid.UUID, ...] = ()
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None

    # --- authority checks ---------------------------------------------------

    def has(self, permission: Permission | str) -> bool:
        return self.is_superuser or str(permission) in self.permissions

    def has_any(self, *permissions: Permission | str) -> bool:
        return self.is_superuser or any(str(p) in self.permissions for p in permissions)

    def has_all(self, *permissions: Permission | str) -> bool:
        return self.is_superuser or all(str(p) in self.permissions for p in permissions)

    def require(self, permission: Permission | str) -> None:
        if not self.has(permission):
            raise PermissionDeniedError(str(permission))

    # --- scope checks -------------------------------------------------------

    @property
    def is_vendor_user(self) -> bool:
        return self.vendor_id is not None

    def can_access_category(self, category_id: uuid.UUID | None) -> bool:
        """Vendor users restricted to specific categories (section 16).

        An empty restriction list means unrestricted, which is why the check is
        written this way rather than as a bare ``in``.
        """
        if not self.allowed_category_ids:
            return True
        return category_id is not None and category_id in self.allowed_category_ids

    def can_access_warehouse(self, warehouse_id: uuid.UUID | None) -> bool:
        if not self.allowed_warehouse_ids:
            return True
        return warehouse_id is not None and warehouse_id in self.allowed_warehouse_ids

    def assert_same_tenant(self, company_id: uuid.UUID) -> None:
        """Belt-and-braces check for code paths that load by primary key.

        Repositories filter by ``company_id`` already; this catches a query that
        was written without the filter before it can return another tenant's row.
        """
        if company_id != self.company_id:
            from src.core.errors import TenantMismatchError

            log.warning(
                "tenant.mismatch_blocked",
                principal_company=str(self.company_id),
                record_company=str(company_id),
            )
            raise TenantMismatchError()


# -----------------------------------------------------------------------------
# Permission resolution
# -----------------------------------------------------------------------------


async def _resolve_permissions(
    session: AsyncSession, user: User
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Union of role permissions plus direct grants, minus direct denials.

    Denials are applied last and unconditionally: an explicit "this person may
    not approve payments" must not be overridable by adding another role. The
    fold itself is :func:`~src.core.permissions.resolve_role_grants`, shared with
    every other reader of an account's authority so none of them can disagree
    with what this request path enforces.
    """
    return resolve_role_grants(user.roles, user.extra_permissions, user.denied_permissions)


async def _load_principal(session: AsyncSession, payload: TokenPayload) -> Principal:
    stmt = (
        select(User)
        .options(selectinload(User.roles))
        .where(
            User.id == payload.subject,
            User.company_id == payload.company_id,
            User.deleted_at.is_(None),
        )
    )
    user = (await session.execute(stmt)).scalar_one_or_none()

    if user is None:
        raise AuthenticationError("Account not found.", code=ErrorCode.TOKEN_INVALID)
    if not user.is_active:
        raise AuthenticationError("Account is deactivated.", code=ErrorCode.ACCOUNT_INACTIVE)
    if user.is_locked:
        raise AuthenticationError("Account is locked.", code=ErrorCode.ACCOUNT_LOCKED)

    # A password change invalidates tokens issued before it, so a stolen access
    # token stops working the moment the user reacts to the compromise.
    if user.password_changed_at and payload.issued_at < int(user.password_changed_at.timestamp()):
        raise AuthenticationError(
            "Session ended because the password was changed.", code=ErrorCode.TOKEN_REVOKED
        )

    permissions, role_codes = await _resolve_permissions(session, user)

    return Principal(
        user_id=user.id,
        company_id=user.company_id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=user.is_superuser,
        permissions=permissions,
        must_change_password=user.must_change_password,
        session_id=payload.session_id,
        role_codes=role_codes,
        vendor_id=user.vendor_id,
        allowed_category_ids=tuple(user.allowed_category_ids or ()),
        allowed_warehouse_ids=tuple(user.allowed_warehouse_ids or ()),
        branch_id=user.branch_id,
        plant_id=user.plant_id,
        department_id=user.department_id,
    )


async def _session_is_live(session: AsyncSession, payload: TokenPayload) -> bool:
    """Confirm the refresh session behind this access token was not revoked.

    Cached briefly: without the cache this is a database round-trip on every
    single authenticated request, which is exactly the kind of avoidable work
    section 30 warns about. The TTL is short so a logout takes effect promptly.
    """
    if not payload.session_id:
        return True

    cache_key = make_key("sessrevoke", payload.company_id, payload.session_id)
    cached = await cache_get(cache_key)
    if cached is not None:
        return bool(cached)

    stmt = select(UserSession.id).where(
        UserSession.session_id == payload.session_id,
        UserSession.company_id == payload.company_id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > utcnow(),
    )
    live = (await session.execute(stmt)).first() is not None
    await cache_set(cache_key, live, ttl=30)
    return live


# -----------------------------------------------------------------------------
# Dependencies
# -----------------------------------------------------------------------------


async def get_current_principal(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Principal:
    """Authenticate the request and resolve full authority."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authorization header with a bearer token is required.")

    payload = decode_token(credentials.credentials, expected_type="access")

    if not await _session_is_live(session, payload):
        raise AuthenticationError("Session has been revoked.", code=ErrorCode.TOKEN_REVOKED)

    principal = await _load_principal(session, payload)

    # A temporary/admin-reset password grants access only to the small
    # self-service surface required to replace it and end sessions. This lives
    # in the shared authentication dependency so a newly added business route
    # cannot accidentally forget the restriction.
    if principal.must_change_password and not password_change_route_allowed(request.url.path):
        raise PasswordChangeRequiredError()

    bind_request_context(user_id=principal.user_id, company_id=principal.company_id)
    request.state.principal = principal
    return principal


CurrentUser = Annotated[Principal, Depends(get_current_principal)]


def require_permission(*permissions: Permission, mode: str = "all") -> DependsParam:
    """Dependency factory gating a route on permissions.

        @router.post("/purchase-orders/{id}/approve",
                     dependencies=[Depends(require_permission(Permission.PURCHASE_ORDER_APPROVE))])

    ``mode="any"`` passes when the caller holds at least one of them - used
    where several roles legitimately reach the same read.
    """
    if not permissions:
        raise ValueError("require_permission needs at least one permission")

    async def _dependency(principal: CurrentUser) -> Principal:
        ok = principal.has_any(*permissions) if mode == "any" else principal.has_all(*permissions)
        if not ok:
            missing = [str(p) for p in permissions if not principal.has(p)]
            log.info(
                "authz.denied",
                user_id=str(principal.user_id),
                required=[str(p) for p in permissions],
                missing=missing,
                mode=mode,
            )
            raise PermissionDeniedError(", ".join(missing))
        return principal

    return Depends(_dependency)


def require_any_permission(*permissions: Permission) -> DependsParam:
    return require_permission(*permissions, mode="any")


async def require_superuser(principal: CurrentUser) -> Principal:
    if not principal.is_superuser:
        raise AuthorizationError("This action is restricted to platform administrators.")
    return principal


def forbid_vendor_users(principal: CurrentUser) -> Principal:
    """Block vendor-portal principals from an internal-only endpoint.

    Belt and braces alongside permissions: a misconfigured vendor role must not
    be able to reach internal costing or other tenants' orders.
    """
    if principal.is_vendor_user:
        raise AuthorizationError(
            "This endpoint is not available to vendor accounts.", code=ErrorCode.SCOPE_DENIED
        )
    return principal


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Everything a repository needs to scope a query correctly."""

    company_id: uuid.UUID
    user_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    allowed_category_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    allowed_warehouse_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    is_superuser: bool = False


def system_tenant(company_id: uuid.UUID) -> TenantContext:
    """Scope for work with no principal - a scheduled sweep acting for one company.

    The user id is the nil :data:`~src.core.actors.SYSTEM_ACTOR_ID`, which the
    repository base class and the audit writer stamp as NULL (see
    :mod:`src.core.actors`), so a service can be called from a sweep unchanged.
    """
    return TenantContext(company_id=company_id, user_id=SYSTEM_ACTOR_ID)


def get_tenant_context(principal: CurrentUser) -> TenantContext:
    """Derive the query scope from the verified principal.

    This is the only supported source of ``company_id``. A client-supplied one is
    never read anywhere in the codebase (section 40).
    """
    return TenantContext(
        company_id=principal.company_id,
        user_id=principal.user_id,
        vendor_id=principal.vendor_id,
        allowed_category_ids=principal.allowed_category_ids,
        allowed_warehouse_ids=principal.allowed_warehouse_ids,
        is_superuser=principal.is_superuser,
    )


Tenant = Annotated[TenantContext, Depends(get_tenant_context)]


def permissions_of(principal: Principal, subset: Sequence[Permission]) -> list[str]:
    """Which of ``subset`` this principal holds - used to drive UI affordances."""
    return [str(p) for p in subset if principal.has(p)]


__all__ = [
    "CurrentUser",
    "DbSession",
    "Principal",
    "Tenant",
    "TenantContext",
    "forbid_vendor_users",
    "get_current_principal",
    "get_tenant_context",
    "password_change_route_allowed",
    "permissions_of",
    "require_any_permission",
    "require_permission",
    "require_superuser",
    "system_tenant",
]
