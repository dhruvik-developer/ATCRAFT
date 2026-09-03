"""HTTP surface for authentication.

Handlers here bind parameters, call one service method and wrap the result.
Every decision - what a failure means, what is recorded, what is revoked - lives
in the service (section 74).

Two things are specific to this module:

* **The unauthenticated routes are the attack surface of the whole system.**
  Sign-in, refresh and the two password-reset endpoints take no principal by
  design; their protection is the rate limiting and the constant-time,
  constant-shape answers the service produces, not a permission check.
* **The authenticated routes are gated on authentication alone.** There is no
  ``self.*`` permission in :class:`~src.core.permissions.Permission`, and
  inventing one would be a mistake: a permission is something an administrator
  can revoke, and revoking a user's ability to read their own profile, change
  their own password or sign out would lock them into a session they cannot
  end. Authority for these endpoints is the token itself, and every one of them
  operates strictly on the caller's own rows.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from src.core.deps import CurrentUser, DbSession
from src.core.pagination import PageParams, page_params
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.auth.schemas import (
    AcknowledgedResult,
    ChangePasswordRequest,
    ChangePasswordResult,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResult,
    MeResponse,
    PasswordResetConfirmPayload,
    PasswordResetRequestPayload,
    RefreshRequest,
    SessionRead,
    TokenPair,
)
from src.modules.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]

#: Identical wording whether or not the address belongs to an account - the
#: whole point of the endpoint pair (section 38).
RESET_REQUEST_MESSAGE = (
    "If an account exists for that email address, a password reset link has been sent to it."
)


def client_ip(request: Request) -> str | None:
    """The caller's address, or None when it is not a usable one.

    ``x-real-ip`` is set by the reverse proxy; ``request.client`` is the socket
    peer, which behind Nginx is Nginx itself. The value is parsed before use
    because it lands in an ``INET`` column: a spoofed or synthetic header (a
    test client sends "testclient") would otherwise fail the INSERT and turn a
    sign-in into a 500.
    """
    candidate = request.headers.get("x-real-ip") or (
        request.client.host if request.client else None
    )
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate.split(",")[0].strip()))
    except ValueError:
        return None


def user_agent(request: Request) -> str | None:
    raw = request.headers.get("user-agent")
    return raw[:512] if raw else None


# -----------------------------------------------------------------------------
# Sign in / out
# -----------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=ApiResponse[LoginResponse],
    summary="Sign in with an email address and password",
)
async def login(
    request: Request, session: DbSession, payload: LoginRequest
) -> ApiResponse[LoginResponse]:
    """Exchange credentials for an access token and a refresh token.

    The access token carries no permissions: they are resolved per request and
    returned by ``/auth/me``, so a role change takes effect immediately instead
    of at the next token expiry (section 39).
    """
    result = await AuthService(session).login(
        payload, ip_address=client_ip(request), user_agent=user_agent(request)
    )
    return ApiResponse.ok(result, "Signed in")


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenPair],
    summary="Rotate a refresh token",
)
async def refresh(
    request: Request, session: DbSession, payload: RefreshRequest
) -> ApiResponse[TokenPair]:
    """Issue a new token pair and retire the presented one.

    Unauthenticated by design: the refresh token *is* the credential, and an
    expired access token must not stand between a user and a new session.
    """
    tokens = await AuthService(session).refresh(
        payload.refresh_token, ip_address=client_ip(request), user_agent=user_agent(request)
    )
    return ApiResponse.ok(tokens, "Token refreshed")


@router.post(
    "/logout",
    response_model=ApiResponse[LogoutResult],
    summary="Revoke the current session",
)
async def logout(
    session: DbSession,
    principal: CurrentUser,
    payload: LogoutRequest | None = None,
) -> ApiResponse[LogoutResult]:
    result = await AuthService(session).logout(
        principal, refresh_token=payload.refresh_token if payload else None
    )
    return ApiResponse.ok(result, "Signed out")


@router.post(
    "/logout-all",
    response_model=ApiResponse[LogoutResult],
    summary="Revoke every session for the current user",
)
async def logout_all(session: DbSession, principal: CurrentUser) -> ApiResponse[LogoutResult]:
    result = await AuthService(session).logout_all(principal)
    return ApiResponse.ok(result, "Signed out of all devices")


# -----------------------------------------------------------------------------
# Current principal
# -----------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=ApiResponse[MeResponse],
    summary="The signed-in user, their roles and their effective permissions",
)
async def me(session: DbSession, principal: CurrentUser) -> ApiResponse[MeResponse]:
    return ApiResponse.ok(await AuthService(session).me(principal))


# -----------------------------------------------------------------------------
# Passwords
# -----------------------------------------------------------------------------


@router.post(
    "/change-password",
    response_model=ApiResponse[ChangePasswordResult],
    summary="Change the signed-in user's password",
)
async def change_password(
    session: DbSession, principal: CurrentUser, payload: ChangePasswordRequest
) -> ApiResponse[ChangePasswordResult]:
    """Every other session is revoked, and the response says how many."""
    result = await AuthService(session).change_password(principal, payload)
    return ApiResponse.ok(result, "Password changed")


@router.post(
    "/password-reset/request",
    response_model=ApiResponse[AcknowledgedResult],
    summary="Request a password reset link",
)
async def request_password_reset(
    request: Request, session: DbSession, payload: PasswordResetRequestPayload
) -> ApiResponse[AcknowledgedResult]:
    """Always 200, always the same message.

    The service's return value carries the reset token for delivery; it is
    discarded here on purpose. Putting it in the response would let anyone who
    can guess an email address take over the account.
    """
    await AuthService(session).request_password_reset(payload, ip_address=client_ip(request))
    return ApiResponse.ok(AcknowledgedResult(), RESET_REQUEST_MESSAGE)


@router.post(
    "/password-reset/confirm",
    response_model=ApiResponse[AcknowledgedResult],
    summary="Redeem a password reset link",
)
async def confirm_password_reset(
    request: Request, session: DbSession, payload: PasswordResetConfirmPayload
) -> ApiResponse[AcknowledgedResult]:
    await AuthService(session).confirm_password_reset(payload, ip_address=client_ip(request))
    return ApiResponse.ok(AcknowledgedResult(), "Password updated. Sign in with your new password.")


# -----------------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------------


@router.get(
    "/sessions",
    response_model=PaginatedResponse[SessionRead],
    summary="List the current user's active sessions",
)
async def list_sessions(
    session: DbSession, principal: CurrentUser, params: PageDep
) -> PaginatedResponse[SessionRead]:
    items, pagination = await AuthService(session).list_sessions(principal, params)
    return PaginatedResponse[SessionRead](data=items, pagination=pagination)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    response_model=ApiResponse[AcknowledgedResult],
    summary="Revoke one of the current user's sessions",
)
async def revoke_session(
    session: DbSession, principal: CurrentUser, session_id: uuid.UUID
) -> ApiResponse[AcknowledgedResult]:
    """``session_id`` is the row id from ``GET /auth/sessions``.

    A session belonging to another user answers 404, not 403: confirming that
    an id exists elsewhere is itself a disclosure.
    """
    await AuthService(session).revoke_session(principal, session_id)
    return ApiResponse.ok(AcknowledgedResult(), "Session revoked")


__all__ = ["router"]
