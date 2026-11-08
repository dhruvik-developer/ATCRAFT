"""A tooltip opens from something the keyboard can reach.

Radix opens a tooltip on pointer hover and on keyboard focus. ``TooltipTrigger
asChild`` hands both to its child, so when that child is a plain ``<span>`` the
tooltip has no keyboard path at all: the requisition queue cut each purpose to
one line inside such a span, and the rest of the text - "usually what decides
the approval" - could only be read with a mouse. (Touch never opens a Radix
tooltip either, so text nobody can do without belongs on the page, not in one.)

The rule reads the child of every ``<TooltipTrigger asChild>``: it must be a
control (a button, link or form field, or the ``Button`` / ``Link`` components)
or carry ``tabIndex``. A trigger without ``asChild`` renders its own button and
is fine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: Elements and components that are focusable without a tabIndex.
FOCUSABLE = frozenset({"button", "a", "input", "select", "textarea", "Button", "Link", "NavLink"})

_TRIGGER = re.compile(r"<TooltipTrigger\b([^>]*)>")
_JSX_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.S)
_CHILD = re.compile(r"<([A-Za-z][\w.]*)\b")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def unfocusable_triggers(source: str) -> list[str]:
    """``line N: <child>`` for each tooltip whose trigger the keyboard cannot reach."""
    found: list[str] = []
    for match in _TRIGGER.finditer(source):
        if not re.search(r"(?<![\w-])asChild(?![\w-])", match.group(1)):
            continue
        rest = _JSX_COMMENT.sub(lambda comment: " " * len(comment.group(0)), source[match.end() :])
        child = _CHILD.match(rest.lstrip())
        if child is None:
            found.append(f"line {_line(source, match.start())}: child not readable")
            continue
        start = len(rest) - len(rest.lstrip())
        tag_end = rest.find(">", start)
        tag = rest[start : tag_end + 1]
        if child.group(1) not in FOCUSABLE and "tabIndex=" not in tag:
            found.append(f"line {_line(source, match.start())}: <{child.group(1)}>")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_tooltips_it_polices() -> None:
    """A rename of the trigger would otherwise leave this passing on nothing."""
    triggers = sum(len(_TRIGGER.findall(path.read_text("utf-8"))) for path in _sources())
    assert triggers >= 1, "found no TooltipTrigger; is the scan looking elsewhere?"


def test_every_tooltip_trigger_can_take_focus() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := unfocusable_triggers(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These tooltips open only under a mouse:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nTrigger on a control, or give the wrapper tabIndex={0} (customers-page.tsx) - "
        "and if the tooltip holds text the reader needs, show the text instead."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_plain_span_trigger() -> None:
    source = (
        "<Tooltip>\n"
        "  <TooltipTrigger asChild>\n"
        '    <span className="block max-w-[18rem] cursor-help truncate">{purpose}</span>\n'
        "  </TooltipTrigger>\n"
        "</Tooltip>\n"
    )
    assert unfocusable_triggers(source) == ["line 2: <span>"]


def test_the_rule_accepts_controls_and_focusable_wrappers() -> None:
    source = (
        "<TooltipTrigger asChild>\n"
        "  {/* A disabled button emits no pointer events, so the wrapper carries it. */}\n"
        '  <span className="inline-flex" tabIndex={0}>\n'
        "</TooltipTrigger>\n"
        '<TooltipTrigger asChild><Button size="icon-sm" aria-label="Help"><Info /></Button>\n'
        "</TooltipTrigger>\n"
        "<TooltipTrigger>Why?</TooltipTrigger>\n"
    )
    assert unfocusable_triggers(source) == []
