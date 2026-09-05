"""The named rate-limit policies and what the exceeded-limit log may say."""

from __future__ import annotations

import hashlib

import pytest

from src.core.config import settings
from src.core.ratelimit import LOGIN_WINDOW_SECONDS, RateLimitPolicy, log_identity

pytestmark = pytest.mark.unit


def test_the_per_address_login_budget_is_driven_by_configuration() -> None:
    """RATE_LIMIT_AUTH_PER_MINUTE was parsed and ignored; an operator raising
    it for a plant behind one NAT saw no change and got no error."""
    scope, limit, window = RateLimitPolicy.LOGIN_PER_IP

    assert scope == "login_ip"
    assert window == LOGIN_WINDOW_SECONDS
    assert limit == settings.rate_limit_auth_per_minute * (LOGIN_WINDOW_SECONDS // 60)


def test_the_default_keeps_the_shipped_budget_of_twenty_per_five_minutes() -> None:
    default = type(settings).model_fields["rate_limit_auth_per_minute"].default
    assert default * (LOGIN_WINDOW_SECONDS // 60) == 20


def test_the_per_account_login_budget_is_a_fixed_security_constant() -> None:
    assert RateLimitPolicy.LOGIN_PER_EMAIL == ("login_email", 5, LOGIN_WINDOW_SECONDS)


@pytest.mark.parametrize("scope", ["login_email", "pwd_reset"])
def test_a_typed_email_address_is_never_logged_in_clear(scope: str) -> None:
    address = "victim@example.com"

    logged = log_identity(address, scope=scope)

    assert address not in logged
    assert "@" not in logged
    assert logged == "sha256:" + hashlib.sha256(address.encode()).hexdigest()[:16]
    # Stable, so a targeted run is still visible as one identity across lines.
    assert logged == log_identity(address, scope=scope)


@pytest.mark.parametrize(
    ("scope", "identifier"),
    [
        ("login_ip", "203.0.113.9"),
        ("refresh", "0190a1b2-0000-7000-8000-000000000000"),
        ("api", "user:0190a1b2-0000-7000-8000-000000000000"),
    ],
)
def test_addresses_and_verified_subjects_are_logged_as_they_are(
    scope: str, identifier: str
) -> None:
    assert log_identity(identifier, scope=scope) == identifier
