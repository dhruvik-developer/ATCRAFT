"""A sentence the message catalogue holds is said through it, never typed again.

``apps/web/src/lib/messages.ts`` words a situation once for every screen that
meets it. A copy typed by hand beside it is a second wording waiting to drift,
and three had: the catalogue's filtered-list sentence said "No records match
the current filters." while thirty lists typed "Nothing matches the current
filters..." for themselves, so every list that left the sentence to the table
read differently from its neighbours; six detail pages each typed "It may have
been removed, or the link may be wrong."; and four reason boxes spelled out
``errors.reasonTooShort`` in full.

The rule reads every string literal the catalogue holds with four or more words
of its own - shorter ones ("Try again", "Reserved") are ordinary words a screen
may use for something else - and fails wherever product source outside the
catalogue contains that text once comments are blanked. A screen that adds its
own hint composes it: ``${empty.filtered} Clear a filter, or add the supplier.``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_copy_internals import GENERATED, blank_comments
from tests.unit.test_frontend_mutation_error_toasts import WEB_SRC, _line, _skip_string

pytestmark = pytest.mark.unit

MESSAGES = WEB_SRC / "lib" / "messages.ts"

#: The fewest words of its own a catalogue string needs before a copy counts.
MIN_WORDS = 4
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_WORD = re.compile(r"[A-Za-z]+(?:'[a-z]+)?")


def catalogue_sentences(catalogue: str) -> list[str]:
    """The source text of every string literal in ``catalogue`` long enough to police.

    A literal passed to a call - ``validation.decimals('a quantity above zero', 4)``
    - is a phrase the builder completes, not a sentence of its own, so it is not
    one a screen could be said to copy.
    """
    code = blank_comments(catalogue)
    found: list[str] = []
    index = 0
    while index < len(code):
        if code[index] in "'\"`":
            end = _skip_string(code, index)
            body = code[index + 1 : end - 1]
            argument = code[:index].rstrip().endswith(("(", ","))
            if not argument and len(_WORD.findall(_INTERPOLATION.sub(" ", body))) >= MIN_WORDS:
                found.append(body)
            index = end
            continue
        index += 1
    return found


def retyped_sentences(source: str, sentences: list[str]) -> list[str]:
    """``line N: 'sentence'`` for each catalogue sentence ``source`` spells out itself."""
    code = blank_comments(source)
    found = []
    for sentence in sentences:
        start = code.find(sentence)
        while start != -1:
            found.append(f"line {_line(code, start)}: {sentence!r}")
            start = code.find(sentence, start + 1)
    return sorted(found, key=lambda entry: int(entry.split(":")[0].split()[1]))


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] != "test"
        and path not in {MESSAGES, GENERATED}
    )


def test_the_scan_reads_the_sentences_it_polices() -> None:
    """A reshaped catalogue would otherwise leave the rule passing on nothing."""
    sentences = catalogue_sentences(MESSAGES.read_text(encoding="utf-8"))
    assert "Nothing matches the current filters." in sentences
    assert "Record at least ${REASON_MIN_LENGTH} characters of reason" in sentences
    assert "Try again" not in sentences, "a two-word entry is not policed"
    assert len(sentences) > 30
    assert len(_sources()) > 300, "source discovery found too few files"


def test_no_screen_types_a_catalogue_sentence_again() -> None:
    sentences = catalogue_sentences(MESSAGES.read_text(encoding="utf-8"))
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := retyped_sentences(path.read_text(encoding="utf-8"), sentences))
    }
    assert not offenders, (
        "These files type out a sentence lib/messages.ts already holds. Read it from "
        "the catalogue - `empty.filtered`, `errors.reasonTooShort` - and compose any "
        f"hint of the screen's own after it:\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    catalogue = "\n".join(
        [
            "export const errors = {",
            "  /** A lifecycle reason shorter than `REASON_MIN_LENGTH`. */",
            "  reasonTooShort: `Record at least ${REASON_MIN_LENGTH} characters of reason`,",
            "} as const",
            "export const empty = {",
            "  title: 'Nothing here yet',",
            "  filtered: 'Nothing matches the current filters.',",
            "  notFoundDetail: 'It may have been removed, or the link may be wrong.',",
            "} as const",
        ]
    )
    sentences = catalogue_sentences(catalogue)
    assert sentences == [
        "Record at least ${REASON_MIN_LENGTH} characters of reason",
        "Nothing matches the current filters.",
        "It may have been removed, or the link may be wrong.",
    ]

    screen = "\n".join(
        [
            '  emptyDescription="Nothing matches the current filters."',
            "  ? 'Nothing matches the current filters. Clear a filter to see every unit.'",
            '  description="It may have been removed, or the link may be wrong."',
            "<FieldError message={`Record at least ${REASON_MIN_LENGTH} characters of reason`} />",
        ]
    )
    assert retyped_sentences(screen, sentences) == [
        "line 1: 'Nothing matches the current filters.'",
        "line 2: 'Nothing matches the current filters.'",
        "line 3: 'It may have been removed, or the link may be wrong.'",
        "line 4: 'Record at least ${REASON_MIN_LENGTH} characters of reason'",
    ]


def test_the_rule_leaves_composition_comments_and_short_words_alone() -> None:
    sentences = catalogue_sentences(
        "export const empty = {\n"
        "  title: 'Nothing here yet',\n"
        "  filtered: 'Nothing matches the current filters.',\n"
        "  loadFailedAction: 'Try again',\n"
        "} as const\n"
        "export const fulfilment = {\n"
        "  quantityInvalid: validation.decimals('a quantity above zero', 4),\n"
        "} as const\n"
    )
    # A builder's argument is no sentence of its own.
    assert sentences == ["Nothing matches the current filters."]
    screen = "\n".join(
        [
            "  emptyDescription={`${empty.filtered} Clear a filter, or add the supplier.`}",
            "  // The table's own default says 'Nothing matches the current filters.'",
            "  {/* Nothing matches the current filters. reads oddly here */}",
            "  <Button>Try again</Button>",
            "  emptyTitle='Nothing here yet'",
            "  quantity: z.string().regex(QTY, validation.decimals('a quantity above zero', 4)),",
        ]
    )

    assert retyped_sentences(screen, sentences) == []
