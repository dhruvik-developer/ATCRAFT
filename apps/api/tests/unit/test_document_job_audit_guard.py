"""Every background document job is audited where it is queued.

A job row - a ``ReportSnapshot``, built by ``reports.service.new_job_snapshot``
or directly - is a request for a file, often a statutory one: an invoice, a
credit note, a debit note, a challan. CLAUDE.md requires every create to write
its audit row in the same transaction, and the queueing functions wrote
``AuditAction.JOB_STARTED`` - except the credit-note and purchase-return
debit-note PDFs, which left no trace of who asked for which document until
November 2026. Nothing compared the siblings, so nothing noticed.

This does: a function that creates a job row must also call ``record_audit``
with ``action=AuditAction.JOB_STARTED``. The rule is a function over source
text, with a self-test that feeds it the violation it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Calls that create a job row: the shared factory, or the model itself.
_JOB_ROW_FACTORIES = frozenset({"new_job_snapshot", "ReportSnapshot"})

#: The factory builds the row its callers audit; it is the one exemption.
_FACTORY = "modules/reports/service.py::new_job_snapshot"

#: Fewer job-creating functions than this means the scan stopped seeing them
#: (a renamed factory, say) and would pass while policing nothing.
_MIN_JOB_CREATORS = 9


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _audits_job_start(call: ast.Call) -> bool:
    return _callee(call) == "record_audit" and any(
        keyword.arg == "action" and ast.unparse(keyword.value).endswith(".JOB_STARTED")
        for keyword in call.keywords
    )


def job_creators(source: str) -> dict[str, bool]:
    """Each function that creates a job row -> whether it audits ``JOB_STARTED``."""
    found: dict[str, bool] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [call for call in ast.walk(node) if isinstance(call, ast.Call)]
        if any(_callee(call) in _JOB_ROW_FACTORIES for call in calls):
            found[node.name] = any(_audits_job_start(call) for call in calls)
    return found


def _creators_in_tree() -> dict[str, bool]:
    creators: dict[str, bool] = {}
    for path in sorted(p for p in (SRC / "modules").rglob("*.py") if "__pycache__" not in p.parts):
        relative = path.relative_to(SRC).as_posix()
        for function, audited in job_creators(path.read_text(encoding="utf-8")).items():
            creators[f"{relative}::{function}"] = audited
    creators.pop(_FACTORY, None)
    return creators


def test_the_scan_finds_the_job_queues_it_polices() -> None:
    assert len(_creators_in_tree()) >= _MIN_JOB_CREATORS, sorted(_creators_in_tree())


def test_every_job_queue_audits_the_job_it_starts() -> None:
    unaudited = sorted(name for name, audited in _creators_in_tree().items() if not audited)
    assert not unaudited, (
        "A function that queues a document job must record_audit(..., "
        "action=AuditAction.JOB_STARTED, context={'job_id': job.id}) in the same "
        f"transaction that adds the row: {unaudited}"
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_job_queued_without_its_audit_row() -> None:
    source = (
        "async def queue_note(session, tenant, note):\n"
        "    job = reports_service.new_job_snapshot(KEY, {}, now=now)\n"
        "    await record_audit(session, action=AuditAction.UPDATE)\n"
        "async def queue_audited(session, tenant):\n"
        "    job = ReportSnapshot(report_key=KEY)\n"
        "    await record_audit(session, action=AuditAction.JOB_STARTED)\n"
        "def unrelated():\n"
        "    return render(KEY)\n"
    )
    assert job_creators(source) == {"queue_note": False, "queue_audited": True}
