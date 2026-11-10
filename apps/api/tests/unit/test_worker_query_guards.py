"""OFFSET is written in one place: ``core.pagination``, one page per request.

A worker has no client page to serve, so an ``.offset(...)`` under
``src/workers`` is a loop re-sending one statement per chunk. Each pass repeats
the whole query - an aggregate is recomputed from scratch for every chunk, so
the cost grows with the square of the row count - and under READ COMMITTED
each pass reads a fresh snapshot, so rows that move between passes shift the
offsets and are exported twice or not at all. The inventory valuation export
did exactly that. Read a bounded result in one statement, as
``reports.service.snapshot_rows`` does, or stream it with ``session.stream``.

Request-path list endpoints page with OFFSET legitimately, one page per request
- but only through ``core.pagination``: ``paginate`` for entities and
``paginate_rows`` for projections. Fifteen copies of the row paginator had been
pasted into the modules, and eight of them counted ``LIMIT COUNT_CEILING`` and
reported the ceiling as an exact total, so a set of 25 000 rows answered
``total=10000`` (docs/assumptions.md 15). A hand-written ``.offset(`` under
``src/modules`` is how such a copy starts, so each one left is listed below with
the reason it is not a page through the shared helpers.

Each rule is a function over source text with a self-test that feeds it the
violation, as in ``test_layering_guards.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKERS = Path(__file__).resolve().parents[2] / "src" / "workers"
MODULES = Path(__file__).resolve().parents[2] / "src" / "modules"

#: Hand-written OFFSET under src/modules, each with the reason it does not page
#: through ``core.pagination``.
MODULE_OFFSET_ALLOWED: dict[str, str] = {
    "production/repository.py": (
        "the work-centre board cuts its page at MAX_WORK_CENTRE_ROWS and reports no "
        "total on purpose - counting the cells would be a second aggregate of the scan"
    ),
}


def offset_calls(source: str) -> list[str]:
    """Every ``<expr>.offset(...)`` call, in source order."""
    lines = sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "offset"
    )
    return [f"line {line}: .offset()" for line in lines]


def _worker_files() -> list[Path]:
    return sorted(path for path in WORKERS.rglob("*.py") if "__pycache__" not in path.parts)


def _module_files() -> list[Path]:
    return sorted(path for path in MODULES.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_scan_finds_the_workers() -> None:
    assert (WORKERS / "tasks" / "reports.py") in _worker_files()


def test_no_worker_walks_a_result_with_offset() -> None:
    offenders = {
        path.relative_to(WORKERS).as_posix(): found
        for path in _worker_files()
        if (found := offset_calls(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "A background job reads a bounded result in one statement or streams it with "
        f"session.stream(...); an OFFSET walk re-runs the query per chunk: {offenders}"
    )


def test_the_rule_rejects_an_offset_walk() -> None:
    source = (
        "async def export(session, stmt):\n"
        "    page = await session.execute(stmt.limit(500).offset(offset))\n"
        "    whole = await session.execute(stmt.limit(100_000))\n"
        "    return params.offset\n"
    )
    assert offset_calls(source) == ["line 2: .offset()"]


def test_the_scan_finds_the_modules() -> None:
    assert (MODULES / "dispatch" / "repository.py") in _module_files()


def test_list_endpoints_page_only_through_core_pagination() -> None:
    offenders = {
        relative: found
        for path in _module_files()
        if (relative := path.relative_to(MODULES).as_posix()) not in MODULE_OFFSET_ALLOWED
        and (found := offset_calls(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Page a list with core.pagination.paginate (entities) or paginate_rows "
        "(projections, aggregates, unions); a hand-written OFFSET is a copy of the "
        f"paginator, with its own count rule: {offenders}"
    )


def test_every_module_offset_allowance_is_still_needed() -> None:
    stale = [
        relative
        for relative in MODULE_OFFSET_ALLOWED
        if not offset_calls((MODULES / relative).read_text(encoding="utf-8"))
    ]
    assert not stale, f"these allowances no longer cover any OFFSET; remove them: {stale}"
