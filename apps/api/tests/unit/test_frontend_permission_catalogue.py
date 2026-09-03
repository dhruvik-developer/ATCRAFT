"""The frontend's permission catalogue must not drift from the backend's.

``apps/web/src/lib/permissions.ts`` mirrors :class:`src.core.permissions.Permission`
by hand. Its own docstring explains why it is duplicated rather than fetched -
menu definitions and route guards have to resolve at build time - and accepts
"the small cost of keeping two lists in step".

Nothing was keeping them in step. ``job_work.close`` existed on the server and
not in the catalogue, so the job-work screen could not name the right its own
endpoints require. What it did instead is the reason this test exists: it
substituted ``job_work.issue`` and ``job_work.receive`` and wrote a comment
asserting "the permission catalogue has no dedicated job-work approve or close
right" - a false statement about the contract, which then showed Approve and
Close to people the API refuses and hid them from the people who hold the right.

A missing constant does not fail the TypeScript build: the screen simply reaches
for a different one that does exist. So the drift is invisible on both sides
until someone is wrongly denied, which is the worst way to discover an
authorisation rule.

This is a backend test on purpose. The backend owns the permission vocabulary;
the frontend copies it. The copy is what must be checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.permissions import Permission

pytestmark = pytest.mark.unit

CATALOGUE = (
    Path(__file__).resolve().parents[3] / "web" / "src" / "lib" / "permissions.ts"
)

#: Every quoted `'domain.action'` string in the catalogue.
_ENTRY = re.compile(r"'([a-z_]+\.[a-z_]+)'")


def _frontend_permissions() -> set[str]:
    if not CATALOGUE.exists():  # pragma: no cover - the frontend is always present
        pytest.skip(f"{CATALOGUE} not found")
    return set(_ENTRY.findall(CATALOGUE.read_text(encoding="utf-8")))


def test_the_catalogue_is_not_empty() -> None:
    """A regex that silently matched nothing would make every test below vacuous."""
    found = _frontend_permissions()
    assert len(found) > 100, (
        f"only {len(found)} permission strings parsed out of {CATALOGUE.name}; "
        "the file format changed and this guard is no longer reading it"
    )


def test_every_backend_permission_exists_in_the_frontend_catalogue() -> None:
    backend = {str(permission) for permission in Permission}
    missing = sorted(backend - _frontend_permissions())
    assert not missing, (
        f"{len(missing)} permission(s) exist on the server and not in "
        f"apps/web/src/lib/permissions.ts:\n  " + "\n  ".join(missing) + "\n\n"
        "A screen that needs one of these cannot name it, and what happens then "
        "is not a build error - it reaches for a neighbouring right that does "
        "exist, and quietly authorises the wrong people."
    )


def test_the_frontend_invents_no_permission_the_server_does_not_have() -> None:
    backend = {str(permission) for permission in Permission}
    invented = sorted(_frontend_permissions() - backend)
    assert not invented, (
        f"{len(invented)} permission(s) in apps/web/src/lib/permissions.ts do not "
        f"exist on the server:\n  " + "\n  ".join(invented) + "\n\n"
        "`can()` on a right nobody can hold is a control that never opens, so the "
        "affordance it gates is dead for every user including an administrator."
    )
