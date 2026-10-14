"""Request and response contracts for authentication.

Three conventions here exist for security reasons rather than tidiness:

* **Nothing in a request identifies a tenant by id.** ``company_code`` is
  accepted only to disambiguate an email address that exists in more than one
  company, and it is resolved against the ``companies`` table - it never becomes
  a ``company_id`` the caller chose (section 40).
* **Passwords are bounded but not policed here.** Only length limits live on the
  schema; the real policy is :func:`src.core.security.validate_password_policy`,
  called by the service. One source of truth means a policy change cannot leave
  the edge and the service disagreeing about what is acceptable.
* **No response model carries a token hash, a password hash, or a permission
  list on the login path.** Permissions are returned by ``/auth/me`` alone, so a
  client cannot cache an authority snapshot that outlives a role change
  (section 39).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# Bounds only. A password below the policy minimum is rejected by the service
# with a field-level explanation; rejecting it here would produce a pydantic
# error that cannot say *which* rule failed.
PasswordField = Field(min_length=1, max_length=128)

#: A reset token is a 32-byte urlsafe secret; anything much shorter cannot be
#: one, and rejecting it early keeps a probing loop off the database.
ResetTokenField = Field(min_length=20, max_length=256)


def _normalise_email(value: str) -> str:
    """Lower-case and trim.

    ``users.email`` is ``CIText`` so the database already matches
    case-insensitively, but the rate-limit bucket is keyed on this string -
    without normalisation ``Bob@x.com`` and ``bob@x.com`` would each get their
    own allowance, and five tries would become ten.
    """
    return value.strip().lower()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


# -----------------------------------------------------------------------------
# Sign in
# -----------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = PasswordField
    #: Required only when the same address has accounts in several companies.
    company_code: str | None = Field(default=None, max_length=32)
    #: Shown to the user in the session list ("Chrome on the shop-floor PC").
    device_label: str | None = Field(default=None, max_length=120)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalise_email(value)

    @field_validator("company_code")
    @classmethod
    def _company_code(cls, value: str | None) -> str | None:
        cleaned = _optional_text(value)
        return cleaned.upper() if cleaned else None

    @field_validator("device_label")
    @classmethod
    def _device_label(cls, value: str | None) -> str | None:
        return _optional_text(value)


class AuthenticatedUser(BaseModel):
    """The minimum a client needs to render a shell after sign-in."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    company_id: uuid.UUID
    is_superuser: bool
    must_change_password: bool
    roles: list[str] = Field(default_factory=list)


class TokenPair(BaseModel):
    """Issued credentials.

    ``expires_in`` is seconds rather than an absolute time: the client's clock
    may be wrong, and a countdown does not care.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int
    session_id: str


class LoginResponse(TokenPair):
    """Internal login result; the route moves ``refresh_token`` to a cookie."""

    user: AuthenticatedUser


class AccessTokenResponse(BaseModel):
    """Browser-visible half of a token pair; never contains a refresh secret."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int
    session_id: str


class BrowserLoginResponse(AccessTokenResponse):
    mfa_required: Literal[False] = False
    user: AuthenticatedUser


class MfaChallengeResponse(BaseModel):
    """Password stage succeeded; no session exists until TOTP verification."""

    mfa_required: Literal[True] = True
    challenge_token: str
    expires_in: int


class MfaVerifyRequest(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=4096)
    code: str = Field(pattern=r"^[0-9]{6}$")


class LogoutResult(BaseModel):
    revoked_sessions: int


# -----------------------------------------------------------------------------
# Current principal
# -----------------------------------------------------------------------------


class MeResponse(BaseModel):
    """Who the caller is and exactly what they may do, resolved this request."""

    user_id: uuid.UUID
    company_id: uuid.UUID
    #: Shown in the sidebar on every screen. With two deployments on one box it
    #: is also how a person tells the client's environment from the test one.
    company_name: str
    email: str
    full_name: str
    is_superuser: bool
    must_change_password: bool
    mfa_enabled: bool
    last_login_at: datetime | None = None
    session_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    vendor_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    allowed_category_ids: list[uuid.UUID] = Field(default_factory=list)
    allowed_warehouse_ids: list[uuid.UUID] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Passwords
# -----------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str = PasswordField
    new_password: str = PasswordField

    @model_validator(mode="after")
    def _must_differ(self) -> ChangePasswordRequest:
        # Caught here as well as in the service: a client that let this through
        # would otherwise sign every other device out for no change at all.
        if self.current_password == self.new_password:
            raise ValueError("The new password must be different from the current one.")
        return self


class ChangePasswordResult(BaseModel):
    """``revoked_sessions`` tells the UI how many other devices were signed out."""

    revoked_sessions: int


# -----------------------------------------------------------------------------
# Multi-factor authentication
# -----------------------------------------------------------------------------


class MfaSetupRequest(BaseModel):
    current_password: str = PasswordField


class MfaSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MfaCodeRequest(BaseModel):
    code: str = Field(pattern=r"^[0-9]{6}$")


class MfaDisableRequest(MfaCodeRequest):
    current_password: str = PasswordField


class MfaChangeResult(BaseModel):
    mfa_enabled: bool
    revoked_sessions: int


class PasswordResetRequestPayload(BaseModel):
    email: EmailStr
    company_code: str | None = Field(default=None, max_length=32)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalise_email(value)

    @field_validator("company_code")
    @classmethod
    def _company_code(cls, value: str | None) -> str | None:
        cleaned = _optional_text(value)
        return cleaned.upper() if cleaned else None


class PasswordResetConfirmPayload(BaseModel):
    token: str = ResetTokenField
    new_password: str = PasswordField


class AcknowledgedResult(BaseModel):
    """Deliberately contentless.

    The password-reset endpoints answer identically whether or not the address
    belongs to an account, so there is nothing here that could differ between
    the two cases (section 38).
    """

    acknowledged: bool = True


# -----------------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------------


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    device_label: str | None = None
    #: True for the session the request itself was made with, so the UI can
    #: label it and warn before revoking it.
    is_current: bool = False


__all__ = [
    "AccessTokenResponse",
    "AcknowledgedResult",
    "AuthenticatedUser",
    "BrowserLoginResponse",
    "ChangePasswordRequest",
    "ChangePasswordResult",
    "LoginRequest",
    "LoginResponse",
    "LogoutResult",
    "MeResponse",
    "MfaChallengeResponse",
    "MfaChangeResult",
    "MfaCodeRequest",
    "MfaDisableRequest",
    "MfaSetupRequest",
    "MfaSetupResponse",
    "MfaVerifyRequest",
    "PasswordResetConfirmPayload",
    "PasswordResetRequestPayload",
    "SessionRead",
    "TokenPair",
]
