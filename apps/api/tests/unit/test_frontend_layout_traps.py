"""A group that wraps its children must be allowed to shrink.

``shrink-0`` (or ``flex-none``) on a flex item pins it at its max-content width
- every child on one line - so a ``flex-wrap`` on the same element never gets
the chance to fire: the group runs past its container instead. CLAUDE.md
records the trap on ``PageHeader``, whose five actions ran off a phone, and it
came back in the packing dialog's carton header, where Print label, Seal carton
and Remove forced the dialog to scroll sideways at 320px.

The rule reads each quoted class string in ``apps/web/src`` and fails when one
variant (unprefixed, ``sm:``, ...) sets ``flex-wrap`` together with
``shrink-0`` or ``flex-none``. Comment lines are skipped, so the history of the
trap can still be written down beside the code it happened in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: Utilities that stop a flex item shrinking below its max-content width.
_NO_SHRINK = frozenset({"shrink-0", "flex-none"})

_QUOTED = re.compile(r"""(['"`])([^'"`\n]*?)\1""")
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")


def _variant(token: str) -> tuple[str, str]:
    """``("sm:", "shrink-0")`` for ``sm:shrink-0``; ``("", token)`` when unprefixed."""
    prefix, _, utility = token.rpartition(":")
    return (f"{prefix}:" if prefix else "", utility)


def wrap_traps(source: str) -> list[str]:
    """``line N: 'classes'`` for each class string that wraps a group it pins."""
    found: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if _COMMENT_LINE.match(line):
            continue
        for _, text in _QUOTED.findall(line):
            utilities: dict[str, set[str]] = {}
            for token in text.split():
                prefix, utility = _variant(token)
                utilities.setdefault(prefix, set()).add(utility)
            if any("flex-wrap" in names and names & _NO_SHRINK for names in utilities.values()):
                found.append(f"line {number}: {text!r}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.tsx", "*.ts")
        for path in WEB_SRC.rglob(pattern)
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_wrapping_groups_it_polices() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    wrapping = sum(path.read_text("utf-8").count("flex-wrap") for path in _sources())
    assert wrapping > 100, f"found only {wrapping} flex-wrap classes; is the scan elsewhere?"


def test_no_wrapping_group_is_pinned_at_its_one_line_width() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := wrap_traps(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These class strings set flex-wrap and shrink-0/flex-none on one element, so the "
        "group never wraps and overflows on a phone:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nDrop shrink-0: a wrapping group must be allowed to shrink (CLAUDE.md, "
        "'shrink-0 on a group that is itself flex-wrap defeats the wrap')."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_pinned_wrapping_group_however_it_is_written() -> None:
    source = (
        '<div className="flex shrink-0 flex-wrap items-center gap-1">\n'
        "const actions = cn('flex flex-none flex-wrap', className)\n"
        "const group = `sm:flex sm:flex-wrap sm:shrink-0`\n"
    )
    assert wrap_traps(source) == [
        "line 1: 'flex shrink-0 flex-wrap items-center gap-1'",
        "line 2: 'flex flex-none flex-wrap'",
        "line 3: 'sm:flex sm:flex-wrap sm:shrink-0'",
    ]


def test_the_rule_allows_what_only_looks_alike() -> None:
    source = (
        '<div className="flex flex-wrap items-center gap-1">\n'
        '<span className="shrink-0 whitespace-nowrap">\n'
        "'inline-flex whitespace-nowrap [&_svg]:shrink-0'\n"
        '<div className="flex flex-wrap md:shrink-0">\n'
        "// The actions box was 'flex shrink-0 flex-wrap', which defeated the wrap.\n"
        "{/* It was 'flex shrink-0 flex-wrap' before. */}\n"
    )
    assert wrap_traps(source) == []
