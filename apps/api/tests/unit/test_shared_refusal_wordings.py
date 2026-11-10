"""A refusal with a shared builder is never written out by hand again.

A reused code was refused in two wordings at twenty-one sites, and a serial or a
lot keyed against an untracked item in eleven endings at twenty-four, until
``src.core.errors.duplicate_code`` and ``src.core.messages.not_tracked`` became
the only way to say either. These guards read every error raised in ``src``
with a literal message and refuse one that states either rule itself; each has
a self-test that feeds it the wordings that shipped.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.message_scan_support import _error_classes, _template

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: rule -> (what a hand-written copy looks like, the builder to raise instead).
RULES: dict[str, tuple[re.Pattern[str], str]] = {
    "duplicate code": (
        re.compile(
            r"\bcode\b.*\balready (exists|used)\b|\balready (exists|used)\b.*\bcode\b",
            re.IGNORECASE,
        ),
        "src.core.errors.duplicate_code",
    ),
    "not tracked": (
        re.compile(r"\bnot (serial|batch)[ -]tracked\b", re.IGNORECASE),
        "src.core.messages.not_tracked",
    ),
}


def hand_written(tree: ast.Module, message_classes: set[str]) -> list[str]:
    """Each error raised with a literal message that one of :data:`RULES` matches."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in message_classes
            and node.args
        ):
            continue
        template = _template(node.args[0])
        if template is None:
            continue
        found.extend(
            f"line {node.lineno}: {rule} - raise {builder}: {template}"
            for rule, (pattern, builder) in RULES.items()
            if pattern.search(template)
        )
    return found


def _source_trees() -> dict[str, ast.Module]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_no_refusal_with_a_shared_builder_is_written_out_by_hand() -> None:
    trees = _source_trees()
    message_classes, _ = _error_classes(list(trees.values()))
    assert {"DuplicateError", "BusinessRuleError", "ValidationError"} <= message_classes

    offenders = {
        path: found
        for path, tree in trees.items()
        if (found := hand_written(tree, message_classes))
    }

    assert not offenders, offenders


def test_the_guard_finds_the_wordings_that_shipped_and_passes_the_builders() -> None:
    source = "\n".join(
        [
            "raise DuplicateError(f\"Vendor code '{code}' is already used in this company.\")",
            "raise BusinessRuleError(f'An account with code {code} already exists.', code=X)",
            "raise ValidationError(f'Item {code} is not batch tracked and cannot carry a batch.')",
            "raise BusinessRuleError(f'Line {n}: {code} is not serial tracked; remove serials.')",
            "raise duplicate_code('Vendor', code)",
            "raise BusinessRuleError(not_tracked(code, 'serial', 'serial numbers'))",
            "raise DuplicateError('This vendor is already linked to the item.')",
            "raise BusinessRuleError(f'Item {code} is serial tracked; name one serial per unit.')",
        ]
    )

    found = hand_written(
        ast.parse(source), {"DuplicateError", "BusinessRuleError", "ValidationError"}
    )

    assert [entry.split(" - ")[0] for entry in found] == [
        "line 1: duplicate code",
        "line 2: duplicate code",
        "line 3: not tracked",
        "line 4: not tracked",
    ]
