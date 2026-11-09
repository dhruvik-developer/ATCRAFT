"""A portal account reaches its portal and its own account, never staff work.

``vendor_route_allowed`` and ``dealer_route_allowed`` admit whole path roots:
each portal's own, ``/notifications`` and the self-service ``/auth`` routes.
``/notifications`` is shared with staff - the bell, its preferences and push
registration belong to everyone, but alert rules, messaging templates, provider
status and the delivery history are company administration - and those routes
were gated only on ``settings.view`` / ``settings.manage``. A portal role can
hold any permission, so one mis-built role or one direct grant let a vendor
rewrite the company's WhatsApp templates.

The rule this file enforces: every route a portal allow-list admits is the
portal's own, an ``/auth`` self-service route, named below as self-service, or
refuses that portal's accounts itself (``forbid_vendor_users`` /
``forbid_dealer_users``). A new staff route under a shared root fails here until
it does one of those.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.dependencies.models import Dependant

from src.core.config import settings
from src.core.deps import (
    dealer_route_allowed,
    forbid_dealer_users,
    forbid_vendor_users,
    vendor_route_allowed,
)
from tests.route_support import MountedRoute, iter_api_routes, module_routes

pytestmark = pytest.mark.unit

Operation = tuple[str, str]

#: Under a root both portals share with staff, the routes a portal account uses
#: for its own bell, preferences and devices.
PORTAL_SELF_SERVICE: frozenset[Operation] = frozenset(
    {
        ("GET", "/notifications"),
        ("GET", "/notifications/unread-count"),
        ("GET", "/notifications/channels"),
        ("GET", "/notifications/preferences"),
        ("PUT", "/notifications/preferences"),
        ("POST", "/notifications/read-all"),
        ("POST", "/notifications/{notification_id}/read"),
        ("GET", "/notifications/push/config"),
        ("GET", "/notifications/push/subscriptions"),
        ("POST", "/notifications/push/subscriptions"),
        ("DELETE", "/notifications/push/subscriptions/{subscription_id}"),
        ("POST", "/notifications/push/subscriptions/unregister"),
        ("POST", "/notifications/push/test"),
    }
)

#: (portal, its allow-list, its own root, the dependency that refuses it).
PORTALS: list[tuple[str, Callable[[str], bool], str, Callable[..., Any]]] = [
    ("vendor", vendor_route_allowed, "/vendor-portal", forbid_vendor_users),
    ("dealer", dealer_route_allowed, "/dealer-portal", forbid_dealer_users),
]


def _dependency_calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    pending = list(dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is not None:
            yield dependency.call
        pending.extend(dependency.dependencies)


def _is_own_or_self_service(path: str, own_root: str) -> bool:
    return path == own_root or path.startswith((f"{own_root}/", "/auth/"))


def portal_scope_violations(
    routes: Iterable[MountedRoute],
    *,
    allowed: Callable[[str], bool],
    own_root: str,
    refusal: Callable[..., Any],
    self_service: frozenset[Operation] = PORTAL_SELF_SERVICE,
) -> list[str]:
    """Admitted routes that are neither the portal's nor refuse its accounts."""
    prefix = settings.api_v1_prefix.rstrip("/")
    found: list[str] = []
    for mounted in routes:
        if not allowed(prefix + mounted.path) or _is_own_or_self_service(mounted.path, own_root):
            continue
        if refusal in set(_dependency_calls(mounted.route.dependant)):
            continue
        found.extend(
            f"{method} {path}"
            for method, path in mounted.operations
            if (method, path) not in self_service
        )
    return sorted(found)


@pytest.fixture(scope="module")
def routes() -> tuple[MountedRoute, ...]:
    return module_routes()


def test_the_scan_sees_the_shared_root(routes: tuple[MountedRoute, ...]) -> None:
    shared = [mounted for mounted in routes if mounted.path.startswith("/notifications")]
    assert len(shared) >= 20, f"route discovery found too few /notifications routes: {len(shared)}"


@pytest.mark.parametrize(("portal", "allowed", "own_root", "refusal"), PORTALS)
def test_a_portal_reaches_no_staff_route_under_a_shared_root(
    routes: tuple[MountedRoute, ...],
    portal: str,
    allowed: Callable[[str], bool],
    own_root: str,
    refusal: Callable[..., Any],
) -> None:
    offenders = portal_scope_violations(routes, allowed=allowed, own_root=own_root, refusal=refusal)
    assert not offenders, (
        f"The {portal} allow-list admits these routes and nothing refuses a {portal} "
        f"account on them. Add Depends({refusal.__name__}) - or, if a {portal} user "
        f"really works there on their own account, name it in PORTAL_SELF_SERVICE: {offenders}"
    )


def test_every_self_service_entry_is_a_real_route(routes: tuple[MountedRoute, ...]) -> None:
    operations = {operation for mounted in routes for operation in mounted.operations}
    assert not PORTAL_SELF_SERVICE - operations, sorted(PORTAL_SELF_SERVICE - operations)


def test_the_rule_rejects_an_admitted_staff_route_that_does_not_refuse_portals() -> None:
    router = APIRouter(prefix="/notifications")

    @router.get("/admin-panel")
    async def unguarded() -> None: ...

    @router.put("/admin-panel", dependencies=[Depends(forbid_vendor_users)])
    async def guarded() -> None: ...

    @router.get("/unread-count")
    async def bell() -> None: ...

    routes = [MountedRoute("notifications", path, route) for path, route in iter_api_routes(router)]

    assert portal_scope_violations(
        routes, allowed=vendor_route_allowed, own_root="/vendor-portal", refusal=forbid_vendor_users
    ) == ["GET /notifications/admin-panel"]
    # The dealer refusal is its own dependency: the vendor one does not count.
    assert portal_scope_violations(
        routes, allowed=dealer_route_allowed, own_root="/dealer-portal", refusal=forbid_dealer_users
    ) == ["GET /notifications/admin-panel", "PUT /notifications/admin-panel"]
