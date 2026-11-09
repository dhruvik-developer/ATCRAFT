"""A date or time field never shares a row on a phone.

A ``type="date"`` or ``type="time"`` input draws its whole value and the
browser's picker icon, and cannot ellipsise; at ``text-sm`` with ``px-3`` it
needs about 120-130px. A dialog at 320px leaves about 240px for its body, so an
unprefixed ``grid-cols-2`` hands each field about 112px and clips the year or
the icon. Four dialogs shipped that way - an assembly order's planned start and
end, the pack-from-assembly date, a roster copy's target dates and a shift's
start and end times - while their sibling form grids said
``grid gap-3 sm:grid-cols-2``: one column below 640px, two above it. The 375px
sweep passed over all four, because they fit from about 360px.

The rule reads each class string in ``apps/web/src`` that sets an unprefixed
``grid-cols-N`` (N >= 2), takes the element it styles - Prettier writes an
element's children indented under its opening line and its closing tag level
with it, so the extent is read from the indentation rather than by parsing JSX
- and fails when a date, time or datetime-local input sits inside.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

_QUOTED = re.compile(r"""(['"`])([^'"`\n]*?)\1""")
#: Two or more columns at every width: no breakpoint prefix.
_UNPREFIXED_COLUMNS = re.compile(r"^grid-cols-(?:[2-9]|1[0-2])$")
#: An input that draws its value whole, with the browser's picker beside it.
_DATE_OR_TIME = re.compile(r"""\btype=(?:"|\{\s*['"])(?:date|time|datetime-local)\b""")
_OPENING = re.compile(r"^\s*<[A-Za-z]")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _extent(lines: list[str], at: int) -> list[str]:
    """The lines of the element whose attributes include line ``at``."""
    opening = at
    while opening > 0 and not _OPENING.match(lines[opening]):
        opening -= 1
    depth = _indent(lines[opening])
    end = opening + 1
    while end < len(lines):
        line = lines[end]
        # The `>` (or `/>`) that ends a multi-line opening tag sits level with it.
        if line.strip() and _indent(line) <= depth and line.strip() not in {">", "/>"}:
            break
        end += 1
    return lines[opening : end + 1]


def squeezed_date_fields(source: str) -> list[str]:
    """``line N: 'classes'`` for each all-widths grid holding a date or time input."""
    lines = source.splitlines()
    found: list[str] = []
    for number, line in enumerate(lines):
        for _, text in _QUOTED.findall(line):
            if not any(_UNPREFIXED_COLUMNS.match(token) for token in text.split()):
                continue
            if _DATE_OR_TIME.search("\n".join(_extent(lines, number))):
                found.append(f"line {number + 1}: {text!r}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_date_fields_it_polices() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    fields = sum(len(_DATE_OR_TIME.findall(path.read_text("utf-8"))) for path in _sources())
    assert fields > 40, f"found only {fields} date or time inputs; is the scan elsewhere?"


def test_no_date_or_time_field_shares_a_row_at_phone_width() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := squeezed_date_fields(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These grids keep two or more columns at every width and hold a date or time "
        "input, which a 320px dialog squeezes until the year or the picker is clipped:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nStack them below 640px: 'grid gap-3 sm:grid-cols-2'."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_date_field_in_an_all_widths_grid() -> None:
    source = "\n".join(
        [
            '<div className="grid grid-cols-2 gap-3">',
            "  <div>",
            '    <Label htmlFor="start">Planned start</Label>',
            '    <Input id="start" type="date" {...register("start")} />',
            "  </div>",
            "</div>",
            "<div",
            '  className="grid grid-cols-3 gap-4"',
            ">",
            "  <Input",
            '    id="end"',
            "    type={'time'}",
            "  />",
            "</div>",
        ]
    )
    assert squeezed_date_fields(source) == [
        "line 1: 'grid grid-cols-2 gap-3'",
        "line 8: 'grid grid-cols-3 gap-4'",
    ]


def test_the_rule_allows_a_phone_column_and_grids_without_dates() -> None:
    source = "\n".join(
        [
            '<div className="grid gap-3 sm:grid-cols-2">',
            '  <Input type="date" />',
            "</div>",
            '<div className="grid grid-cols-2 gap-3">',
            '  <Input type="number" />',
            "</div>",
            '<Input type="date" />',
            '<dl className="grid grid-cols-2 gap-3">',
            "  <dt>Due</dt>",
            "  <dd>{formatDate(order.due_date)}</dd>",
            "</dl>",
            '<Input type="time" />',
        ]
    )
    assert squeezed_date_fields(source) == []
