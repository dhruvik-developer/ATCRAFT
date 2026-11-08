"""The frontend has exactly one reason dialog.

``ReasonDialog`` in ``apps/web/src/components/shared.tsx`` is the confirmation
that collects an audit reason: it opens empty, holds the API's
``REASON_MIN_LENGTH`` floor before the round trip, says so in the shared
``errors.reasonTooShort`` sentence, and forgets the reason when it closes.

Five features had grown their own copy - assembly, packing, customer and vendor
relations, the item masters - and they had drifted the way copies do: one
nested its reason box 24px inside its own title, one let the last reason greet
the next removal, and each said the same rule in a different sentence. A
private ``*ReasonDialog`` / ``*WithReasonDialog`` / ``*RemovalDialog`` in a
feature is how the sixth copy starts, so this fails the build on one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CANONICAL = WEB_SRC / "components" / "shared.tsx"
FEATURES = WEB_SRC / "features"

#: A component declared under a reason-capture name: function, bound arrow or class.
_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:function|const|let|var|class)\s+"
    r"(?P<name>\w*(?:Reason|Removal)Dialog)\b",
    re.MULTILINE,
)


def reason_dialog_declarations(source: str) -> list[str]:
    """``line N: Name`` for every reason-capture dialog the source declares."""
    return [
        f"line {source.count(chr(10), 0, match.start()) + 1}: {match.group('name')}"
        for match in _DECLARATION.finditer(source)
    ]


def _feature_sources() -> list[Path]:
    return sorted(
        path
        for path in FEATURES.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_shared_dialog_is_where_the_features_import_it_from() -> None:
    """If this moves, the guard below would pass on an empty set of copies."""
    assert CANONICAL.is_file(), f"{CANONICAL} is gone; every reason confirmation imports it"
    declared = reason_dialog_declarations(CANONICAL.read_text(encoding="utf-8"))
    assert [found.split(": ", 1)[1] for found in declared] == ["ReasonDialog"], (
        f"components/shared.tsx should declare exactly one reason dialog, got {declared}"
    )
    assert len(_feature_sources()) > 100, "feature discovery found too few files"


def test_no_feature_declares_its_own_reason_dialog() -> None:
    copies = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _feature_sources()
        if (found := reason_dialog_declarations(path.read_text(encoding="utf-8")))
    }
    assert not copies, (
        "A feature declares its own reason dialog:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in copies.items())
        + "\n\nImport ReasonDialog from '@/components/shared' instead. It holds the "
        "API's minimum before the request, reports it with errors.reasonTooShort and "
        "resets when the dialog closes; a private copy re-decides all three."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_every_spelling_of_a_private_copy() -> None:
    source = (
        "export function ReasonDialog() {}\n"
        "function DeleteWithReasonDialog() {}\n"
        "const RemovalDialog = () => null\n"
        "  export const RetireWithReasonDialog = memo(Inner)\n"
    )
    assert reason_dialog_declarations(source) == [
        "line 1: ReasonDialog",
        "line 2: DeleteWithReasonDialog",
        "line 3: RemovalDialog",
        "line 4: RetireWithReasonDialog",
    ]


def test_the_rule_ignores_uses_and_unrelated_dialogs() -> None:
    source = (
        "import { ReasonDialog } from '@/components/shared'\n"
        "<ReasonDialog open confirmLabel='Cancel return' />\n"
        "function ReasonCodePicker() {}\n"
        "export function RemoveCartonDialog() {}\n"
    )
    assert reason_dialog_declarations(source) == []
