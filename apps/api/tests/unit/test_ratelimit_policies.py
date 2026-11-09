"""The named rate-limit policies and what the exceeded-limit log may say.

Also a mechanical guard: a budget keyed on a client address is a budget for a
whole site, so only a ``*_PER_IP`` policy - sized for one - may be keyed on
one, and a ``*_PER_IP`` policy may be keyed on nothing else. MFA verify and
the reset request both shipped the per-account budget keyed on the address.
"""

from __future__ import annotations

import ast
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.core import ratelimit
from src.core.config import settings
from src.core.errors import RateLimitedError
from src.core.ratelimit import (
    LOGIN_WINDOW_SECONDS,
    PER_ADDRESS_AUTH_LIMIT,
    RateLimitPolicy,
    log_identity,
)
from src.core.security import create_mfa_challenge_token
from src.modules.auth import service as auth_service
from src.modules.auth.schemas import MfaVerifyRequest, PasswordResetRequestPayload
from src.modules.auth.service import AuthService
from tests.unit.test_mrp_rate_limit import FakeRedis

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Each unauthenticated auth step, as (per-address policy, per-account twin).
ADDRESS_AND_ACCOUNT_BUDGETS = [
    (RateLimitPolicy.LOGIN_PER_IP, RateLimitPolicy.LOGIN_PER_EMAIL),
    (RateLimitPolicy.MFA_VERIFY_PER_IP, RateLimitPolicy.MFA_VERIFY),
    (RateLimitPolicy.PASSWORD_RESET_PER_IP, RateLimitPolicy.PASSWORD_RESET),
]


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


# -----------------------------------------------------------------------------
# Every unauthenticated auth step: a site-sized address budget beside a strict
# account budget
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(("per_address", "_per_account"), ADDRESS_AND_ACCOUNT_BUDGETS)
def test_each_address_budget_is_the_configured_site_budget(
    per_address: tuple[str, int, int], _per_account: tuple[str, int, int]
) -> None:
    _scope, limit, window = per_address

    assert window == LOGIN_WINDOW_SECONDS
    assert limit == PER_ADDRESS_AUTH_LIMIT
    assert limit == settings.rate_limit_auth_per_minute * (LOGIN_WINDOW_SECONDS // 60)


@pytest.mark.parametrize(("per_address", "per_account"), ADDRESS_AND_ACCOUNT_BUDGETS)
def test_each_address_budget_is_looser_than_its_account_twin(
    per_address: tuple[str, int, int], per_account: tuple[str, int, int]
) -> None:
    """In count and in rate: otherwise the account budget can never be the
    thing that fires, and one person's typing refuses the whole site."""
    _, address_limit, address_window = per_address
    _, account_limit, account_window = per_account

    assert address_limit > account_limit
    assert address_limit / address_window > account_limit / account_window


def test_the_account_budgets_are_fixed_security_constants() -> None:
    assert RateLimitPolicy.MFA_VERIFY == ("mfa_verify", 5, 300)
    assert RateLimitPolicy.PASSWORD_RESET == ("pwd_reset", 3, 3600)


def test_every_policy_counts_on_its_own_scope() -> None:
    """A shared scope would let a site's sign-ins spend its MFA budget."""
    scopes = [
        value[0]
        for name, value in vars(RateLimitPolicy).items()
        if name.isupper() and isinstance(value, tuple)
    ]

    assert len(scopes) == len(set(scopes))


class _StopError(Exception):
    """Raised by the recording limiter once the budgets are taken."""


def _recording_limiter(
    monkeypatch: pytest.MonkeyPatch, *, stop_after: int
) -> list[tuple[tuple[str, int, int], str]]:
    taken: list[tuple[tuple[str, int, int], str]] = []

    async def enforce(policy: tuple[str, int, int], identifier: str) -> None:
        taken.append((policy, identifier))
        if len(taken) == stop_after:
            raise _StopError

    monkeypatch.setattr(auth_service, "enforce_policy", enforce)
    return taken


async def test_an_mfa_code_spends_the_account_budget_and_the_site_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = _recording_limiter(monkeypatch, stop_after=2)
    subject = uuid.uuid4()
    token, _ = create_mfa_challenge_token(subject, uuid.uuid4())

    with pytest.raises(_StopError):
        await AuthService(object()).verify_mfa(  # type: ignore[arg-type]
            MfaVerifyRequest(challenge_token=token, code="123456"), ip_address="203.0.113.9"
        )

    assert taken == [
        (RateLimitPolicy.MFA_VERIFY, str(subject)),
        (RateLimitPolicy.MFA_VERIFY_PER_IP, "203.0.113.9"),
    ]


async def test_a_reset_request_spends_the_mailbox_budget_and_the_site_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = _recording_limiter(monkeypatch, stop_after=2)

    with pytest.raises(_StopError):
        await AuthService(object()).request_password_reset(  # type: ignore[arg-type]
            PasswordResetRequestPayload(email="person@example.com"), ip_address="203.0.113.9"
        )

    assert taken == [
        (RateLimitPolicy.PASSWORD_RESET, "person@example.com"),
        (RateLimitPolicy.PASSWORD_RESET_PER_IP, "203.0.113.9"),
    ]


class LimiterPassedError(Exception):
    """The first step after the budgets raises this: the request got through."""


@pytest.fixture
def live_limiter(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """The real limiter, switched on, over the sliding-window stub."""
    stub = FakeRedis()

    async def _get_redis() -> FakeRedis:
        return stub

    monkeypatch.setattr(ratelimit, "get_redis", _get_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return stub


async def test_a_site_of_mfa_users_gets_through_and_one_account_is_still_held_to_five(
    live_limiter: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reached(*_args: Any, **_kwargs: Any) -> Any:
        raise LimiterPassedError

    monkeypatch.setattr(auth_service, "transaction", reached)
    company_id = uuid.uuid4()

    async def code_from(subject: uuid.UUID, address: str) -> None:
        token, _ = create_mfa_challenge_token(subject, company_id)
        await AuthService(object()).verify_mfa(  # type: ignore[arg-type]
            MfaVerifyRequest(challenge_token=token, code="123456"), ip_address=address
        )

    _, per_account, _ = RateLimitPolicy.MFA_VERIFY
    # One more person behind the plant's NAT than one account may try codes.
    for _ in range(per_account + 1):
        with pytest.raises(LimiterPassedError):
            await code_from(uuid.uuid4(), "203.0.113.9")

    target = uuid.uuid4()
    for n in range(per_account):
        with pytest.raises(LimiterPassedError):
            await code_from(target, f"198.51.100.{n}")
    with pytest.raises(RateLimitedError):
        await code_from(target, "198.51.100.200")


async def test_a_site_of_reset_requests_gets_through_and_one_mailbox_is_still_held_to_three(
    live_limiter: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reached(*_args: Any, **_kwargs: Any) -> Any:
        raise LimiterPassedError

    monkeypatch.setattr(auth_service, "find_accounts_by_email", reached)

    async def reset_for(email: str, address: str) -> None:
        await AuthService(object()).request_password_reset(  # type: ignore[arg-type]
            PasswordResetRequestPayload(email=email), ip_address=address
        )

    _, per_mailbox, _ = RateLimitPolicy.PASSWORD_RESET
    for n in range(per_mailbox + 1):
        with pytest.raises(LimiterPassedError):
            await reset_for(f"person-{n}@example.com", "203.0.113.9")

    for n in range(per_mailbox):
        with pytest.raises(LimiterPassedError):
            await reset_for("target@example.com", f"198.51.100.{n}")
    with pytest.raises(RateLimitedError):
        await reset_for("target@example.com", "198.51.100.200")


# -----------------------------------------------------------------------------
# Guard: an address-keyed budget is a *_PER_IP policy, and only that
# -----------------------------------------------------------------------------

#: A name that holds a client address: ``ip``, ``ip_address``, ``client_ip``.
_ADDRESS_NAME = re.compile(r"(?:^|_)ip(?:_|$)")


def _keyed_on_an_address(identifier: ast.expr) -> bool:
    for node in ast.walk(identifier):
        if isinstance(node, ast.Name) and _ADDRESS_NAME.search(node.id):
            return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("ip:")
        ):
            return True
    return False


def _policy_name(policy: ast.expr) -> str:
    return policy.attr if isinstance(policy, ast.Attribute) else ast.unparse(policy)


def enforce_policy_calls(source: str) -> list[tuple[int, str, ast.expr]]:
    """Every ``enforce_policy(policy, identifier)`` call, as (line, policy, identifier)."""
    calls: list[tuple[int, str, ast.expr]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func: Any = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "enforce_policy":
            calls.append((node.lineno, _policy_name(node.args[0]), node.args[1]))
    return sorted(calls, key=lambda call: call[0])


def address_budget_violations(source: str) -> list[str]:
    found: list[str] = []
    for line, policy, identifier in enforce_policy_calls(source):
        per_address = policy.endswith("_PER_IP")
        if _keyed_on_an_address(identifier) != per_address:
            wanted = "an address" if per_address else "a *_PER_IP policy"
            found.append(
                f"line {line}: {policy} keyed on {ast.unparse(identifier)}; needs {wanted}"
            )
    return found


def _source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_scan_finds_the_budgets_it_polices() -> None:
    calls = [
        call
        for path in _source_files()
        for call in enforce_policy_calls(path.read_text(encoding="utf-8"))
    ]
    assert len(calls) >= 10, f"enforce_policy discovery found too few calls: {len(calls)}"


def test_only_a_per_address_policy_is_keyed_on_an_address() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): found
        for path in _source_files()
        if (found := address_budget_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "A budget keyed on a client address is shared by everyone behind that "
        "address - a whole plant behind its NAT. Key it with a *_PER_IP policy "
        f"sized for a site, and key a per-account policy on the account: {offenders}"
    )


def test_the_address_rule_rejects_an_account_budget_on_an_address() -> None:
    source = (
        "async def verify(challenge, ip_address):\n"
        "    await enforce_policy(RateLimitPolicy.MFA_VERIFY, str(challenge.subject))\n"
        "    await enforce_policy(RateLimitPolicy.MFA_VERIFY, f'ip:{ip_address}')\n"
        "    await enforce_policy(RateLimitPolicy.LOGIN_PER_IP, payload.email)\n"
        "    await enforce_policy(RateLimitPolicy.LOGIN_PER_IP, ip_address or 'unknown')\n"
    )

    assert address_budget_violations(source) == [
        "line 3: MFA_VERIFY keyed on f'ip:{ip_address}'; needs a *_PER_IP policy",
        "line 4: LOGIN_PER_IP keyed on payload.email; needs an address",
    ]
