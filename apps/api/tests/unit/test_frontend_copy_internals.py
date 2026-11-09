"""A sentence a person reads states the business rule, not the machinery behind it.

Sixty-eight strings across twenty features told an operator about the HTTP
layer and the repository instead of the rule they had to act on: a price dialog
said "This endpoint cannot reopen a closed price", a payment amendment said "The
endpoint ignores an empty value", the documents screen asked for "UUID of the
record", the settings page reported "the code default (on)", order totals were
"Calculated by the API", and two screens cited ``docs/assumptions.md`` section
numbers that nobody on the floor can open. How the server is built is not the
reader's business; naming it buries what is.

The rule blanks every comment - ``//``, ``/* */`` and JSX's ``{/* */}``, string
literals left intact - and then fails on what is left: "the API", "an API",
"this API" or "that API", "API's", the noun "endpoint", the noun "UUID", "code
default", and a ``docs/...md`` path. Say what happens instead: "is refused",
"the system calculates", "Record id", "the built-in default"; and put a pointer
to a decision record in a code comment beside the sentence, where the developer
who needs it will read it. An identifier is not copy, so ``subscription.endpoint``,
``{ endpoint }`` and ``const UUID = /.../`` are left alone, and so is
"API credentials", which names a courier's real external interface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
#: Generated from the API's OpenAPI document; its descriptions are not screens.
GENERATED = WEB_SRC / "types" / "openapi.ts"

_INTERNALS = re.compile(
    r"\b(?:[Tt]he|[Aa]n|[Tt]his|[Tt]hat) API\b"
    r"|\bAPI(?:'|&apos;|&rsquo;|\u2019)s\b"
    # The noun, not an identifier: not after a dot, and not followed by the
    # punctuation that makes it a key, a parameter, a call or a member access.
    r"|(?<![\w$.])endpoints?\b(?!\s*[:,)}(=\]?]|\.\w)"
    r"|(?<![\w$.])UUIDs?\b(?!\s*[=:]|\.\w)"
    r"|\bcode default"
    r"|docs/[\w./-]+\.md"
)


def blank_comments(source: str) -> str:
    """``source`` with every comment blanked to spaces; strings and newlines kept.

    A quoted string cannot span lines, so one ends at a newline: an apostrophe
    in JSX text ("Don't") then costs the scan the rest of that line rather than
    the rest of the file. ``${...}`` inside a template is read as code, comments
    and all.
    """
    out = list(source)
    length = len(source)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = " "

    def comment_end(index: int) -> int | None:
        """Where the comment opening at ``index`` ends, blanking it; None if none opens."""
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end == -1 else end + 2
        else:
            return None
        blank(index, end)
        return end

    def string_end(index: int) -> int:
        quote = source[index]
        index += 1
        while index < length:
            char = source[index]
            if char == "\\":
                index += 2
                continue
            if char == quote:
                return index + 1
            if char == "\n" and quote != "`":
                return index
            if quote == "`" and source.startswith("${", index):
                index = code_end(index + 2, closing_brace=True)
                continue
            index += 1
        return length

    def code_end(index: int, *, closing_brace: bool) -> int:
        depth = 0
        while index < length:
            end = comment_end(index)
            if end is not None:
                index = end
                continue
            char = source[index]
            if char in "'\"`":
                index = string_end(index)
                continue
            if closing_brace and char == "{":
                depth += 1
            elif closing_brace and char == "}":
                if depth == 0:
                    return index + 1
                depth -= 1
            index += 1
        return length

    code_end(0, closing_brace=False)
    return "".join(out)


def internal_words(source: str) -> list[str]:
    """``line N: 'word'`` for each implementation detail left outside a comment."""
    code = blank_comments(source)
    return [
        f"line {code.count(chr(10), 0, match.start()) + 1}: {match.group(0)!r}"
        for match in _INTERNALS.finditer(code)
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] != "test"
        and path != GENERATED
    )


def test_the_scan_reaches_the_screens_it_polices() -> None:
    sources = _sources()
    assert len(sources) > 300, "source discovery found too few files"
    assert WEB_SRC / "features" / "masters" / "price-dialogs.tsx" in sources


def test_no_screen_tells_a_person_how_the_server_is_built() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := internal_words(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These strings name the API, an endpoint, a UUID, the code or a repository "
        "document to the person reading the screen. State the rule instead - "
        "'is refused', 'the system calculates', 'Record id', 'the built-in default' - "
        f"and keep any docs/ pointer in a code comment:\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "  message: 'This endpoint cannot reopen a closed price.',",
            "  ? 'The API records this against the account - it needs a reason'",
            '  placeholder="UUID of the record"',
            "  ? 'No company override is stored; the code default (on) is in force.'",
            "  many as it moves (docs/assumptions.md section 23).",
            "  This order has more boxes than one page. The totals above are the API&apos;s",
            "  . Tax is the API&rsquo;s calculation from the item master.",
            "  An amendment only applies what is filled in. The endpoint ignores an empty value",
            '  placeholder="Record id (UUID)"',
        ]
    )

    assert internal_words(source) == [
        "line 1: 'endpoint'",
        "line 2: 'The API'",
        "line 3: 'UUID'",
        "line 4: 'code default'",
        "line 5: 'docs/assumptions.md'",
        "line 6: 'the API'",
        "line 7: 'the API'",
        "line 8: 'endpoint'",
        "line 9: 'UUID'",
    ]


def test_the_rule_ignores_comments_and_identifiers() -> None:
    source = "\n".join(
        [
            "// The API refuses half a reference (docs/assumptions.md DOC-1).",
            "/* A UUID is what the endpoint takes,",
            "   and the API says so. */",
            "{/* The rule is docs/assumptions.md section 23. */}",
            "const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}$/i",
            "if (!UUID.test(id)) return",
            "await unregisterEndpoint(subscription.endpoint, signal)",
            "api.post('/notifications/push/unregister', { endpoint }, { signal })",
            "const body = { endpoint: subscription.endpoint, keys }",
            "? 'API credentials configured' : 'API credentials not configured'",
            '<Input accept="image/*" /> {/* image/* is not a comment opener */}',
            "`${row.code} - ${/* the API's own label */ row.name}`",
        ]
    )

    assert internal_words(source) == []


def test_a_string_holding_a_comment_opener_does_not_hide_what_follows() -> None:
    source = "\n".join(
        [
            '<Input accept="image/*" />',
            "<p>The API refuses a posting without lines.</p>",
            "const url = 'https://example.test//path'",
            "<p>Don't worry - this endpoint takes no reason.</p>",
        ]
    )

    assert internal_words(source) == ["line 2: 'The API'", "line 4: 'endpoint'"]
