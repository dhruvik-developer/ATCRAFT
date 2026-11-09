"""A listbox in the web app is the shared search picker's.

Thirteen pickers - customer, vendor, item, bin, batch, dispatch, job-work order,
purchase order, goods receipt, RFQ, requisition - each wrote a
``role="listbox"`` of ``role="option"`` buttons by hand, and none implemented
what those roles announce. A listbox is one Tab stop whose rows the arrow keys
move through, with Home and End, Enter to choose and Escape to close; these had
every option as its own Tab stop, arrow keys that did nothing, and
"Searching..." and "No match" sitting inside the listbox among the options. The
copies had drifted apart as well ("Loading bins...", a skeleton).

``components/ui/search-picker.tsx`` is that keyboard model written once, with
the status said in a ``role="status"`` region beside the list, 44px rows and a
popover that fits a 320px screen, and every server-search picker renders it.
So the two roles are spelled there and nowhere else: a new search renders
``SearchPicker``, and a fixed set of choices is a ``Select``, whose listbox
Radix writes itself. Comment lines are skipped, so the history can still be
told beside the code it happened in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: The one file that implements the listbox keyboard model.
HOME = WEB_SRC / "components" / "ui" / "search-picker.tsx"

#: ``role="listbox"``, ``role='option'``, ``role={'listbox'}`` and a role chosen
#: in an expression, ``role={open ? 'listbox' : undefined}``.
_ROLE = re.compile(
    r"""role=(?:(["'])(?P<plain>listbox|option)\1"""
    r"""|\{[^}]*?(["'`])(?P<expr>listbox|option)\3[^}]*\})"""
)
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")


def hand_rolled_listboxes(source: str) -> list[str]:
    """``line N: role=R`` for each listbox or option role the source writes."""
    found: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if _COMMENT_LINE.match(line):
            continue
        found.extend(
            f"line {number}: role={match['plain'] or match['expr']}"
            for match in _ROLE.finditer(line)
        )
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.tsx", "*.ts")
        for path in WEB_SRC.rglob(pattern)
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_shared_picker_is_where_the_listbox_lives() -> None:
    """A move or rename would otherwise leave the exemption pointing at nothing."""
    source = HOME.read_text(encoding="utf-8")
    roles = {line.rsplit("=", 1)[1] for line in hand_rolled_listboxes(source)}
    assert roles == {"listbox", "option"}, f"{HOME.name} no longer writes both roles: {roles}"
    # The roles are a promise of the arrow keys; the picker is what keeps it.
    for key in ("ArrowDown", "ArrowUp", "Home", "End"):
        assert f"'{key}'" in source, f"{HOME.name} no longer handles {key}"


def test_no_other_file_writes_a_listbox_by_hand() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != HOME and (found := hand_rolled_listboxes(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These files write a listbox by hand, which announces a keyboard model "
        "(one Tab stop, the arrow keys, Home/End, Enter, Escape) they would have to "
        "implement themselves:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nRender SearchPicker from components/ui/search-picker.tsx for a search on the "
        "server, or Select from components/ui/select.tsx for a fixed set of choices."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_listbox_however_it_is_written() -> None:
    source = (
        '<div className="max-h-64 overflow-y-auto p-1" role="listbox" aria-label="Customers">\n'
        "  <button type=\"button\" role='option' aria-selected={chosen}>\n"
        "<li role={'option'}>\n"
        "<div role={open ? 'listbox' : undefined}>\n"
        '<ul role={"listbox"}><li role="option">One</li></ul>\n'
    )
    assert hand_rolled_listboxes(source) == [
        "line 1: role=listbox",
        "line 2: role=option",
        "line 3: role=option",
        "line 4: role=listbox",
        "line 5: role=listbox",
        "line 5: role=option",
    ]


def test_the_rule_ignores_what_only_mentions_one() -> None:
    source = (
        '// Each picker wrote a role="listbox" of role="option" buttons by hand.\n'
        ' * out by hand: a popover, a search box, a `role="listbox"` of `role="option"`\n'
        '{/* It was a role="listbox" of buttons before. */}\n'
        "screen.getByRole('listbox', { name: 'Batches' })\n"
        '<SelectTrigger aria-haspopup="listbox" aria-controls={listboxId}>\n'
        '<p role="status" className="text-sm">\n'
        '<div role="group" aria-label="Options">\n'
    )
    assert hand_rolled_listboxes(source) == []
