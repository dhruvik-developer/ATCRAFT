"""Finish queued demonstration invoice PDFs without an external worker.

The local seed uses an isolated broker. This child command runs the existing
report tasks synchronously on one event loop, retaining their claims, audit,
storage and failure handling. It never renders arbitrary user report jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import engine, session_scope
from src.core.demo_data import DEMO_PREFIX, DemoDataError, _resolve_principal
from src.core.deps import TenantContext
from src.core.logging import configure_logging, get_logger
from src.core.registry import import_all_models
from src.core.seed import DEFAULT_ADMIN_EMAIL
from src.modules.dispatch.models import Dispatch
from src.modules.finance.models import CreditNote, SalesInvoice
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot

log = get_logger(__name__)

# Only document types issued by the demonstration finance stage. A catalogue
# export or an unrelated document queued by a real user is outside this command.
REPORT_PARAMETERS = {
    ReportKey.SALES_INVOICE: "sales_invoice_id",
    ReportKey.CREDIT_NOTE: "credit_note_id",
}


@dataclass(frozen=True)
class SeedReportJob:
    report_key: ReportKey
    subject_id: uuid.UUID
    snapshot_id: uuid.UUID
    company_id: uuid.UUID
    user_id: uuid.UUID


def _select_demo_jobs(
    snapshots: list[ReportSnapshot],
    allowed_ids: dict[ReportKey, set[uuid.UUID]],
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    statuses: frozenset[ReportJobStatus] = frozenset({ReportJobStatus.QUEUED}),
) -> list[SeedReportJob]:
    jobs = []
    for snapshot in snapshots:
        parameter = REPORT_PARAMETERS.get(snapshot.report_key)
        if (
            parameter is None
            or snapshot.company_id != company_id
            or snapshot.created_by_id != user_id
            or snapshot.status not in statuses
        ):
            continue
        try:
            subject_id = uuid.UUID(str((snapshot.parameters or {}).get(parameter)))
        except ValueError:
            continue
        if subject_id not in allowed_ids.get(snapshot.report_key, set()):
            continue
        jobs.append(
            SeedReportJob(snapshot.report_key, subject_id, snapshot.id, company_id, user_id)
        )
    return jobs


async def _prepare_missing_jobs(
    session: AsyncSession,
    tenant: TenantContext,
    required_ids: dict[ReportKey, set[uuid.UUID]],
    snapshots: list[ReportSnapshot],
) -> list[SeedReportJob]:
    from src.modules.finance.credit_note_service import queue_credit_note_pdf
    from src.modules.finance.sales_invoice_service import queue_sales_invoice_pdf

    queues = {
        ReportKey.SALES_INVOICE: queue_sales_invoice_pdf,
        ReportKey.CREDIT_NOTE: queue_credit_note_pdf,
    }
    # Snapshots arrive oldest first. The latest attempt determines whether to
    # resume, regenerate through the owning service, or wait for an active task.
    owned = _select_demo_jobs(
        snapshots,
        required_ids,
        company_id=tenant.company_id,
        user_id=tenant.user_id,
        statuses=frozenset(ReportJobStatus),
    )
    latest = {(job.report_key, job.subject_id): job for job in owned}
    states = {snapshot.id: snapshot.status for snapshot in snapshots}
    jobs = []
    for key, subject_ids in required_ids.items():
        for subject_id in sorted(subject_ids):
            job = latest.get((key, subject_id))
            state = states.get(job.snapshot_id) if job else None
            if state == ReportJobStatus.RUNNING:
                raise DemoDataError(
                    "A demonstration PDF job is still running. Let it finish before rerunning."
                )
            if job and state == ReportJobStatus.QUEUED:
                jobs.append(job)
                continue
            # FAILED is terminal. Queue a new job through the normal service;
            # never force its status backwards or silently skip a missing PDF.
            identifier = await queues[key](session, tenant, subject_id)
            jobs.append(
                SeedReportJob(
                    key, subject_id, uuid.UUID(identifier), tenant.company_id, tenant.user_id
                )
            )
    return jobs


async def queued_demo_jobs(
    admin_email: str, *, company_code: str | None = None
) -> list[SeedReportJob]:
    import_all_models()
    async with session_scope() as session:
        principal = await _resolve_principal(session, admin_email, company_code=company_code)
        # Invoices issued from a demonstration dispatch carry no demo marker of
        # their own: the finance service owns their notes. Identify them by the
        # dispatch they were raised from, whose notes are tagged by the operations
        # stage. This also covers invoices created before the reports helper
        # existed, which is why the check does not depend on invoice.notes.
        demo_dispatch_ids = select(Dispatch.id).where(
            Dispatch.company_id == principal.company_id,
            Dispatch.notes.like(f"{DEMO_PREFIX}-%"),
        )
        invoice_rows = (
            (
                await session.execute(
                    select(SalesInvoice.id, SalesInvoice.pdf_document_id).where(
                        SalesInvoice.company_id == principal.company_id,
                        SalesInvoice.dispatch_id.in_(demo_dispatch_ids),
                    )
                )
            )
            .tuples()
            .all()
        )
        invoice_ids = {invoice_id for invoice_id, _ in invoice_rows}
        credit_rows = (
            (
                await session.execute(
                    select(CreditNote.id, CreditNote.pdf_document_id).where(
                        CreditNote.company_id == principal.company_id,
                        CreditNote.sales_invoice_id.in_(invoice_ids),
                    )
                )
            )
            .tuples()
            .all()
        )
        snapshots = list(
            await session.scalars(
                select(ReportSnapshot)
                .where(
                    ReportSnapshot.company_id == principal.company_id,
                    ReportSnapshot.created_by_id == principal.user_id,
                    ReportSnapshot.report_key.in_(REPORT_PARAMETERS),
                )
                .order_by(ReportSnapshot.created_at, ReportSnapshot.id)
            )
        )
        return await _prepare_missing_jobs(
            session,
            TenantContext(
                company_id=principal.company_id,
                user_id=principal.user_id,
                is_superuser=True,
            ),
            {
                ReportKey.SALES_INVOICE: {
                    invoice_id for invoice_id, document_id in invoice_rows if document_id is None
                },
                ReportKey.CREDIT_NOTE: {
                    note_id for note_id, document_id in credit_rows if document_id is None
                },
            },
            snapshots,
        )


def render_job(job: SeedReportJob) -> None:
    from src.workers.tasks.reports import generate_credit_note, generate_sales_invoice

    tasks = {
        ReportKey.SALES_INVOICE: generate_sales_invoice,
        ReportKey.CREDIT_NOTE: generate_credit_note,
    }
    kwargs: dict[str, Any] = {
        REPORT_PARAMETERS[job.report_key]: str(job.subject_id),
        "snapshot_id": str(job.snapshot_id),
        "company_id": str(job.company_id),
        "user_id": str(job.user_id),
    }
    result = tasks[job.report_key].run(**kwargs)
    if result.get("status") != "COMPLETED":
        raise RuntimeError(f"Seed report {job.snapshot_id} did not complete.")


def run_reports(admin_email: str, company_code: str | None) -> int:
    # Celery's synchronous task adapter uses the current event loop. Retain it
    # across discovery and rendering so pooled async connections stay on it.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        jobs = loop.run_until_complete(
            queued_demo_jobs(admin_email.lower(), company_code=company_code)
        )
        for job in jobs:
            render_job(job)
        print(f"  Demonstration invoice/credit PDFs completed: {len(jobs)}")
        return 0
    except Exception as exc:
        log.error("demo.report_generation_failed", error_type=type(exc).__name__)
        return 1
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def main() -> int:
    from scripts.seed_local import isolated_seed_process, validate_local_target

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--admin-email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--company-code", default=None)
    args = parser.parse_args()
    validate_local_target(settings)
    with isolated_seed_process():
        configure_logging()
        return run_reports(args.admin_email, args.company_code)


if __name__ == "__main__":
    sys.exit(main())
