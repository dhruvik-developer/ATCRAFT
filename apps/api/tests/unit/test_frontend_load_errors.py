"""A read that failed is shown one way: ``LoadError``.

``apps/web/src/components/shared.tsx`` defines ``LoadError`` (what did not
load, the API's reason, a retry labelled ``empty.loadFailedAction``) and
``PanelDenied`` (a panel the role may not read, in ``lib/messages.ts`` words).
Before they existed the same failure was drawn about thirty ways: three local
panels (``PanelError``, ``DealerResult``'s alert, the vendor portal's
``QueryFailure``), ``PanelDenied`` pasted into two pages, twenty retry alerts
each spelling "Try again" themselves, and four failures dressed as empty states.
So a failure read as a headline with a retry on one panel and as a bare red
sentence on the next, and none of them changed when the shared one did.

The rules: the two components are defined once, and the retry label is never
spelled outside ``lib/messages.ts`` - a hand-written "Try again" is how every
one of the copies above began.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
SHARED = WEB_SRC / "components" / "shared.tsx"
MESSAGES = WEB_SRC / "lib" / "messages.ts"

#: A render crash, not a failed read: its "Try again" re-renders the tree, and
#: it deliberately imports as little as possible.
_CRASH_BOUNDARY = WEB_SRC / "components" / "error-boundary.tsx"

_DEFINITION = re.compile(
    r"\b(?:function\s+(?P<fn>LoadError|PanelDenied)\b"
    r"|(?:const|let|var)\s+(?P<bound>LoadError|PanelDenied)\s*=)"
)
#: The retry label written out: JSX text or a whole quoted literal.
_RETRY_LABEL = re.compile(r">\s*Try again\s*<|(['\"`])Try again\1")


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def definitions(source: str) -> list[str]:
    """``line N: Name`` for every definition of LoadError or PanelDenied."""
    return [
        f"line {_line(source, match.start())}: {match.group('fn') or match.group('bound')}"
        for match in _DEFINITION.finditer(source)
    ]


def _in_comment(source: str, offset: int) -> bool:
    line_start = source.rfind("\n", 0, offset) + 1
    return source[line_start:].lstrip().startswith(("//", "*", "/*"))


def retry_labels(source: str) -> list[str]:
    """``line N`` for every retry label spelled out rather than shared."""
    return [
        f"line {_line(source, match.start())}"
        for match in _RETRY_LABEL.finditer(source)
        if not _in_comment(source, match.start())
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
    )


def test_the_shared_components_and_label_are_where_the_screens_import_them_from() -> None:
    """If these move, the guards below would pass on an empty set of copies."""
    names = sorted(
        found.split(": ")[1] for found in definitions(SHARED.read_text(encoding="utf-8"))
    )
    assert names == ["LoadError", "PanelDenied"], f"components/shared.tsx defines {names}"
    assert "loadFailedAction: 'Try again'" in MESSAGES.read_text(encoding="utf-8")
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_screen_defines_its_own_load_error_or_denied_panel() -> None:
    copies = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != SHARED and (found := definitions(path.read_text(encoding="utf-8")))
    }
    assert not copies, (
        "LoadError or PanelDenied is defined outside components/shared.tsx:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in copies.items())
        + "\n\nImport it from '@/components/shared'."
    )


def test_no_screen_spells_the_retry_label() -> None:
    spelled = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path not in {MESSAGES, _CRASH_BOUNDARY}
        and (found := retry_labels(path.read_text(encoding="utf-8")))
    }
    assert not spelled, (
        "A retry is labelled by hand:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in spelled.items())
        + "\n\nShow a failed read with LoadError from '@/components/shared' (pass onRetry), "
        "or label a bespoke retry with empty.loadFailedAction from '@/lib/messages'."
    )


# -- the rules reject what they exist to catch --------------------------------


def test_the_rules_find_a_pasted_panel_and_a_spelled_retry() -> None:
    source = (
        "function PanelDenied({ what }: { what: string }) {\n"
        "const LoadError = ({ error }: { error: unknown }) => null\n"
        '<Button variant="outline" size="sm" onClick={() => void q.refetch()}>\n'
        "  <RefreshCw />\n"
        "  Try again\n"
        "</Button>\n"
        "{retryAfter === null ? 'Try again' : `Try again in ${retryAfter}s`}\n"
    )
    assert definitions(source) == ["line 1: PanelDenied", "line 2: LoadError"]
    assert retry_labels(source) == ["line 4", "line 7"]


def test_the_rules_ignore_uses_and_a_countdown() -> None:
    source = (
        "import { LoadError, PanelDenied } from '@/components/shared'\n"
        '<LoadError title="Could not load this bill" error={q.error} onRetry={retry} />\n'
        '<PanelDenied what="Prices" />\n'
        "{empty.loadFailedAction}\n"
        "{retryAfter === null ? empty.loadFailedAction : `Try again in ${retryAfter}s`}\n"
        "// a comment about Try again is not a label\n"
        ' * and "Try again" can never fix it, because\n'
    )
    assert definitions(source) == []
    assert retry_labels(source) == []
