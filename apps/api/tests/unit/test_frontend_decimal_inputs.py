"""A decimal box's rule comes from ``lib/decimal-input.ts``, never a hand-written regex.

A form checks a typed decimal against the column it is saved into: a
``Quantity`` is ``NUMERIC(18, 4)``, so 14 whole digits and 4 places. That rule
had been written out by hand 91 times in 30 spellings across 50 files - eight
of them with no bound on the whole digits at all (``/^\\d+(\\.\\d{1,4})?$/``),
and ``QUANTITY_INPUT`` exported by dispatch, packing and quality under two
rules, one refusing a blank and two taking it, so an auto-import could pick
either. A change of precision touched fifty files.

``lib/decimal-input.ts`` states the bounds once per column (``QUANTITY``,
``UNIT_PRICE``, ``MONEY``, ``PERCENTAGE``, ``CONVERSION_FACTOR``, and the
two-place ``ROUNDED_MONEY`` the API's ``to_money`` keeps), builds a pattern
with ``decimalPattern(bounds, { signed, optional })``, and offers the common
ones ready made (``QUANTITY_INPUT``, ``OPTIONAL_QUANTITY_INPUT``,
``UNIT_PRICE_INPUT``...). A field with a genuinely different rule passes its
own bounds.

So outside that module this guard refuses, on comment-blanked source, a regex
literal anchored at ``^`` that reads a decimal point followed by digits
(``\\.\\d``), and a ``new RegExp(...)`` string that does. It leaves alone what is
not a typed decimal's rule: a field-path parser (``/^entries\\.(\\d+)/``), the
trailing-zero trimmer (``/(\\.\\d*?)0+$/``, not anchored at the start), a date,
and the two parsers that read numbers in other people's formats - a CSV cell
(``lib/csv.ts``) and a product specification value
(``product-descriptions/specification-model.ts``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_copy_internals import GENERATED, blank_comments
from tests.unit.test_frontend_mutation_error_toasts import WEB_SRC, _line

pytestmark = pytest.mark.unit

OWNER = WEB_SRC / "lib" / "decimal-input.ts"

#: Parsers that read a number someone else formatted, not a rule for a box.
PARSERS = {
    "lib/csv.ts": "reads a number out of an imported CSV cell, signed and unbounded",
    "features/product-descriptions/specification-model.ts": (
        "reads a specification value as the catalogue wrote it, which may start with a point"
    ),
}

# A one-line regex literal: the opening slash follows neither an identifier,
# a closing bracket nor another slash (a division or a comment).
_LITERAL = re.compile(
    r"(?<![\w)\]$/])/((?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[*])"
    r"(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])*)/[a-z]*"
)
_NEW_REGEXP = re.compile(r"new RegExp\(\s*(['\"`])((?:\\.|(?!\1).)*)\1")
#: A decimal point followed by digits, as a regex reads it.
_DECIMAL_PART = re.compile(r"\\\.(?:\\d|\[0-9\])")


def hand_written_decimal_rules(source: str) -> list[str]:
    """``line N: /pattern/`` for each regex that states a typed decimal's precision."""
    code = blank_comments(source)
    found = [
        (match.start(), match.group(0))
        for match in _LITERAL.finditer(code)
        if match.group(1).startswith("^") and _DECIMAL_PART.search(match.group(1))
    ]
    found += [
        (match.start(), match.group(0))
        for match in _NEW_REGEXP.finditer(code)
        if _DECIMAL_PART.search(match.group(2).replace("\\\\", "\\"))
    ]
    return [f"line {_line(code, start)}: {text}" for start, text in sorted(found)]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] != "test"
        and path not in {OWNER, GENERATED}
    )


def test_the_presets_the_rule_points_to_exist() -> None:
    source = OWNER.read_text(encoding="utf-8")
    for name in (
        "export function decimalPattern(",
        "export const QUANTITY_INPUT",
        "export const OPTIONAL_QUANTITY_INPUT",
        "export const UNIT_PRICE_INPUT",
        "export const MONEY_INPUT",
        "export const ROUNDED_MONEY_INPUT",
        "export const PERCENT_INPUT",
    ):
        assert name in source, name
    assert len(_sources()) > 300, "source discovery found too few files"


def test_the_parsers_it_spares_still_exist() -> None:
    for name, reason in PARSERS.items():
        path = WEB_SRC / name
        assert path.is_file(), f"{name} is gone; drop its entry ({reason})"
        assert hand_written_decimal_rules(path.read_text(encoding="utf-8")), (
            f"{name} no longer holds a decimal pattern; drop its entry"
        )


def test_no_screen_writes_a_decimal_rule_by_hand() -> None:
    found = {
        name: hits
        for path in _sources()
        if (name := path.relative_to(WEB_SRC).as_posix()) not in PARSERS
        and (hits := hand_written_decimal_rules(path.read_text(encoding="utf-8")))
    }
    assert not found, (
        "These state a decimal's precision in a regex of their own:\n"
        f"{found}\n\nUse lib/decimal-input.ts: a preset such as QUANTITY_INPUT or "
        "OPTIONAL_UNIT_PRICE_INPUT, or decimalPattern(QUANTITY, { signed: true }) - and "
        "decimalPattern({ whole, places }) for a field whose rule is genuinely its own."
    )


# -- the rule rejects what it exists to catch -----------------------------------


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "const QUANTITY = /^\\d{1,14}(\\.\\d{1,4})?$/",
            "export const QUANTITY_INPUT = /^$|^\\d{1,14}(\\.\\d{1,4})?$/",
            "const OPTIONAL_PRICE = /^(\\d{1,12}(\\.\\d{1,6})?)?$/",
            "const QUANTITY_PATTERN = /^\\d+(\\.\\d{1,4})?$/",
            "const COST_DELTA = /^(-?\\d{1,14}(\\.\\d{1,4})?)?$/",
            "const approvalAmountPattern = /^\\d+(?:\\.\\d{1,4})?$/",
            "  if (!/^\\d{1,14}(\\.\\d{1,4})?$/.test(value)) throw new Error('x')",
            "    .regex(/^\\d{1,5}(\\.\\d{1,4})?$/, validation.decimals('a rate', 4))",
            "const BUILT = new RegExp('^\\\\d{1,14}(\\\\.\\\\d{1,4})?$')",
        ]
    )
    assert [hit.split(":")[0] for hit in hand_written_decimal_rules(source)] == [
        f"line {n}" for n in range(1, 10)
    ]


def test_the_rule_leaves_other_patterns_alone() -> None:
    source = "\n".join(
        [
            "        const position = /^entries\\.(\\d+)/.exec(path)",
            "const FIELD = /^values\\.(\\d+)(?:\\.|$)/",
            "  return text.replace(/(\\.\\d*?)0+$/, '$1').replace(/\\.$/, '')",
            "const ISO_DATE = /^\\d{4}-\\d{2}-\\d{2}$/",
            "const WHOLE_DAYS = /^\\d{0,4}$/",
            "export const QUANTITY_INPUT = decimalPattern(QUANTITY)",
            "const MINUTES = decimalPattern({ whole: 6, places: 4 }, { optional: true })",
            "// const QUANTITY = /^\\d{1,14}(\\.\\d{1,4})?$/ once lived here",
            "const ratio = total / count / 2",
            "const CODE = /^[A-Za-z0-9][A-Za-z0-9._\\-/ ]*$/",
        ]
    )
    assert hand_written_decimal_rules(source) == []
