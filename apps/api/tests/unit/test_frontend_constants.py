"""The frontend's shared numbers live in ``lib/constants.ts`` and nowhere else.

``docs/architecture.md`` says every one of them - the search debounce, the
picker page size, the reference-list limit - is a decision made once in that
file. A value spelled inline in six dialogs is six chances for them to
disagree, and the claim is only true while nothing re-spells them.

The check is deliberately literal: it looks for the *numbers* the constants
currently hold, wherever they appear in a ``setTimeout(...)`` or a
``limit: N`` under ``apps/web/src``. A file that imports ``SEARCH_DEBOUNCE_MS``
never contains the digits, so the honest code cannot trip it; a pasted ``300``
can. The numbers are read from ``constants.ts`` rather than repeated here, so
changing a constant does not leave this guard policing a value nobody uses.

The reason minimum is the same kind of decision: the API holds every audited
reason to ``REASON_MIN_LENGTH`` characters, and a sentence that spells the bound
("at least three characters") keeps saying so after the constant moves, while
the API refuses on the new one. Three did - the catalogue's own
``errors.reasonTooShort`` among them. A sentence states the bound by
interpolating the constant, so only a spelled number can trip this.

Lives in the backend suite beside the permission guards, for the same reason:
the API caps ``limit`` at 200 and the debounce is tuned against its latency
targets, so the backend is where the values are actually decided.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_load_errors import _in_comment

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CONSTANTS = WEB_SRC / "lib" / "constants.ts"

_SET_TIMEOUT = re.compile(r"\bsetTimeout\s*\(")
_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _constant(name: str) -> int:
    source = CONSTANTS.read_text("utf-8")
    visited: set[str] = set()
    while name not in visited:
        visited.add(name)
        match = re.search(rf"^export const {name}\s*=\s*(\d+|[A-Z][A-Z0-9_]*)\b", source, re.M)
        assert match, f"{name} is not a numeric constant or alias in lib/constants.ts"
        value = match.group(1)
        if value.isdigit():
            return int(value)
        name = value
    raise AssertionError(f"Circular constant alias in lib/constants.ts: {name}")


def _parenthesised(text: str, start: int) -> str:
    """The argument list from just past ``(`` to its matching ``)``."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _sources() -> list[Path]:
    return [
        source
        for source in WEB_SRC.rglob("*.ts*")
        if "node_modules" not in source.parts
        and source.suffix in {".ts", ".tsx"}
        and source != CONSTANTS
    ]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def test_the_constants_file_still_holds_the_values_this_guard_polices() -> None:
    assert _constant("SEARCH_DEBOUNCE_MS") > 0
    assert _constant("PICKER_PAGE_SIZE") > 0
    assert _constant("LOOKUP_LIST_LIMIT") > 0


def test_no_screen_respells_the_debounce_or_a_page_size() -> None:
    debounce = _constant("SEARCH_DEBOUNCE_MS")
    limits = {_constant("PICKER_PAGE_SIZE"), _constant("LOOKUP_LIST_LIMIT")}
    literal_debounce = re.compile(rf"(?<![\w.]){debounce}(?![\w.])")
    literal_limit = re.compile(r"\blimit:\s*(\d+)\b")

    failures: list[str] = []
    for source in _sources():
        text = source.read_text(encoding="utf-8", errors="ignore")
        where = source.relative_to(WEB_SRC).as_posix()
        for call in _SET_TIMEOUT.finditer(text):
            if literal_debounce.search(_parenthesised(text, call.end())):
                failures.append(
                    f"  {where}:{_line_of(text, call.start())} setTimeout with a literal "
                    f"{debounce}; use SEARCH_DEBOUNCE_MS"
                )
        for found in literal_limit.finditer(text):
            if int(found.group(1)) in limits:
                failures.append(
                    f"  {where}:{_line_of(text, found.start())} `{found.group(0)}`; use "
                    "PICKER_PAGE_SIZE or LOOKUP_LIST_LIMIT"
                )

    assert not failures, (
        f"{len(failures)} value(s) re-spelled outside lib/constants.ts:\n"
        + "\n".join(failures)
        + "\n\ndocs/architecture.md promises every one of these lives in one place. "
        "Import the constant; do not add the number here to quiet the test."
    )


def spelled_reason_minimums(text: str, minimum: int) -> list[str]:
    """``line N`` for every sentence outside a comment that spells ``minimum`` out."""
    spellings = [str(minimum)]
    if minimum < len(_NUMBER_WORDS):
        spellings.append(_NUMBER_WORDS[minimum])
    pattern = re.compile(rf"\bat least (?:{'|'.join(spellings)}) characters\b", re.IGNORECASE)
    return [
        f"line {_line_of(text, found.start())}"
        for found in pattern.finditer(text)
        if not _in_comment(text, found.start())
    ]


def test_no_screen_spells_out_the_reason_minimum() -> None:
    minimum = _constant("REASON_MIN_LENGTH")
    failures = [
        f"  {source.relative_to(WEB_SRC).as_posix()}:{hit}"
        for source in _sources()
        # A test pins the sentence a person reads, number and all.
        if not source.name.endswith((".test.ts", ".test.tsx"))
        for hit in spelled_reason_minimums(
            source.read_text(encoding="utf-8", errors="ignore"), minimum
        )
    ]

    assert not failures, (
        f"{len(failures)} sentence(s) spell the reason minimum ({minimum}) out:\n"
        + "\n".join(failures)
        + "\n\nUse errors.reasonTooShort or prompts.reasonRequired from lib/messages.ts, "
        "or interpolate REASON_MIN_LENGTH, so the sentence follows the constant."
    )


def test_the_reason_rule_finds_a_spelled_minimum_and_ignores_the_constant() -> None:
    source = "\n".join(
        [
            '<FieldError message="A cancellation needs at least three characters." />',
            "  reasonTooShort: 'Record at least 3 characters of reason',",
            "  // The API holds a reason to at least three characters.",
            " *    a required query parameter of at least three characters. Sending it",
            "  message={`Give a reason of at least ${REASON_MIN_LENGTH} characters`}",
            "  .min(2, 'Code is at least 2 characters')",
        ]
    )

    assert spelled_reason_minimums(source, 3) == ["line 1", "line 2"]
    assert spelled_reason_minimums(source, 2) == ["line 6"]
