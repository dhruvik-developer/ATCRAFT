"""A failure headline says "Could not <verb> ..." and ends without a full stop.

``apps/web/src/lib/messages.ts`` words the shared failure headlines that way -
"Could not load data", "Could not complete", "Could not load this list" - and
about a hundred screens followed it. Thirty-two did not: "The brand was not
retired", "Courier accounts could not be loaded", "Last poll failed", "Unable
to load item variants", and two toast titles that were whole sentences with a
full stop. The same kind of failure read three or four ways depending on the
screen it happened on.

The rule reads a headline wherever a screen sets one: the title of
``toast.error`` and ``toast.failure``, the title ``reportFieldErrors`` toasts, a
mutation's ``meta.errorTitle``, an ``errorHeadline`` prop, and an
``AlertTitle``'s text. Each position is read as an expression, so a title
chosen by a condition (``cancelled ? 'MRP run cancelled' : 'MRP run failed'``)
has both its results checked and neither its condition nor a call argument. A
description under the headline is free to be a sentence; the headline is not.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_frontend_confirm_titles import _shown_literals
from tests.unit.test_frontend_mutation_error_toasts import (
    WEB_SRC,
    _expression_end,
    _line,
    _skip_string,
)

pytestmark = pytest.mark.unit

#: The minority forms: a passive or past statement, "<X> failed", "Unable to",
#: or a full stop at the end. An ellipsis is not a full stop: "Calculating..."
#: is a progress title.
MINORITY_FORM = re.compile(
    r"could not be|\bwas not\b|\bwere not\b| failed$|^Unable to|(?<!\.\.)(?<!…)\.$", re.I
)
_TAG = re.compile(r"<[^<>]*>")

#: Calls whose title is the first argument, and calls whose title is the second.
_FIRST_ARGUMENT_TITLE = re.compile(r"\btoast\.error\s*\(\s*")
_SECOND_ARGUMENT_TITLE = re.compile(r"\b(?:toast\.failure|reportFieldErrors)\s*\(\s*")
_META_TITLE = re.compile(r"(?<![\w$.])errorTitle\s*:\s*")
_HEADLINE_PROP = re.compile(r"(?<![\w-])errorHeadline=(?:(?P<quoted>\")|\{\s*)")
_ALERT_TITLE = re.compile(r"<AlertTitle(?:\s[^>]*)?>(?P<body>.*?)</AlertTitle>", re.S)


def _literal_at(source: str, index: int) -> str | None:
    """The text of the string literal opening at ``index``, if one does."""
    if index < len(source) and source[index] in "'\"`":
        return source[index + 1 : _skip_string(source, index) - 1]
    return None


def _value(source: str, start: int) -> str:
    """The expression from ``start`` to the comma or bracket that ends it."""
    return source[start : _expression_end(source, start, stop_at_comma=True)]


def _jsx_children(body: str) -> tuple[str, list[str]]:
    """A JSX body's own text, and the source of each top-level ``{expression}`` child."""
    text, expressions = [], []
    index = 0
    while index < len(body):
        if body[index] == "{":
            close = _expression_end(body, index + 1, stop_at_comma=False)
            expressions.append(body[index + 1 : close])
            index = close + 1
            continue
        text.append(body[index])
        index += 1
    return "".join(text), expressions


def _headlines(source: str) -> list[tuple[int, str]]:
    """``(offset, text)`` for every literal failure headline in ``source``."""
    found: list[tuple[int, str]] = []

    def read(offset: int, expression: str) -> None:
        found.extend((offset, text) for text in _shown_literals(expression))

    for match in _FIRST_ARGUMENT_TITLE.finditer(source):
        read(match.start(), _value(source, match.end()))
    for match in _SECOND_ARGUMENT_TITLE.finditer(source):
        comma = _expression_end(source, match.end(), stop_at_comma=True)
        if source[comma : comma + 1] == ",":
            read(match.start(), _value(source, comma + 1))
    for match in _META_TITLE.finditer(source):
        read(match.start(), _value(source, match.end()))
    for match in _HEADLINE_PROP.finditer(source):
        if match.group("quoted"):
            if (text := _literal_at(source, match.end() - 1)) is not None:
                found.append((match.start(), text))
        else:
            read(match.start(), _value(source, match.end()))
    for match in _ALERT_TITLE.finditer(source):
        # An icon beside the words is not part of them.
        text, expressions = _jsx_children(_TAG.sub(" ", match.group("body")))
        if text.strip():
            found.append((match.start(), text))
        for expression in expressions:
            read(match.start(), expression)
    return found


def minority_headlines(source: str) -> list[str]:
    """``line N: 'headline'`` for every failure headline in a minority form."""
    return [
        f"line {_line(source, offset)}: {' '.join(text.split())!r}"
        for offset, text in sorted(_headlines(source), key=lambda headline: headline[0])
        if MINORITY_FORM.search(" ".join(text.split()))
    ]


def _sources() -> list:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and not path.name.endswith((".test.ts", ".test.tsx"))
        and path.relative_to(WEB_SRC).parts[0] != "test"
    )


def test_the_scan_reads_the_headlines_it_polices() -> None:
    """A renamed helper or component would otherwise leave this passing on nothing."""
    read = sum(len(_headlines(path.read_text(encoding="utf-8"))) for path in _sources())
    assert read > 150, f"read only {read} failure headlines; is the scan looking elsewhere?"


def test_every_failure_headline_says_could_not() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := minority_headlines(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These failure headlines are worded differently from lib/messages.ts; say "
        "'Could not <verb> <the thing>' with no full stop, and put any sentence in "
        f"the description:\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "<AlertTitle>The brand was not retired</AlertTitle>",
            '<RetireDialog errorHeadline="The accessory was not removed" />',
            "onError: (error) => reportFieldErrors(error, 'Delivery could not be recorded'),",
            "toast.error('Serials were registered, but labels could not be downloaded.', why)",
            "toast.failure(describe(error, 'x'), 'Last poll failed')",
            "<AlertTitle>",
            "  {stranded ? 'The bill was not submitted' : 'The bill was not saved'}",
            "</AlertTitle>",
            "meta: { errorTitle: 'Unable to load item variants' },",
            "toast.error(cancelled ? 'MRP run cancelled' : 'MRP run failed', reason)",
        ]
    )

    assert minority_headlines(source) == [
        "line 1: 'The brand was not retired'",
        "line 2: 'The accessory was not removed'",
        "line 3: 'Delivery could not be recorded'",
        "line 4: 'Serials were registered, but labels could not be downloaded.'",
        "line 5: 'Last poll failed'",
        "line 6: 'The bill was not submitted'",
        "line 6: 'The bill was not saved'",
        "line 9: 'Unable to load item variants'",
        "line 10: 'MRP run failed'",
    ]


def test_the_rule_leaves_the_catalogue_form_and_descriptions_alone() -> None:
    source = "\n".join(
        [
            "<AlertTitle>Could not retire the brand</AlertTitle>",
            "toast.error('Could not download the serial labels', describeError(error))",
            "toast.error(errors.unexpected, 'The file could not be generated. Try again.')",
            "reportFieldErrors(error, `Could not record ${code}`)",
            "<AlertTitle>",
            "  {mode === 'CREATE.' ? 'Could not create it' : 'Could not save it'}",
            "</AlertTitle>",
            "<AlertDescription>The change was not saved.</AlertDescription>",
            "<AlertTitle>",
            '  <Timer className="h-4 w-4 animate-pulse" aria-hidden />',
            "  Calculating...",
            "</AlertTitle>",
            "toast.error(status === 'FAILED.' ? errors.unexpected : 'Could not post', why)",
            "<AlertTitle>{`Could not post ${receipt.code}`}</AlertTitle>",
        ]
    )

    assert minority_headlines(source) == []
