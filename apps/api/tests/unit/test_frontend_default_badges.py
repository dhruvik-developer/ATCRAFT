"""The default of its kind is marked with ``DefaultBadge``, never a hand-made badge.

``apps/web/src/components/default-badge.tsx`` is the one marker for the default
bill of materials, routing, warehouse or address. Before it the same fact wore
three colours: green on the bill and its list, grey on the item's where-used
panel and the brand colour on the warehouse list, so one bill read as a
different state depending on the screen it was read from.

The rule fails on a ``<Badge>`` whose whole text is "Default" - an icon beside
it included - anywhere in the product source but the component itself.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_frontend_mutation_error_toasts import WEB_SRC, _line

pytestmark = pytest.mark.unit

COMPONENT = WEB_SRC / "components" / "default-badge.tsx"

#: A badge whose only text is "Default", optionally after one self-closing icon.
_DEFAULT_BADGE = re.compile(
    r"<Badge\b[^>]*>\s*(?:<[A-Z]\w*\b[^>]*/>\s*)?Default\s*</Badge>",
    re.S,
)


def hand_made_default_badges(source: str) -> list[str]:
    """``line N`` for each badge that marks a default by hand."""
    return [f"line {_line(source, found.start())}" for found in _DEFAULT_BADGE.finditer(source)]


def _sources() -> list:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts
        and not path.name.endswith(".test.tsx")
        and path != COMPONENT
    )


def test_the_component_the_rule_points_to_exists() -> None:
    assert "export function DefaultBadge(" in COMPONENT.read_text(encoding="utf-8")


def test_every_default_marker_is_the_shared_badge() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := hand_made_default_badges(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These draw their own 'Default' badge; render <DefaultBadge /> from "
        f"components/default-badge.tsx so a default reads the same everywhere:\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            '{row.original.is_default && <Badge variant="secondary">Default</Badge>}',
            '<Badge variant="default" className="gap-1">',
            '  <Star className="h-3 w-3" aria-hidden />',
            "  Default",
            "</Badge>",
            '{bom.is_default && <Badge variant="success">Default</Badge>}',
        ]
    )

    assert hand_made_default_badges(source) == ["line 1", "line 2", "line 6"]


def test_the_rule_leaves_the_component_and_other_badges_alone() -> None:
    source = "\n".join(
        [
            "{row.original.is_default && <DefaultBadge />}",
            '<Badge variant="success">Primary</Badge>',
            '<Badge variant="secondary">Default recipe</Badge>',
            '<Badge variant="secondary">{humanise(row.original.bom_type)}</Badge>',
            "<SelectItem value={DEFAULT_BOM}>Default recipe for this item</SelectItem>",
        ]
    )

    assert hand_made_default_badges(source) == []
