"""A radio group in the web app is made of native radio inputs.

ARIA's ``role="radio"`` on a ``<button>`` makes a screen reader announce "radio
button, 1 of 3" and supplies none of the behaviour that announcement promises:
no single Tab stop, no arrow keys - the page has to write the roving tabindex
and the key handling itself. The theme toggle, on every screen, shipped exactly
that: three Tab stops and arrow keys that did nothing, under a comment saying
the arrows "come with it". A native ``<input type="radio">`` sharing a ``name``
gets all of it from the browser, so that is the rule:

* no element carries ``role="radio"`` - the native input already has the role;
* a file that declares ``role="radiogroup"`` holds native radio inputs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

_ARIA_RADIO = re.compile(r"""role=(?:"radio"|'radio'|\{\s*['"]radio['"]\s*\})""")
_RADIOGROUP = re.compile(r"""role=(?:"radiogroup"|'radiogroup'|\{\s*['"]radiogroup['"]\s*\})""")
_NATIVE_RADIO = re.compile(r"""type=(?:"radio"|'radio'|\{\s*['"]radio['"]\s*\})""")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def radio_role_violations(source: str) -> list[str]:
    """``line N: ...`` for each ARIA radio, and for a radio group with no native radios."""
    found = [
        f"line {_line(source, match.start())}: role=radio on custom markup"
        for match in _ARIA_RADIO.finditer(source)
    ]
    if not _NATIVE_RADIO.search(source):
        found.extend(
            f"line {_line(source, match.start())}: role=radiogroup with no native radio inputs"
            for match in _RADIOGROUP.finditer(source)
        )
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_radio_groups_it_polices() -> None:
    """A rename or move would otherwise leave this passing on nothing."""
    groups = sum(len(_RADIOGROUP.findall(path.read_text("utf-8"))) for path in _sources())
    assert groups >= 2, f"found only {groups} radio groups; is the scan looking elsewhere?"


def test_every_radio_group_is_made_of_native_radios() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := radio_role_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These radio groups announce a keyboard model they do not implement:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + '\n\nUse <input type="radio" name={sharedName} className="sr-only"> inside each '
        "option's <label> (components/theme-toggle.tsx): the browser then gives the group "
        "one Tab stop and the arrow keys."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_aria_radios_on_buttons() -> None:
    source = (
        '<div role="radiogroup" aria-label="Theme">\n'
        '  <button type="button" role="radio" aria-checked={selected}>Light</button>\n'
        "</div>\n"
    )
    assert radio_role_violations(source) == [
        "line 2: role=radio on custom markup",
        "line 1: role=radiogroup with no native radio inputs",
    ]


def test_the_rule_accepts_native_radios_in_a_group() -> None:
    source = (
        '<div role="radiogroup" aria-label="Theme">\n'
        "  <label>\n"
        '    <input type="radio" name={name} className="sr-only" checked={selected} />\n'
        "  </label>\n"
        "</div>\n"
    )
    assert radio_role_violations(source) == []
