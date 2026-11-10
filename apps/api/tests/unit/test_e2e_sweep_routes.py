"""Browser sweeps open routes the application actually serves.

``theme-sweep.mjs`` walked ``/inventory/balances``, ``/products/items`` and
``/finance/receivables``. Those are API paths. The screens are ``/inventory``,
``/items`` and ``/finance``, so the sweep measured the 404 page and reported
that the theme was fine. ``permission-sweep.mjs`` omitted six screens, so a
gate added on one of them would not be exercised.

``/change-password`` is absent on purpose: once the temporary password has been
replaced the page redirects to the dashboard, so a signed-in sweep never sees it.

docs/ui-readiness.md states how many routes the registry defines and how many
static screens the accessibility sweep derives from it. Those figures went
stale - 79 and 65 while the registry held 87 and 72 - and so implied browser
evidence for eight routes that had none; they are read from the registry here.
"""

from __future__ import annotations

import re

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

SWEEPS = REPO_ROOT / "tests" / "e2e" / "scripts"
WEB = REPO_ROOT / "apps" / "web" / "src"
READINESS_DOC = REPO_ROOT / "docs" / "ui-readiness.md"

#: The two present-tense counts ui-readiness.md states, each read across a line wrap.
ROUTE_COUNT_SENTENCE = r"defines\s+(\d+)\s+application\s+routes"
STATIC_SCREEN_COUNT_SENTENCE = r"derives\s+its\s+(\d+)\s+static\s+application\s+screens"

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


def _registered_routes() -> set[str]:
    text = (WEB / "app" / "route-loaders.ts").read_text(encoding="utf-8")
    return set(re.findall(r"'(/[^']*)':", text))


def _static_screens() -> set[str]:
    routes = _registered_routes()
    return {route for route in routes if ":" not in route} - NOT_A_SIGNED_IN_SCREEN


def readiness_count_faults(document: str, *, routes: int, static_screens: int) -> list[str]:
    """Where ui-readiness.md's route and static-screen counts disagree with the registry."""
    faults: list[str] = []
    for sentence, count in (
        (ROUTE_COUNT_SENTENCE, routes),
        (STATIC_SCREEN_COUNT_SENTENCE, static_screens),
    ):
        stated = [int(number) for number in re.findall(sentence, document)]
        if not stated:
            faults.append(f"no sentence matches {sentence!r}")
        faults += [
            f"says {number} where the registry gives {count}"
            for number in stated
            if number != count
        ]
    return faults


def _route_permissions() -> dict[str, set[str]]:
    router = (WEB / "app" / "router.tsx").read_text(encoding="utf-8")
    catalogue = (WEB / "lib" / "permissions.ts").read_text(encoding="utf-8")
    names = dict(re.findall(r"\b([A-Z][A-Z0-9_]*): '([^']+)'", catalogue))
    found: dict[str, set[str]] = {}
    pairs = re.findall(
        r"path: '([^']+)'[\s\S]*?permissions:\s*(?:\[([^\]]*)\]|([A-Z][A-Z0-9_]*))",
        router,
    )
    for path, body, permission_set in pairs:
        if ":" in path:
            continue
        if permission_set == "APPROVAL_PERMISSIONS":
            found[path] = {
                permission
                for permission in names.values()
                if permission.endswith(".approve") or permission.endswith("_approve")
            }
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
    # Optional route filters select from the complete registry; an unfiltered
    # run must retain every screen, including the populated portal details.
    assert "process.env.READINESS_ROUTES?.split(',') ?? []" in readiness
    assert "const selectedScreens = registered.filter(" in readiness
    assert "!filters.length || filters.some(" in readiness
    assert "for (const route of selectedScreens)" in readiness
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


def test_the_readiness_page_states_the_registrys_counts() -> None:
    faults = readiness_count_faults(
        READINESS_DOC.read_text(encoding="utf-8"),
        routes=len(_registered_routes()),
        static_screens=len(_static_screens()),
    )
    assert faults == [], (
        "docs/ui-readiness.md must state the route registry as it is - and name any route "
        "that has no recorded browser evidence yet: " + "; ".join(faults)
    )


def test_the_count_rule_finds_stale_and_missing_counts() -> None:
    stale = (
        "The current loader registry defines 79 application routes, including the portals.\n"
        "The accessibility sweep derives its 65 static application\nscreens from it.\n"
    )

    assert readiness_count_faults(stale, routes=87, static_screens=72) == [
        "says 79 where the registry gives 87",
        "says 65 where the registry gives 72",
    ]
    assert readiness_count_faults(stale, routes=79, static_screens=65) == []
    assert readiness_count_faults("No counts at all.", routes=1, static_screens=1) == [
        f"no sentence matches {ROUTE_COUNT_SENTENCE!r}",
        f"no sentence matches {STATIC_SCREEN_COUNT_SENTENCE!r}",
    ]
