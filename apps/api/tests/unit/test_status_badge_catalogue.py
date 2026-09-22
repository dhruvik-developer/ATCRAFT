"""Every lifecycle value the API can return has a status-badge colour.

``StatusBadge`` falls back to ``secondary`` for a status it does not know.
That fallback is what hid ``POSTED``: every goods receipt and stock adjustment
in a live tenant rendered the same grey as a draft, and nothing errored. The
same thing was true of inactive masters, packed orders, quotations under
review and a machine in breakdown — the word was right and the colour was the
unmapped default.

The map is allowed to name a display status the database does not store
(``BLOCKED``, ``LATE``). It is not allowed to omit a member of a ``Status`` or
``State`` enum, because those are the values the screens render.
"""

from __future__ import annotations

import re

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

_CLASS = re.compile(r"^class (\w*(?:Status|State)\w*)\(StrEnum\):", re.M)
_MEMBER = re.compile(r"""^\s+[A-Z][A-Z0-9_]*\s*=\s*["']([A-Z][A-Z0-9_]*)["']""", re.M)
_NEXT = re.compile(r"\n(?:class |[A-Z][A-Z0-9_]+(?::| =))")


def _enum_values() -> set[str]:
    found: set[str] = set()
    root = REPO_ROOT / "apps" / "api" / "src"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "StrEnum" not in text:
            continue
        matches = list(_CLASS.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end() : end]
            cut = _NEXT.search(body)
            if cut:
                body = body[: cut.start()]
            found.update(_MEMBER.findall(body))
    return found


def _badge_keys() -> set[str]:
    shared = REPO_ROOT / "apps" / "web" / "src" / "components" / "shared.tsx"
    text = shared.read_text(encoding="utf-8")
    block = text.split("const STATUS_VARIANTS", 1)[1].split("export function StatusBadge", 1)[0]
    return set(re.findall(r"^\s+([A-Z][A-Z0-9_]+):", block, re.M))


def test_every_lifecycle_value_has_a_badge_colour() -> None:
    missing = sorted(_enum_values() - _badge_keys())
    assert missing == [], f"status badge has no colour for: {missing}"


def test_the_badge_map_is_not_empty() -> None:
    """A parse that matches nothing would make the test above pass vacuously."""
    assert len(_enum_values()) >= 50
    assert len(_badge_keys()) >= 50
