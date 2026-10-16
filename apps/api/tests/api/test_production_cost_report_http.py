"""Production and job-work costs stay private on report and document routes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.service import resolve_storage_path
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(base: Principal, *permissions: Permission) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(permission) for permission in permissions),
        role_codes=base.role_codes,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


@pytest.mark.parametrize("report_key", [ReportKey.PRODUCTION_COST, ReportKey.JOB_WORK_COST])
async def test_catalogue_and_execution_require_item_cost_view(
    api_client, principal, report_key: ReportKey
) -> None:
    route = report_key.value.lower().replace("_", "-")
    _as(_holding(principal, Permission.REPORT_VIEW))
    catalogue = await api_client.get("/reports/catalogue")
    assert catalogue.status_code == 200
    assert report_key.value not in {entry["key"] for entry in catalogue.json()["data"]}

    refused = await api_client.get(
        f"/reports/{route}",
        params={"date_from": "2026-10-01", "date_to": "2026-10-16"},
    )
    assert refused.status_code == 403

    _as(_holding(principal, Permission.REPORT_VIEW, Permission.ITEM_COST_VIEW))
    permitted = await api_client.get("/reports/catalogue")
    assert report_key.value in {entry["key"] for entry in permitted.json()["data"]}
    result = await api_client.get(
        f"/reports/{route}",
        params={"date_from": "2026-10-01", "date_to": "2026-10-16"},
    )
    assert result.status_code == 200
    assert result.json()["data"] == []


@pytest.mark.parametrize("report_key", [ReportKey.PRODUCTION_COST, ReportKey.JOB_WORK_COST])
async def test_background_cost_report_job_requires_item_cost_view(
    api_client,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    report_key: ReportKey,
) -> None:
    now = utcnow()
    job = ReportSnapshot(
        company_id=tenant.company_id,
        created_by_id=principal.user_id,
        report_key=report_key,
        parameters_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        parameters={"report_key": report_key.value},
        status=ReportJobStatus.QUEUED,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    await session.flush()
    job_id = job.id

    _as(_holding(principal, Permission.REPORT_VIEW))
    denied = await api_client.get(f"/reports/jobs/{job_id}")
    assert denied.status_code == 404

    _as(_holding(principal, Permission.REPORT_VIEW, Permission.ITEM_COST_VIEW))
    allowed = await api_client.get(f"/reports/jobs/{job_id}")
    assert allowed.status_code == 200


@pytest.mark.parametrize("report_key", [ReportKey.PRODUCTION_COST, ReportKey.JOB_WORK_COST])
async def test_cost_report_document_is_hidden_on_generic_document_routes(
    api_client,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    report_key: ReportKey,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    payload = b"protected cost workbook"
    now = utcnow()
    document = Document(
        company_id=tenant.company_id,
        file_name=f"{report_key.value.lower()}.xlsx",
        storage_key=f"reports/{tenant.company_id}/{report_key.value.lower()}.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=len(payload),
        checksum_sha256="0" * 64,
        category=DocumentCategory.REPORT,
        entity_type="report_snapshot",
        entity_id=uuid.uuid4(),
        uploaded_by_id=principal.user_id,
        is_public=False,
    )
    session.add(document)
    await session.flush()
    snapshot = ReportSnapshot(
        company_id=tenant.company_id,
        created_by_id=principal.user_id,
        report_key=report_key,
        parameters_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        parameters={"report_key": report_key.value},
        status=ReportJobStatus.COMPLETED,
        generated_at=now,
        row_count=1,
        document_id=document.id,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    session.add(snapshot)
    await session.flush()
    document.entity_id = snapshot.id
    await session.flush()
    document_id = document.id
    path = resolve_storage_path(document.storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    _as(_holding(principal, Permission.DOCUMENT_VIEW, Permission.DOCUMENT_DELETE))
    listed = await api_client.get("/documents")
    assert listed.status_code == 200
    assert str(document_id) not in {item["id"] for item in listed.json()["data"]}
    assert (await api_client.get(f"/documents/{document_id}")).status_code == 404
    assert (await api_client.get(f"/documents/{document_id}/download")).status_code == 404
    assert (
        await api_client.delete(
            f"/documents/{document_id}", params={"reason": "Remove protected report"}
        )
    ).status_code == 404

    _as(_holding(principal, Permission.DOCUMENT_VIEW, Permission.ITEM_COST_VIEW))
    visible = await api_client.get(f"/documents/{document_id}")
    assert visible.status_code == 200
    allowed_list = await api_client.get("/documents")
    assert str(document_id) in {item["id"] for item in allowed_list.json()["data"]}
    downloaded = await api_client.get(f"/documents/{document_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == payload
