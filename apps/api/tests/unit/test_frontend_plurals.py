"""A count is written with its noun in the right number, never as "line(s)".

``apps/web/src/lib/format.ts`` has ``plural(count, one, many?)``: "1 line",
"3 lines", "2 boxes". Before it, 45 strings a person reads printed the noun
with "(s)" - a toast said "1 line(s) are short and need planning.", a button
read "Register 1 serial(s)" - while other screens on the same pages pluralised
properly, so one count was phrased two ways on one screen.

The rule fails on ``<letter>(s)`` outside a comment anywhere in the product
source under ``apps/web/src``. A test is free to say "expected 2 request(s)" in
an assertion message nobody but a developer reads.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_frontend_mutation_error_toasts import WEB_SRC, _line

pytestmark = pytest.mark.unit

_PAREN_PLURAL = re.compile(r"[a-z]\(s\)")
_COMMENT_LINE = ("//", "*", "/*", "{/*")


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
            "`${lines.length} line${lines.length === 1 ? '' : 's'}`",
        ]
    )

    assert paren_plurals(source) == []
