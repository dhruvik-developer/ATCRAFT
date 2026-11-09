"""Every entry in the frontend message catalogue is said somewhere.

``apps/web/src/lib/messages.ts`` is the shared vocabulary: an entry there is a
claim that this is how the situation is worded on every screen. An entry no
screen reads is the opposite - it advertises a canonical wording nobody uses.
Seven did: four confirmation prompts (``unsavedChanges``, ``confirmRetire``,
``confirmDelete``, ``confirmCancel``) while every confirmation dialog wrote its
own copy, ``empty.search``, ``validation.nonnegative`` and
``errors.sessionExpired``. An author reaching for the catalogue could not tell
which wording was the real one.

The rule reads the keys of each object the catalogue exports
(``export const errors = { ... }``) and looks for ``errors.key`` anywhere under
``apps/web/src`` outside a test, the catalogue itself included - ``errorTitle``
reads ``errors.duplicate`` - and ``messages.errors.key`` matches too. A key used
only by a test is still one no person ever reads. Delete the entry, or use it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
MESSAGES = WEB_SRC / "lib" / "messages.ts"

_OBJECT = re.compile(r"^export const (?P<name>\w+) = \{\n(?P<body>.*?)^\}", re.S | re.M)
#: A property of the object itself: two spaces in, then ``key:``. Nested
#: objects, comments and shorthand properties (``messages = { success, ... }``)
#: are not keys of their own.
_KEY = re.compile(r"^  (\w+):", re.M)


def catalogue_keys(source: str) -> dict[str, list[str]]:
    """``{object: [key, ...]}`` for each object literal the catalogue exports."""
    return {
        found.group("name"): _KEY.findall(found.group("body")) for found in _OBJECT.finditer(source)
    }


def unread_keys(catalogue: dict[str, list[str]], sources: list[str]) -> list[str]:
    """``object.key`` for each entry no source reads."""
    corpus = "\n".join(sources)
    return [
        f"{name}.{key}"
        for name, keys in catalogue.items()
        for key in keys
        if not re.search(rf"\b{name}\.{key}\b", corpus)
    ]


def _product_sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and not path.name.endswith((".test.ts", ".test.tsx"))
        and path.relative_to(WEB_SRC).parts[0] != "test"
    )


def test_the_scan_reads_the_catalogue_it_polices() -> None:
    """A reshaped catalogue would otherwise leave this passing on nothing."""
    catalogue = catalogue_keys(MESSAGES.read_text(encoding="utf-8"))
    for name in ("success", "errors", "empty", "prompts", "validation"):
        assert catalogue.get(name), f"found no keys in `{name}`; is the scan reading messages.ts?"
    assert sum(len(keys) for keys in catalogue.values()) > 80


def test_every_catalogue_entry_is_read_by_the_product() -> None:
    catalogue = catalogue_keys(MESSAGES.read_text(encoding="utf-8"))
    sources = [path.read_text(encoding="utf-8") for path in _product_sources()]

    unread = unread_keys(catalogue, sources)

    assert not unread, (
        f"lib/messages.ts holds {len(unread)} entr(ies) nothing outside a test reads: "
        f"{unread}. Use the shared wording where the situation arises, or delete it."
    )


def test_the_rule_finds_an_entry_nothing_reads() -> None:
    catalogue = catalogue_keys(
        "export const prompts = {\n"
        "  /** Under the reason box of a status change. */\n"
        "  reasonRequired: 'Give a reason',\n"
        "  confirmDelete: (noun: string) =>\n"
        "    `Delete this ${noun.toLowerCase()}? This cannot be undone.`,\n"
        "} as const\n"
        "\n"
        "export const messages = {\n"
        "  prompts,\n"
        "} as const\n"
    )

    assert catalogue == {"prompts": ["reasonRequired", "confirmDelete"], "messages": []}
    assert unread_keys(catalogue, ["<FieldError message={prompts.reasonRequired} />"]) == [
        "prompts.confirmDelete"
    ]
    assert (
        unread_keys(catalogue, ["messages.prompts.confirmDelete", "prompts.reasonRequired"]) == []
    )
