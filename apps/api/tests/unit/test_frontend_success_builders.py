"""A success message in a standard verb comes from its builder.

``apps/web/src/lib/messages.ts`` keeps one builder per standard verb -
``success.created('Purchase order')`` reads "Purchase order created" - so the
wording of every confirmation lives in one place. Twenty-seven titles spelled
the verb by hand instead, several inside a map whose siblings used the
builders ("Assembly started" between ``success.released`` and
``success.closed``), and one map carried a comment saying ``started`` had no
builder, which it has: each hand-written copy is a second, unguarded spelling
of the same sentence.

The verbs are read from ``success`` itself, so a builder added there is guarded
from then on. A title is looked for where a success message is said: the first
argument of ``toast.success``, ``toast.show`` or ``toast.warning``; the value of a
``successTitle``, ``toastTitle``, ``done``, ``success`` or ``start`` key (the
lifecycle maps); and the message ``useDealerAction`` toasts. A negated phrase
("Goods receipt saved but not posted", "Nothing recorded") is not a builder's
sentence and is left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_mutation_error_toasts import _expression_end, _skip_string

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
MESSAGES = WEB_SRC / "lib" / "messages.ts"

_BUILDER = re.compile(r"^\s+\w+: \(noun: string\) => `\$\{noun\} (?P<verb>[a-z]+)`", re.M)
_CONTEXT = re.compile(
    r"\btoast\.(?:success|show|warning)\s*\(\s*"
    r"|(?<![\w$.])(?:successTitle|toastTitle|done|success|start)\s*:\s*"
)
_DEALER_ACTION = re.compile(r"\buseDealerAction\s*(?:<[^>]*>)?\(\s*")
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_NEGATED = re.compile(r"\b(?:not|nothing|no|never)\b", re.IGNORECASE)


def builder_verbs(source: str) -> list[str]:
    """The past-tense verb of every one-noun builder in ``success``."""
    start = source.index("export const success = {")
    block = source[start : source.index("} as const", start)]
    return _BUILDER.findall(block)


def _shape(verbs: list[str]) -> re.Pattern[str]:
    return re.compile(
        rf"^[A-Z][^.!?]{{0,60}} (?:{'|'.join(verbs)})$"
        # `statusChanged`: "Shipment marked delivered".
        r"|^[A-Z][^.!?]{0,60} marked [a-z ]+$"
    )


def _literals(text: str) -> list[str]:
    found = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        if text[index] in "'\"`":
            end = _skip_string(text, index)
            found.append(text[index + 1 : end - 1])
            index = end
            continue
        index += 1
    return found


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _in_comment(source: str, offset: int) -> bool:
    line_start = source.rfind("\n", 0, offset) + 1
    before = source[line_start:offset]
    return "//" in before or before.lstrip().startswith(("*", "/*"))


def hand_written_titles(source: str, verbs: list[str]) -> list[str]:
    """``line N: 'title'`` for every standard-verb success title spelled by hand."""
    shape = _shape(verbs)
    spans = [
        (
            match.start(),
            source[match.end() : _expression_end(source, match.end(), stop_at_comma=True)],
        )
        for match in _CONTEXT.finditer(source)
    ]
    for match in _DEALER_ACTION.finditer(source):
        first = _expression_end(source, match.end(), stop_at_comma=True)
        if first < len(source) and source[first] == ",":
            second = source[first + 1 : _expression_end(source, first + 1, stop_at_comma=True)]
            spans.append((match.start(), second))
    found = []
    for offset, text in spans:
        if _in_comment(source, offset):
            continue
        for literal in _literals(text):
            # An interpolated code or name stands in for the noun's own words.
            words = _INTERPOLATION.sub("X", literal).strip()
            if shape.match(words) and not _NEGATED.search(words):
                found.append(f"line {_line(source, offset)}: {literal.strip()!r}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path != MESSAGES
    )


def test_the_builders_are_where_the_rule_reads_them() -> None:
    verbs = builder_verbs(MESSAGES.read_text(encoding="utf-8"))
    assert {"created", "updated", "started", "raised", "submitted"} <= set(verbs)
    assert "success.statusChanged" not in verbs
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_screen_spells_a_standard_verb_by_hand() -> None:
    verbs = builder_verbs(MESSAGES.read_text(encoding="utf-8"))
    spelled = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := hand_written_titles(path.read_text(encoding="utf-8"), verbs))
    }
    assert not spelled, (
        "A success message spells a standard verb instead of using its builder:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in spelled.items())
        + "\n\nUse success.<verb>('Noun') from '@/lib/messages' - success.created('Purchase "
        "order'), success.started('Assembly'), success.statusChanged('Shipment', "
        "'delivered') - and pass the code or name as the toast description."
    )


# -- the rule rejects what it exists to catch ---------------------------------

_VERBS = ["created", "updated", "started", "submitted", "uploaded", "activated"]


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = (
        "toast.success('Courier account created')\n"
        "toast.success(\n"
        "  saved.is_default ? 'Approval defaults restored' : 'Approval thresholds updated',\n"
        "  'The values apply next time.',\n"
        ")\n"
        "const titles = { approve: success.approved('Order'), start: 'Assembly started' }\n"
        "const copy = { successTitle: 'Customer activated', reason: 'x' }\n"
        "toast.success(`Invoice ${invoice.vendor_invoice_number} submitted`)\n"
        "toast.success(`${file.file_name} uploaded`)\n"
        "toast.success(`Shipment ${row.shipment_number} marked delivered`)\n"
        "const cart = useDealerAction((v: string) => patch(v), 'Cart updated', { meta })\n"
    )
    assert hand_written_titles(source, [*_VERBS, "restored"]) == [
        "line 1: 'Courier account created'",
        "line 2: 'Approval defaults restored'",
        "line 2: 'Approval thresholds updated'",
        "line 6: 'Assembly started'",
        "line 7: 'Customer activated'",
        "line 8: 'Invoice ${invoice.vendor_invoice_number} submitted'",
        "line 9: '${file.file_name} uploaded'",
        "line 10: 'Shipment ${row.shipment_number} marked delivered'",
        "line 11: 'Cart updated'",
    ]


def test_the_rule_ignores_builders_bespoke_sentences_and_other_keys() -> None:
    source = (
        "toast.success(success.created('Courier account'))\n"
        "toast.success('Added to the cart')\n"
        "toast.success('Test message sent to your verified number')\n"
        "toast.warning('Goods receipt saved but not posted', reason)\n"
        "toast.warning('Nothing recorded', result.reason)\n"
        "toast.error('Courier account created')\n"
        "events.push({ at, title: `Challan ${challan.code} issued` })\n"
        "const range = { start: '2026-01-01', end: '2026-01-31' }\n"
        "useDealerAction(() => post(path), 'Moved to the cart')\n"
        "// toast.success('Assembly started') in a comment\n"
    )
    assert hand_written_titles(source, _VERBS) == []
