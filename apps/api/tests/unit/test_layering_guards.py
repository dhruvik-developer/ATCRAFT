"""Mechanical guards for three CLAUDE.md layering rules that review alone kept.

* A router is thin: parse, authorise, delegate, return the envelope. It imports
  nothing from SQLAlchemy and never drives the session itself - "Raw database
  access in a route is a review rejection."
* Only ``src/core/database.py`` commits or rolls back. ``transaction()`` runs
  its body in a SAVEPOINT so a failure unwinds that operation and nothing
  else; a bare ``session.rollback()`` in a service discards the caller's work
  too (see the CLAUDE.md trap of the same name).
* No ``float`` in the business modules. Money and quantities are ``Decimal``;
  the few floats that are not business values are listed below with the
  reason, so a new one is a decision someone has to write down.

Every rule is a function over source text, and each has a self-test that feeds
it the violation it exists to catch - a guard that cannot fail is worse than
none.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Session methods a router must leave to the service layer.
_SESSION_METHODS = frozenset(
    {"execute", "scalar", "scalars", "get", "add", "add_all", "flush", "commit"}
    | {"rollback", "refresh", "delete", "merge", "stream", "stream_scalars"}
)

#: The one module allowed to end a transaction.
_TRANSACTION_OWNER = "core/database.py"

#: ``float`` in src/modules, each a non-business value.
FLOAT_ALLOWED: dict[str, str] = {
    "modules/audit/schemas.py": "a reporting window's length in days, not a stored value",
    "modules/courier_tracking/provider.py": "epoch seconds compared for webhook clock skew",
    "modules/vendors/router.py": "the min_rating query filter; the filter model holds a Decimal",
}


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _router_files() -> list[Path]:
    return sorted(path for path in (SRC / "modules").glob("*/*.py") if "router" in path.stem)


def _line_number(finding: str) -> int:
    # ast.walk is breadth-first; report in source order so a failure reads top-down.
    return int(finding.split(":")[0].removeprefix("line "))


def router_violations(source: str) -> list[str]:
    """SQLAlchemy imports and direct session calls in a router module."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlalchemy":
            found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            found.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] == "sqlalchemy"
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SESSION_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"session", "db"}
        ):
            found.append(f"line {node.lineno}: {node.func.value.id}.{node.func.attr}()")
    return sorted(found, key=_line_number)


def transaction_end_violations(source: str) -> list[str]:
    """``.commit()`` / ``.rollback()`` calls, wherever they appear."""
    found = [
        f"line {node.lineno}: .{node.func.attr}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    ]
    return sorted(found, key=_line_number)


def float_uses(source: str) -> list[str]:
    found = [
        f"line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id == "float"
    ]
    return sorted(found, key=_line_number)


def test_the_scans_find_the_code_they_police() -> None:
    assert len(_router_files()) >= 30, "router discovery found too few files"
    assert (SRC / _TRANSACTION_OWNER).is_file()


def test_routers_do_not_touch_the_database() -> None:
    offenders = {
        _relative(path): found
        for path in _router_files()
        if (found := router_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Routers parse, authorise, delegate and return the envelope; move the query "
        f"into the module's repository and call it through the service: {offenders}"
    )


def test_only_the_transaction_helper_commits_or_rolls_back() -> None:
    offenders = {
        _relative(path): found
        for path in _python_files(SRC)
        if _relative(path) != _TRANSACTION_OWNER
        and (found := transaction_end_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Use `async with transaction(session):` from src.core.database; a direct "
        f"commit or rollback ends work that belongs to the caller: {offenders}"
    )


def test_business_modules_hold_no_float() -> None:
    offenders = {
        _relative(path): found
        for path in _python_files(SRC / "modules")
        if _relative(path) not in FLOAT_ALLOWED
        and (found := float_uses(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Money and quantities are Decimal (src.core.types). If this float is not a "
        f"business value, add it to FLOAT_ALLOWED with the reason: {offenders}"
    )


def test_every_float_allowance_is_still_needed() -> None:
    stale = [
        name
        for name in FLOAT_ALLOWED
        if not (SRC / name).is_file() or not float_uses((SRC / name).read_text(encoding="utf-8"))
    ]
    assert not stale, f"remove these FLOAT_ALLOWED entries, the float is gone: {stale}"


# -- the rules reject what they exist to catch --------------------------------


def test_the_router_rule_rejects_queries_and_session_calls() -> None:
    source = (
        "from sqlalchemy import select\n"
        "import sqlalchemy.orm\n"
        "async def handler(session):\n"
        "    await session.execute(select(1))\n"
        "    session.add(object())\n"
        "    await service(session).run()\n"
    )
    assert router_violations(source) == [
        "line 1: from sqlalchemy import ...",
        "line 2: import sqlalchemy.orm",
        "line 4: session.execute()",
        "line 5: session.add()",
    ]


def test_the_transaction_rule_rejects_commit_and_rollback() -> None:
    source = "async def f(session):\n    await session.rollback()\n    await self.db.commit()\n"
    assert transaction_end_violations(source) == ["line 2: .rollback()", "line 3: .commit()"]


def test_the_float_rule_rejects_annotations_and_conversions() -> None:
    source = "def total(x: float) -> str:\n    return str(float(x))\n"
    assert float_uses(source) == ["line 1", "line 2"]
