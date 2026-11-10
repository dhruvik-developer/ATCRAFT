"""A screen's button is ``Button``, ``CellButton`` or a ``FilterChip``'s remover.

Twenty cells that open a row's record were raw ``<button>`` elements in eleven
class strings - bold or not, underlined on hover or always, a focus ring or
none - and one had no type, so it submitted whatever form held its table. Four
filter chips were drawn four ways, and the shell's menu and account triggers
copied the ghost ``Button`` beside them by hand. The shared controls are
``Button`` (``components/ui/button.tsx``), ``CellButton`` for a cell that opens or
acts on its row (``components/data-table.tsx``) and ``FilterChip`` for a filter
the list is narrowed by (``components/shared.tsx``).

The rule counts the raw ``<button>`` elements each file under
``apps/web/src/features`` and the shell under ``apps/web/src/app`` writes - the
sidebar's close and collapse controls were still drawn by hand there, out of the
scan's sight, after the shell's other triggers became Buttons. A few are
surfaces of their own - a card that opens a record, a control drawn inside a
text field - and ``OWN_SURFACES`` lists each with how many it holds and why; any
other file writing one fails, and so does a listed file writing more than it is
allowed. Comment lines are skipped. ``components/`` is not scanned: it is where
the shared controls themselves are drawn.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
#: The screens, and the shell that frames them.
SCANNED = (WEB_SRC / "features", WEB_SRC / "app")

#: screen -> (raw buttons it may write, what they are).
OWN_SURFACES = {
    "features/admin/permission-matrix.tsx": (1, "the filter's clear control, inside the field"),
    "features/admin/users-page.tsx": (1, "a role badge that opens what the role grants"),
    "features/assembly/assembly-board.tsx": (1, "an order card that opens the order"),
    "features/auth/login-page.tsx": (1, "the show-password toggle, inside the field"),
    "features/finance/accounts-page.tsx": (1, "the parent-account chip's clear control"),
    "features/mrp/mrp-run-detail-page.tsx": (1, "the disclosure of a requirement's origins"),
    "features/production/planning-page.tsx": (1, "an operation's block on the timeline"),
    "features/quality/inspection-attachments.tsx": (1, "a photo's thumbnail, which downloads it"),
    "features/quality/inspection-dialog.tsx": (1, "the attached photo's remove control"),
    "features/reports/reports-page.tsx": (1, "a report card that opens the report"),
}

_BUTTON = re.compile(r"<button(?=[\s>]|$)")
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")


def raw_buttons(source: str) -> list[int]:
    """The line of each raw ``<button>`` the source writes."""
    return [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if not _COMMENT_LINE.match(line)
        for _ in _BUTTON.finditer(line)
    ]


def _screens() -> list[Path]:
    return sorted(
        path
        for root in SCANNED
        for path in root.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def _name(path: Path) -> str:
    return path.relative_to(WEB_SRC).as_posix()


def test_the_scan_reaches_the_shared_controls() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    cells = sum(path.read_text("utf-8").count("<CellButton") for path in _screens())
    assert cells >= 20, f"found only {cells} CellButton uses; is the scan elsewhere?"


def test_the_scan_reaches_the_shell_and_not_its_tests() -> None:
    """The shell is a screen too; its tests draw throwaway buttons on purpose."""
    names = {_name(path) for path in _screens()}
    assert "app/app-layout.tsx" in names
    assert "app/theme-context.test.tsx" not in names
    assert not any(name.startswith("components/") for name in names)


def test_no_screen_writes_a_button_by_hand() -> None:
    offenders = {}
    for path in _screens():
        found = raw_buttons(path.read_text(encoding="utf-8"))
        allowed = OWN_SURFACES.get(_name(path), (0, ""))[0]
        if len(found) > allowed:
            offenders[_name(path)] = found
    assert not offenders, (
        "These screens write a raw <button> (lines listed):\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nUse Button from components/ui/button.tsx, CellButton from "
        "components/data-table.tsx for a cell that opens its row, or FilterChip from "
        "components/shared.tsx. A surface of its own goes in OWN_SURFACES with why."
    )


@pytest.mark.parametrize("name", sorted(OWN_SURFACES))
def test_each_allowance_is_still_used(name: str) -> None:
    """A spare allowance would let the next hand-written button in unnoticed."""
    found = raw_buttons((WEB_SRC / name).read_text(encoding="utf-8"))
    assert len(found) == OWN_SURFACES[name][0], (
        f"{name} writes {len(found)} raw buttons; set its OWN_SURFACES count to match"
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_counts_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "cell: ({ row }) => (",
            '  <button className="font-medium hover:underline" onClick={() => open(row)}>',
            "    {row.original.code}",
            "  </button>",
            "),",
            "<button",
            '  type="button"',
            '  className="rounded-md p-1.5 hover:bg-accent lg:hidden"',
            ">",
            "<DialogPrimitive.Close asChild>",
            '  <button type="button" aria-label="Close navigation">',
        ]
    )
    assert raw_buttons(source) == [2, 6, 11]


def test_the_rule_ignores_the_shared_controls_and_history() -> None:
    source = "\n".join(
        [
            "<CellButton onClick={() => open(row)}>{row.original.code}</CellButton>",
            '<Button variant="ghost" size="icon-sm" aria-label="Open navigation">',
            "// It was a raw <button> before, in eleven class strings.",
            '<FilterChip label="One goods receipt" removeLabel="Clear" onRemove={clear} />',
        ]
    )
    assert raw_buttons(source) == []
