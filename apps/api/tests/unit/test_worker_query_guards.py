"""A background job never walks a result set with OFFSET.

A worker has no client page to serve, so an ``.offset(...)`` under
``src/workers`` is a loop re-sending one statement per chunk. Each pass repeats
the whole query - an aggregate is recomputed from scratch for every chunk, so
the cost grows with the square of the row count - and under READ COMMITTED
each pass reads a fresh snapshot, so rows that move between passes shift the
offsets and are exported twice or not at all. The inventory valuation export
did exactly that. Read a bounded result in one statement, as
``reports.service.snapshot_rows`` does, or stream it with ``session.stream``.

The rule is scoped to the workers on purpose: request-path list endpoints page
with OFFSET legitimately (``core.pagination`` and the modules' ``paginate_rows``
helpers), one page per request.

The rule is a function over source text with a self-test that feeds it the
violation, as in ``test_layering_guards.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKERS = Path(__file__).resolve().parents[2] / "src" / "workers"


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
