"""Edge-level contracts for the authentication schemas - no database.

The assertions that matter here are the security-relevant ones: an address is
normalised before it becomes a rate-limit key, a change-password request that
changes nothing is refused, and no login-path response model can carry a
permission list.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    PasswordResetConfirmPayload,
    PasswordResetRequestPayload,
    TokenPair,
)

pytestmark = pytest.mark.unit

VALID = "S3cure-Passphrase!2026"


def test_login_email_is_lowercased_and_trimmed() -> None:
    """Both spellings must land in the same rate-limit bucket, or five attempts
    per address becomes five per capitalisation."""
    payload = LoginRequest(email="  Plant.Head@Atcraft.CO.in ", password=VALID)
    assert payload.email == "plant.head@atcraft.co.in"


def test_password_reset_request_normalises_the_same_way() -> None:
    payload = PasswordResetRequestPayload(email="Buyer@Atcraft.co.in")
    assert payload.email == "buyer@atcraft.co.in"


def test_company_code_is_upper_cased_and_blank_becomes_none() -> None:
    assert (
        LoginRequest(email="a@b.com", password=VALID, company_code=" at-1 ").company_code == "AT-1"
    )
    assert LoginRequest(email="a@b.com", password=VALID, company_code="   ").company_code is None


def test_device_label_whitespace_is_collapsed() -> None:
    payload = LoginRequest(email="a@b.com", password=VALID, device_label="  Chrome   on   Shop PC ")
    assert payload.device_label == "Chrome on Shop PC"


def test_malformed_email_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        LoginRequest(email="not-an-address", password=VALID)


def test_empty_password_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        LoginRequest(email="a@b.com", password="")


def test_absurdly_long_password_is_rejected_before_hashing() -> None:
    """Argon2 hashes whatever it is given; an unbounded field is a cheap way to
    make every login attempt expensive."""
    with pytest.raises(PydanticValidationError):
        LoginRequest(email="a@b.com", password="x" * 129)


def test_change_password_refuses_an_unchanged_password() -> None:
    with pytest.raises(PydanticValidationError):
        ChangePasswordRequest(current_password=VALID, new_password=VALID)


def test_change_password_accepts_a_different_password() -> None:
    payload = ChangePasswordRequest(current_password=VALID, new_password="An0ther-Passphrase!")
    assert payload.new_password != payload.current_password


def test_reset_confirm_rejects_a_token_too_short_to_be_real() -> None:
    with pytest.raises(PydanticValidationError):
        PasswordResetConfirmPayload(token="abc123", new_password=VALID)


def test_reset_confirm_accepts_a_full_length_token() -> None:
    payload = PasswordResetConfirmPayload(token="t" * 43, new_password=VALID)
    assert payload.token == "t" * 43


def test_no_login_response_model_carries_permissions() -> None:
    """Permissions are resolved per request and returned by /auth/me only.

    Shipping them on the login path would let a client cache an authority
    snapshot that survives a role change (section 39).
    """
    for model in (TokenPair, LoginResponse):
        assert "permissions" not in model.model_fields
    assert "permissions" not in LoginResponse.model_fields["user"].annotation.model_fields
