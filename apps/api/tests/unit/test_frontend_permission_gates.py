"""A screen must gate an action on the right its endpoint actually enforces.

:mod:`tests.unit.test_frontend_permission_catalogue` proves the two permission
*vocabularies* agree. That is necessary and not sufficient: it cannot see a
screen that names ``P.USER_UPDATE`` on a button whose endpoint is declared
``require_permission(Permission.USER_RESET_PASSWORD)``. Both constants exist,
both spellings are real, and TypeScript is perfectly happy.

Five defects of exactly that shape have shipped in this codebase:

===========================  ==========================  =========================
Action                       Gated on                    Endpoint enforces
===========================  ==========================  =========================
Job work Approve / Close     ``job_work.issue``          ``job_work.close``
Sales order Confirm          ``sales_order.approve``     ``sales_order.update``
Users Reset password         ``user.update``             ``user.reset_password``
Packing Hand to dispatch     ``packing.manage``          ``dispatch.create``
Notifications (whole route)  ungated                     ``notification.view``
===========================  ==========================  =========================

Each one fails in both directions at once, which is what makes the class worth a
guard. Someone holding the right cannot see the control; someone without it sees
the control and is refused by the API. Neither user files a bug that names the
cause, and no existing check fires: the build passes, the lint passes, the
contract matches, and the backend is behaving exactly as designed.

Scope: mutations only (POST/PATCH/PUT/DELETE). Read permissions are enforced at
the route level in ``apps/web/src/app/router.tsx`` rather than restated at each
call site, so a GET is satisfied by the route's own guard.

Satisfaction rule: the enforced permission must be named *somewhere* in the
feature folder that makes the call, or on the route that mounts it. This is
deliberately loose. The test is not trying to prove the gate is on the correct
button - it cannot know that - only that the folder has heard of the right at
all. Every one of the five defects above is caught by that weak condition,
because in each case the correct constant appeared nowhere near the screen.

Like the catalogue test, this lives in the backend suite because the backend
owns the vocabulary and the enforcement. The frontend is the copy, and the copy
is what drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
API_MODULES = ROOT / "api" / "src" / "modules"
WEB_SRC = ROOT / "web" / "src"
ROUTER = WEB_SRC / "app" / "router.tsx"

#: Methods that change state. A GET is covered by its route guard.
MUTATIONS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

_PERMISSION_REF = re.compile(r"\bP\.([A-Z_0-9]+)\b")
_API_CALL = re.compile(
    r"api\.(get|getPage|post|put|patch|delete)\s*"
    r"(?:<[^<>]*(?:<[^<>]*>)?[^<>]*>)?\s*\("
)
_STRING_PIECE = re.compile(r"`([^`]*)`|'([^']*)'|\"([^\"]*)\"")


# --------------------------------------------------------------- exemptions --

#: ``(METHOD, path)`` deliberately not gated in its own folder, with the reason.
#: Keep this empty unless there is a real argument; an entry here is a promise
#: that a human checked the call site, not a way to silence the test.
EXEMPT: dict[tuple[str, str], str] = {}


# ----------------------------------------------------------- backend routes --


def _parenthesised(text: str, start: int) -> str:
    """The decorator's argument list, from just past ``(`` to its match."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _normalise(path: str) -> str:
    """``/users/{user_id}/roles`` and ``/users/${id}/roles`` both -> ``/users/*/roles``."""
    path = re.sub(r"\{[^}]+\}", "*", path)
    path = re.sub(r"\*+", "*", path)
    return path.rstrip("/") or "/"


def _backend_routes() -> dict[tuple[str, str], str]:
    """``(METHOD, normalised path) -> enforced permission``.

    Sub-routers matter: transporters hang off ``@transporters_router`` inside the
    vendors module, and a parser that only understood ``@router.`` reported the
    whole transporter master as unprotected.
    """
    routes: dict[tuple[str, str], str] = {}
    for router_file in API_MODULES.glob("*/router.py"):
        text = router_file.read_text(encoding="utf-8")

        prefixes = {
            m.group(1): (
                p.group(1) if (p := re.search(r'prefix\s*=\s*"([^"]+)"', m.group(2))) else ""
            )
            for m in re.finditer(r"(\w+)\s*=\s*APIRouter\((.*?)\)\s*\n", text, re.S)
        }
        mounted = {
            m.group(1): (
                p.group(1) if (p := re.search(r'prefix\s*=\s*"([^"]+)"', m.group(2))) else ""
            )
            for m in re.finditer(r"include_router\(\s*(\w+)([^)]*)\)", text)
        }

        for m in re.finditer(r"@(\w+)\.(get|post|patch|put|delete)\(", text):
            var, method = m.group(1), m.group(2).upper()
            body = _parenthesised(text, m.end())
            literal = re.match(r'\s*"([^"]*)"', body)
            if not literal:
                continue
            permissions = re.findall(r"Permission\.([A-Z_0-9]+)", body)
            if not permissions:
                continue
            full = mounted.get(var, "") + prefixes.get(var, "") + literal.group(1)
            routes[(method, _normalise(full))] = permissions[0]
    return routes


# ---------------------------------------------------------- frontend calls --


def _first_argument(text: str, start: int) -> str:
    """The path expression passed to ``api.x(...)``, up to the first top-level comma."""
    depth, i = 1, start
    while i < len(text) and depth:
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 1:
            break
        i += 1
    return text[start:i]


def _path_of(expression: str) -> str | None:
    """Recover the route shape from a path expression.

    Both interpolation and concatenation appear in this codebase::

        `/packing/${order.id}/hand-over`
        '/allocation/reservations/' + reservation.id + '/release'

    Every non-literal piece is an id, so it becomes ``*``. Anything that does not
    resolve to an absolute path (a variable holding the whole path, say) returns
    ``None`` and is skipped rather than guessed at.
    """
    pieces = [a or b or c for a, b, c in _STRING_PIECE.findall(expression)]
    if not pieces:
        return None
    literal = "*".join(pieces) if len(pieces) > 1 else pieces[0]
    if not literal.startswith("/"):
        return None
    return _normalise(re.sub(r"\$\{[^}]*\}", "*", literal))


def _frontend_call_sites() -> list[tuple[str, str, Path]]:
    """``(METHOD, normalised path, source file)`` for every ``api.*`` call.

    Test files are excluded. A spec calls ``api.post('/documents', form)`` to
    prove the client does not set a Content-Type on FormData - it is exercising
    the HTTP boundary, not offering the user an upload, so there is no
    affordance to gate and demanding ``P.DOCUMENT_UPLOAD`` beside it would be
    noise. Excluding them costs nothing: a test cannot be the only caller of a
    real endpoint, because then the feature does not exist.
    """
    sites: list[tuple[str, str, Path]] = []
    for source in WEB_SRC.rglob("*.ts*"):
        if source.name == "openapi.ts" or "node_modules" in source.parts:
            continue
        if ".test." in source.name or ".spec." in source.name:
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        for m in _API_CALL.finditer(text):
            path = _path_of(_first_argument(text, m.end()))
            if path is None:
                continue
            method = m.group(1).upper()
            sites.append(("GET" if method == "GETPAGE" else method, path, source))
    return sites


def _permissions_named_in(directory: Path) -> set[str]:
    """Every ``P.X`` mentioned by any file in one feature folder."""
    named: set[str] = set()
    for source in directory.glob("*.ts*"):
        named |= set(_PERMISSION_REF.findall(source.read_text(encoding="utf-8", errors="ignore")))
    return named


# -------------------------------------------------------------------- tests --


def test_the_parsers_still_read_both_sides() -> None:
    """A regex that quietly stopped matching would make the real test vacuous."""
    routes = _backend_routes()
    assert len(routes) > 300, (
        f"only {len(routes)} permission-guarded backend routes parsed; the router "
        "style changed and this guard is no longer reading it"
    )
    sites = _frontend_call_sites()
    assert len(sites) > 100, (
        f"only {len(sites)} frontend api calls parsed; the client style changed "
        "and this guard is no longer reading it"
    )
    assert any(method in MUTATIONS for method, _, _ in sites), "no mutations found at all"


def test_every_mutation_is_gated_on_the_right_its_endpoint_enforces() -> None:
    routes = _backend_routes()
    route_level = set(_PERMISSION_REF.findall(ROUTER.read_text(encoding="utf-8")))

    failures: list[str] = []
    for method, path, source in sorted(set(_frontend_call_sites())):
        if method not in MUTATIONS:
            continue
        required = routes.get((method, path))
        if required is None:  # not a guarded endpoint, or a path we cannot resolve
            continue
        if (method, path) in EXEMPT:
            continue
        if required in route_level:
            continue
        if required in _permissions_named_in(source.parent):
            continue
        failures.append(
            f"  {source.relative_to(WEB_SRC)}\n"
            f"      calls {method} {path}\n"
            f"      which the API guards with Permission.{required}, but neither "
            f"{source.parent.name}/ nor the route table ever names P.{required}"
        )

    assert not failures, (
        f"{len(failures)} mutation(s) gated on a right their endpoint does not "
        "enforce:\n\n" + "\n\n".join(failures) + "\n\n"
        "This fails in both directions: whoever holds the right cannot see the "
        "control, and whoever sees it is refused by the API. Gate the affordance "
        "on the permission the endpoint declares - do not add the constant to the "
        "folder just to quiet this test."
    )
