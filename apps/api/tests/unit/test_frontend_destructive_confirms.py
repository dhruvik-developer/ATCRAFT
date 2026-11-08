"""A confirmation that cancels, rejects, retires, deletes, removes or revokes is red.

``ConfirmDialog`` and ``ReasonDialog`` (``apps/web/src/components/shared.tsx``)
take ``destructive`` to show the warning icon and the destructive confirm
button, and ``DropdownMenuItem`` takes it to colour the item that starts one.
Leaving it off renders the irreversible last step of cancelling a consignment
or a material issue in the same primary blue as "Approve" - which shipped on
the dispatch list, the internal-issue tab, the shift roster and the routing
panel while 29 sibling confirmations set it.

The rule reads the label the person presses: a literal ``confirmLabel`` (or a
string literal inside a ``confirmLabel={...}`` expression), and the visible text
of a menu item. A label that starts with one of the verbs below needs the
``destructive`` attribute on the same tag. Labels built entirely at run time -
the dispatch list's ``PendingAction`` - cannot be read here; those configs carry
a typed ``destructive`` field instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: Verbs whose confirmation ends something for good.
DESTRUCTIVE_VERB = re.compile(r"^(?:Cancel|Reject|Retire|Delete|Remove|Revoke)\b")

#: Components whose confirm button follows ``destructive``.
CONFIRM_COMPONENTS = ("ConfirmDialog", "ReasonDialog")

_DESTRUCTIVE_ATTRIBUTE = re.compile(r"(?<![\w-])destructive(?![\w-])")
_STRING_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`$]*)")


def _tag_end(text: str, start: int) -> int:
    """Index of the ``>`` closing the JSX opening tag that begins at ``start``."""
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ">" and depth == 0:
            return index
        index += 1
    return index


def _attribute(tag: str, name: str) -> list[str]:
    """The string literals an attribute's value holds: one for ``x="..."``, all for ``x={...}``."""
    match = re.search(rf"(?<![\w-]){name}=(?:\"([^\"]*)\"|\{{)", tag)
    if not match:
        return []
    if match.group(1) is not None:
        return [match.group(1)]
    depth = 1
    index = match.end()
    while index < len(tag) and depth:
        depth += {"{": 1, "}": -1}.get(tag[index], 0)
        index += 1
    expression = tag[match.end() : index - 1]
    return [next(part for part in found if part) for found in _STRING_LITERAL.findall(expression)]


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def missing_destructive(source: str) -> list[str]:
    """``line N: <Tag> label`` for each destructive-sounding confirmation left primary."""
    found: list[str] = []
    for component in CONFIRM_COMPONENTS:
        for match in re.finditer(rf"<{component}(?=[\s>/])", source):
            tag = source[match.start() : _tag_end(source, match.end()) + 1]
            labels = [
                label for label in _attribute(tag, "confirmLabel") if DESTRUCTIVE_VERB.match(label)
            ]
            if labels and not _DESTRUCTIVE_ATTRIBUTE.search(tag):
                found.append(f"line {_line(source, match.start())}: <{component}> {labels}")
    for match in re.finditer(r"<DropdownMenuItem(?=[\s>/])", source):
        end = _tag_end(source, match.end())
        tag = source[match.start() : end + 1]
        if tag.endswith("/>"):
            continue
        close = source.find("</DropdownMenuItem>", end)
        body = source[end + 1 : close] if close != -1 else ""
        text = " ".join(re.sub(r"<[^>]*>", " ", re.sub(r"\{[^{}]*\}", " ", body)).split())
        if DESTRUCTIVE_VERB.match(text) and not _DESTRUCTIVE_ATTRIBUTE.search(tag):
            found.append(f"line {_line(source, match.start())}: <DropdownMenuItem> {text!r}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_confirmations_it_polices() -> None:
    """A rename of the components would otherwise leave this passing on nothing."""
    confirms = sum(
        len(re.findall(rf"<(?:{'|'.join(CONFIRM_COMPONENTS)})[\s>]", path.read_text("utf-8")))
        for path in _sources()
    )
    assert confirms > 50, (
        f"found only {confirms} confirmation dialogs; is the scan looking elsewhere?"
    )


def test_every_destructive_confirmation_is_marked_destructive() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := missing_destructive(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These confirmations end something for good but render the primary confirm:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nAdd `destructive` to the tag (or a typed destructive field to a dynamic "
        "config), so the last step reads differently from 'Approve'."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_primary_confirmations_and_menu_items() -> None:
    source = (
        '<ConfirmDialog open title="Cancel?" confirmLabel="Cancel dispatch" onConfirm={go} />\n'
        "<ReasonDialog\n"
        "  open\n"
        "  confirmLabel={verb === 'reject' ? 'Reject issue' : 'Cancel issue'}\n"
        "/>\n"
        "<DropdownMenuItem onSelect={() => cancel(row)}>\n"
        "  <Ban />\n"
        "  Cancel dispatch\n"
        "</DropdownMenuItem>\n"
    )
    assert missing_destructive(source) == [
        "line 1: <ConfirmDialog> ['Cancel dispatch']",
        "line 2: <ReasonDialog> ['Reject issue', 'Cancel issue']",
        "line 6: <DropdownMenuItem> 'Cancel dispatch'",
    ]


def test_the_rule_accepts_marked_and_non_destructive_confirmations() -> None:
    source = (
        '<ConfirmDialog confirmLabel="Cancel dispatch" destructive onConfirm={go} />\n'
        "<ReasonDialog destructive={verb !== 'approve'} confirmLabel={label('Retire')} />\n"
        '<ConfirmDialog confirmLabel="Approve order" cancelLabel="Cancel" />\n'
        "<ConfirmDialog confirmLabel={pending.confirmLabel} />\n"
        "<DropdownMenuItem destructive onSelect={go}><Ban />Cancel order</DropdownMenuItem>\n"
        "<DropdownMenuItem onSelect={go}>Mark ready</DropdownMenuItem>\n"
        '<ConfirmDialog confirmLabel="Cancellation fee waived" />\n'
    )
    assert missing_destructive(source) == []
