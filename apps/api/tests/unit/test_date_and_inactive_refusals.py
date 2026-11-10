"""An end-before-start or an inactive-master refusal is built by its shared helper.

``date_before`` and ``inactive_record`` in ``core/messages.py`` existed while
thirty-five end-before-start refusals were written out in seven wordings ("cannot
precede", "must be on or after", "ends before it starts", "is before the ...")
and thirty-six inactive-master refusals by hand, some "is not active", some
naming no consequence. One rule reached the user in several sentences, twice
within one module: production's schema said "planned end date" and its service
"planned finish".

The rule is a function over source text with a self-test that feeds it the
wordings that shipped. Any string literal in ``src`` (docstrings aside) that
reads like one of these refusals is a finding unless it is listed below with
the reason the helper cannot say it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.message_scan_support import _template

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: The owner of both helpers, whose own templates are the sentences.
_OWNER = "core/messages.py"

_DATE_ORDER = re.compile(
    r"cannot (be before|precede)|must be on or after|is before the|"
    r"before it starts|ends before|must not be (before|earlier)"
)
_INACTIVE = re.compile(r"\b(is|are) (inactive|not active)\b")

#: ``(file, text the literal contains)`` -> why ``inactive_record`` cannot say it.
ALLOWED: dict[tuple[str, str], str] = {
    ("modules/procurement/service.py", "These items are inactive and cannot be ordered: "): (
        "lists every inactive item on the requisition at once, not one record"
    ),
    ("modules/production/service.py", "'s account is inactive and cannot be assigned work."): (
        "names a person by name, not a master record by its code"
    ),
    ("modules/routing/service.py", "'s account is inactive and cannot be planned on a shift."): (
        "names a person by name, not a master record by its code"
    ),
    ("modules/organization/service.py", "is inactive; an active cost centre cannot sit "): (
        "the refusal falls on the child being saved, not on the inactive parent"
    ),
}


def _docstrings(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def hand_written_refusals(source: str) -> list[str]:
    """Literal text in ``source`` worded as an end-before-start or inactive refusal."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip or not isinstance(node, ast.Constant | ast.JoinedStr):
            continue
        text = _template(node)
        if text is not None and (_DATE_ORDER.search(text) or _INACTIVE.search(text)):
            found.append((node.lineno, text))
    return [text for _, text in sorted(found)]


def _offenders() -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if "__pycache__" in path.parts or relative == _OWNER:
            continue
        texts = [
            text
            for text in hand_written_refusals(path.read_text(encoding="utf-8"))
            if not any(file == relative and part in text for file, part in ALLOWED)
        ]
        if texts:
            offenders[relative] = texts
    return offenders


@pytest.mark.parametrize(
    "shipped",
    [
        'raise ValueError("The validity end date must be on or after the start date.")',
        'raise ValueError("Expected delivery cannot precede dispatch.")',
        'raise ValidationError("The report period ends before it starts.")',
        'raise ValidationError("The expected return date is before the challan date.")',
        'raise BusinessRuleError(f"Customer {code} is not active and cannot be sold to.")',
        'raise BusinessRuleError(f"Vendor {vendor.code} is inactive.")',
        'failure = f"Customer {customer.code} is not active and cannot take new orders."',
    ],
)
def test_the_guard_finds_the_wordings_that_shipped(shipped: str) -> None:
    assert hand_written_refusals(shipped)


def test_the_guard_passes_the_helpers_and_their_docstrings() -> None:
    source = (
        "def check(order):\n"
        '    """Refuse a date that is before the order date."""\n'
        "    raise ValidationError(date_before('required date', 'order date'))\n"
        "def inactive(item):\n"
        "    raise BusinessRuleError(inactive_record('Item', item.code, 'cannot be sold'))\n"
    )

    assert hand_written_refusals(source) == []


def test_every_end_before_start_and_inactive_refusal_uses_its_helper() -> None:
    offenders = _offenders()

    assert not offenders, (
        "Build the sentence with core.messages.date_before(later, earlier) or "
        "inactive_record(entity, code, consequence), or list it in ALLOWED with the "
        f"reason the helper cannot say it: {offenders}"
    )


def test_every_exemption_still_matches_a_literal() -> None:
    unused = []
    for file, part in ALLOWED:
        texts = hand_written_refusals((SRC / file).read_text(encoding="utf-8"))
        if not any(part in text for text in texts):
            unused.append((file, part))

    assert not unused
