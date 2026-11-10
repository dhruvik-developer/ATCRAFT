"""A shared helper is defined once, and the copies it replaced do not come back.

Each of these was copied into module after module, every copy's docstring
promising "one constructor" or "one predicate, in one place": six
``referenced_in_use``, two ``assert_parent_allowed`` that had already drifted
(only one counted the levels that move with a record), five ``can_approve`` and
four ``_clean_name`` validators in warehouses - two of which returned ``None``,
so a PATCH of ``{"name": null}`` reached a NOT NULL column and came back as a
422 naming no field.

A definition elsewhere that delegates to the owner - ``mrp``'s
``_failure_message`` binds the shared sentence to the planning run - is a call,
not a copy, and passes. A re-implementation does not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: helper -> the one module that defines it.
OWNERS: dict[str, str] = {
    "referenced_in_use": "core/errors.py",
    "failure_message": "core/errors.py",
    "assert_parent_allowed": "common/hierarchy.py",
    "can_approve": "common/approvals.py",
    "clean_name": "common/codes.py",
    "reject_null": "core/validation.py",
    "require_changes": "core/validation.py",
    "date_before": "core/messages.py",
    "inactive_record": "core/messages.py",
}

#: The error code only :func:`src.core.errors.referenced_in_use` attaches by hand
#: (the integrity handler maps a foreign-key violation to it from a table).
_IN_USE_CODE = "REFERENCED_RECORD_IN_USE"
_IN_USE_OWNERS = frozenset({"core/errors.py"})


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def copies(source: str, relative: str) -> list[str]:
    """Definitions in ``source`` that re-implement a helper :data:`OWNERS` names."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        helper = node.name.lstrip("_")
        if helper not in OWNERS or OWNERS[helper] == relative:
            continue
        if helper in _calls(node):
            continue  # delegates to the shared helper
        found.append(
            (node.lineno, f"line {node.lineno}: {node.name} (defined in {OWNERS[helper]})")
        )
    # ast.walk is breadth-first; report in source order so a failure reads top-down.
    return [finding for _, finding in sorted(found)]


def hand_built_in_use_errors(source: str) -> list[str]:
    """Calls that attach ``code=ErrorCode.REFERENCED_RECORD_IN_USE`` themselves."""
    return [
        f"line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and any(
            keyword.arg == "code"
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == _IN_USE_CODE
            for keyword in node.keywords
        )
    ]


def _sources() -> dict[str, str]:
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_the_guard_finds_a_copied_helper() -> None:
    source = (
        "from pydantic import BaseModel, field_validator\n"
        "class WarehouseUpdate(BaseModel):\n"
        "    @field_validator('name')\n"
        "    @classmethod\n"
        "    def _clean_name(cls, value):\n"
        "        return None if value is None else ' '.join(value.split())\n"
        "def referenced_in_use(message, **details):\n"
        "    return BusinessRuleError(message, code=ErrorCode.REFERENCED_RECORD_IN_USE)\n"
    )

    assert copies(source, "modules/warehouses/schemas.py") == [
        "line 5: _clean_name (defined in common/codes.py)",
        "line 7: referenced_in_use (defined in core/errors.py)",
    ]
    assert hand_built_in_use_errors(source) == ["line 8"]


def test_the_guard_lets_a_delegating_wrapper_through() -> None:
    source = (
        "def _failure_message(exc):\n"
        "    return failure_message(exc, 'The planning run', logged_against='worker log')\n"
    )

    assert copies(source, "workers/tasks/mrp.py") == []


def test_every_owner_defines_its_helper() -> None:
    sources = _sources()
    for helper, owner in OWNERS.items():
        defined = {
            node.name
            for node in ast.walk(ast.parse(sources[owner]))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert helper in defined, f"{owner} no longer defines {helper}"


def test_no_module_redefines_a_shared_helper() -> None:
    offenders = {
        relative: found
        for relative, source in _sources().items()
        if (found := copies(source, relative))
    }

    assert not offenders, (
        f"Import the shared helper instead of defining another copy of it: {offenders}"
    )


def test_only_referenced_in_use_attaches_the_in_use_code() -> None:
    offenders = {
        relative: found
        for relative, source in _sources().items()
        if relative not in _IN_USE_OWNERS and (found := hand_built_in_use_errors(source))
    }

    assert not offenders, (
        "Raise src.core.errors.referenced_in_use(message, **details) rather than "
        f"attaching REFERENCED_RECORD_IN_USE by hand: {offenders}"
    )
