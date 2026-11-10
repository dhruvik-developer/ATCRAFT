"""Every route authorises its caller, or is public on purpose and says why.

All but twenty of the API's routes depend on ``require_permission``. That was a
property of review; this file makes it a property of the build. A new route
with no permission dependency fails here until it is gated, or added to one of
the allowlists below together with the reason it may be reached without one.

The HTTP half drives the real application and its exception handlers: every
non-public route is called anonymously and must answer 401, and every gated
route is called by a signed-in principal holding no permissions and must answer
403, both in the shared error envelope. The database is replaced by a session
that records any use, which proves a second property on the way: no route
reads or writes anything before it has decided the caller may. Nothing here
needs PostgreSQL, so it runs in the unit suite and therefore in the pre-push
hook.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from httpx import ASGITransport, AsyncClient

from src.core.config import settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal, require_superuser
from src.core.errors import ErrorCode
from tests.route_support import (
    MountedRoute,
    UntouchableSession,
    assert_matches_contract,
    module_routes,
    mounted_application,
    route_url,
)

pytestmark = pytest.mark.unit

Operation = tuple[str, str]

#: Reached with no principal at all. Each one authenticates by other means,
#: named beside it.
_WEBHOOK = ("POST", "/integrations/courier/webhook/{token}")
_RESET_REQUEST = ("POST", "/auth/password-reset/request")
PUBLIC_ROUTES: dict[Operation, str] = {
    ("POST", "/auth/login"): "the submitted credentials are the check",
    ("POST", "/auth/refresh"): "the HttpOnly refresh cookie and its CSRF token",
    ("POST", "/auth/mfa/verify"): "the short-lived MFA challenge token of a login",
    _RESET_REQUEST: "serves a user who cannot sign in; non-enumerating and rate limited",
    ("POST", "/auth/password-reset/confirm"): "the emailed single-use reset token",
    _WEBHOOK: "the courier account's path token plus the provider's payload signature",
}

#: Any signed-in user, acting only on their own account and sessions.
SELF_SERVICE_ROUTES: frozenset[Operation] = frozenset(
    {
        ("POST", "/auth/logout"),
        ("POST", "/auth/logout-all"),
        ("GET", "/auth/me"),
        ("PUT", "/auth/me/phone"),
        ("POST", "/auth/me/phone/verification"),
        ("POST", "/auth/me/phone/verification/confirm"),
        ("POST", "/auth/me/messaging-consent"),
        ("POST", "/auth/mfa/setup"),
        ("POST", "/auth/mfa/confirm"),
        ("POST", "/auth/mfa/disable"),
        ("POST", "/auth/change-password"),
        ("GET", "/auth/sessions"),
        ("DELETE", "/auth/sessions/{session_id}"),
    }
)

#: Gated on the platform-administrator flag instead of a tenant permission.
SUPERUSER_ROUTES: dict[Operation, str] = {
    (
        "POST",
        "/organization/financial-years/{year_id}/reopen",
    ): "reopening a closed financial year is a platform-administrator action",
}


def _dependency_calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    pending = list(dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is not None:
            yield dependency.call
        pending.extend(dependency.dependencies)


def _is_permission_gate(call: Callable[..., Any]) -> bool:
    # require_permission(...) returns its inner ``_dependency`` closure.
    return getattr(call, "__qualname__", "").startswith("require_permission.")


def _shape(mounted: MountedRoute) -> tuple[bool, bool, bool]:
    calls = list(_dependency_calls(mounted.route.dependant))
    return (
        any(_is_permission_gate(call) for call in calls),
        any(call is require_superuser for call in calls),
        any(call is get_current_principal for call in calls),
    )


def _operations() -> list[tuple[Operation, MountedRoute]]:
    return [(operation, mounted) for mounted in module_routes() for operation in mounted.operations]


def test_the_inventory_matches_the_contract() -> None:
    assert_matches_contract(module_routes())


def test_every_route_is_gated_or_public_on_purpose() -> None:
    ungated = []
    for operation, mounted in _operations():
        permission, superuser, _ = _shape(mounted)
        if permission or superuser:
            continue
        if operation in PUBLIC_ROUTES or operation in SELF_SERVICE_ROUTES:
            continue
        ungated.append(operation)
    assert not ungated, (
        "Routes with no permission dependency. Gate each with "
        "require_permission(Permission.X), or - only if it genuinely must be "
        f"reachable without one - add it to an allowlist here with the reason: {ungated}"
    )


def test_each_allowlist_entry_is_a_real_route_of_the_shape_it_claims() -> None:
    by_operation = dict(_operations())
    for operation in PUBLIC_ROUTES:
        assert operation in by_operation, f"stale public entry {operation}"
        permission, superuser, principal = _shape(by_operation[operation])
        assert not (permission or superuser or principal), (
            f"{operation} is listed as public but authenticates a principal; move it"
        )
    for operation in SELF_SERVICE_ROUTES:
        assert operation in by_operation, f"stale self-service entry {operation}"
        permission, superuser, principal = _shape(by_operation[operation])
        assert principal and not permission and not superuser, (
            f"{operation} is listed as self-service but is not a principal-only route"
        )
    for operation in SUPERUSER_ROUTES:
        assert operation in by_operation, f"stale superuser entry {operation}"
        assert _shape(by_operation[operation])[1], f"{operation} no longer requires a superuser"


# -----------------------------------------------------------------------------
# HTTP: refusals through the real application
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return mounted_application()


@pytest.fixture
def touched() -> list[str]:
    return []


@pytest.fixture
async def client(
    app: FastAPI, touched: list[str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)

    async def _session() -> AsyncIterator[UntouchableSession]:
        yield UntouchableSession(touched)

    app.dependency_overrides[get_db] = _session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


def _cases(predicate: Callable[[Operation, MountedRoute], bool]) -> list[Any]:
    return [
        pytest.param(operation, mounted, id=f"{operation[0]} {operation[1]}")
        for operation, mounted in _operations()
        if predicate(operation, mounted)
    ]


def _assert_refused(response: Any, status: int, codes: set[str], touched: list[str]) -> None:
    assert not touched, f"the database was used before the caller was authorised: {touched}"
    assert response.status_code == status, response.text[:300]
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] in codes, body["error"]
    assert "Traceback" not in response.text and "sqlalchemy" not in response.text.lower()


@pytest.mark.parametrize(
    ("operation", "mounted"),
    _cases(lambda operation, _: operation not in PUBLIC_ROUTES),
)
async def test_an_anonymous_caller_is_refused_before_anything_is_read(
    operation: Operation, mounted: MountedRoute, client: AsyncClient, touched: list[str]
) -> None:
    method, _ = operation
    response = await client.request(method, settings.api_v1_prefix + route_url(mounted))
    _assert_refused(response, 401, {ErrorCode.UNAUTHENTICATED}, touched)


@pytest.mark.parametrize(
    ("operation", "mounted"),
    _cases(lambda _, mounted: _shape(mounted)[0] or _shape(mounted)[1]),
)
async def test_a_caller_without_the_permission_is_refused_before_anything_is_read(
    operation: Operation,
    mounted: MountedRoute,
    app: FastAPI,
    client: AsyncClient,
    touched: list[str],
) -> None:
    nobody = Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="no.permissions@example.com",
        full_name="No Permissions",
        is_superuser=False,
        permissions=frozenset(),
        role_codes=(),
    )
    app.dependency_overrides[get_current_principal] = lambda: nobody
    method, _ = operation
    response = await client.request(method, settings.api_v1_prefix + route_url(mounted))
    _assert_refused(
        response,
        403,
        # SCOPE_DENIED: a portal route refuses a staff principal on its account
        # type before it reaches the permission check. Still a refusal.
        {ErrorCode.PERMISSION_DENIED, ErrorCode.FORBIDDEN, ErrorCode.SCOPE_DENIED},
        touched,
    )
