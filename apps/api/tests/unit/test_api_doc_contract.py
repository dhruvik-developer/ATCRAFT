"""docs/api.md's error-code table and endpoint map, checked against the code.

Both drifted while every suite stayed green. The table said ``MFA_REQUIRED``
"is never sent" while the shared authentication dependency raised it for every
role in ``MFA_REQUIRED_ROLES``, left out the two malware-scan codes, and listed
three codes nothing raises as though a client should expect them. The endpoint
map sent readers to ``/admin/alerts`` and ``/approvals``, which do not exist,
and left out ``/integrations/courier`` and ``/transporters``.

A code is taken as raised when the source names it (``ErrorCode.X``): the enum
body assigns its members rather than naming them, and every exception class and
call site that sends one does name it. A code nothing names, nothing sends.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from src.core.config import API_ROOT, REPO_ROOT
from src.core.errors import ErrorCode
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

API_DOC = REPO_ROOT / "docs" / "api.md"
CODE = re.compile(r"`([A-Z][A-Z0-9_]*)`")


def _section(doc: str, heading: str) -> str:
    """The body of the ``## heading`` section, up to the next one."""
    start = doc.index(f"\n## {heading}\n")
    end = doc.find("\n## ", start + 1)
    return doc[start : end if end != -1 else len(doc)]


def documented_codes(doc: str) -> set[str]:
    """Every code the error-code table's rows name."""
    rows = [
        line
        for line in _section(doc, "Error codes").splitlines()
        if line.startswith("| ") and not line.startswith("| Group")
    ]
    return {code for row in rows for code in CODE.findall(row)}


def reserved_codes(doc: str) -> set[str]:
    """The codes the section lists as reserved: in ``ErrorCode``, sent by nothing."""
    match = re.search(r"Reserved, not emitted:(.*?)\.", _section(doc, "Error codes"), re.S)
    assert match, "docs/api.md lost its 'Reserved, not emitted:' list"
    return set(CODE.findall(match.group(1)))


def named_codes(paths: Iterable[Path]) -> set[str]:
    """Every ``ErrorCode.X`` the given source files name."""
    named: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ErrorCode"
            ):
                named.add(node.attr)
    return named


def code_faults(
    documented: set[str], reserved: set[str], members: set[str], named: set[str]
) -> list[str]:
    """Where the table and its reserved list disagree with the enum and the source."""
    silent = members - named
    return [
        *(f"{code}: an ErrorCode the table leaves out" for code in sorted(members - documented)),
        *(f"{code}: in the table but no ErrorCode" for code in sorted(documented - members)),
        *(
            f"{code}: nothing raises it, yet it is not reserved"
            for code in sorted(silent - reserved)
        ),
        *(
            f"{code}: listed as reserved, yet the source raises it"
            for code in sorted(reserved - silent)
        ),
    ]


def mapped_prefixes(doc: str) -> set[str]:
    """The path prefixes in the endpoint map's first column."""
    block = _section(doc, "Endpoint map").split("```")[1]
    prefixes: set[str] = set()
    for line in block.splitlines():
        for token in line.split():
            if not token.startswith("/"):
                break
            prefixes.add(token)
    return prefixes


def map_faults(mapped: set[str], served: Iterable[str]) -> list[str]:
    """Map prefixes the API does not serve, and served prefixes the map leaves out."""
    paths = list(served)
    faults = [
        f"{prefix}: in the endpoint map, but the API serves nothing under it"
        for prefix in sorted(mapped)
        if not any(path == prefix or path.startswith(f"{prefix}/") for path in paths)
    ]
    firsts = sorted({"/" + path.strip("/").split("/")[0] for path in paths})
    faults.extend(
        f"{first}: served, but missing from the endpoint map"
        for first in firsts
        if not any(prefix == first or prefix.startswith(f"{first}/") for prefix in mapped)
    )
    return faults


@pytest.fixture(scope="module")
def doc() -> str:
    return API_DOC.read_text(encoding="utf-8")


def test_the_error_code_table_is_the_enum_and_names_what_nothing_raises(doc: str) -> None:
    members = set(ErrorCode.__members__)
    named = named_codes((API_ROOT / "src").rglob("*.py"))
    assert code_faults(documented_codes(doc), reserved_codes(doc), members, named) == []


def test_the_endpoint_map_names_exactly_the_prefixes_the_api_serves(doc: str) -> None:
    assert map_faults(mapped_prefixes(doc), (route.path for route in module_routes())) == []


def test_the_code_rule_finds_each_way_the_table_can_drift() -> None:
    faults = code_faults(
        documented={"KEPT", "INVENTED", "SILENT"},
        reserved={"KEPT"},
        members={"KEPT", "LEFT_OUT", "SILENT"},
        named={"KEPT", "LEFT_OUT"},
    )
    assert faults == [
        "LEFT_OUT: an ErrorCode the table leaves out",
        "INVENTED: in the table but no ErrorCode",
        "SILENT: nothing raises it, yet it is not reserved",
        "KEPT: listed as reserved, yet the source raises it",
    ]


def test_the_map_rule_finds_a_dead_prefix_and_a_missing_one() -> None:
    mapped = mapped_prefixes(
        "\n## Endpoint map\n\n```\n"
        "/auth                 login · me\n"
        "/users  /roles        users, roles\n"
        "/admin/alerts         smart-alert thresholds\n"
        "                      continued, and /not/a/prefix\n"
        "```\n"
    )
    assert mapped == {"/auth", "/users", "/roles", "/admin/alerts"}
    assert map_faults(mapped, ["/auth/login", "/users", "/roles/{role_id}", "/transporters"]) == [
        "/admin/alerts: in the endpoint map, but the API serves nothing under it",
        "/transporters: served, but missing from the endpoint map",
    ]
