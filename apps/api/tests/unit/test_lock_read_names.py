"""A repository read that takes a row lock says so in its name.

``SalesOrderLookupRepository.get_header`` read like any other getter and took
``FOR UPDATE`` on the sales order. The carton-label GET and the packing-list
renderer called it outside any ``transaction()`` block, so the lock was held by
the autobegun transaction until the session closed - through the PDF render, the
file write and the document insert - and every dispatch, packing order and order
edit on that sales order queued behind a label print. Nothing at the call site
said a lock was being taken: the name was the only signal, and it said "read".

The rule: a repository method whose body takes a lock unconditionally - a
``with_for_update()`` call, or a module helper that always makes one - is named
``lock*`` or ``*_for_update``, or takes a ``lock`` / ``for_update`` parameter
that decides it. A conditional lock is the caller's choice, visible at the call.

Each rule is a function over source text with a self-test that feeds it the
violation, as in ``test_layering_guards.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MODULES = Path(__file__).resolve().parents[2] / "src" / "modules"

#: Parameter names that make a lock the caller's explicit decision.
LOCK_PARAMETERS = frozenset({"lock", "for_update", "for_assignment"})


def _repository_files() -> list[Path]:
    return sorted(
        path
        for path in MODULES.rglob("*.py")
        if "__pycache__" not in path.parts
        and (path.stem == "repository" or path.stem.endswith("_repository"))
    )


def _is_lock_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
    )


def _branch_nodes(function: ast.AST) -> set[int]:
    """Every node that runs only on one side of an ``if`` or a conditional expression."""
    inside: set[int] = set()
    for node in ast.walk(function):
        branches: list[ast.AST] = []
        if isinstance(node, ast.If):
            branches = [*node.body, *node.orelse]
        elif isinstance(node, ast.IfExp):
            branches = [node.body, node.orelse]
        for branch in branches:
            inside.update(id(child) for child in ast.walk(branch))
    return inside


def _locking_helpers(tree: ast.Module) -> set[str]:
    """Module-level functions that lock whatever statement they are handed."""
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_lock_call(child) for child in ast.walk(node))
    }


def _locks_unconditionally(method: ast.AST, helpers: set[str]) -> bool:
    conditional = _branch_nodes(method)
    for node in ast.walk(method):
        if id(node) in conditional or not isinstance(node, ast.Call):
            continue
        if _is_lock_call(node):
            return True
        if isinstance(node.func, ast.Name) and node.func.id in helpers:
            return True
    return False


def _says_it_locks(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    name = method.name.lstrip("_")
    if name.startswith("lock") or name.endswith("_for_update"):
        return True
    arguments = method.args
    named = {arg.arg for arg in [*arguments.args, *arguments.kwonlyargs]}
    return bool(named & LOCK_PARAMETERS)


def misnamed_lock_reads(source: str) -> list[str]:
    """Repository methods that lock without saying so, as ``line N: Class.method``."""
    tree = ast.parse(source)
    helpers = _locking_helpers(tree)
    found: list[str] = []
    for owner in tree.body:
        if not isinstance(owner, ast.ClassDef):
            continue
        for method in owner.body:
            if (
                isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
                and not _says_it_locks(method)
                and _locks_unconditionally(method, helpers)
            ):
                found.append(f"line {method.lineno}: {owner.name}.{method.name}")
    return found


def test_the_scan_finds_the_repositories() -> None:
    files = _repository_files()
    assert MODULES / "packing" / "repository.py" in files
    assert MODULES / "products" / "specification_repository.py" in files


def test_every_lock_taking_read_is_named_for_its_lock() -> None:
    offenders = {
        path.relative_to(MODULES).as_posix(): found
        for path in _repository_files()
        if (found := misnamed_lock_reads(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "A repository read that takes a row lock is named lock_* (or *_for_update), or takes "
        "a lock/for_update parameter, so no caller takes one by accident on a read path: "
        f"{offenders}"
    )


def test_the_rule_rejects_a_lock_behind_a_reading_name() -> None:
    source = (
        "def _locked(stmt):\n"
        "    return stmt.with_for_update(read=True)\n"
        "\n"
        "class Repo:\n"
        "    async def get_header(self, order_id):\n"
        "        return await self.session.execute(select(Order).with_for_update())\n"
        "\n"
        "    async def for_assignment(self, ids):\n"
        "        return await self.session.execute(_locked(select(Order)))\n"
    )
    assert misnamed_lock_reads(source) == [
        "line 5: Repo.get_header",
        "line 8: Repo.for_assignment",
    ]


def test_the_rule_accepts_a_named_or_requested_lock() -> None:
    source = (
        "class Repo:\n"
        "    async def lock_header(self, order_id):\n"
        "        return select(Order).with_for_update()\n"
        "\n"
        "    async def account_for_update(self):\n"
        "        return select(Account).with_for_update()\n"
        "\n"
        "    async def get(self, order_id, *, lock=False):\n"
        "        stmt = select(Order)\n"
        "        if lock:\n"
        "            stmt = stmt.with_for_update()\n"
        "        return stmt\n"
        "\n"
        "    async def code(self, order_id):\n"
        "        return select(Order.code)\n"
    )
    assert misnamed_lock_reads(source) == []
