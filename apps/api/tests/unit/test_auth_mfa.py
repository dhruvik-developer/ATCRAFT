"""Pure cryptographic and route-policy checks for MFA/auth remediation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pyotp
import pytest

from src.core.deps import password_change_route_allowed
from src.core.security import (
    create_mfa_challenge_token,
    decode_token,
    decrypt_password_reset_delivery_token,
    decrypt_totp_secret,
    encrypt_password_reset_delivery_token,
    encrypt_totp_secret,
    generate_totp_secret,
    matching_totp_step,
    totp_provisioning_uri,
)

pytestmark = pytest.mark.unit


def test_totp_secret_is_encrypted_at_rest_and_round_trips() -> None:
    secret = generate_totp_secret()
    encrypted = encrypt_totp_secret(secret)

    assert len(secret) == 32
    assert encrypted != secret
    assert secret not in encrypted
    assert decrypt_totp_secret(encrypted) == secret


def test_jwt_rotation_does_not_invalidate_encrypted_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config import settings

    secret = generate_totp_secret()
    reset_token = "live-reset-credential"
    encrypted_mfa = encrypt_totp_secret(secret)
    encrypted_reset = encrypt_password_reset_delivery_token(reset_token)

    monkeypatch.setattr(settings, "jwt_secret", "rotated-jwt-signing-secret" * 3)

    assert decrypt_totp_secret(encrypted_mfa) == secret
    assert decrypt_password_reset_delivery_token(encrypted_reset) == reset_token


def test_legacy_base32_seed_remains_readable_during_rolling_upgrade() -> None:
    secret = generate_totp_secret()
    assert decrypt_totp_secret(secret) == secret


def test_matching_totp_returns_the_rfc_counter() -> None:
    secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    moment = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    expected_step = int(moment.timestamp()) // 30
    code = pyotp.TOTP(secret).at(moment)

    assert matching_totp_step(secret, code, at=moment) == expected_step
    assert matching_totp_step(secret, "000000", at=moment) is None
    assert matching_totp_step(secret, "not-six", at=moment) is None


def test_mfa_challenge_is_short_lived_and_password_authenticated() -> None:
    user_id = uuid.uuid4()
    company_id = uuid.uuid4()
    token, _ = create_mfa_challenge_token(user_id, company_id, device_label="Shop floor tablet")

    payload = decode_token(token, expected_type="mfa")
    assert payload.subject == user_id
    assert payload.company_id == company_id
    assert payload.expires_at - payload.issued_at <= 5 * 60
    assert payload.raw is not None
    assert payload.raw["amr"] == ["pwd"]
    assert payload.raw["device"] == "Shop floor tablet"


def test_provisioning_uri_contains_no_password_or_internal_key() -> None:
    secret = generate_totp_secret()
    uri = totp_provisioning_uri(secret, account_name="operator@example.test")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "operator%40example.test" in uri


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/me",
        "/api/v1/auth/change-password",
        "/api/v1/auth/logout",
        "/api/v1/auth/logout-all/",
    ],
)
def test_temporary_password_allows_only_remediation_routes(path: str) -> None:
    assert password_change_route_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/products/items",
        "/api/v1/auth/sessions",
        "/api/v1/auth/mfa/setup",
        "/metrics",
    ],
)
def test_temporary_password_blocks_every_other_route(path: str) -> None:
    assert not password_change_route_allowed(path)
