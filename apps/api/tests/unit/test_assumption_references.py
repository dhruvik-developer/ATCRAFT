"""Every assumption a citation names is one docs/assumptions.md records.

CLAUDE.md sends a reader from an ``ASSUMPTION:`` comment to docs/assumptions.md,
the one place the owner reviews a business rule. Nineteen citations in the
allocation, sales, assembly, job-work, dispatch and packing code named sections
E2-1, E2-2 and G1-1 to G1-4 - one reservation per position, a reservation
closed elsewhere, sales-only dispatch, the packing-order claim, the boxes a
cancellation gives back, the statuses a tracking event may not reach - and none
of them had been written. One more citation had lost its section sign to a
question mark, and nine to the two characters a UTF-8 section sign becomes when
it is read as Latin-1 and saved again. Nothing failed: a citation is prose.

The rules, over the backend's source, migrations and tests, the web app's
source and the top-level documents:

* a lane-shaped id after a section sign - ``E2-2``, ``INPUT-1``,
  ``JW-batch-1``, ``D3-n`` - names a ``## §<id>.`` heading of
  docs/assumptions.md, and so does any id written straight after
  ``assumptions.md`` (``docs/assumptions.md §51``, ``... section 23``). A bare
  number after a section sign is not judged on its own: the master rules and
  the proposal number their sections too, and neither lives in this repository;
* a section sign is never written as a question mark before an id, or as the
  Latin-1 reading of its two UTF-8 bytes.

docs/implementation is not read: the lane specs name sections by placeholder
(``E2-n``) before anyone writes them, and the reports there are dated records.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

ASSUMPTIONS = REPO_ROOT / "docs" / "assumptions.md"

#: Where citations are written: code, migrations, tests and the documents a
#: reader starts from. docs/*.md is added below, without docs/implementation.
SOURCE_ROOTS = (
    API_ROOT / "src",
    API_ROOT / "migrations",
    API_ROOT / "tests",
    REPO_ROOT / "apps" / "web" / "src",
)
TOP_LEVEL_DOCUMENTS = ("README.md", "AGENTS.md", "CLAUDE.md", "ONE-BOX-RUNBOOK.md")
SUFFIXES = frozenset({".py", ".ts", ".tsx", ".md"})

#: A UTF-8 section sign read as Latin-1 and written back as UTF-8.
MANGLED_SIGN = "Â§"

_LANE_ID = r"[A-Z][A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
_HEADING = re.compile(r"^## (?:§([A-Za-z0-9-]+)|(\d+))\.", re.MULTILINE)
_LANE_CITATION = re.compile(rf"§\s?({_LANE_ID})")
_NAMED_CITATION = re.compile(rf"assumptions\.md[`)]?,?\s+(?:§\s?|sections?\s+)(\d+|{_LANE_ID})")
_LOST_SIGN = re.compile(rf"\(\?{_LANE_ID}\)")


def recorded_sections(document: str) -> set[str]:
    """The ids of the document's ``## §<id>.`` and ``## <n>.`` headings."""
    return {lane or number for lane, number in _HEADING.findall(document)}


def citation_faults(source: str, recorded: set[str]) -> list[str]:
    """Each citation in ``source`` naming no recorded section, or written without its sign."""
    faults: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        cited = {match.group(1) for match in _LANE_CITATION.finditer(line)}
        cited |= {match.group(1) for match in _NAMED_CITATION.finditer(line)}
        faults.extend(
            f"line {number}: §{section} is not a heading of docs/assumptions.md"
            for section in sorted(cited - recorded)
        )
        if MANGLED_SIGN in line or _LOST_SIGN.search(line):
            faults.append(f"line {number}: a section sign was lost: {line.strip()}")
    return faults


def citing_files() -> list[Path]:
    """Every file the rule reads - except this one, whose self-tests hold the faults."""
    paths = [
        path
        for root in SOURCE_ROOTS
        for path in root.rglob("*")
        if path.suffix in SUFFIXES and path.is_file()
    ]
    paths += (REPO_ROOT / "docs").glob("*.md")
    paths += (REPO_ROOT / name for name in TOP_LEVEL_DOCUMENTS if (REPO_ROOT / name).is_file())
    this = Path(__file__).resolve()
    return sorted(
        {
            path
            for path in paths
            if "__pycache__" not in path.parts
            and "node_modules" not in path.parts
            and path.resolve() != this
        }
    )


@pytest.fixture(scope="module")
def recorded() -> set[str]:
    return recorded_sections(ASSUMPTIONS.read_text(encoding="utf-8"))


def test_the_scan_reads_the_places_citations_are_written(recorded: set[str]) -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in citing_files()}
    assert {
        "apps/api/src/modules/sales/service.py",
        "apps/api/tests/unit/test_allocation_position_plan.py",
        "apps/web/src/components/shared.tsx",
        "docs/database.md",
        "README.md",
    } <= names
    assert not any(name.startswith("docs/implementation/") for name in names)
    assert {"2", "E2-2", "G1-4", "INPUT-1", "JW-batch-1"} <= recorded


def test_every_cited_assumption_is_recorded(recorded: set[str]) -> None:
    faults = [
        f"{path.relative_to(REPO_ROOT).as_posix()} {fault}"
        for path in citing_files()
        for fault in citation_faults(path.read_text(encoding="utf-8"), recorded)
    ]
    assert not faults, (
        "A citation names no section of docs/assumptions.md, or lost its section sign. "
        "Write the section (Assumed / Why / Where / Impact if wrong) or cite the one "
        "that records the rule:\n" + "\n".join(faults)
    )


def test_the_rule_finds_an_unwritten_section_and_a_lost_sign() -> None:
    source = (
        "    ASSUMPTION (docs/assumptions.md §E2-1): skipped rather than refused.\n"
        "    # held at each position (§E2-2); the boxes go back (§G1-3).\n"
        "    # ASSUMPTION (?D3-n): timestamp it at observation time.\n"
        "    (docs/assumptions.md Â§51), and a foreign key\n"
        '    """docs/assumptions.md section 99 states the number."""\n'
    )

    assert citation_faults(source, {"E2-2", "51"}) == [
        "line 1: §E2-1 is not a heading of docs/assumptions.md",
        "line 2: §G1-3 is not a heading of docs/assumptions.md",
        "line 3: a section sign was lost: # ASSUMPTION (?D3-n): timestamp it at observation time.",
        "line 4: a section sign was lost: (docs/assumptions.md Â§51), and a foreign key",
        "line 5: §99 is not a heading of docs/assumptions.md",
    ]


def test_the_rule_passes_recorded_sections_and_other_documents_numbers() -> None:
    source = (
        "    (docs/assumptions.md §E2-2), the lot rule (§JW-batch-1) and §51\n"
        "    docs/assumptions.md section 23, and `docs/assumptions.md` §E1-2\n"
        "    two identical windows store different ratings (§84).\n"
        "    ASSUMPTION: ADMIN watches inbound (proposal Add-on §07).\n"
        "    re.compile(r'(?P<code>[A-Z]+)(?:-\\d+)?') and the B2 rules (§B2-*).\n"
    )

    assert citation_faults(source, {"E2-2", "JW-batch-1", "23", "E1-2"}) == []


def test_the_headings_are_read_as_section_ids() -> None:
    document = (
        "## How to read this\n"
        "## 2. Segregation of duties on approval\n"
        "## §E2-2. A demand line is held by one reservation per position\n"
        "## §JW-batch-1. Batch SKU conversion is one-to-one lot lineage\n"
        "### §Z9-9. Not a section heading\n"
    )

    assert recorded_sections(document) == {"2", "E2-2", "JW-batch-1"}
