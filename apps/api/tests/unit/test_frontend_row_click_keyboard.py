"""A table row that opens its record on a click also offers a control that does.

``DataTable`` (``apps/web/src/components/data-table.tsx``) renders ``onRowClick``
as a ``<tr onClick>``, which is not in the tab order and has no key handler: a
keyboard or switch user cannot press a table row. So every screen that opens a
record from its row also makes a cell a real control for the same action - the
code as a ``<Link>`` to the same page, or a ``<button>`` calling the same
handler (``mrp-page.tsx``, ``purchase-returns-page.tsx``). Six tables shipped
without one, and on four of them (an RFQ, a ledger account, a routing, a
decided inspection) the row was the only way into the record from its list.

The rule reads each ``onRowClick={...}`` and asks the same file for the second
way in:

* ``navigate(`/x/${...}`)`` needs a ``to={`/x/${`` link, or an ``onClick`` /
  ``onSelect`` that navigates to the same prefix;
* any other handler needs an ``onClick`` / ``onSelect`` that calls the same
  function with something other than ``null`` - ``setDetailId(null)`` is the
  close button, not a way in - or passes it as is.

A row handler the rule cannot read is reported too, so a new shape is a
decision someone makes here rather than a silent pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: First arguments that close or clear rather than open a record.
_EMPTY_ARGUMENTS = frozenset({"", "null", "undefined", "false", "''", '""'})

_ROW_CLICK = re.compile(r"onRowClick=\{")
_CONTROL_HANDLER = re.compile(r"on(?:Click|Select)=\{")
_NAVIGATION = re.compile(r"navigate\(`([^`$]*)\$\{")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\(")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


def _closing_brace(text: str, opening: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``opening``.

    Strings and comments are skipped: a handler's comment saying "the row's
    click" would otherwise open a quote that never closes.
    """
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close == -1 else close + 2
            continue
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _expressions(source: str, pattern: re.Pattern[str]) -> list[tuple[int, int, str]]:
    """``(start, end, expression)`` for each ``attr={expression}`` the pattern opens."""
    found = []
    for match in pattern.finditer(source):
        end = _closing_brace(source, match.end() - 1)
        found.append((match.start(), end, source[match.end() : end - 1].strip()))
    return found


def _first_argument(expression: str, open_paren: int) -> str:
    """The first argument of the call whose ``(`` sits at ``open_paren``."""
    depth = 0
    for index in range(open_paren, len(expression)):
        char = expression[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return expression[open_paren + 1 : index].strip()
        elif char == "," and depth == 1:
            return expression[open_paren + 1 : index].strip()
    return expression[open_paren + 1 :].strip()


def _opens_with(expression: str, handler: str) -> bool:
    """Whether a control's handler expression opens a record through ``handler``."""
    if expression == handler:
        return True
    for call in re.finditer(rf"(?<![\w$.]){re.escape(handler)}\(", expression):
        if _first_argument(expression, call.end() - 1) not in _EMPTY_ARGUMENTS:
            return True
    return False


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def keyboardless_row_clicks(source: str) -> list[str]:
    """``line N: onRowClick={...}`` for each row click no control in the file repeats."""
    found: list[str] = []
    controls = _expressions(source, _CONTROL_HANDLER)
    for start, _end, expression in _expressions(source, _ROW_CLICK):
        label = f"line {_line(source, start)}: onRowClick={{{expression}}}"
        navigation = _NAVIGATION.search(expression)
        if navigation:
            prefix = re.escape(navigation.group(1))
            linked = re.search(rf"to=\{{`{prefix}\$\{{", source)
            navigated = any(
                re.search(rf"navigate\(`{prefix}\$\{{", control) for _, _, control in controls
            )
            if not (linked or navigated):
                found.append(label)
            continue
        calls = [name for name in _CALL.findall(expression) if name != "navigate"]
        handler = calls[0] if calls else expression if _IDENTIFIER.fullmatch(expression) else None
        if handler is None:
            found.append(f"{label} (handler not readable)")
        elif not any(_opens_with(control, handler) for _, _, control in controls):
            found.append(label)
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_row_clicks_it_polices() -> None:
    """A rename of the prop would otherwise leave this passing on nothing."""
    row_clicks = sum(len(_ROW_CLICK.findall(path.read_text("utf-8"))) for path in _sources())
    assert row_clicks > 20, f"found only {row_clicks} onRowClick props; is the scan elsewhere?"


def test_every_row_click_has_a_control_for_the_keyboard() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := keyboardless_row_clicks(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These tables open a record only from a pointer click on the <tr>:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nMake a cell a real control for the same action - the code as a <Link> to "
        "the same page, or a <button type='button'> calling the same handler after "
        "event.stopPropagation() - as mrp-page.tsx and purchase-returns-page.tsx do."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_row_that_is_the_only_way_in() -> None:
    source = (
        "const [detailId, setDetailId] = useState(null)\n"
        "cell: ({ row }) => <span>{row.original.code}</span>,\n"
        "<DataTable onRowClick={(row) => setDetailId(row.id)} />\n"
    )
    assert keyboardless_row_clicks(source) == ["line 3: onRowClick={(row) => setDetailId(row.id)}"]


def test_the_rule_does_not_count_the_close_button_as_a_way_in() -> None:
    source = (
        "<DataTable onRowClick={(row) => setDetailId(row.id)} />\n"
        "<Button onClick={() => setDetailId(null)}>Close</Button>\n"
    )
    assert len(keyboardless_row_clicks(source)) == 1


def test_the_rule_accepts_a_cell_button_calling_the_same_handler() -> None:
    source = (
        "cell: ({ row }) => (\n"
        '  <button type="button" onClick={(event) => {\n'
        "    event.stopPropagation()\n"
        "    setDetailId(row.original.id)\n"
        "  }}>{row.original.code}</button>\n"
        "),\n"
        "<DataTable onRowClick={(row) => setDetailId(row.id)} />\n"
        "<DataTable onRowClick={onOpen} />\n"
        "<DropdownMenuItem onSelect={() => onOpen(row.original)}>Open</DropdownMenuItem>\n"
    )
    assert keyboardless_row_clicks(source) == []


def test_the_rule_wants_a_link_to_the_page_a_row_navigates_to() -> None:
    linked = (
        "cell: ({ row }) => <Link to={`/mrp/runs/${row.original.id}`}>{row.original.code}</Link>,\n"
        "<DataTable onRowClick={(row) => navigate(`/mrp/runs/${row.id}`)} />\n"
    )
    assert keyboardless_row_clicks(linked) == []

    elsewhere = (
        "<Link to={`/items/${row.original.item_id}`}>Open item</Link>\n"
        "<DataTable onRowClick={(row) => navigate(`/procurement/rfqs/${row.id}`)} />\n"
    )
    assert keyboardless_row_clicks(elsewhere) == [
        "line 2: onRowClick={(row) => navigate(`/procurement/rfqs/${row.id}`)}"
    ]


def test_the_rule_reports_a_row_handler_it_cannot_read() -> None:
    source = "<DataTable onRowClick={handlers[kind]} />\n"
    assert keyboardless_row_clicks(source) == [
        "line 1: onRowClick={handlers[kind]} (handler not readable)"
    ]
