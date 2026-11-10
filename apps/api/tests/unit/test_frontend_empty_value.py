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

The same mark typed as a literal hyphen is the second half. About two hundred
cells, fallbacks and description values spelled ``'-'`` or ``<span>-</span>``
beside some seventy that imported ``EMPTY_VALUE``, so the constant no longer
decided how a blank reads: changing it would have put two marks on one screen
again. The hyphen rule refuses a lone ``-`` only where it is a value - JSX
text, a nullish or falsy fallback, a ternary branch, a property value, a
``return``, an arrow body or a JSX expression. A hyphen anywhere else is
something else and is left alone: a separator (``join(' - ')``, ``split('-')``,
``code + ' - ' + name``), a sign (``inflow ? '+' : '-'``), a type
(``'-' | '+'``), an input's ``placeholder="-"``, or decoration between two
fields that a screen reader is told to skip (``<span aria-hidden>-</span>``
between the two dates of a range).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.test_frontend_copy_internals import blank_comments

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
FORMAT = WEB_SRC / "lib" / "format.ts"
#: Generated from the API's OpenAPI document; its descriptions are not screens.
GENERATED = WEB_SRC / "types" / "openapi.ts"

_EM_DASH = "—"
#: A quoted literal - single, double or backtick quotes - that is nothing but the dash.
_QUOTED = re.compile(rf"(['\"`])[ \t]*{_EM_DASH}[ \t]*\1")
#: JSX text that is nothing but the dash: <span>—</span>.
_JSX_TEXT = re.compile(rf">\s*{_EM_DASH}\s*<")

#: A literal that is exactly one hyphen. With spaces round it (``' - '``) it is
#: a separator between two values, never a blank.
_HYPHEN_LITERAL = re.compile(r"""(['"`])-\1""")
#: What a value - and so a blank - follows: a nullish or falsy fallback, either
#: ternary branch or a property value, a ``return``, an arrow body, or a JSX
#: expression; with the parenthesis prettier wraps round a JSX ternary branch.
_VALUE_LEAD = re.compile(r"(?:\?\?|\|\||\?|:|\breturn|=>|\{)\s*\(?\s*$")
#: A sign, not a blank: ``inflow ? '+' : '-'`` and ``negative ? '-' : ''``.
_SIGN_THEN = re.compile(r"""(['"`])\+?\1\s*:\s*$""")
_SIGN_ELSE = re.compile(r"""^\s*:\s*(['"`])\+?\1""")
#: A type, not a value: ``sign: '-' | '+'``.
_UNION_NEXT = re.compile(r"^\s*\|(?!\|)")
#: JSX text that is nothing but a hyphen: ``<span>-</span>``.
_JSX_HYPHEN = re.compile(r">\s*-\s*<")


def _on_comment_line(source: str, offset: int) -> bool:
    line_start = source.rfind("\n", 0, offset) + 1
    return source[line_start:].lstrip().startswith(("//", "*", "/*"))


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def em_dash_placeholders(source: str) -> list[str]:
    """``line N`` for every em-dash placeholder outside a comment line."""
    found: set[int] = set()
    for pattern in (_QUOTED, _JSX_TEXT):
        for match in pattern.finditer(source):
            if _on_comment_line(source, match.start()):
                continue
            found.add(_line_of(source, match.start()))
    return [f"line {number}" for number in sorted(found)]


def hyphen_placeholder_spans(source: str) -> Iterator[tuple[int, int, str]]:
    """``(start, end, kind)`` of each lone hyphen shown as a value.

    ``kind`` is ``"literal"`` for the quoted ``'-'`` itself, or ``"jsx"`` for
    the hyphen of a JSX text node (``start`` and ``end`` span the text between
    ``>`` and ``<``).
    """
    code = blank_comments(source)
    for match in _HYPHEN_LITERAL.finditer(code):
        start, end = match.span()
        if _on_comment_line(code, start):
            continue
        # A lead and its sign partner sit within a few lines of the mark.
        before, after = code[max(0, start - 240) : start], code[end : end + 240]
        lead = _VALUE_LEAD.search(before)
        if lead is None:
            continue
        if lead.group(0).startswith("{") and not after.lstrip().startswith("}"):
            continue  # an object key, `{ '-': ... }`, not a JSX expression
        if _UNION_NEXT.match(after):
            continue
        token = lead.group(0)
        if token.startswith("?") and not token.startswith("??") and _SIGN_ELSE.match(after):
            continue
        if token.startswith(":") and _SIGN_THEN.search(before[: lead.start() + 1]):
            continue
        yield start, end, "literal"
    for match in _JSX_HYPHEN.finditer(code):
        if _on_comment_line(code, match.start()):
            continue
        tag_start = code.rfind("<", 0, match.start() + 1)
        if "aria-hidden" in code[tag_start : match.start() + 1]:
            continue  # decoration between two fields, not a value
        yield match.start() + 1, match.end() - 1, "jsx"


def hyphen_placeholders(source: str) -> list[str]:
    """``line N`` for every lone hyphen a screen shows as a blank value."""
    lines = {_line_of(source, start) for start, _end, _kind in hyphen_placeholder_spans(source)}
    return [f"line {number}" for number in sorted(lines)]


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


def test_no_screen_spells_a_blank_as_a_literal_hyphen() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != FORMAT and (found := hyphen_placeholders(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "A missing value is typed as a literal hyphen instead of EMPTY_VALUE:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in offenders.items())
        + "\n\nUse EMPTY_VALUE from '@/lib/format' (`?? EMPTY_VALUE`, "
        "`<span>{EMPTY_VALUE}</span>`), so the one constant decides how a blank reads."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_quoted_and_jsx_placeholders() -> None:
    source = (
        "cell: ({ row }) => row.original.reason ?? '—',\n"
        '{ label: "HSN", value: item.hsn_code || "—" },\n'
        'return <span className="text-muted-foreground">—</span>\n'
        "const blank = ` — `\n"
        "<td>\n  —\n</td>\n"
    )
    assert em_dash_placeholders(source) == ["line 1", "line 2", "line 3", "line 4", "line 5"]


def test_the_rule_ignores_punctuation_and_comments() -> None:
    source = (
        "<CardTitle>Webhook secret — copy it now</CardTitle>\n"
        "{vendor.code} — {vendor.name}\n"
        "// never print '—' for a blank\n"
        " * the old spelling was '—'\n"
        "value: item.hsn_code || EMPTY_VALUE,\n"
    )
    assert em_dash_placeholders(source) == []


def test_the_hyphen_rule_finds_every_shape_a_blank_shipped_in() -> None:
    source = "\n".join(
        [
            "  { label: 'GSTIN', value: record.gstin ?? '-' },",
            "        {row.original.reason || '-'}",
            "    value: data ? formatMoney(data.total) : '-',",
            "  standard_cost === undefined ? '-' : formatMoney(item.standard_cost)",
            "  if (ms === null) return '-'",
            "  cell: () => '-',",
            '  <TableCell>{"-"}</TableCell>',
            '  if (!status) return <span className="text-muted-foreground">-</span>',
            "  value: toDecimal(order.rejected_quantity).isZero() ? (",
            "    '-'",
            "  ) : (",
            "  <td>",
            "    -",
            "  </td>",
        ]
    )
    assert hyphen_placeholders(source) == [
        f"line {number}" for number in (1, 2, 3, 4, 5, 6, 7, 8, 10, 12)
    ]


def test_the_hyphen_rule_leaves_separators_signs_types_and_decoration_alone() -> None:
    source = "\n".join(
        [
            "  {inflow ? '+' : '-'} {humanise(row.original.transaction_type)}",
            "  {difference.isPositive() ? '+' : '-'}${formatMoney(difference.abs())}",
            "  const sign = negative ? '-' : ''",
            "  type Sign = '-' | '+'",
            "  interface Delta { sign: '-' | '+' }",
            "  const [year, month, day] = iso.split('-').map(Number)",
            "  .join(' - ')",
            "  {selected ? selected.code + ' - ' + selected.name : null}",
            "  value.replaceAll('-', '+').replaceAll('_', '/')",
            "  const FORMULA_LEADS = ['=', '+', '-', '@']",
            '  <Input placeholder="-" {...register("min")} />',
            '  <span className="text-muted-foreground" aria-hidden>',
            "    -",
            "  </span>",
            "  const KEYS = { '-': 'minus' }",
            "  value: item.hsn_code ?? EMPTY_VALUE,",
            "  // a blank used to read '-' here",
            "  x = y ?? z // or '-'",
            "  <p>From 1 Nov - 30 Nov</p>",
        ]
    )
    assert hyphen_placeholders(source) == []


def test_the_hyphen_spans_point_at_the_mark_itself() -> None:
    """The spans are what a conversion replaces, so they must cover the mark exactly."""
    source = "a ?? '-'\n<span>-</span>\n<span>\n  -\n</span>\n"
    marks = [(source[start:end], kind) for start, end, kind in hyphen_placeholder_spans(source)]
    assert marks == [("'-'", "literal"), ("-", "jsx"), ("\n  -\n", "jsx")]
