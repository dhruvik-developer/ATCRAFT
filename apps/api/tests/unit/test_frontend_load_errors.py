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

The rules: the two components are defined once, the retry label is never
spelled outside ``lib/messages.ts`` - a hand-written "Try again" is how every
one of the copies above began - and no paragraph is drawn as an error by hand.
A failure's reason (``describeError``) goes in ``LoadError`` when a panel's
content did not load, or in ``FieldError`` when a field's options or a submit
did; and the small destructive line under a control *is* ``FieldError``, whose
``role="alert"`` fifteen pasted copies had mostly left out.

Nor is a failure ever shown by its raw ``.message``. Only the HTTP boundary
writes messages meant for a reader; a runtime error's text can carry stack
frames, internal URLs or input values, and a 5xx's lacks the reference support
needs to find the request. Two load alerts printed ``query.error.message``.
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
#: The retry label written out: JSX text or a whole quoted literal. A bare
#: "Retry" is the same label in other words - the sales-order line dialog and
#: the assembly release check each drew a failed read with one, and neither was
#: seen. A retry that names what it retries ("Retry status") is its own label.
_RETRY_LABEL = re.compile(r">\s*(?:Try again|Retry)\s*<|(['\"`])(?:Try again|Retry)\1")
#: A failure shown by its own text: ``q.error.message``, ``(q.error as Error).message``.
_RAW_MESSAGE = re.compile(r"\.error\.message\b|\.error\s+as\s+Error\s*\)\.message\b")
#: A paragraph whose classes are a plain string, and what it contains.
_PARAGRAPH = re.compile(r'<p\b[^>]*?className="(?P<classes>[^"]*)"[^>]*>(?P<body>.*?)</p>', re.S)


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


def raw_error_messages(source: str) -> list[str]:
    """``line N`` for every failure rendered by its raw ``.message``."""
    return [
        f"line {_line(source, match.start())}"
        for match in _RAW_MESSAGE.finditer(source)
        if not _in_comment(source, match.start())
    ]


def hand_drawn_errors(source: str) -> list[str]:
    """``line N: what`` for every paragraph drawn as an error by hand."""
    found = []
    for match in _PARAGRAPH.finditer(source):
        classes = match.group("classes").split()
        if "text-destructive" not in classes:
            continue
        if "describeError(" in match.group("body"):
            found.append(f"line {_line(source, match.start())}: a failure's reason")
        elif "text-xs" in classes:
            found.append(f"line {_line(source, match.start())}: a field error")
    return found


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
        "label a bespoke retry with empty.loadFailedAction from '@/lib/messages', or name "
        "what it retries ('Retry status')."
    )


def test_no_screen_draws_an_error_paragraph_by_hand() -> None:
    drawn = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path.suffix == ".tsx"
        and path != WEB_SRC / "components" / "field-error.tsx"
        and (found := hand_drawn_errors(path.read_text(encoding="utf-8")))
    }
    assert not drawn, (
        "An error is drawn as a hand-styled paragraph:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in drawn.items())
        + "\n\nA panel whose content did not load: LoadError from '@/components/shared'. A "
        "field's options, a refused submit or a field's own complaint: FieldError, which is "
        "announced (role=alert) and takes an id for aria-describedby. A warning that is not an "
        "error belongs in an Alert, not in destructive small print."
    )


def test_no_screen_shows_a_failure_by_its_raw_message() -> None:
    raw = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        # lib/ is the boundary that turns a raw error into a sentence.
        if WEB_SRC / "lib" not in path.parents
        and (found := raw_error_messages(path.read_text(encoding="utf-8")))
    }
    assert not raw, (
        "A failure is shown by its raw message:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in raw.items())
        + "\n\nShow a failed read with LoadError from '@/components/shared', or say the "
        "reason with describeError from '@/lib/messages': it keeps the API's sentence, adds "
        "a 5xx's reference, and puts errors.unexpectedDetail in place of a runtime error."
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
        '<Button type="button" variant="outline" size="sm" onClick={() => void q.refetch()}>\n'
        "  Retry\n"
        "</Button>\n"
        "{failed ? 'Retry' : 'PDF'}\n"
    )
    assert definitions(source) == ["line 1: PanelDenied", "line 2: LoadError"]
    assert retry_labels(source) == ["line 4", "line 7", "line 8", "line 11"]


def test_the_rules_ignore_uses_and_a_countdown() -> None:
    source = (
        "import { LoadError, PanelDenied } from '@/components/shared'\n"
        '<LoadError title="Could not load this bill" error={q.error} onRetry={retry} />\n'
        '<PanelDenied what="Prices" />\n'
        "{empty.loadFailedAction}\n"
        "{retryAfter === null ? empty.loadFailedAction : `Try again in ${retryAfter}s`}\n"
        "// a comment about Try again is not a label\n"
        ' * and "Try again" can never fix it, because\n'
        '<Button size="sm" onClick={controller.retry}>\n  Retry status\n</Button>\n'
        "{failedMessage ? 'Retry PDF' : 'PDF'}\n"
        "headers.get('Retry-After')\n"
    )
    assert definitions(source) == []
    assert retry_labels(source) == []


def test_the_paragraph_rule_finds_the_shapes_that_shipped() -> None:
    source = (
        '{errors.email && <p className="text-xs text-destructive">{errors.email.message}</p>}\n'
        '<p id="email-error" className="text-xs text-destructive">\n  {errors.email}\n</p>\n'
        '<p className="py-8 text-center text-sm text-destructive" role="alert">\n'
        "  {describeError(worklist.error)}\n"
        "</p>\n"
        '<p role="alert" className="text-sm text-destructive">\n'
        "  Could not load GSTR-3B: {describeError(summary.error)}\n"
        "</p>\n"
    )
    assert hand_drawn_errors(source) == [
        "line 1: a field error",
        "line 2: a field error",
        "line 5: a failure's reason",
        "line 8: a failure's reason",
    ]


def test_the_paragraph_rule_ignores_the_shared_components_and_other_text() -> None:
    source = (
        "<p className={overLimit ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>\n"
        "  {numbers.length} captured\n"
        "</p>\n"
        '<FieldError className="mt-0 px-2 py-6 text-sm" message={describeError(results.error)} />\n'
        "<LoadError error={detail.error} onRetry={() => void detail.refetch()} />\n"
        '<p className="text-sm font-medium text-destructive">Deleting cannot be undone.</p>\n'
        '<p className="text-xs text-muted-foreground">{describeError(job.error)}</p>\n'
    )
    assert hand_drawn_errors(source) == []


def test_the_raw_message_rule_finds_the_shapes_that_shipped() -> None:
    source = (
        "<AlertDescription>{approvals.error.message}</AlertDescription>\n"
        "<AlertDescription>{String((report.error as Error).message)}</AlertDescription>\n"
    )
    assert raw_error_messages(source) == ["line 1", "line 2"]


def test_the_raw_message_rule_ignores_a_described_failure_and_a_field_message() -> None:
    source = (
        '<LoadError error={approvals.error} title="Could not load approvals" />\n'
        "<AlertDescription>{describeError(report.error)}</AlertDescription>\n"
        "<FieldError message={errors.email?.message} />\n"
        "setFormError((error as Error).message)\n"
        "// a note that approvals.error.message used to be printed here\n"
    )
    assert raw_error_messages(source) == []
