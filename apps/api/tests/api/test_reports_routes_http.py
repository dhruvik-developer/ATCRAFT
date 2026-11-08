"""``POST /reports/export`` over HTTP: queue any catalogue report as a file.

The export answers 202 with a job the client polls at ``GET /reports/jobs/{id}``;
the worker that renders the file is followed end to end in
``tests/api/test_report_jobs_for_documents.py`` and is not repeated here. What
this file pins is what the route itself decides before the broker is reached:

* the job row it files - QUEUED, the format and the caller's visible fields and
  warehouse scope captured in its parameters, the task id recorded - and the
  JOB_STARTED audit entry written with it;
* reuse: a completed-period (cacheable) report asked twice is one job, an
  uncacheable one is a fresh job every time, and another company's identical
  job is never handed out;
* the refusals: an unknown format or key (field errors), a rendered document
  that is not a tabular report, a period report with no period, a report the
  caller lacks the right to (403), and a broker that refuses the task (503,
  with the job marked FAILED so a poller is told).

Celery's ``send_task`` is stubbed; nothing leaves the process.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.vendor_portal_support import sign_in

pytestmark = [pytest.mark.api, pytest.mark.integration]

EXPORT = "/reports/export"
OCTOBER = {"date_from": "2026-10-01", "date_to": "2026-10-31"}


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def accepted(response: Response) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def relative(status_url: str) -> str:
    prefix = settings.api_v1_prefix.rstrip("/")
    assert status_url.startswith(prefix), status_url
    return status_url[len(prefix) :]


async def snapshots(session: AsyncSession, company_id: uuid.UUID) -> list[ReportSnapshot]:
    stmt = (
        select(ReportSnapshot)
        .where(ReportSnapshot.company_id == company_id)
        .order_by(ReportSnapshot.created_at, ReportSnapshot.id)
    )
    return list((await session.scalars(stmt)).all())


def holding(base: Principal, *missing: Permission, **changes: Any) -> Principal:
    """The same person without some rights, or with a narrower scope."""
    return dataclasses.replace(
        base,
        permissions=frozenset(str(p) for p in Permission if p not in missing),
        **changes,
    )


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    def accept(name: str, **options: Any) -> Any:
        sent.append((name, options.get("kwargs") or {}))
        return SimpleNamespace(id=f"stub-{uuid.uuid4().hex[:12]}")

    monkeypatch.setattr(celery_module.celery_app, "send_task", accept)
    return sent


# -----------------------------------------------------------------------------
# Queueing
# -----------------------------------------------------------------------------


async def test_an_export_files_a_queued_job_audits_it_and_hands_it_to_the_broker(
    api_client, session, tenant, broker
):
    job = accepted(
        await api_client.post(EXPORT, params={"report_key": "STOCK_AVAILABILITY", "format": "pdf"})
    )

    job_id = uuid.UUID(job["job_id"])
    assert job["status"] == "queued"
    assert job["status_url"] == reports_service.job_status_url(job_id)
    [snapshot] = await snapshots(session, tenant.company_id)
    assert snapshot.id == job_id
    assert (snapshot.report_key, snapshot.status) == (
        ReportKey.STOCK_AVAILABILITY,
        ReportJobStatus.QUEUED,
    )
    assert snapshot.parameters[reports_service.REPORT_FORMAT_PARAMETER] == "pdf"
    assert (
        "valuation_amount" in snapshot.parameters[reports_service.REPORT_VISIBLE_FIELDS_PARAMETER]
    )
    assert [(name, kwargs["snapshot_id"]) for name, kwargs in broker] == [
        (reports_service.REPORT_TASK_NAME, str(job_id))
    ]
    assert snapshot.celery_task_id is not None and snapshot.celery_task_id.startswith("stub-")

    [started] = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == reports_service.AUDIT_ENTITY, AuditLog.entity_id == job_id
            )
        )
    ).all()
    assert (started.action, started.created_by_id) == (AuditAction.JOB_STARTED, tenant.user_id)
    assert started.new_values["report_key"] == "STOCK_AVAILABILITY"

    polled = await api_client.get(relative(job["status_url"]))
    assert polled.status_code == 200, polled.text
    assert (polled.json()["data"]["status"], polled.json()["data"]["report_key"]) == (
        "QUEUED",
        "STOCK_AVAILABILITY",
    )


async def test_the_job_captures_what_this_caller_may_see_and_where(
    api_client, session, tenant, principal, unique_code
):
    store = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("WH")
    )
    sign_in(
        holding(principal, Permission.INVENTORY_VALUATION_VIEW, allowed_warehouse_ids=(store.id,))
    )

    accepted(await api_client.post(EXPORT, params={"report_key": "STOCK_AVAILABILITY"}))

    [snapshot] = await snapshots(session, tenant.company_id)
    assert snapshot.parameters[reports_service.REPORT_FORMAT_PARAMETER] == "xlsx"
    visible = snapshot.parameters[reports_service.REPORT_VISIBLE_FIELDS_PARAMETER]
    assert "valuation_amount" not in visible and "available" in visible
    assert snapshot.parameters[reports_service.REPORT_WAREHOUSE_SCOPE_PARAMETER] == [str(store.id)]


async def test_a_completed_period_report_asked_twice_is_one_job(
    api_client, session, tenant, broker
):
    params = {"report_key": "SALES_ORDER_STATUS", **OCTOBER}

    first = accepted(await api_client.post(EXPORT, params=params))
    second = accepted(await api_client.post(EXPORT, params=params))
    other_format = accepted(await api_client.post(EXPORT, params={**params, "format": "pdf"}))

    assert first["job_id"] == second["job_id"] != other_format["job_id"]
    assert len(broker) == 2
    assert len(await snapshots(session, tenant.company_id)) == 2


async def test_an_uncacheable_report_is_a_fresh_job_every_time(api_client, session, tenant, broker):
    params = {"report_key": "STOCK_AVAILABILITY"}

    first = accepted(await api_client.post(EXPORT, params=params))
    second = accepted(await api_client.post(EXPORT, params=params))

    assert first["job_id"] != second["job_id"]
    assert len(broker) == 2


async def test_another_companys_identical_job_is_never_handed_out(
    api_client, session, tenant, other_company, principal
):
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    theirs = await reports_service.queue_report_export(
        session,
        TenantContext(company_id=other_company.id, user_id=owner.id),
        key=ReportKey.SALES_ORDER_STATUS,
        period=ReportPeriod(date_from=date(2026, 10, 1), date_to=date(2026, 10, 31)),
        filters=ReportFilters(),
        file_format="xlsx",
        principal=dataclasses.replace(principal, user_id=owner.id, company_id=other_company.id),
    )
    their_id = theirs.id

    job = accepted(
        await api_client.post(EXPORT, params={"report_key": "SALES_ORDER_STATUS", **OCTOBER})
    )

    assert job["job_id"] != str(their_id)
    [ours] = await snapshots(session, tenant.company_id)
    assert ours.parameters_hash == (await session.get(ReportSnapshot, their_id)).parameters_hash
    refused(await api_client.get(f"/reports/jobs/{their_id}"), 404, "JOB_NOT_FOUND")


# -----------------------------------------------------------------------------
# Refusals
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"report_key": "STOCK_AVAILABILITY", "format": "csv"}, "query.format"),
        ({"report_key": "NOT_A_REPORT"}, "query.report_key"),
        ({}, "query.report_key"),
    ],
    ids=["unknown-format", "unknown-report", "no-report"],
)
async def test_an_export_outside_the_catalogue_is_a_field_error(
    api_client, session, tenant, broker, params, field
):
    error = refused(await api_client.post(EXPORT, params=params), 422, "VALIDATION_ERROR")

    assert field in error["details"]["fields"]
    assert broker == []
    assert await snapshots(session, tenant.company_id) == []


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"report_key": "PACKING_LIST"}, "report_key"),
        ({"report_key": "GSTR1"}, "report_key"),
        ({"report_key": "SALES_ORDER_STATUS"}, "required"),
        ({"report_key": "SALES_ORDER_STATUS", "date_from": "2026-10-01"}, "date_to"),
    ],
    ids=["rendered-document", "return-workbook", "no-period", "half-a-period"],
)
async def test_an_export_the_catalogue_cannot_answer_is_refused_before_a_job_exists(
    api_client, session, tenant, broker, params, detail
):
    error = refused(await api_client.post(EXPORT, params=params), 422, "VALIDATION_ERROR")

    assert detail in error["details"]
    assert broker == []
    assert await snapshots(session, tenant.company_id) == []


async def test_a_cost_report_needs_its_own_right_on_top_of_the_export_right(
    api_client, session, tenant, principal, broker
):
    sign_in(holding(principal, Permission.ITEM_COST_VIEW))

    error = refused(
        await api_client.post(EXPORT, params={"report_key": "PRODUCT_PROFITABILITY", **OCTOBER}),
        403,
        "PERMISSION_DENIED",
    )

    assert error["details"]["required_permission"] == str(Permission.ITEM_COST_VIEW)
    assert broker == []
    assert await snapshots(session, tenant.company_id) == []


async def test_a_broker_that_refuses_the_task_fails_the_job_it_filed(
    api_client, session, tenant, monkeypatch
):
    def unreachable(*_: Any, **__: Any) -> Any:
        raise ConnectionError("broker down")

    monkeypatch.setattr(celery_module.celery_app, "send_task", unreachable)

    refused(
        await api_client.post(EXPORT, params={"report_key": "STOCK_AVAILABILITY"}),
        503,
        "SERVICE_UNAVAILABLE",
    )

    [snapshot] = await snapshots(session, tenant.company_id)
    assert snapshot.status == ReportJobStatus.FAILED
    assert snapshot.error_message
    assert snapshot.celery_task_id is None
    count = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.entity_id == snapshot.id, AuditLog.action == AuditAction.JOB_FAILED)
    )
    assert count == 1
