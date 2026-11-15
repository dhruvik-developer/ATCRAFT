"""A count is written with its noun in the right number, never as "line(s)".

``apps/web/src/lib/format.ts`` has ``plural(count, one, many?)``: "1 line",
"3 lines", "2 boxes". Before it, 45 strings a person reads printed the noun
with "(s)" - a toast said "1 line(s) are short and need planning.", a button
read "Register 1 serial(s)" - while other screens on the same pages pluralised
properly, so one count was phrased two ways on one screen.

The rule fails on ``<letter>(s)`` outside a comment anywhere in the product
source under ``apps/web/src``. A test is free to say "expected 2 request(s)" in
an assertion message nobody but a developer reads.

The second rule fails on the count spelled by hand, which forty-odd strings did
after ``plural`` existed: a ternary on a count of one that picks the noun's
ending (``day{n === 1 ? '' : 's'}``, ``line{n === 1 ? ' is' : 's are'}``,
``sub-assembl{n === 1 ? 'y was' : 'ies were'}``), that writes the count into
the literal (``n === 1 ? '1 line' : `${n} lines```), or that picks a whole word
right after printing the count (``{n} {n === 1 ? 'account' : 'accounts'}``).
They printed 1234 where every other count reads 1,234, and one said "1 shortage
on this page have". A word chosen by the count with no count printed beside it -
"Line 3" against "Lines 3, 5" - is not a count, and is left alone.
"""

from __future__ import annotations

import re

import pytest

from tests.frontend_scan_support import WEB_SRC, _line, _skip_string, blank_comments

pytestmark = pytest.mark.unit

_PAREN_PLURAL = re.compile(r"[a-z]\(s\)")
_COMMENT_LINE = ("//", "*", "/*", "{/*")

#: A comparison of a count with one, then the ``?`` of its ternary.
_COUNT_OF_ONE = re.compile(r"(?P<count>[\w$.?\[\]()]+?)\s*(?P<op>===|!==)\s*1\s*\?\s*")
_SPACE = " \t\n"
_QUOTES = "'\"`"


def _commented(source: str, offset: int) -> bool:
    """Whether ``offset`` sits in a comment line or after a trailing ``//`` comment."""
    line_start = source.rfind("\n", 0, offset) + 1
    before = source[line_start:offset]
    return before.lstrip().startswith(_COMMENT_LINE) or " //" in before


def paren_plurals(source: str) -> list[str]:
    """``line N: 'line text'`` for every "(s)" plural outside a comment."""
    found = []
    for match in _PAREN_PLURAL.finditer(source):
        if _commented(source, match.start()):
            continue
        line_end = source.find("\n", match.start())
        line_start = source.rfind("\n", 0, match.start()) + 1
        text = source[line_start : len(source) if line_end == -1 else line_end].strip()
        found.append(f"line {_line(source, match.start())}: {text!r}")
    return found


def _literal_at(code: str, index: int) -> tuple[str, int] | None:
    """``(text, end)`` of the string literal starting at ``index`` past any space."""
    while index < len(code) and code[index] in _SPACE:
        index += 1
    if index >= len(code) or code[index] not in _QUOTES:
        return None
    end = _skip_string(code, index)
    return code[index + 1 : end - 1], end


def _ending(one: str, many: str) -> bool:
    """Whether two branches differ only by the ending of a word written before them."""
    return (
        (one == "" and many in {"s", "es"})
        or (one.startswith(" ") and many.startswith(("s ", "es ")))
        or (one.startswith("y") and many.startswith("ies"))
    )


def _counted_word(code: str, start: int, count: str, one: str, many: str) -> bool:
    """Whether a whole word is chosen right after the same count is printed."""
    word = many in {f"{one}s", f"{one}es"} or (one.endswith("y") and many == f"{one[:-1]}ies")
    printed = re.search(
        rf"{re.escape(count)}\)?\s*\}}\s*\$?\{{\s*$", code[max(0, start - 80) : start]
    )
    return word and printed is not None


def hand_written_plurals(source: str) -> list[str]:
    """``line N: 'ternary'`` for every count whose noun is put in number by hand."""
    code = blank_comments(source)
    found = []
    for match in _COUNT_OF_ONE.finditer(code):
        first = _literal_at(code, match.end())
        if first is None:
            continue
        colon = code.find(":", first[1])
        if colon == -1 or code[first[1] : colon].strip():
            continue
        second = _literal_at(code, colon + 1)
        if second is None:
            continue
        one, many = (first[0], second[0]) if match["op"] == "===" else (second[0], first[0])
        if (
            one.startswith("1 ")
            or _ending(one, many)
            or _counted_word(code, match.start(), match["count"], one, many)
        ):
            found.append(f"line {_line(code, match.start())}: {code[match.start() : second[1]]!r}")
    return found


def _sources() -> list:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and not path.name.endswith((".test.ts", ".test.tsx"))
        and path.relative_to(WEB_SRC).parts[0] != "test"
    )


def test_the_helper_the_rule_points_to_exists() -> None:
    assert "export function plural(" in (WEB_SRC / "lib" / "format.ts").read_text("utf-8")


def test_no_screen_writes_a_count_with_an_s_in_brackets() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := paren_plurals(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These strings write a noun as '(s)'; use plural(count, 'line') from "
        f"lib/format.ts so one reads '1 line' and three read '3 lines':\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "toast.success(success.created('Serials'), `${created.length} unit(s)`)",
            "            Register {numbers.length || ''} serial(s)",
            "          ' line(s) recorded on ' +",
        ]
    )

    assert paren_plurals(source) == [
        "line 1: \"toast.success(success.created('Serials'), `${created.length} unit(s)`)\"",
        "line 2: \"Register {numbers.length || ''} serial(s)\"",
        "line 3: \"' line(s) recorded on ' +\"",
    ]


def test_the_rule_ignores_comments_and_a_proper_plural() -> None:
    source = "\n".join(
        [
            "// The API counts the line(s) it wrote.",
            " * which line(s) went short is the API's answer",
            "  {/* one entry per serial(s) chosen */}",
            "const label = plural(count, 'line') // not 'line(s)'",
            "`${plural(lines.length, 'line')} on this bill`",
        ]
    )

    assert paren_plurals(source) == []


def test_no_screen_puts_a_count_in_number_by_hand() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := hand_written_plurals(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These put a count's noun in number with a ternary; use plural(count, 'line') - "
        "or plural(count, 'line is', 'lines are') for the verb too - from lib/format.ts, "
        f"so every count is grouped and worded one way:\n{offenders}"
    )


def test_the_hand_written_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "      {days} day{days === 1 ? '' : 's'} late",
            "          {short.length} line{short.length === 1 ? ' is' : 's are'} short.",
            "            {codes.length === 1 ? 'y was' : 'ies were'} passed through",
            "        {orderCount === 1 ? '1 purchase order' : `${orderCount} purchase orders`}",
            "          {rows.length} {rows.length === 1 ? 'account' : 'accounts'}",
            "            : `${rows.length} ${rows.length !== 1 ? 'postings' : 'posting'} loaded`}",
            "  `${marked} notification${marked !== 1 ? 's' : ''} marked as read.`",
        ]
    )

    assert [found.split(":")[0] for found in hand_written_plurals(source)] == [
        "line 1",
        "line 2",
        "line 3",
        "line 4",
        "line 5",
        "line 6",
        "line 7",
    ]


def test_the_hand_written_rule_leaves_a_word_with_no_count_and_comments_alone() -> None:
    source = "\n".join(
        [
            "    `${short.length === 1 ? 'Line' : 'Lines'} ${short.join(', ')} went out short.`",
            "          {documents === 1 ? 'covers' : 'cover'} the shortfall",
            "  // {days} day{days === 1 ? '' : 's'} late, before plural() existed",
            "      {plural(days, 'day')} late",
            "  const only = blocks.length === 1 ? blocks[0] : undefined",
            "    warehouseId: warehouseIds.length === 1 ? warehouseIds[0] : undefined,",
        ]
    )

    assert hand_written_plurals(source) == []
