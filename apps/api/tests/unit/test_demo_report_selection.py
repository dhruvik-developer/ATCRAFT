"""Local PDF generation must not claim another tenant's or user's report job."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.demo_data import reports as demo_reports
from src.core.demo_data.reports import SeedReportJob, _select_demo_jobs
from src.core.deps import TenantContext
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "case",
    ["seed", "other_company", "other_user", "real_invoice", "malformed", "completed", "export"],
)
def test_only_owned_queued_demo_documents_are_selected(case: str) -> None:
    company_id, user_id, invoice_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    snapshot = ReportSnapshot(
        id=uuid.uuid4(),
        company_id=company_id,
        created_by_id=user_id,
        report_key=ReportKey.SALES_INVOICE,
        status=ReportJobStatus.QUEUED,
        parameters={"sales_invoice_id": str(invoice_id)},
    )
    if case == "other_company":
        snapshot.company_id = uuid.uuid4()
    elif case == "other_user":
        snapshot.created_by_id = uuid.uuid4()
    elif case == "real_invoice":
        snapshot.parameters = {"sales_invoice_id": str(uuid.uuid4())}
    elif case == "malformed":
        snapshot.parameters = {"sales_invoice_id": "not-an-id"}
    elif case == "completed":
        snapshot.status = ReportJobStatus.COMPLETED
    elif case == "export":
        snapshot.report_key = ReportKey.INVENTORY_VALUATION

    selected = _select_demo_jobs(
        [snapshot],
        {ReportKey.SALES_INVOICE: {invoice_id}},
        company_id=company_id,
        user_id=user_id,
    )
    assert len(selected) == (1 if case == "seed" else 0)
    if selected:
        assert selected[0].subject_id == invoice_id
        assert selected[0].snapshot_id == snapshot.id


async def test_discovery_resolves_seed_invoice_ids_before_selecting_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, user_id, invoice_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    snapshot = ReportSnapshot(
        id=uuid.uuid4(),
        company_id=company_id,
        created_by_id=user_id,
        report_key=ReportKey.SALES_INVOICE,
        status=ReportJobStatus.QUEUED,
        parameters={"sales_invoice_id": str(invoice_id)},
    )

    def result(rows):
        return SimpleNamespace(tuples=lambda: SimpleNamespace(all=lambda: rows))

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                result([(invoice_id, None)]),
                result([]),
            ]
        ),
        scalars=AsyncMock(return_value=[snapshot]),
    )

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(demo_reports, "session_scope", scope)
    monkeypatch.setattr(
        demo_reports,
        "_resolve_principal",
        AsyncMock(return_value=SimpleNamespace(company_id=company_id, user_id=user_id)),
    )
    jobs = await demo_reports.queued_demo_jobs("admin@atcraft.example")
    assert jobs == [
        SeedReportJob(ReportKey.SALES_INVOICE, invoice_id, snapshot.id, company_id, user_id)
    ]
    assert session.execute.await_count == 2
    assert session.scalars.await_count == 1


@pytest.mark.parametrize("key", [ReportKey.SALES_INVOICE, ReportKey.CREDIT_NOTE])
@pytest.mark.parametrize("state", [None, *ReportJobStatus])
async def test_missing_pdfs_resume_or_requeue_without_resetting_terminal_jobs(
    monkeypatch: pytest.MonkeyPatch, key: ReportKey, state: ReportJobStatus | None
) -> None:
    from src.core.demo_data import DemoDataError
    from src.modules.finance import credit_note_service, sales_invoice_service

    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    subject_id, existing_id, next_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    snapshot = ReportSnapshot(
        id=existing_id,
        company_id=tenant.company_id,
        created_by_id=tenant.user_id,
        report_key=key,
        status=state or ReportJobStatus.QUEUED,
        parameters={demo_reports.REPORT_PARAMETERS[key]: str(subject_id)},
    )
    queue = AsyncMock(return_value=str(next_id))
    monkeypatch.setattr(sales_invoice_service, "queue_sales_invoice_pdf", queue)
    monkeypatch.setattr(credit_note_service, "queue_credit_note_pdf", queue)
    session = SimpleNamespace()
    if state == ReportJobStatus.RUNNING:
        with pytest.raises(DemoDataError, match="still running"):
            await demo_reports._prepare_missing_jobs(
                session, tenant, {key: {subject_id}}, [snapshot]
            )
        queue.assert_not_awaited()
        return

    jobs = await demo_reports._prepare_missing_jobs(
        session, tenant, {key: {subject_id}}, [snapshot] if state is not None else []
    )
    if state == ReportJobStatus.QUEUED:
        queue.assert_not_awaited()
        assert jobs[0].snapshot_id == existing_id
    else:
        queue.assert_awaited_once_with(session, tenant, subject_id)
        assert jobs[0].snapshot_id == next_id
    assert jobs[0].subject_id == subject_id
    assert snapshot.status == (state or ReportJobStatus.QUEUED)


async def test_completed_documents_require_no_new_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.modules.finance import credit_note_service, sales_invoice_service

    queue = AsyncMock()
    monkeypatch.setattr(sales_invoice_service, "queue_sales_invoice_pdf", queue)
    monkeypatch.setattr(credit_note_service, "queue_credit_note_pdf", queue)
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    assert (
        await demo_reports._prepare_missing_jobs(
            SimpleNamespace(), tenant, {key: set() for key in demo_reports.REPORT_PARAMETERS}, []
        )
        == []
    )
    queue.assert_not_awaited()


@pytest.mark.parametrize("report_key", [ReportKey.SALES_INVOICE, ReportKey.CREDIT_NOTE])
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_rendering_uses_the_real_task_contract_and_requires_completion(
    monkeypatch: pytest.MonkeyPatch, report_key: ReportKey, status: str
) -> None:
    from src.workers.tasks import reports as tasks

    task = (
        tasks.generate_sales_invoice
        if report_key == ReportKey.SALES_INVOICE
        else tasks.generate_credit_note
    )
    run = Mock(return_value={"status": status})
    monkeypatch.setattr(task, "run", run)
    job = SeedReportJob(report_key, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    if status == "COMPLETED":
        demo_reports.render_job(job)
    else:
        with pytest.raises(RuntimeError, match="did not complete"):
            demo_reports.render_job(job)
    parameter = demo_reports.REPORT_PARAMETERS[report_key]
    run.assert_called_once_with(
        **{
            parameter: str(job.subject_id),
            "snapshot_id": str(job.snapshot_id),
            "company_id": str(job.company_id),
            "user_id": str(job.user_id),
        }
    )


@pytest.mark.parametrize("fail", [False, True])
def test_cli_cleans_up_its_event_loop_after_success_or_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    import asyncio
    import sys

    from scripts import seed_local

    monkeypatch.setattr(seed_local, "validate_local_target", Mock())
    monkeypatch.setattr(demo_reports, "configure_logging", Mock())
    monkeypatch.setattr(sys, "argv", ["reports"])
    job = SeedReportJob(
        ReportKey.SALES_INVOICE, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )
    monkeypatch.setattr(demo_reports, "queued_demo_jobs", AsyncMock(return_value=[job]))
    render = Mock(side_effect=RuntimeError("report failed") if fail else None)
    monkeypatch.setattr(demo_reports, "render_job", render)
    dispose = AsyncMock()
    monkeypatch.setattr(demo_reports, "engine", SimpleNamespace(dispose=dispose))
    original_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(original_loop)
    try:
        assert demo_reports.main() == (1 if fail else 0)
        render.assert_called_once_with(job)
        dispose.assert_awaited_once()
    finally:
        original_loop.close()
        asyncio.set_event_loop(None)
