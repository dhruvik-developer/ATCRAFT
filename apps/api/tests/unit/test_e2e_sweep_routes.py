"""Browser sweeps open routes the application actually serves.

``theme-sweep.mjs`` walked ``/inventory/balances``, ``/products/items`` and
``/finance/receivables``. Those are API paths. The screens are ``/inventory``,
``/items`` and ``/finance``, so the sweep measured the 404 page and reported
that the theme was fine. ``permission-sweep.mjs`` omitted six screens, so a
gate added on one of them would not be exercised.

``/change-password`` is absent on purpose: once the temporary password has been
replaced the page redirects to the dashboard, so a signed-in sweep never sees it.
"""

from __future__ import annotations

import re

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

SWEEPS = REPO_ROOT / "tests" / "e2e" / "scripts"
WEB = REPO_ROOT / "apps" / "web" / "src"

# Reached only while a temporary password is still in force, and it redirects
# home the moment that is no longer true.
NOT_A_SIGNED_IN_SCREEN = frozenset({"/change-password"})


def _array_body(text: str, const: str) -> str:
    """The inside of ``const NAME = [ ... ]``.

    A nested ``['/route', ...]`` contains a ``]`` of its own, so the array ends
    at a closing bracket on its own line, not at the first one.
    """
    match = re.search(rf"const {const} = \[([\s\S]*?)\n\]", text)
    assert match, const
    return match.group(1)


def _static_screens() -> set[str]:
    text = (WEB / "app" / "route-loaders.ts").read_text(encoding="utf-8")
    routes = set(re.findall(r"'(/[^']*)':", text))
    return {route for route in routes if ":" not in route} - NOT_A_SIGNED_IN_SCREEN


def _route_permissions() -> dict[str, set[str]]:
    router = (WEB / "app" / "router.tsx").read_text(encoding="utf-8")
    catalogue = (WEB / "lib" / "permissions.ts").read_text(encoding="utf-8")
    names = dict(re.findall(r"\b([A-Z][A-Z0-9_]*): '([^']+)'", catalogue))
    found: dict[str, set[str]] = {}
    pairs = re.findall(r"path: '([^']+)'[\s\S]*?permissions: \[([^\]]*)\]", router)
    for path, body in pairs:
        if ":" in path:
            continue
        keys = re.findall(r"P\.([A-Z][A-Z0-9_]*)", body)
        found[path] = {names[key] for key in keys}
    return found


def test_signed_in_sweeps_open_every_static_screen() -> None:
    screens = _static_screens()
    assert len(screens) >= 40
    # Browser and responsive entry points share the same populated-route audit.
    # Reintroducing private route arrays lets new screens silently escape it.
    for script in ("browser-sweep.mjs", "responsive-sweep.mjs"):
        source = (SWEEPS / script).read_text(encoding="utf-8")
        assert "await import('./ui-readiness-sweep.mjs')" in source
        assert "STATIC_ROUTES" not in source
    readiness = (SWEEPS / "ui-readiness-sweep.mjs").read_text(encoding="utf-8")
    assert "const registered = registeredScreens" in readiness
    assert "for (const route of registered)" in readiness
    accessibility = (SWEEPS / "a11y-sweep.mjs").read_text(encoding="utf-8")
    assert "const ROUTES = staticScreenRoutes(registeredScreens)" in accessibility
    shared = (SWEEPS.parent / "lib" / "screen-registry.mjs").read_text(encoding="utf-8")
    assert "apps/web/src/app/route-loaders.ts" in shared
    assert "!route.includes(':') && route !== '/change-password'" in shared
    # Actual parsing, portal inclusion and duplicate/empty catalogue failures
    # are exercised by lib/screen-registry.test.mjs in handbook:check.


def test_theme_sweep_uses_application_routes() -> None:
    text = (SWEEPS / "theme-sweep.mjs").read_text(encoding="utf-8")
    paths = re.findall(r"\['(/[^']*)'", _array_body(text, "SCREENS"))
    assert len(paths) >= 5
    unknown = sorted(set(paths) - _static_screens())
    assert unknown == [], f"theme sweep opens routes that do not exist: {unknown}"
    assert "readyAdminToken" in text


def test_permission_sweep_names_a_real_gate_for_every_screen() -> None:
    text = (SWEEPS / "permission-sweep.mjs").read_text(encoding="utf-8")
    declared = {
        route: None if permission == "null" else permission.strip("'")
        for route, permission in re.findall(
            r"\['(/[^']*)',\s*(null|'[^']*')\]",
            _array_body(text, "ROUTES"),
        )
    }
    assert len(declared) >= 40
    problems: list[str] = []
    for path, permissions in _route_permissions().items():
        if path not in declared:
            problems.append(f"{path} is not in the sweep")
            continue
        named = declared[path]
        if not permissions:
            if named is not None:
                problems.append(f"{path} is ungated but the sweep requires {named}")
            continue
        if named not in permissions:
            problems.append(f"{path} sweep says {named}, route allows {sorted(permissions)}")
    assert problems == []
