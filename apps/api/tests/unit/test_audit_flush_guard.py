"""Mechanical guard: audit rows written in a loop are staged and flushed once.

``record_audit``, ``record_status_change`` and the allocation engine's
``audit_reservation`` flush by default - one INSERT round trip per call, so a
constraint violation surfaces at the call. Inside a loop that is one round trip
per row: serial registration flushed once per unit (up to 10,000 on an assembly
completion), the reservation expiry sweep twice per reservation while it held a
batch of balance locks, and the MRP bulk dismissal once per suggestion. The
documented pattern (``audit/service.py``) is ``flush=False`` in the loop and one
flush before the ``transaction()`` block exits.

The rule: an audit write inside a ``for``/``while`` body in ``src/modules`` passes
``flush=False``, or its function is listed below with the reason the per-row
flush stays. Some are load-bearing: a demotion's flush must reach the database
before the promotion it makes room for, because a partial unique index is
checked per statement (CLAUDE.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

_AUDIT_WRITERS = frozenset({"record_audit", "record_status_change", "audit_reservation"})

#: Loops that still flush each audit row, keyed ``path::function``, with why.
PER_ROW_FLUSH_ALLOWED: dict[str, str] = {
    "modules/auth/service.py::_record_unresolved_attempt": (
        "one row per account sharing the address - normally one, at most one per company"
    ),
    "modules/bom/service.py::approve_bom": (
        "load-bearing: the flush sends each demotion before the new default is promoted "
        "(partial unique index, checked per statement)"
    ),
    "modules/routing/service.py::approve_routing": (
        "load-bearing: the flush sends each demotion before the new default is promoted "
        "(partial unique index, checked per statement)"
    ),
    "modules/collections/service.py::scan_overdue_receivables": (
        "the nightly sweep of one tenant's live payment chases, off the request path; "
        "staging its rows is a recorded follow-up"
    ),
    "modules/dispatch/service.py::_advance_sales_order": "one order's status hops: at most three",
    "modules/finance/service.py::ensure_system_accounts": (
        "a fixed handful of control accounts, provisioned once per tenant"
    ),
    "modules/mrp/service.py::_close_abandoned_runs": (
        "the tenant's active planning runs: one, rarely a stale one beside it"
    ),
    "modules/procurement/purchase_order_changes.py::_cancel_demand": (
        "the requisitions one order was raised from: a handful"
    ),
}


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _stages(call: ast.Call) -> bool:
    return any(
        keyword.arg == "flush"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _own_nodes(node: ast.AST) -> list[ast.AST]:
    """Every node under ``node``, stopping at nested function definitions."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(node))
    while pending:
        current = pending.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        found.append(current)
        pending.extend(ast.iter_child_nodes(current))
    return found


def per_row_flushes(source: str) -> dict[str, list[int]]:
    """``function -> [line, ...]`` for each audit write in a loop that flushes per row."""
    found: dict[str, list[int]] = {}
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        lines = sorted(
            {
                node.lineno
                for loop in _own_nodes(function)
                if isinstance(loop, ast.For | ast.AsyncFor | ast.While)
                for node in _own_nodes(loop)
                if isinstance(node, ast.Call)
                and _called_name(node) in _AUDIT_WRITERS
                and not _stages(node)
            }
        )
        if lines:
            found[function.name] = lines
    return found


def _offenders() -> dict[str, list[int]]:
    offenders: dict[str, list[int]] = {}
    for path in sorted((SRC / "modules").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(SRC).as_posix()
        for function, lines in per_row_flushes(path.read_text(encoding="utf-8")).items():
            offenders[f"{relative}::{function}"] = lines
    return offenders


def test_audit_rows_written_in_a_loop_are_staged() -> None:
    unexplained = {
        key: lines for key, lines in _offenders().items() if key not in PER_ROW_FLUSH_ALLOWED
    }
    assert not unexplained, (
        "Pass flush=False to audit writes inside a loop and flush once before the "
        "transaction block exits. If the per-row flush is load-bearing - a demotion that "
        "must reach the database before a promotion - list the function in "
        f"PER_ROW_FLUSH_ALLOWED with the reason: {unexplained}"
    )


def test_every_allowance_is_still_needed() -> None:
    stale = sorted(set(PER_ROW_FLUSH_ALLOWED) - set(_offenders()))
    assert not stale, f"remove these PER_ROW_FLUSH_ALLOWED entries, they stage now: {stale}"


# -- the rule rejects what it exists to catch ----------------------------------


def test_the_rule_rejects_a_flushing_write_in_a_loop_and_accepts_a_staged_one() -> None:
    source = (
        "async def register(session, serials):\n"
        "    await record_audit(session, summary='one before the loop')\n"
        "    for serial in serials:\n"
        "        await record_audit(session, summary=serial)\n"
        "        await record_status_change(session, from_status='A', to_status='B')\n"
        "    for serial in serials:\n"
        "        await record_audit(session, summary=serial, flush=False)\n"
        "    await session.flush()\n"
        "async def sweep(engine, rows):\n"
        "    while rows:\n"
        "        await engine.audit_reservation(rows.pop(), summary='expired', flush=True)\n"
        "async def nested(session, rows):\n"
        "    for row in rows:\n"
        "        async def later():\n"
        "            await record_audit(session, summary=row)\n"
    )
    assert per_row_flushes(source) == {"register": [4, 5], "sweep": [11]}
