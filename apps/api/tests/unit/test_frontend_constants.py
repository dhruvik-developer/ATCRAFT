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

Lives in the backend suite beside the permission guards, for the same reason:
the API caps ``limit`` at 200 and the debounce is tuned against its latency
targets, so the backend is where the values are actually decided.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CONSTANTS = WEB_SRC / "lib" / "constants.ts"

_SET_TIMEOUT = re.compile(r"\bsetTimeout\s*\(")


def _constant(name: str) -> int:
    match = re.search(rf"^export const {name}\s*=\s*(\d+)\b", CONSTANTS.read_text("utf-8"), re.M)
    assert match, f"{name} is not a numeric export of lib/constants.ts; the guard is stale"
    return int(match.group(1))


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
