"""Every assumption a citation names is recorded, and every backend ASSUMPTION cites one.

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

And one rule over the backend's source alone: every comment or docstring
paragraph holding ``ASSUMPTION`` cites, in one of those two forms, a section the
document records. A marker that cites nothing sends its reader nowhere, and the
rule behind it is one the owner never sees: on 16 November, 94 of the 312 places
the backend wrote it cited no section, and nothing failed for them either. A
comment paragraph is the run of ``#`` lines around the marker, up to a bare
``#`` or ``#:`` line or a line opening a ``* `` or ``- `` bullet; a docstring
paragraph is the run of non-blank lines around it, inside its own string
literal.

docs/implementation is not read: the lane specs name sections by placeholder
(``E2-n``) before anyone writes them, and the reports there are dated records.
"""

from __future__ import annotations

import io
import re
import textwrap
import tokenize
from collections.abc import Callable
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

#: Where every paragraph holding the marker must cite a section: the backend's
#: source, where every one of them did when this rule was written.
MARKED_ROOT = API_ROOT / "src"

_MARKER = re.compile(r"\bASSUMPTION\b")
#: A comment's leader, ``#`` or ``#:``, dropped when a paragraph is read as text.
_COMMENT_LEADER = re.compile(r"^\s*#:?\s?")
#: The lines that end one comment paragraph inside a longer comment block.
_COMMENT_BREAKS = frozenset({"#", "#:"})
#: A comment line that opens a ``* `` or ``- `` bullet, which starts a paragraph.
_COMMENT_BULLET = re.compile(r"^\s*#:?\s*[*-]\s")


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


def _continues_comment(line: str) -> bool:
    """Whether ``line`` belongs to the comment paragraph beside it."""
    stripped = line.strip()
    return stripped.startswith("#") and stripped not in _COMMENT_BREAKS


def _opens_bullet(line: str) -> bool:
    """Whether comment ``line`` opens a bullet, and so a paragraph of its own."""
    return bool(_COMMENT_BULLET.match(line))


def _continues_text(line: str) -> bool:
    """Whether ``line`` belongs to the docstring paragraph beside it."""
    return bool(line.strip())


def _opens_nothing(line: str) -> bool:
    """Docstring text: a paragraph runs to its blank line, bullets included."""
    return False


def _string_rows(source: str) -> list[tuple[int, int]]:
    """The first and last line index of every string literal in ``source``."""
    return [
        (token.start[0] - 1, token.end[0] - 1)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.STRING
    ]


def marked_paragraphs(source: str) -> list[tuple[int, str]]:
    """Each comment or docstring paragraph of ``source`` that holds the marker.

    A comment paragraph is the run of ``#`` lines around the marker, ended by a
    bare ``#`` or ``#:`` line, and a line opening a ``* `` or ``- `` bullet
    starts one - two bullets are two rules, and one's citation must not cover
    the other's marker. Any other line holding the marker is docstring text -
    or a comment trailing code - and its paragraph is the run of non-blank
    lines around it, kept inside the string literal it sits in, so code after a
    docstring's closing quotes cannot lend it a citation. That is the rule of
    the scan that found the uncited paragraphs, with a docstring held to its
    own quotes.

    Each paragraph comes back once, as ``(line of its first marker, text)``,
    with comment leaders dropped and its lines joined by single spaces - a
    citation wrapped across two lines reads as one.
    """
    lines = source.splitlines()
    last = len(lines) - 1
    strings: list[tuple[int, int]] | None = None
    found: dict[tuple[int, int], int] = {}
    for index, line in enumerate(lines):
        if not _MARKER.search(line):
            continue
        joins: Callable[[str], bool]
        opens: Callable[[str], bool]
        if line.lstrip().startswith("#"):
            low, high, joins, opens = 0, last, _continues_comment, _opens_bullet
        else:
            if strings is None:
                strings = _string_rows(source)
            low, high = next(
                ((first, final) for first, final in strings if first <= index <= final),
                (0, last),
            )
            joins, opens = _continues_text, _opens_nothing
        start = end = index
        while start > low and joins(lines[start - 1]) and not opens(lines[start]):
            start -= 1
        while end < high and joins(lines[end + 1]) and not opens(lines[end + 1]):
            end += 1
        found.setdefault((start, end), index + 1)
    return [
        (number, " ".join(_COMMENT_LEADER.sub("", line).strip() for line in lines[start : end + 1]))
        for (start, end), number in sorted(found.items())
    ]


def uncited_assumptions(source: str, recorded: set[str]) -> list[tuple[int, str]]:
    """Each paragraph of ``source`` holding the marker that cites no recorded section.

    A citation is one of the two forms :func:`citation_faults` reads: a lane id
    after a section sign (``§DISPATCH-2``), or any id after ``assumptions.md``
    (``docs/assumptions.md section 21``). A bare number after a section sign is
    not one - it may be the master rules' or the proposal's.
    """
    uncited: list[tuple[int, str]] = []
    for number, text in marked_paragraphs(source):
        cited = {match.group(1) for match in _LANE_CITATION.finditer(text)}
        cited |= {match.group(1) for match in _NAMED_CITATION.finditer(text)}
        if not cited & recorded:
            uncited.append((number, text))
    return uncited


def marked_sources() -> dict[str, str]:
    """Each backend source file that holds the marker, by its path under src."""
    sources: dict[str, str] = {}
    for path in sorted(MARKED_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if _MARKER.search(source):
            sources[path.relative_to(MARKED_ROOT).as_posix()] = source
    return sources


@pytest.fixture(scope="module")
def recorded() -> set[str]:
    return recorded_sections(ASSUMPTIONS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def marked() -> dict[str, str]:
    return marked_sources()


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


def test_the_marker_scan_reads_the_whole_backend_source(marked: dict[str, str]) -> None:
    assert {
        "common/approvals.py",
        "core/fields.py",
        "modules/dispatch/repository.py",
        "modules/production/service.py",
        "workers/tasks/notifications.py",
    } <= set(marked)


def test_every_backend_assumption_cites_a_recorded_section(
    recorded: set[str], marked: dict[str, str]
) -> None:
    faults = [
        f"src/{name} line {number}: {textwrap.shorten(text, 110)}"
        for name, source in marked.items()
        for number, text in uncited_assumptions(source, recorded)
    ]
    assert not faults, (
        "An ASSUMPTION paragraph cites no section of docs/assumptions.md. Cite the one "
        "that records its rule - 'ASSUMPTION (docs/assumptions.md §<id>): ...' - or write "
        "it (Assumed / Why / Where / Impact if wrong) and cite that:\n" + "\n".join(faults)
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


def test_a_paragraph_ends_at_a_bare_comment_line_and_at_its_docstring_quotes() -> None:
    source = (
        "#: ASSUMPTION: the first paragraph,\n"
        "#: wrapped onto a second line.\n"
        "#:\n"
        "#: A second paragraph of the same block.\n"
        "BOARD = 1\n"
        "\n"
        "\n"
        "def sweep() -> None:\n"
        '    """Raise the alert.\n'
        "\n"
        "    ASSUMPTION: the docstring's paragraph.\n"
        '    """\n'
        "    # A comment after the docstring.\n"
    )

    assert marked_paragraphs(source) == [
        (1, "ASSUMPTION: the first paragraph, wrapped onto a second line."),
        (11, 'ASSUMPTION: the docstring\'s paragraph. """'),
    ]


def test_a_comment_bullet_opens_a_paragraph_of_its_own() -> None:
    """Two bullets of one comment block are two rules, each with its own citation.

    Read as one paragraph, the first bullet's citation covered the second's
    marker - the shape of the purchase order graph's edge list.
    """
    source = (
        "# The order graph, plus two edges it does not draw:\n"
        "#\n"
        "# * SENT -> RECEIVED. ASSUMPTION (docs/assumptions.md §PO-1): goods that\n"
        "#   arrive can be received.\n"
        "# * RECEIVED -> DONE. ASSUMPTION: an order with nothing to inspect\n"
        "#   completes, and this bullet cites nothing of its own.\n"
        "# - A dash bullet. ASSUMPTION: neither does this one.\n"
        "GRAPH = 1\n"
    )

    assert marked_paragraphs(source) == [
        (
            3,
            "* SENT -> RECEIVED. ASSUMPTION (docs/assumptions.md §PO-1): goods that "
            "arrive can be received.",
        ),
        (
            5,
            "* RECEIVED -> DONE. ASSUMPTION: an order with nothing to inspect "
            "completes, and this bullet cites nothing of its own.",
        ),
        (7, "- A dash bullet. ASSUMPTION: neither does this one."),
    ]
    assert [number for number, _ in uncited_assumptions(source, {"PO-1"})] == [5, 7]


def test_the_marker_rule_finds_a_paragraph_that_cites_no_recorded_section() -> None:
    source = (
        "#: ASSUMPTION: the board reads the consignment's own status, which the\n"
        "#: section 7 flow models (master rules §7). ASSUMPTION twice, reported once.\n"
        "#:\n"
        "#: The paragraph after the break cites §DISPATCH-2, which is not this one's.\n"
        "BOARD = 1\n"
        "\n"
        "\n"
        "def sweep() -> None:\n"
        '    """Raise the delay alert.\n'
        "\n"
        "    ASSUMPTION: one audit entry per company per sweep.\n"
        '    """\n'
        "    # Code after the docstring cites docs/assumptions.md §DISPATCH-3.\n"
        "\n"
        "\n"
        "def plan() -> None:\n"
        '    """ASSUMPTION (docs/assumptions.md §E9-9): a section nobody wrote."""\n'
        "\n"
        "\n"
        "LIMIT = 5  # ASSUMPTION: a trailing comment that cites nothing.\n"
    )

    # "7" is recorded, as the document's "## 7." heading is, so the first
    # paragraph is reported because a bare number after a section sign is no
    # citation - not because the number names no section.
    uncited = uncited_assumptions(source, {"DISPATCH-2", "DISPATCH-3", "7"})

    assert [number for number, _ in uncited] == [1, 11, 17, 20]


def test_the_marker_rule_passes_each_way_a_paragraph_cites_its_section() -> None:
    source = (
        "# ASSUMPTION (docs/assumptions.md §DISPATCH-2): read off the status.\n"
        "BOARD = 1\n"
        "\n"
        "#: ASSUMPTION: one source lot maps to one output lot (§JW-batch-1).\n"
        "LOTS = 1\n"
        "\n"
        "#: ASSUMPTION: a rule wrapped where docs/assumptions.md\n"
        "#: section 21 records it.\n"
        "NETTING = 1\n"
        "\n"
        "\n"
        "def cart() -> None:\n"
        '    """One open cart per dealer user. ASSUMPTION: docs/assumptions.md §C3-4.\n'
        "\n"
        "    A second paragraph, which needs no citation of its own.\n"
        '    """\n'
    )

    assert uncited_assumptions(source, {"DISPATCH-2", "JW-batch-1", "21", "C3-4"}) == []


def test_the_headings_are_read_as_section_ids() -> None:
    document = (
        "## How to read this\n"
        "## 2. Segregation of duties on approval\n"
        "## §E2-2. A demand line is held by one reservation per position\n"
        "## §JW-batch-1. Batch SKU conversion is one-to-one lot lineage\n"
        "### §Z9-9. Not a section heading\n"
    )

    assert recorded_sections(document) == {"2", "E2-2", "JW-batch-1"}
