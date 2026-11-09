"""A section heading in a screen is ``SectionHeader``, at one size.

Forty section headings were drawn by hand as raw ``<h2>`` elements in ten class
sets - 12px all-caps eyebrows, 14, 16 and 18px - so sibling admin and finance
screens ranked the same level of content three ways, and the page outline read
differently from screen to screen. ``SectionHeader`` in
``components/shared.tsx`` draws every one at ``text-base``, with a muted
description and the section's actions beside it.

The rule reads ``apps/web/src/features`` and fails on a raw ``<h2>`` unless it
is listed in ``OWN_SIZE`` with its reason. Comment lines are skipped, so the
history can still be told beside the code it happened in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
FEATURES = WEB_SRC / "features"

#: (screen, the heading's content) -> why it keeps a size of its own.
OWN_SIZE = {
    (
        "features/dealer-portal/dealer-catalog-pages.tsx",
        "{slide.title}",
    ): "a promotion banner's headline, sized with the banner's artwork",
}

_H2 = re.compile(r"<h2\b[^>]*>(?P<body>.*?)</h2>", re.S)
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")


def raw_section_headings(source: str) -> list[tuple[int, str]]:
    """``(line, content)`` for each raw ``<h2>`` the source writes."""
    commented = {
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if _COMMENT_LINE.match(line)
    }
    found: list[tuple[int, str]] = []
    for match in _H2.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        if line not in commented:
            found.append((line, " ".join(match["body"].split())))
    return found


def _screens() -> list[Path]:
    return sorted(
        path
        for path in FEATURES.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def _name(path: Path) -> str:
    return path.relative_to(WEB_SRC).as_posix()


def test_the_scan_reaches_the_section_headings() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    uses = sum(path.read_text("utf-8").count("<SectionHeader") for path in _screens())
    assert uses >= 35, f"found only {uses} SectionHeader uses; is the scan elsewhere?"


def test_every_section_heading_is_a_section_header() -> None:
    offenders = {
        _name(path): [
            f"line {line}: {body[:60]}"
            for line, body in found
            if (_name(path), body) not in OWN_SIZE
        ]
        for path in _screens()
        if (found := raw_section_headings(path.read_text(encoding="utf-8")))
    }
    offenders = {name: found for name, found in offenders.items() if found}
    assert not offenders, (
        "These screens draw a section heading by hand, at a size of their own:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nUse SectionHeader from components/shared.tsx (title, description, actions; "
        "className for spacing only)."
    )


@pytest.mark.parametrize(("name", "body"), sorted(OWN_SIZE))
def test_each_exemption_still_names_a_heading_that_exists(name: str, body: str) -> None:
    source = (WEB_SRC / name).read_text(encoding="utf-8")
    assert body in [found for _, found in raw_section_headings(source)], (
        f"{name} no longer draws the heading {body}; remove it from OWN_SIZE"
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            '<h2 className="text-lg font-semibold">Accounts</h2>',
            '<h2 className="mb-3 text-sm font-semibold uppercase tracking-wide">',
            "  Run history",
            "</h2>",
            '<h2 id="carton-scan-heading" className="font-semibold">',
            "  Find a carton",
            "</h2>",
            '<h2 className="mb-2 text-sm font-semibold">',
            "  Load against capacity",
            '  <span className="ml-2 font-normal">Planned / available minutes</span>',
            "</h2>",
        ]
    )
    assert [line for line, _ in raw_section_headings(source)] == [1, 2, 5, 8]


def test_the_rule_ignores_the_shared_header_and_history() -> None:
    source = "\n".join(
        [
            '<SectionHeader title="Accounts" className="mb-3" />',
            "// It was an <h2> at text-lg before. </h2>",
            "<CardTitle asChild><h1>Sign in</h1></CardTitle>",
            "screen.getByRole('heading', { name: 'Accounts', level: 2 })",
        ]
    )
    assert raw_section_headings(source) == []
