"""A missing value is shown as one mark on every screen.

``apps/web/src/lib/format.ts`` exports ``EMPTY_VALUE`` (``-``): every
formatter returns it for an absent value and ``DescriptionList`` renders it for
an empty description. Twenty-eight feature cells across twelve files printed an
em dash instead, so a blank GSTIN, tracking id or reason read as ``-`` in one
panel and as an em dash in the next - inside the dealer portal, on the same
screen - and a test or a person looking for the blank found only one of them.

The rule is narrow on purpose: a string literal or a JSX text node that is
*only* an em dash, which is the placeholder spelling. An em dash used as
punctuation inside a sentence or between two values is untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
FORMAT = WEB_SRC / "lib" / "format.ts"
#: Generated from the API's OpenAPI document; its descriptions are not screens.
GENERATED = WEB_SRC / "types" / "openapi.ts"

_EM_DASH = "\u2014"
#: A quoted literal - single, double or backtick quotes - that is nothing but the dash.
_QUOTED = re.compile(rf"(['\"`])[ \t]*{_EM_DASH}[ \t]*\1")
#: JSX text that is nothing but the dash: <span>\u2014</span>.
_JSX_TEXT = re.compile(rf">\s*{_EM_DASH}\s*<")


def em_dash_placeholders(source: str) -> list[str]:
    """``line N`` for every em-dash placeholder outside a comment line."""
    found: set[int] = set()
    for pattern in (_QUOTED, _JSX_TEXT):
        for match in pattern.finditer(source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            if source[line_start:].lstrip().startswith(("//", "*", "/*")):
                continue
            found.add(source.count("\n", 0, match.start()) + 1)
    return [f"line {number}" for number in sorted(found)]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path != GENERATED
    )


def test_the_shared_blank_is_where_the_screens_import_it_from() -> None:
    """If this moves, the guard below would pass while the screens drift."""
    assert FORMAT.is_file(), f"{FORMAT} is gone; it owns how a blank value is shown"
    assert "export const EMPTY_VALUE = '-'" in FORMAT.read_text(encoding="utf-8")
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_screen_spells_a_blank_as_an_em_dash() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := em_dash_placeholders(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "A missing value is printed as an em dash:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in offenders.items())
        + "\n\nUse EMPTY_VALUE from '@/lib/format' - the mark every formatter and "
        "DescriptionList already show - so a blank reads the same on every screen."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_quoted_and_jsx_placeholders() -> None:
    source = (
        "cell: ({ row }) => row.original.reason ?? '\u2014',\n"
        '{ label: "HSN", value: item.hsn_code || "\u2014" },\n'
        'return <span className="text-muted-foreground">\u2014</span>\n'
        "const blank = ` \u2014 `\n"
        "<td>\n  \u2014\n</td>\n"
    )
    assert em_dash_placeholders(source) == ["line 1", "line 2", "line 3", "line 4", "line 5"]


def test_the_rule_ignores_punctuation_and_comments() -> None:
    source = (
        "<CardTitle>Webhook secret \u2014 copy it now</CardTitle>\n"
        "{vendor.code} \u2014 {vendor.name}\n"
        "// never print '\u2014' for a blank\n"
        " * the old spelling was '\u2014'\n"
        "value: item.hsn_code || EMPTY_VALUE,\n"
    )
    assert em_dash_placeholders(source) == []
