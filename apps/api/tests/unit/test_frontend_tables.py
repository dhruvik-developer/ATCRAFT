"""A table scrolls in a named, focusable region, and draws its cells from the shared parts.

Outside DataTable, thirty tables re-spelled their header and cell classes a
dozen ways. Six left money and quantities left-aligned in proportional figures -
a dealer's statement, a vendor's job-work quantities - and eighteen scrolled
sideways in a plain ``div``, which Safari and Chrome before 130 do not make
focusable, so a keyboard user could not reach the right-hand columns: the
amounts, totals and statuses. ``components/ui/table.tsx`` holds the parts
DataTable itself renders with, and ``TableScroller``, the named region.

The rules read ``apps/web/src`` outside ``components/``, where DataTable, the
parts and the charts' screen-reader table live:

1. Every ``<table>`` or ``<Table>`` has a ``TableScroller`` among its ancestors.
   Prettier writes an element's children indented under it, so the ancestors
   are the opening tags met walking up the file at ever smaller indentation.
2. A raw ``<table>`` is written only by the screens in ``OWN_CELLS``, each a
   matrix, board or editable grid whose cells are its own layout; every other
   table is ``<Table>`` with ``TableHead`` and ``TableCell``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
COMPONENTS = WEB_SRC / "components"

#: Screens whose table is a layout of its own rather than rows of figures.
OWN_CELLS = {
    "features/bom/bom-tree.tsx": "an indented component tree; the first cell draws the level",
    "features/finance/payment-dialogs.tsx": "the allocation editor, an amount input on every row",
    "features/notifications/notifications-page.tsx": "a matrix of channel switches by event",
    "features/packing/packing-order-dialog.tsx": "a carton's contents, a remove control per row",
    "features/procurement/rfq-comparison-panel.tsx": "a vendor-by-item comparison matrix",
    "features/production/planning-page.tsx": "the capacity strip and timeline, first column sticky",
    "features/shifts/shift-roster-page.tsx": "the day and week roster boards",
}

_TABLE = re.compile(r"<(?P<tag>table|Table)(?=[\s>])")
_OPENING = re.compile(r"^\s*<(?P<tag>[A-Za-z][\w.]*)")
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _ancestors(lines: list[str], at: int) -> list[str]:
    """The tag names of the elements enclosing line ``at``, innermost first."""
    found: list[str] = []
    limit = _indent(lines[at])
    for line in reversed(lines[:at]):
        if not line.strip() or _indent(line) >= limit:
            continue
        opening = _OPENING.match(line)
        if opening:
            found.append(opening["tag"])
            limit = _indent(line)
            if limit == 0:
                break
    return found


def unscrolled_tables(source: str) -> list[str]:
    """``line N: <tag>`` for each table with no TableScroller around it."""
    lines = source.splitlines()
    return [
        f"line {number + 1}: <{match['tag']}>"
        for number, line in enumerate(lines)
        if not _COMMENT_LINE.match(line)
        for match in _TABLE.finditer(line)
        if "TableScroller" not in _ancestors(lines, number)
    ]


def raw_tables(source: str) -> list[str]:
    """``line N`` for each raw ``<table>`` the source writes."""
    return [
        f"line {number}"
        for number, line in enumerate(source.splitlines(), start=1)
        if not _COMMENT_LINE.match(line) and re.search(r"<table(?=[\s>])", line)
    ]


def _screens() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts
        and ".test." not in path.name
        and not path.is_relative_to(COMPONENTS)
    )


def _name(path: Path) -> str:
    return path.relative_to(WEB_SRC).as_posix()


def test_the_scan_reaches_the_tables_it_polices() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    tables = sum(len(_TABLE.findall(path.read_text("utf-8"))) for path in _screens())
    assert tables >= 25, f"found only {tables} tables; is the scan elsewhere?"


def test_every_table_scrolls_in_a_named_region() -> None:
    offenders = {
        _name(path): found
        for path in _screens()
        if (found := unscrolled_tables(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These tables scroll, if at all, in a wrapper a keyboard cannot reach:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nWrap each in <TableScroller label=...> from components/ui/table.tsx, named "
        "as its caption or heading names it."
    )


def test_only_a_screen_drawing_its_own_cells_writes_a_raw_table() -> None:
    offenders = {
        _name(path): found
        for path in _screens()
        if _name(path) not in OWN_CELLS and (found := raw_tables(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These screens write a raw <table>, whose header and cells then drift from every "
        "other table's:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nUse Table, TableHeader, TableBody, TableRow, TableHead and TableCell from "
        "components/ui/table.tsx (align='right' for money and quantities)."
    )


@pytest.mark.parametrize("name", sorted(OWN_CELLS))
def test_each_exemption_still_draws_its_own_table(name: str) -> None:
    """An exemption that no longer applies would hide the next raw table written there."""
    assert raw_tables((WEB_SRC / name).read_text(encoding="utf-8")), (
        f"{name} no longer writes a raw <table>; remove it from OWN_CELLS"
    )


# -- the rules reject what they exist to catch --------------------------------


def test_the_rules_reject_a_plain_scroller_and_a_hand_drawn_table() -> None:
    source = "\n".join(
        [
            "export function Statement() {",
            "  return (",
            '    <div className="overflow-x-auto">',
            '      <table className="w-full text-sm">',
            "        <thead>",
            '          <tr><th className="p-2 text-left">Debit</th></tr>',
            "        </thead>",
            "      </table>",
            "    </div>",
            "  )",
            "}",
            "function Lines() {",
            "  return (",
            '    <Card className="p-3">',
            "      <Table>",
            "        <TableBody />",
            "      </Table>",
            "    </Card>",
            "  )",
            "}",
        ]
    )
    assert unscrolled_tables(source) == ["line 4: <table>", "line 15: <Table>"]
    assert raw_tables(source) == ["line 4"]


def test_the_rules_accept_a_table_in_a_scroller_however_deep() -> None:
    source = "\n".join(
        [
            "export function Statement() {",
            "  return (",
            "    <TableScroller",
            '      label="Account statement"',
            '      className="mt-4"',
            "    >",
            "      {rows.length > 0 && (",
            "        <Table>",
            '          <caption className="sr-only">Account statement</caption>',
            "        </Table>",
            "      )}",
            "    </TableScroller>",
            "  )",
            "}",
            "// A <table> in a comment is history, not markup.",
        ]
    )
    assert unscrolled_tables(source) == []
    assert raw_tables(source) == []
