"""The shipped ``MFA_REQUIRED_ROLES`` names roles that exist, everywhere it ships.

Enforcement (``get_current_principal`` in ``core/deps.py``) intersects the
configured codes with the codes of the roles a user holds, so a code that no
role carries enforces nothing - and nothing says so. The env templates and the
Compose fallback shipped ``SUPER_ADMIN,COMPANY_ADMIN,FINANCE_MANAGER`` while the
role presets define ``ADMIN`` and ``ACCOUNTS``: company administrators and
finance users, the population docs/assumptions.md §MFA-1 exists to protect,
signed in with a password alone. Commissioning could not notice, because the
seeded first administrator holds ``SUPER_ADMIN``, the one code that was right,
and every enforcement test supplied a value of its own.

So this reads the value from every file that ships it, requires each to name
only role preset codes and all of them to agree, and then drives the real
authentication dependency with the template's value to prove it refuses the
principals it is meant to and nobody else.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from src.core import deps
from src.core.config import REPO_ROOT, settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.errors import ErrorCode, MfaRequiredError
from src.core.permissions import ROLE_PRESETS
from src.core.security import TokenPayload, create_access_token
from tests.route_support import UntouchableSession, mounted_application

pytestmark = pytest.mark.unit

_ENV_LINE = re.compile(r"^MFA_REQUIRED_ROLES=(?P<value>[^\s#]*)", re.MULTILINE)
_COMPOSE_FALLBACK = re.compile(r"\$\{MFA_REQUIRED_ROLES:-(?P<value>[^}]*)\}")
_RUNBOOK_ROW = re.compile(r"^\|\s*`MFA_REQUIRED_ROLES`\s*\|\s*`(?P<value>[^`]*)`", re.MULTILINE)

#: Every file that ships the default, by repository-relative path, with the
#: pattern that finds its value there.
SOURCES: dict[str, re.Pattern[str]] = {
    ".env.example": _ENV_LINE,
    "deploy/main.env.example": _ENV_LINE,
    "deploy/dev.env.example": _ENV_LINE,
    "docker-compose.yml": _COMPOSE_FALLBACK,
    "ONE-BOX-RUNBOOK.md": _RUNBOOK_ROW,
}

#: What shipped before this guard existed, kept to prove the guard refuses it.
_SHIPPED_BEFORE = "SUPER_ADMIN,COMPANY_ADMIN,FINANCE_MANAGER"


def _value_in(text: str, pattern: re.Pattern[str]) -> str:
    """The one value ``pattern`` finds in ``text``; none or several is a stale guard."""
    values = [match.group("value") for match in pattern.finditer(text)]
    assert len(values) == 1, f"expected exactly one MFA_REQUIRED_ROLES value, found {values}"
    return values[0]


def shipped_default(source: str) -> str:
    return _value_in((REPO_ROOT / source).read_text(encoding="utf-8"), SOURCES[source])


def role_codes(value: str) -> frozenset[str]:
    """The codes a value names, normalised the way enforcement compares them."""
    return frozenset(code.strip().casefold() for code in value.split(",") if code.strip())


def unknown_role_codes(value: str) -> list[str]:
    """The codes in ``value`` no role preset defines; each one enforces nothing."""
    known = {code.casefold() for code in ROLE_PRESETS}
    named = (code.strip() for code in value.split(","))
    return [code for code in named if code and code.casefold() not in known]


# -----------------------------------------------------------------------------
# Every shipped source
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_every_shipped_default_names_only_role_preset_codes(source: str) -> None:
    value = shipped_default(source)

    assert role_codes(value), f"{source} ships an empty MFA_REQUIRED_ROLES"
    assert unknown_role_codes(value) == [], (
        f"{source} names role codes no preset in core/permissions.py defines, so "
        f"they enforce nothing: {unknown_role_codes(value)}"
    )


def test_every_source_ships_the_same_default() -> None:
    shipped = {source: role_codes(shipped_default(source)) for source in SOURCES}

    assert len(set(shipped.values())) == 1, shipped


def test_the_guard_refuses_the_codes_that_once_shipped() -> None:
    """Fed the old value in each source's own shape, the guard names both codes."""
    old_sources = (
        (f"# roles\nMFA_REQUIRED_ROLES={_SHIPPED_BEFORE}  # note\n", _ENV_LINE),
        (f"  MFA_REQUIRED_ROLES: ${{MFA_REQUIRED_ROLES:-{_SHIPPED_BEFORE}}}\n", _COMPOSE_FALLBACK),
        (f"| `MFA_REQUIRED_ROLES`   | `{_SHIPPED_BEFORE}` | Comma-separated. |\n", _RUNBOOK_ROW),
    )
    for text, pattern in old_sources:
        assert unknown_role_codes(_value_in(text, pattern)) == ["COMPANY_ADMIN", "FINANCE_MANAGER"]

    # Matching is case-insensitive and tolerates spacing, as enforcement does.
    assert unknown_role_codes(" super_admin , Accounts,admin ,") == []


# -----------------------------------------------------------------------------
# The template's value, enforced by the real dependency
# -----------------------------------------------------------------------------


@pytest.fixture
def template_policy(monkeypatch: pytest.MonkeyPatch) -> str:
    value = shipped_default(".env.example")
    monkeypatch.setattr(settings, "mfa_required_roles", value)
    return value


def _principal(role_code: str, *, mfa_enabled: bool = False) -> Principal:
    granted = ROLE_PRESETS[role_code]["permissions"]
    assert isinstance(granted, frozenset)
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email=f"{role_code.lower()}@example.com",
        full_name=role_code.title(),
        is_superuser=False,
        permissions=frozenset(str(permission) for permission in granted),
        mfa_enabled=mfa_enabled,
        role_codes=(role_code,),
    )


def _sign_in(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> str:
    """A real access token whose session is live and whose user is ``principal``.

    Only the two database reads are replaced; decoding the token and the MFA
    decision run as they do for a request.
    """

    async def live(_session: object, _payload: TokenPayload) -> bool:
        return True

    async def load(_session: object, _payload: TokenPayload) -> Principal:
        return principal

    monkeypatch.setattr(deps, "_session_is_live", live)
    monkeypatch.setattr(deps, "_load_principal", load)
    token, _ = create_access_token(principal.user_id, principal.company_id)
    return token


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    app = mounted_application()
    touched: list[str] = []

    async def _session() -> AsyncIterator[UntouchableSession]:
        yield UntouchableSession(touched)

    app.dependency_overrides[get_db] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http, touched
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("role_code", ["SUPER_ADMIN", "ADMIN", "ACCOUNTS"])
async def test_the_shipped_default_refuses_an_unenrolled_privileged_role(
    role_code: str,
    template_policy: str,
    client: tuple[AsyncClient, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, touched = client
    token = _sign_in(monkeypatch, _principal(role_code))

    response = await http.get(
        f"{settings.api_v1_prefix}/products/items",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403, response.text[:300]
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == ErrorCode.MFA_REQUIRED
    assert touched == [], "the route read the database before the MFA refusal"


async def test_the_shipped_default_lets_an_unenrolled_admin_reach_enrolment(
    template_policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = _principal("ADMIN")
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=_sign_in(monkeypatch, principal)
    )
    prefix = settings.api_v1_prefix
    touched: list[str] = []
    session = UntouchableSession(touched)

    with pytest.raises(MfaRequiredError):
        await get_current_principal(_request(f"{prefix}/products/items"), session, credentials)
    for path in (f"{prefix}/auth/mfa/setup", f"{prefix}/auth/mfa/confirm", f"{prefix}/auth/me"):
        reached = await get_current_principal(_request(path), session, credentials)
        assert reached is principal
    assert touched == []


@pytest.mark.parametrize(
    "principal",
    [
        pytest.param(_principal("STORE_KEEPER"), id="unlisted-role"),
        pytest.param(_principal("ADMIN", mfa_enabled=True), id="enrolled-admin"),
        pytest.param(_principal("ACCOUNTS", mfa_enabled=True), id="enrolled-accounts"),
    ],
)
async def test_the_shipped_default_leaves_unlisted_and_enrolled_principals_alone(
    principal: Principal, template_policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=_sign_in(monkeypatch, principal)
    )

    touched: list[str] = []

    reached = await get_current_principal(
        _request(f"{settings.api_v1_prefix}/products/items"),
        UntouchableSession(touched),
        credentials,
    )

    assert reached is principal
    assert touched == []
