"""A typed value reaches a payload through lib/form-values.ts, and a date through lib/format.ts.

``lib/form-values.ts`` holds the rules a form applies to what was typed before it
is sent: ``asText`` (trimmed, and blank is null), ``zeroIfBlank`` (trimmed, and
blank is "0"), ``asId`` and ``asUpperText``. Twenty-three dialogs had defined
their own ``orNull`` with ``asText``'s body; the blank-to-"0" rule went by
three names (``asDecimal`` six times, ``zeroIfBlank``, ``asDelta``) while the
settings page defined an ``asDecimal`` meaning the *opposite* - blank is null,
because zero is a real threshold there - so the copy-paste hazard was a blank
threshold silently becoming a real zero. About fifty more payloads spelled the
same two rules inline. ``indexById`` was redefined twice beside lib's,
``itemLabel`` was exported twice, byte for byte, by the assembly and quality
models (now once, in ``lib/format.ts``), and "today" was wrapped in six private
helpers and rebuilt by hand from ``getMonth()`` in two more.

So outside ``lib/``:

* the two rules are refused when spelled inline - ``x.trim() === '' ? null :
  x.trim()`` and ``x.trim() || null`` are ``asText(x)``; ``x.trim() === '' ? '0'
  : x.trim()`` and ``x.trim() || '0'`` are ``zeroIfBlank(x)``; and an optional
  field's ``x?.trim() || null`` is ``asText(x ?? '')``;
* a module-level declaration that reuses the name of a ``lib/form-values.ts``,
  ``lib/utils.ts`` or ``lib/format.ts`` export is refused - that is how a private
  copy looks;
* a ``yyyy-MM-dd`` built by hand from ``getMonth() + 1`` and ``padStart`` is
  refused: the local date is ``toDateInput(new Date())``.

Comments are blanked before scanning. A function-local name is left alone (a
``const plural = ...`` inside a component is a variable, not a copy), and so is
a value already trimmed (``text === '' ? null : text``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.frontend_scan_support import GENERATED, WEB_SRC, _line, blank_comments

pytestmark = pytest.mark.unit

FORM_VALUES = WEB_SRC / "lib" / "form-values.ts"
UTILS = WEB_SRC / "lib" / "utils.ts"
FORMAT = WEB_SRC / "lib" / "format.ts"

_CHAIN = r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*"

#: ``.trim()`` or ``?.trim()``: an optional field's ``x?.trim() || null`` is the
#: same rule as ``x.trim() || null``, and ``_CHAIN`` cannot end in the ``?``.
_TRIM = r"\??\.trim\(\)"

#: Not after an identifier character, a dot or a negation: ``!x.trim() || null``
#: tests the text, it does not produce a value.
_START = r"(?<![\w$.!])"
_TERNARY = _TRIM + r"\s*===\s*''\s*\?\s*{blank}\s*:\s*\1" + _TRIM

#: (pattern, what to call instead)
_INLINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"{_START}({_CHAIN})" + _TERNARY.format(blank="null")), "asText"),
    (re.compile(rf"{_START}{_CHAIN}{_TRIM}\s*\|\|\s*null\b"), "asText"),
    (re.compile(rf"{_START}({_CHAIN})" + _TERNARY.format(blank="'0'")), "zeroIfBlank"),
    (re.compile(rf"{_START}{_CHAIN}{_TRIM}\s*\|\|\s*'0'"), "zeroIfBlank"),
)

_HAND_BUILT_DATE = re.compile(
    r"getMonth\(\)\s*\+\s*1\b[^\n]*padStart|padStart[^\n]*getMonth\(\)\s*\+\s*1\b"
)

_MODULE_DECLARATION = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function\*?|const|let|var)\s+([A-Za-z_$][\w$]*)\b",
    re.M,
)
_EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|const|let)\s+([A-Za-z_$][\w$]*)", re.M)


def lib_names() -> set[str]:
    """Every name ``lib/form-values.ts``, ``lib/utils.ts`` and ``lib/format.ts`` export."""
    return {
        name
        for path in (FORM_VALUES, UTILS, FORMAT)
        for name in _EXPORT.findall(path.read_text(encoding="utf-8"))
    }


def inline_copies(source: str) -> list[str]:
    """``line N: 'text' -> helper`` for each inline spelling of a lib rule."""
    code = blank_comments(source)
    found = [
        (match.start(), f"line {_line(code, match.start())}: {match.group(0)!r} -> {helper}")
        for pattern, helper in _INLINE_RULES
        for match in pattern.finditer(code)
    ]
    found += [
        (
            match.start(),
            f"line {_line(code, match.start())}: a date built by hand -> toDateInput(new Date())",
        )
        for match in _HAND_BUILT_DATE.finditer(code)
    ]
    return [entry for _, entry in sorted(found)]


def redefinitions(source: str, names: set[str]) -> list[str]:
    """``line N: name`` for each module-level declaration reusing a lib export's name."""
    code = blank_comments(source)
    return [
        f"line {_line(code, match.start())}: {match.group(1)}"
        for match in _MODULE_DECLARATION.finditer(code)
        if match.group(1) in names
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] not in {"lib", "test"}
        and path != GENERATED
    )


def test_the_scan_reaches_the_screens_and_the_helpers_exist() -> None:
    assert len(_sources()) > 250, "source discovery found too few files"
    assert {
        "asText",
        "asId",
        "asUpperText",
        "zeroIfBlank",
        "indexById",
        "toDateInput",
        "itemLabel",
    } <= lib_names()


def test_no_screen_spells_a_lib_rule_inline() -> None:
    found = {
        path.relative_to(WEB_SRC).as_posix(): hits
        for path in _sources()
        if (hits := inline_copies(path.read_text(encoding="utf-8")))
    }
    assert not found, (
        "These spell a lib/form-values.ts or lib/format.ts rule inline; call the helper "
        f"instead:\n{found}"
    )


def test_no_module_redefines_a_lib_helper() -> None:
    names = lib_names()
    found = {
        path.relative_to(WEB_SRC).as_posix(): hits
        for path in _sources()
        if (hits := redefinitions(path.read_text(encoding="utf-8"), names))
    }
    assert not found, (
        "These modules declare a name lib/ already exports - "
        f"import the lib helper rather than keeping a copy:\n{found}"
    )


# -- the rules reject what they exist to catch -----------------------------------


def test_the_rule_finds_the_inline_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "const orNull = (value: string) => (value.trim() === '' ? null : value.trim())",
            "  const bound = (value: string) => (value.trim() === '' ? null : value.trim())",
            "        notes: values.notes.trim() === '' ? null : values.notes.trim(),",
            "        buyer_reference: buyer.trim() || null,",
            "const asDecimal = (value: string) => (value.trim() === '' ? '0' : value.trim())",
            "        initial_quantity: initialQuantity.trim() || '0',",
            "  const month = `${now.getMonth() + 1}`.padStart(2, '0')",
            "  const month = String(now.getMonth() + 1).padStart(2, '0')",
            # An optional field's spellings: the first two shipped, uncaught.
            "          company_code: values.company_code?.trim() || null,",
            "        notes: values.notes?.trim() || null,",
            "        notes: values.notes?.trim() === '' ? null : values.notes?.trim(),",
            "        weight: values.weight?.trim() || '0',",
            "        freight: freight?.trim() === '' ? '0' : freight?.trim(),",
        ]
    )
    assert inline_copies(source) == [
        "line 1: \"value.trim() === '' ? null : value.trim()\" -> asText",
        "line 2: \"value.trim() === '' ? null : value.trim()\" -> asText",
        "line 3: \"values.notes.trim() === '' ? null : values.notes.trim()\" -> asText",
        "line 4: 'buyer.trim() || null' -> asText",
        "line 5: \"value.trim() === '' ? '0' : value.trim()\" -> zeroIfBlank",
        "line 6: \"initialQuantity.trim() || '0'\" -> zeroIfBlank",
        "line 7: a date built by hand -> toDateInput(new Date())",
        "line 8: a date built by hand -> toDateInput(new Date())",
        "line 9: 'values.company_code?.trim() || null' -> asText",
        "line 10: 'values.notes?.trim() || null' -> asText",
        "line 11: \"values.notes?.trim() === '' ? null : values.notes?.trim()\" -> asText",
        "line 12: \"values.weight?.trim() || '0'\" -> zeroIfBlank",
        "line 13: \"freight?.trim() === '' ? '0' : freight?.trim()\" -> zeroIfBlank",
    ]


def test_the_rule_leaves_other_shapes_alone() -> None:
    source = "\n".join(
        [
            "        reason: text === '' ? null : text,",
            "  if (!values.name.trim() || null) return",
            "        name: values.name.trim(),",
            "        total_boxes: boxes.trim() === '' ? null : Number(boxes.trim()),",
            "        notes: asText(values.notes),",
            "        quantity: zeroIfBlank(line.quantity),",
            "  // const orNull = (v: string) => (v.trim() === '' ? null : v.trim())",
            "  const month = now.getMonth() + 1",
            "  if (!values.code?.trim() || null) return",
            "            reason_code: reasons[line.id]?.trim() || 'CUSTOMER_RETURN',",
        ]
    )
    assert inline_copies(source) == []


def test_the_rule_finds_a_module_level_copy_of_a_lib_helper() -> None:
    names = {"indexById", "asText", "plural", "itemLabel"}
    source = "\n".join(
        [
            "export function indexById<T extends { id: string }>(rows: readonly T[]) {",
            "  return new Map(rows.map((row) => [row.id, row]))",
            "}",
            "const asText = (value: string) => value.trim() || null",
            "function Component() {",
            "  const plural = count === 1 ? '' : 's'",
            "  const itemLabel = demand.itemCode ?? 'this item'",
            "}",
            "// function indexById() {}",
            "const indexByCode = new Map()",
            # The copy that shipped beside assembly-model's, in quality-types.ts.
            "export function itemLabel(row: { item_code?: string | null }): string {",
        ]
    )
    assert redefinitions(source, names) == [
        "line 1: indexById",
        "line 4: asText",
        "line 11: itemLabel",
    ]
