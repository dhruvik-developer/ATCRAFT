"""A document names a migration revision that exists, or names it as history.

The 8 November squash replaced thirty-eight revisions with one baseline, and
the documents went on describing the retired ones as current: docs/database.md
- binding - gave the lock bounds and downgrade refusals of eight revisions that
no longer exist and cited two deleted migration tests; the runbook and the
squash note promised that ``alembic upgrade head`` would run nothing after the
stamp, and printed ``20261127_0100 (head)``, after ``20261128_0100`` had landed.
Nothing failed: a revision id is prose.

The rules, over README.md, AGENTS.md, CLAUDE.md, ONE-BOX-RUNBOOK.md and
docs/*.md (not docs/implementation, whose dated records name what was current
then):

* every revision id (``NNNNNNNN_NNNN``) a document names is a file in
  ``migrations/versions``, or is listed below as history that document keeps on
  purpose - and every listed id is still named, so the list only shrinks;
* a document that prints ``<revision> (head)`` - these and the squash note the
  baseline's docstring points at - names the current head.

At the next squash every id these documents name as current becomes history,
and this test sends whoever squashes to each sentence that still says otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

import pytest

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

VERSIONS = API_ROOT / "migrations" / "versions"
TOP_LEVEL_DOCUMENTS = ("README.md", "AGENTS.md", "CLAUDE.md", "ONE-BOX-RUNBOOK.md")

_REVISION = re.compile(r"(?<![\w-])(\d{8}_\d{4})(?![\w-])")
_HEAD_CLAIM = re.compile(r"(?<![\w-])(\d{8}_\d{4})(?![\w-])\s*\(head\)")
_DECLARED = re.compile(r'^revision: str = "([^"]+)"', re.MULTILINE)
_REVISES = re.compile(r'^down_revision: str \| None = (?:"([^"]+)"|None)', re.MULTILINE)
_SQUASH_NOTE = re.compile(r"docs/implementation/migration-squash-\d{8}\.md")

#: Retired revisions a document names on purpose, as history: what a section
#: once added, the former head a pre-squash database must sit at, the revision a
#: rehearsal migrated to. Each entry is a sentence someone checked says "retired",
#: "formerly" or "before the squash".
HISTORY: Mapping[str, frozenset[str]] = {
    # The revision a pre-squash database must record before it is stamped.
    "ONE-BOX-RUNBOOK.md": frozenset({"20261126_0100"}),
    "docs/deployment.md": frozenset({"20261126_0100"}),
    "docs/database.md": frozenset(
        {
            "20261030_0100",
            "20261101_0100",
            "20261102_0100",
            "20261103_0100",
            "20261104_0100",
            "20261124_0100",
            "20261126_0100",
        }
    ),
    "docs/assumptions.md": frozenset(
        {
            "20261021_0100",
            "20261026_0100",
            "20261028_0100",
            "20261101_0100",
            "20261106_0100",
            "20261107_0100",
            "20261108_0100",
            "20261109_0100",
            "20261110_0100",
            "20261113_0100",
            "20261115_0100",
        }
    ),
    # Dated rehearsal records: the database each run was migrated to.
    "docs/local-seed-data.md": frozenset({"20261031_0100", "20261102_0100"}),
    "docs/ui-readiness.md": frozenset({"20261016_1100"}),
}


def revisions(sources: Iterable[str]) -> tuple[set[str], set[str]]:
    """Every revision the migration files declare, and the heads among them."""
    declared: set[str] = set()
    revised: set[str] = set()
    for source in sources:
        declared |= set(_DECLARED.findall(source))
        revised |= {parent for parent in _REVISES.findall(source) if parent}
    return declared, declared - revised


def unlisted_revisions(
    name: str, document: str, current: set[str], history: Mapping[str, frozenset[str]]
) -> list[str]:
    """Each revision ``document`` names that is neither current nor its listed history."""
    named = set(_REVISION.findall(document))
    return [
        f"{name} names {revision}, which is no file in migrations/versions"
        for revision in sorted(named - current - history.get(name, frozenset()))
    ]


def stale_history(names: Mapping[str, str], history: Mapping[str, frozenset[str]]) -> list[str]:
    """Listed history a document no longer names - the list only shrinks."""
    return [
        f"{name} no longer names {revision}; drop it from HISTORY"
        for name, listed in sorted(history.items())
        for revision in sorted(listed - set(_REVISION.findall(names.get(name, ""))))
    ]


def head_claim_faults(name: str, document: str, heads: set[str]) -> list[str]:
    """Each ``<revision> (head)`` in ``document`` that is not the current head."""
    return [
        f"{name} prints {claimed} (head), but the head is {', '.join(sorted(heads))}"
        for claimed in _HEAD_CLAIM.findall(document)
        if claimed not in heads
    ]


def _migration_sources() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in sorted(VERSIONS.glob("*.py"))]


def current_documents() -> dict[str, str]:
    paths = [REPO_ROOT / name for name in TOP_LEVEL_DOCUMENTS] + sorted(
        (REPO_ROOT / "docs").glob("*.md")
    )
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    }


def squash_notes(sources: Iterable[str]) -> dict[str, str]:
    """The squash notes a revision's docstring sends an operator to."""
    names = {note for source in sources for note in _SQUASH_NOTE.findall(source)}
    return {name: (REPO_ROOT / name).read_text(encoding="utf-8") for name in sorted(names)}


def test_the_rules_read_the_chain_and_the_documents() -> None:
    declared, heads = revisions(_migration_sources())
    assert {"20261127_0100", "20261128_0100"} <= declared
    assert len(heads) == 1
    documents = current_documents()
    assert {"README.md", "CLAUDE.md", "docs/database.md", "docs/deployment.md"} <= set(documents)
    assert not any(name.startswith("docs/implementation/") for name in documents)
    assert "docs/implementation/migration-squash-20261127.md" in squash_notes(_migration_sources())


def test_every_revision_a_document_names_exists_or_is_history() -> None:
    declared, _ = revisions(_migration_sources())
    documents = current_documents()
    faults = [
        fault
        for name, text in documents.items()
        for fault in unlisted_revisions(name, text, declared, HISTORY)
    ]
    faults += stale_history(documents, HISTORY)
    assert not faults, (
        "A document names a migration revision that does not exist. Say what is current "
        "(the baseline, or the revision that replaced it), or - where the sentence is "
        "history and says so - list the id in HISTORY:\n" + "\n".join(faults)
    )


def test_every_head_claim_names_the_head() -> None:
    sources = _migration_sources()
    _, heads = revisions(sources)
    documents = current_documents() | squash_notes(sources)
    faults = [
        fault for name, text in documents.items() for fault in head_claim_faults(name, text, heads)
    ]
    assert not faults, "\n".join(faults)


BASELINE = '''"""Baseline.

See ``docs/implementation/migration-squash-20261127.md``.
"""
revision: str = "20261127_0100"
down_revision: str | None = None
'''
FOLLOWER = """revision: str = "20261128_0100"
down_revision: str | None = "20261127_0100"
"""


def test_the_chain_reader_finds_the_head_and_the_squash_note() -> None:
    assert revisions([BASELINE, FOLLOWER]) == (
        {"20261127_0100", "20261128_0100"},
        {"20261128_0100"},
    )
    assert revisions([BASELINE]) == ({"20261127_0100"}, {"20261127_0100"})
    assert set(_SQUASH_NOTE.findall(BASELINE)) == {
        "docs/implementation/migration-squash-20261127.md"
    }


def test_the_rules_find_a_retired_revision_a_stale_head_and_unused_history() -> None:
    current = {"20261127_0100", "20261128_0100"}
    document = (
        "Revision `20261124_0100` takes an exclusive lock on downgrade.\n"
        "alembic current        # 20261127_0100 (head)\n"
        "Retired revision `20261103_0100` backfilled these rows.\n"
        "The file 20261128_0100_stock_batch_conversions.py and `20261128_0100`.\n"
    )
    history = {"docs/database.md": frozenset({"20261103_0100", "20261030_0100"})}

    assert unlisted_revisions("docs/database.md", document, current, history) == [
        "docs/database.md names 20261124_0100, which is no file in migrations/versions"
    ]
    assert head_claim_faults("note.md", document, {"20261128_0100"}) == [
        "note.md prints 20261127_0100 (head), but the head is 20261128_0100"
    ]
    assert head_claim_faults("note.md", document, {"20261127_0100"}) == []
    assert stale_history({"docs/database.md": document}, history) == [
        "docs/database.md no longer names 20261030_0100; drop it from HISTORY"
    ]
