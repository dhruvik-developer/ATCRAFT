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


#: What the per-address login budget has to survive: a shift change, where a
#: plant's worth of people sign in within one window and everybody shares one
#: NAT address. Some sign in twice - a mistyped password, a second device.
SHIFT_CHANGE_USERS = 50
SIGN_INS_PER_USER = 2


def test_the_default_survives_a_shift_change_behind_one_nat_address() -> None:
    """The default has to fit the deployment this ERP actually has.

    It was 4/min, or 20 attempts per five-minute window, which a plant floor
    exhausts before half its people have signed in - and the twenty-first person
    is refused *before* their password is checked, which from the floor is
    indistinguishable from the ERP being down.

    Raising it does not weaken the defence that matters: credential stuffing is
    stopped per account by LOGIN_PER_EMAIL and the account lockout, both fixed
    security constants (see the tests below). This budget bounds *spraying* many
    accounts from one address, which it still does.
    """
    default = type(settings).model_fields["rate_limit_auth_per_minute"].default
    per_window = default * (LOGIN_WINDOW_SECONDS // 60)

    assert per_window >= SHIFT_CHANGE_USERS * SIGN_INS_PER_USER, (
        f"{per_window} sign-ins per window cannot carry {SHIFT_CHANGE_USERS} people "
        "arriving at once behind one address"
    )


def test_the_per_address_budget_stays_looser_than_the_per_account_one() -> None:
    """Otherwise the per-account rule can never be the thing that fires.

    The two exist to catch different attacks - one account guessed repeatedly
    against many accounts tried once each - and the address budget sitting below
    the account budget would collapse them into one, silently.
    """
    _, per_account, _ = RateLimitPolicy.LOGIN_PER_EMAIL
    _, per_address, _ = RateLimitPolicy.LOGIN_PER_IP

    assert per_address > per_account


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
