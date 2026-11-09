"""A confirmation dialog's title asks the question its confirm button answers.

``ConfirmDialog`` and ``ReasonDialog`` (``apps/web/src/components/shared.tsx``)
title the last step before something irreversible: "Retire C-0001?", "Cancel
this invoice?". Fifty-six of them asked; the vendor bill's "Approve this
invoice" and "Delete this draft bill", the packing line's "Replace final QC
lot for line 2" and the shift roster's bare "Retire" did not, so the same kind
of step read as a statement on one screen and a question on the next - and
"Retire" did not say what would be retired.

The rule reads the literals a ``title`` attribute can show: the text of
``title="..."``, and each string or template literal inside ``title={...}``
that is a result rather than a test - an operand of ``===``/``!==`` is a
condition, a literal right after ``(`` or ``,`` is an argument
(``copy.title.replace('this order', code)``), and a literal followed by ``+`` is
the head of a concatenation whose tail is checked instead. An empty fallback
for a closed dialog is allowed.
Titles drawn from a copy map (``copy.title``) cannot be read here; those maps
are written as questions too.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_frontend_destructive_confirms import (
    CONFIRM_COMPONENTS,
    WEB_SRC,
    _line,
    _sources,
)
from tests.unit.test_frontend_mutation_error_toasts import _skip_string

pytestmark = pytest.mark.unit

_TITLE = re.compile(r"(?<![\w-])title=(?:\"([^\"]*)\"|\{)")
_COMPARED = re.compile(r"(?:===|!==|==|!=)\s*$")
_ARGUMENT = re.compile(r"[(,]\s*$")
_CONCATENATED = re.compile(r"^\s*\+")


def _past_comment(text: str, index: int) -> int:
    """The offset after a comment opening at ``index``, or ``index`` if none opens there."""
    if text.startswith("//", index):
        newline = text.find("\n", index)
        return len(text) if newline == -1 else newline
    if text.startswith("/*", index):
        close = text.find("*/", index + 2)
        return len(text) if close == -1 else close + 2
    return index


def _opening_tag_end(text: str, start: int) -> int:
    """Index of the ``>`` closing the opening tag at ``start``, past strings and comments.

    A comment between two attributes is legal TSX, and one that says "the order's
    status" would otherwise open a string that never closes.
    """
    depth = 0
    index = start
    while index < len(text):
        if (skipped := _past_comment(text, index)) != index:
            index = skipped
            continue
        char = text[index]
        if char in "'\"`":
            index = _skip_string(text, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ">" and depth == 0:
            return index
        index += 1
    return index


def _title_expression(tag: str) -> str | None:
    """``"text"`` for ``title="text"``, the expression for ``title={...}``, else None."""
    match = _TITLE.search(tag)
    if not match:
        return None
    if match.group(1) is not None:
        return f'"{match.group(1)}"'
    depth = 1
    index = match.end()
    while index < len(tag) and depth:
        if (skipped := _past_comment(tag, index)) != index:
            index = skipped
            continue
        char = tag[index]
        if char in "'\"`":
            index = _skip_string(tag, index)
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        index += 1
    return tag[match.end() : index - 1]


def _shown_literals(expression: str) -> list[str]:
    """The literals of ``expression`` that can end up as the title's text."""
    shown = []
    index = 0
    while index < len(expression):
        if (skipped := _past_comment(expression, index)) != index:
            index = skipped
            continue
        if expression[index] not in "'\"`":
            index += 1
            continue
        end = _skip_string(expression, index)
        text = expression[index + 1 : end - 1]
        if (
            text.strip()
            and not _COMPARED.search(expression[:index])
            and not _ARGUMENT.search(expression[:index])
            and not _CONCATENATED.match(expression[end:])
        ):
            shown.append(text)
        index = end
    return shown


def unquestioned_titles(source: str) -> list[str]:
    """``line N: <Tag> 'title'`` for each confirmation titled with a statement."""
    found = []
    for component in CONFIRM_COMPONENTS:
        for match in re.finditer(rf"<{component}(?=[\s>/])", source):
            tag = source[match.start() : _opening_tag_end(source, match.end()) + 1]
            expression = _title_expression(tag)
            if expression is None:
                continue
            found.extend(
                f"line {_line(source, match.start())}: <{component}> {text!r}"
                for text in _shown_literals(expression)
                if not text.rstrip().endswith("?")
            )
    return found


def test_the_scan_reads_the_titles_it_polices() -> None:
    """A change to the attribute or the components would otherwise pass on nothing."""
    titled = 0
    for path in _sources():
        source = path.read_text(encoding="utf-8")
        for component in CONFIRM_COMPONENTS:
            for match in re.finditer(rf"<{component}(?=[\s>/])", source):
                tag = source[match.start() : _opening_tag_end(source, match.end()) + 1]
                expression = _title_expression(tag)
                titled += bool(expression and _shown_literals(expression))
    assert titled > 40, f"read only {titled} literal titles; is the scan looking elsewhere?"


def test_every_confirmation_title_is_a_question() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := unquestioned_titles(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These confirmations are titled with a statement; ask the question the confirm "
        "button answers, naming what it acts on ('Retire C-0001?', 'Delete this draft "
        f"bill?'):\n{offenders}"
    )


def test_the_rule_finds_the_statements_that_shipped() -> None:
    source = "\n".join(
        [
            "<ConfirmDialog",
            "  title={blocked ? 'Approve over a match variance' : 'Approve this invoice'}",
            "/>",
            "<ReasonDialog",
            "  title={`Replace final QC lot for line ${line.line_number}`}",
            "/>",
            '<ConfirmDialog title="Retire" />',
        ]
    )

    assert unquestioned_titles(source) == [
        "line 1: <ConfirmDialog> 'Approve over a match variance'",
        "line 1: <ConfirmDialog> 'Approve this invoice'",
        "line 7: <ConfirmDialog> 'Retire'",
        "line 4: <ReasonDialog> 'Replace final QC lot for line ${line.line_number}'",
    ]


def test_the_rule_reads_past_conditions_concatenations_fallbacks_and_comments() -> None:
    source = "\n".join(
        [
            "<ConfirmDialog",
            "  title={command === 'delete' ? 'Delete this draft bill?' : 'Send this bill?'}",
            "/>",
            "<ConfirmDialog title={'Confirm ' + count.code + '?'} />",
            "<ConfirmDialog title={copy ? copy.title.replace('this order', order.code) : ''} />",
            "<ConfirmDialog title={pendingPost ? `Post ${pendingPost.code ?? ''}?` : ''} />",
            "<ConfirmDialog",
            "  title={copy?.title ?? ''}",
            "  // the order's status, said afterwards",
            "  description='Not a title'",
            "/>",
        ]
    )

    assert unquestioned_titles(source) == []
