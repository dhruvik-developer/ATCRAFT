"""The browser turns typed or scanned text into serial labels in one place.

The API normalises every serial label before it stores or matches one -
whitespace removed, upper-cased - and refuses a label that appears twice in one
request (``_normalise_serial_labels`` in :mod:`src.modules.inventory.schemas`).
``apps/web/src/features/inventory/serial-numbers.ts`` mirrors that rule
(``splitSerialNumbers`` / ``normaliseSerial``), so the count shown under a field
is the count the request will carry.

Two screens carried a private splitter instead. The register-serials dialog
trimmed but did not upper-case or de-duplicate, so ``sn-1 SN-1`` read as two
units and the register refused the whole run as a duplicate; the physical-count
cell split only on newlines and commas, so a space between two scanned labels
glued them into one label the API would store joined. Each looked right on the
day it was written. This fails the build on a third copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CANONICAL = WEB_SRC / "features" / "inventory" / "serial-numbers.ts"

_PARSER_VERB = r"(?:split|parse|normali[sz]e)"
#: A function declaration, or a const bound to an arrow or function expression,
#: whose name pairs a parsing verb with "serial" in either order.
_PRIVATE_PARSER = re.compile(
    r"(?:\bfunction\s+|\b(?:const|let|var)\s+)"
    rf"(?P<name>\w*{_PARSER_VERB}\w*serial\w*|\w*serial\w*{_PARSER_VERB}\w*)"
    r"\s*(?:\(|=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]*)?=>|[A-Za-z_]\w*\s*=>))",
    re.IGNORECASE,
)
#: A label list built by splitting text inline: ``serial_numbers: text.split(...)``.
_INLINE_SPLIT = re.compile(r"\w*serial_numbers\s*:[^,\n]*\.split\(")


def serial_parser_sites(source: str) -> list[str]:
    """``line N: name`` for every private serial parser and inline label split."""
    sites = [(match.start(), match.group("name")) for match in _PRIVATE_PARSER.finditer(source)]
    sites += [(match.start(), "inline split") for match in _INLINE_SPLIT.finditer(source)]
    return [f"line {source.count(chr(10), 0, start) + 1}: {name}" for start, name in sorted(sites)]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
    )


def test_the_shared_splitter_is_where_the_capture_surfaces_import_it_from() -> None:
    """If this moves, the guard below would pass on an empty set of copies."""
    assert CANONICAL.is_file(), f"{CANONICAL} is gone; every serial capture surface uses it"
    source = CANONICAL.read_text(encoding="utf-8")
    assert "export function splitSerialNumbers" in source
    assert "export function normaliseSerial" in source
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_other_file_parses_serial_labels() -> None:
    copies = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != CANONICAL and (found := serial_parser_sites(path.read_text(encoding="utf-8")))
    }
    assert not copies, (
        "Serial labels are parsed outside features/inventory/serial-numbers.ts:\n  "
        + "\n  ".join(f"{name}: {'; '.join(found)}" for name, found in copies.items())
        + "\n\nUse splitSerialNumbers (or the SerialNumbersField control). It removes "
        "whitespace, upper-cases and de-duplicates the way the API does, so the count "
        "on screen is the count the request carries instead of a 422 for a duplicate."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_both_copies_that_shipped_and_an_inline_split() -> None:
    source = (
        "function splitSerials(value: string): string[] {\n"
        "  return value.split(/[\\s,;]+/).map((entry) => entry.trim()).filter(Boolean)\n"
        "}\n"
        "function parseCountedSerialNumbers(value: string): string[] {\n"
        "  return value.split(/[\\n,]/).map((serial) => serial.trim()).filter(Boolean)\n"
        "}\n"
        "const normaliseSerialLabel = (value: string): string => value.trim()\n"
        "const body = { item_id, serial_numbers: text.split('\\n').filter(Boolean) }\n"
    )
    assert serial_parser_sites(source) == [
        "line 1: splitSerials",
        "line 4: parseCountedSerialNumbers",
        "line 7: normaliseSerialLabel",
        "line 8: inline split",
    ]


def test_the_rule_ignores_callers_and_results_of_the_shared_splitter() -> None:
    source = (
        "const numbers = splitSerialNumbers(text)\n"
        "const parsedSerials = splitSerialNumbers(line.serial_numbers ?? '')\n"
        "export function SerialNumbersField({ id, value }: Props) {\n"
        "function serialLabel(serial: StockSerialRow) { return serial.serial_number }\n"
        "  ...(serials.length ? { serial_numbers: splitSerialNumbers(line.serial_numbers) } : {})\n"
        "const labels = row.serial_numbers?.join(', ')\n"
    )
    assert serial_parser_sites(source) == []
