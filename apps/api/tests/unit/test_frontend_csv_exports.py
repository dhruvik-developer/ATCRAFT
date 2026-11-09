"""The browser builds a CSV in exactly one place.

Server-rendered exports pass every text cell through ``_neutralise_formula``
(:mod:`src.workers.tasks.reports`), so a value beginning with ``=`` cannot run
as a formula in the spreadsheet that opens it. The job-work vendor ledger was
the one export the browser wrote itself, and it escaped quotes and commas but
not formulas: an item code is free text, one beginning with ``=`` is valid, and
it ran for whoever opened the file - usually not the person who typed it.

``apps/web/src/lib/csv.ts`` now carries the same remedy (``csvCell``) and the
only ``text/csv`` Blob (``downloadCsv``). A second CSV built anywhere else is
how the unneutralised copy comes back, so this fails the build on one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CANONICAL = WEB_SRC / "lib" / "csv.ts"

#: A Blob typed as CSV (one level of nested parentheses inside the call), or a
#: CSV data URI - the two ways a browser hands a user a CSV file.
_CSV_FILE = re.compile(
    r"new\s+Blob\s*\((?:[^()]|\([^()]*\))*?['\"`]text/csv|data:text/csv",
    re.DOTALL,
)


def csv_file_sites(source: str) -> list[str]:
    """``line N`` for every place the source builds a CSV file."""
    return [
        f"line {source.count(chr(10), 0, match.start()) + 1}"
        for match in _CSV_FILE.finditer(source)
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
    )


def test_the_shared_writer_is_where_the_exports_import_it_from() -> None:
    """If this moves, the guard below would pass on an empty set of copies."""
    assert CANONICAL.is_file(), f"{CANONICAL} is gone; every browser CSV is built there"
    source = CANONICAL.read_text(encoding="utf-8")
    assert csv_file_sites(source), "lib/csv.ts no longer builds the text/csv Blob"
    assert "export function csvCell" in source, "lib/csv.ts no longer neutralises cells"
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_other_file_builds_a_csv() -> None:
    copies = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != CANONICAL and (found := csv_file_sites(path.read_text(encoding="utf-8")))
    }
    assert not copies, (
        "A CSV file is built outside lib/csv.ts:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in copies.items())
        + "\n\nUse downloadCsv from '@/lib/csv'. It neutralises a cell that would run as a "
        "formula when the file is opened, as every server-side export does; a private "
        "writer re-decides that and has already shipped without it once."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_a_csv_blob_and_a_csv_data_uri() -> None:
    source = (
        "const body = [header.join(','), ...rows].join('\\n')\n"
        "const blob = new Blob([[header.join(','), ...body].join('\\n')], { type: 'text/csv' })\n"
        'link.href = "data:text/csv;charset=utf-8," + encodeURIComponent(body)\n'
        "const other = new Blob([body], {\n  type: `text/csv;charset=utf-8`,\n})\n"
    )
    assert csv_file_sites(source) == ["line 2", "line 3", "line 4"]


def test_the_rule_ignores_other_blobs_and_a_mime_table() -> None:
    source = (
        "const pdf = new Blob([bytes], { type: 'application/pdf' })\n"
        "const KINDS = { 'text/csv': { label: 'CSV', kind: 'sheet' } }\n"
        "downloadCsv('ledger.csv', rows)\n"
    )
    assert csv_file_sites(source) == []
