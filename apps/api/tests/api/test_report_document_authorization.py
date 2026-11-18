"""Saved report bytes retain the authority of the report that produced them.

These are real PostgreSQL/HTTP checks: a generic document list or download must
not bypass the domain and field checks on the report's normal job route. Bytes
are deliberately small; rendering and Celery delivery have their own tests.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.service import DocumentService, resolve_storage_path
from src.modules.finance import service as finance_service
from src.modules.reports import service
from src.modules.reports.models import InventorySummary, ReportKey, ReportSnapshot
from src.modules.reports.repository import ReportSnapshotRepository
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "storage_path", tmp_path)


async def _artifact(
    session: AsyncSession,
    tenant: TenantContext,
    key: ReportKey,
    *,
    parameters: dict[str, Any] | None = None,
    legacy_attachment: bool = False,
) -> tuple[ReportSnapshot, Document, bytes]:
    """File a report as its worker does, completing its real state machine."""
    now = utcnow()
    params = {
        "report_key": str(key),
        "filters": {},
        service.REPORT_WAREHOUSE_SCOPE_PARAMETER: [],
        **(parameters or {}),
    }
    payload = b"%PDF-1.4\nconfidential report fixture\n%%EOF\n"
    async with transaction(session):
        snapshot = service.new_job_snapshot(key, params, now=now)
        ReportSnapshotRepository(session, tenant).add(snapshot)
        await session.flush()
        document = Document(
            company_id=tenant.company_id,
            file_name=f"{key.value.lower().replace('_', '-')}-{uuid.uuid4().hex}.pdf",
            storage_key=f"reports/{tenant.company_id}/{uuid.uuid4()}.pdf",
            mime_type="application/pdf",
            size_bytes=len(payload),
            checksum_sha256=hashlib.sha256(payload).hexdigest(),
            category=DocumentCategory.REPORT,
            entity_type=None if legacy_attachment else service.REPORT_DOCUMENT_ENTITY,
            entity_id=None if legacy_attachment else snapshot.id,
            uploaded_by_id=tenant.user_id,
            is_public=False,
            expires_at=now + timedelta(days=7),
        )
        session.add(document)
        await session.flush()
        destination = resolve_storage_path(document.storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        await service.start_snapshot(session, tenant, snapshot.id)
        await service.complete_snapshot(
            session, tenant, snapshot_id=snapshot.id, row_count=1, document_id=document.id
        )
    return snapshot, document, payload


def _read_as(principal: Principal, *permissions: Permission) -> None:
    from src.main import app

    reader = replace(
        principal,
        is_superuser=False,
        permissions=frozenset(
            str(permission) for permission in (Permission.DOCUMENT_VIEW, *permissions)
        ),
    )
    app.dependency_overrides[get_current_principal] = lambda: reader


async def _assert_visible(
    client: AsyncClient, document: Document, payload: bytes, *, visible: bool
) -> None:
    path = f"/documents/{document.id}"
    metadata = await client.get(path)
    download = await client.get(f"{path}/download")
    listed = await client.get(
        "/documents", params={"search": document.file_name, "with_total": "true"}
    )
    assert listed.status_code == 200, listed.text
    if visible:
        assert metadata.status_code == 200, metadata.text
        assert download.status_code == 200, download.text
        assert download.content == payload
        assert [row["id"] for row in listed.json()["data"]] == [str(document.id)]
        assert listed.json()["pagination"]["total"] == 1
    else:
        assert metadata.status_code == 404, metadata.text
        assert download.status_code == 404, download.text
        assert listed.json()["data"] == []
        assert listed.json()["pagination"]["total"] == 0
        assert "confidential report fixture" not in download.text


@pytest.mark.parametrize(
    ("key", "permission"),
    [
        (ReportKey.INVENTORY_VALUATION, Permission.INVENTORY_VALUATION_VIEW),
        (ReportKey.PRODUCT_PROFITABILITY, Permission.ITEM_COST_VIEW),
        (ReportKey.PRODUCTION_COST, Permission.ITEM_COST_VIEW),
        (ReportKey.JOB_WORK_COST, Permission.ITEM_COST_VIEW),
        # Rendered finance workbooks have no catalogue definition to name a
        # right; theirs is the one their generating route demands.
        (ReportKey.TRIAL_BALANCE, Permission.LEDGER_VIEW),
        (ReportKey.GSTR1, Permission.GST_REPORT_VIEW),
    ],
)
@pytest.mark.parametrize("legacy_attachment", [False, True])
async def test_generic_documents_require_the_report_domain_right_on_every_read(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
    permission: Permission,
    legacy_attachment: bool,
) -> None:
    _, document, payload = await _artifact(
        session, tenant, key, legacy_attachment=legacy_attachment
    )
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)
    _read_as(principal, permission)
    await _assert_visible(api_client, document, payload, visible=True)
    # A stored file must stop working on the first read after a role is narrowed.
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)


@pytest.mark.parametrize("key", [ReportKey.STOCK_AVAILABILITY, ReportKey.DEAD_STOCK])
@pytest.mark.parametrize(
    "visible_fields",
    [None, "valuation_amount", {"valuation_amount": False}, ["item_id", "valuation_amount"]],
)
async def test_missing_or_malformed_field_evidence_cannot_hide_a_valuation_column(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
    visible_fields: Any,
) -> None:
    parameters = {} if visible_fields is None else {"visible_fields": visible_fields}
    _, document, payload = await _artifact(session, tenant, key, parameters=parameters)
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)
    _read_as(principal, Permission.INVENTORY_VALUATION_VIEW)
    await _assert_visible(api_client, document, payload, visible=True)


@pytest.mark.parametrize("key", [ReportKey.STOCK_AVAILABILITY, ReportKey.DEAD_STOCK])
async def test_a_report_generated_without_cost_columns_remains_readable_without_cost_right(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
) -> None:
    definition = service.definition_for(key)
    public_fields = sorted(set(definition.row_model.model_fields) - {"valuation_amount"})
    _, document, payload = await _artifact(
        session, tenant, key, parameters={"visible_fields": public_fields}
    )
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=True)


async def test_finance_valuation_task_cannot_bypass_its_right_through_generic_documents(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
) -> None:
    from src.modules.finance.service import VALUATION_DOCUMENT_ENTITY, VALUATION_TASK_NAME

    _, document, payload = await _artifact(
        session,
        tenant,
        ReportKey.INVENTORY_VALUATION,
        parameters={
            "task": VALUATION_TASK_NAME,
            service.VALUATION_WAREHOUSE_SCOPE_PARAMETER: [],
        },
    )
    async with transaction(session):
        document.entity_type = VALUATION_DOCUMENT_ENTITY
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)
    _read_as(principal, Permission.INVENTORY_VALUATION_VIEW)
    await _assert_visible(api_client, document, payload, visible=True)


#: The rendered finance workbooks: the parameters finance queues each with, and
#: the right its generating route demands beside ``report.export``.
FINANCE_WORKBOOKS = [
    pytest.param(
        ReportKey.TRIAL_BALANCE,
        finance_service.trial_balance_export_parameters(date(2026, 9, 30)),
        Permission.LEDGER_VIEW,
        id="trial-balance",
    ),
    pytest.param(
        ReportKey.GSTR1,
        finance_service.gstr1_export_parameters(date(2026, 4, 1), date(2026, 4, 30)),
        Permission.GST_REPORT_VIEW,
        id="gstr1",
    ),
]


@pytest.mark.parametrize(("key", "parameters", "permission"), FINANCE_WORKBOOKS)
async def test_a_finance_workbook_job_needs_its_domain_right_beside_report_view(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
    parameters: dict[str, Any],
    permission: Permission,
) -> None:
    from src.main import app

    snapshot, document, payload = await _artifact(session, tenant, key, parameters=parameters)
    status_url = f"/reports/jobs/{snapshot.id}"
    download_url = f"{status_url}/download"

    # A store keeper's rights - report.view and document.view, neither the
    # ledger nor the GST returns - even as the job's own creator: the job is
    # one that does not exist on both job routes, as any job out of reach is,
    # and the generic document routes withhold its file the same way.
    _read_as(principal, Permission.REPORT_VIEW)
    for url in (status_url, download_url):
        refused = await api_client.get(url)
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "JOB_NOT_FOUND"
        assert "confidential report fixture" not in refused.text
    await _assert_visible(api_client, document, payload, visible=False)

    # A colleague holding the right shares the job and its file.
    _read_as(principal, Permission.REPORT_VIEW, permission)
    assert (await api_client.get(status_url)).status_code == 200
    shared = await api_client.get(download_url)
    assert shared.status_code == 200, shared.text
    assert shared.content == payload
    await _assert_visible(api_client, document, payload, visible=True)

    # Whoever queued it keeps it with exactly the rights queuing it needed.
    exporter = replace(
        principal,
        is_superuser=False,
        permissions=frozenset({str(permission), str(Permission.REPORT_EXPORT)}),
    )
    app.dependency_overrides[get_current_principal] = lambda: exporter
    assert (await api_client.get(status_url)).status_code == 200
    owned = await api_client.get(download_url)
    assert owned.status_code == 200, owned.text
    assert owned.content == payload


async def test_profitability_permission_does_not_widen_captured_warehouse_scope(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
) -> None:
    allowed = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"ALLOW-{uuid.uuid4().hex[:8]}"
    )
    outside = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"OTHER-{uuid.uuid4().hex[:8]}"
    )
    _, document, payload = await _artifact(
        session,
        tenant,
        ReportKey.PRODUCT_PROFITABILITY,
        parameters={service.REPORT_WAREHOUSE_SCOPE_PARAMETER: [str(allowed.id)]},
    )
    _read_as(replace(principal, allowed_warehouse_ids=(allowed.id,)), Permission.ITEM_COST_VIEW)
    await _assert_visible(api_client, document, payload, visible=True)
    _read_as(replace(principal, allowed_warehouse_ids=(outside.id,)), Permission.ITEM_COST_VIEW)
    await _assert_visible(api_client, document, payload, visible=False)


async def test_legacy_catalogue_valuation_xlsx_uses_its_own_captured_scope(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
) -> None:
    from openpyxl import Workbook

    allowed = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"LEGACY-{uuid.uuid4().hex[:8]}"
    )
    outside = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"OUTSIDE-{uuid.uuid4().hex[:8]}"
    )
    _, document, _ = await _artifact(
        session,
        tenant,
        ReportKey.INVENTORY_VALUATION,
        legacy_attachment=True,
        parameters={service.REPORT_WAREHOUSE_SCOPE_PARAMETER: [str(allowed.id)]},
    )
    workbook = Workbook()
    workbook.active.append(["Warehouse", "Valuation"])
    workbook.active.append([allowed.code, 100])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    payload = buffer.getvalue()
    previous_path = resolve_storage_path(document.storage_key)
    async with transaction(session):
        document.file_name = f"inventory-valuation-legacy-{uuid.uuid4().hex}.xlsx"
        document.storage_key = f"reports/{tenant.company_id}/{uuid.uuid4()}.xlsx"
        document.mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        document.size_bytes = len(payload)
        document.checksum_sha256 = hashlib.sha256(payload).hexdigest()
        resolve_storage_path(document.storage_key).write_bytes(payload)
    previous_path.unlink()

    # This prefix also names finance-owned workbooks. A linked catalogue job
    # with legacy blank attachments must use its catalogue capture instead of
    # being mistaken for an untracked finance export.
    restricted = replace(principal, allowed_warehouse_ids=(allowed.id,))
    _read_as(restricted, Permission.INVENTORY_VALUATION_VIEW)
    await _assert_visible(api_client, document, payload, visible=True)
    _read_as(restricted)
    await _assert_visible(api_client, document, payload, visible=False)
    _read_as(
        replace(restricted, allowed_warehouse_ids=(outside.id,)),
        Permission.INVENTORY_VALUATION_VIEW,
    )
    await _assert_visible(api_client, document, payload, visible=False)


@pytest.mark.parametrize(
    "key",
    [ReportKey.PRODUCT_PROFITABILITY, ReportKey.PRODUCTION_COST, ReportKey.DEAD_STOCK],
)
async def test_access_evidence_outlives_the_job_until_its_file_is_deleted(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
) -> None:
    snapshot, document, payload = await _artifact(session, tenant, key)
    snapshot_id, document_id = snapshot.id, document.id
    async with transaction(session):
        snapshot.expires_at = utcnow() - timedelta(days=1)
    assert await service.purge_expired_snapshots(session, tenant) == 0
    assert await session.get(ReportSnapshot, snapshot_id) is not None
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)
    await DocumentService(session, tenant).delete_document(
        document_id, reason="Remove authorization retention fixture"
    )
    await session.refresh(snapshot)
    assert snapshot.document_id is None
    assert await service.purge_expired_snapshots(session, tenant) == 1


@pytest.mark.parametrize(
    "key",
    [
        ReportKey.INVENTORY_VALUATION,
        ReportKey.PRODUCT_PROFITABILITY,
        ReportKey.TRIAL_BALANCE,
        ReportKey.GSTR1,
    ],
)
async def test_recognizable_legacy_report_without_a_job_still_requires_its_domain_right(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    key: ReportKey,
) -> None:
    snapshot, document, payload = await _artifact(session, tenant, key, legacy_attachment=True)
    async with transaction(session):
        await session.delete(snapshot)
    _read_as(principal)
    await _assert_visible(api_client, document, payload, visible=False)


async def test_generated_report_permissions_never_replace_company_isolation(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    other_company,
) -> None:
    _, document, payload = await _artifact(session, tenant, ReportKey.INVENTORY_VALUATION)
    _read_as(replace(principal, company_id=other_company.id), Permission.INVENTORY_VALUATION_VIEW)
    await _assert_visible(api_client, document, payload, visible=False)


@pytest.mark.parametrize(
    "key", [ReportKey.DEAD_STOCK, ReportKey.SCRAP_REGISTER, ReportKey.CONSUMPTION_BY_DEPARTMENT]
)
async def test_every_warehouse_scoped_export_captures_the_callers_verified_assignment(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
    key: ReportKey,
) -> None:
    from src.main import app
    from src.workers.celery_app import celery_app

    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"CAPTURE-{uuid.uuid4().hex[:8]}"
    )
    reader = replace(principal, allowed_warehouse_ids=(warehouse.id,))
    app.dependency_overrides[get_current_principal] = lambda: reader
    monkeypatch.setattr(
        celery_app, "send_task", lambda *_args, **_kwargs: SimpleNamespace(id="stub")
    )
    params = {"report_key": str(key)}
    if service.definition_for(key).requires_period:
        today = utcnow().date().isoformat()
        params.update(date_from=today, date_to=today)
    accepted = await api_client.post("/reports/export", params=params)
    assert accepted.status_code == 202, accepted.text
    snapshot = await session.get(ReportSnapshot, uuid.UUID(accepted.json()["data"]["job_id"]))
    assert snapshot is not None
    assert snapshot.parameters[service.REPORT_WAREHOUSE_SCOPE_PARAMETER] == [str(warehouse.id)]


async def test_dead_stock_workbook_uses_captured_scope_and_retains_it_on_download(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpyxl import load_workbook

    from src.main import app
    from src.workers.celery_app import AsyncTask, celery_app
    from src.workers.tasks import reports as report_tasks

    warehouses = [
        await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=f"{code}-{uuid.uuid4().hex[:8]}"
        )
        for code in ("VISIBLE", "PRIVATE")
    ]
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=f"OLD-{uuid.uuid4().hex[:8]}"
    )
    now = utcnow()
    for warehouse, value in zip(warehouses, ("100", "900"), strict=True):
        session.add(
            InventorySummary(
                company_id=tenant.company_id,
                item_id=item.id,
                warehouse_id=warehouse.id,
                on_hand=Decimal("10"),
                reserved=Decimal("0"),
                available=Decimal("10"),
                valuation_amount=Decimal(value),
                last_movement_at=now - timedelta(days=180),
                refreshed_at=now,
            )
        )
    await session.flush()
    allowed, outside = warehouses
    reader = replace(principal, allowed_warehouse_ids=(allowed.id,))
    app.dependency_overrides[get_current_principal] = lambda: reader
    monkeypatch.setattr(
        celery_app, "send_task", lambda *_args, **_kwargs: SimpleNamespace(id="stub")
    )
    accepted = await api_client.post(
        "/reports/export", params={"report_key": str(ReportKey.DEAD_STOCK)}
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["data"]["job_id"]

    @asynccontextmanager
    async def worker_session() -> AsyncIterator[AsyncSession]:
        yield session

    # The transport adapter is not under test. Execute the worker coroutine on
    # this fixture's loop so its actual queries share the rollback transaction.
    monkeypatch.setattr(report_tasks, "session_scope", worker_session)
    monkeypatch.setattr(AsyncTask, "run_async", staticmethod(lambda coroutine: coroutine))
    outcome = await report_tasks.generate_report(
        company_id=str(tenant.company_id), user_id=str(tenant.user_id), snapshot_id=job_id
    )
    assert outcome["status"] == "COMPLETED"
    assert outcome["row_count"] == 1
    download = await api_client.get(f"/reports/jobs/{job_id}/download")
    assert download.status_code == 200, download.text
    workbook = load_workbook(BytesIO(download.content), read_only=True, data_only=True)
    try:
        contents = repr(list(workbook.active.values))
        assert allowed.code in contents
        assert outside.code not in contents
    finally:
        workbook.close()
    app.dependency_overrides[get_current_principal] = lambda: replace(
        reader, allowed_warehouse_ids=(outside.id,)
    )
    assert (await api_client.get(f"/reports/jobs/{job_id}/download")).status_code == 404
    assert (
        await api_client.get(f"/documents/{outcome['document_id']}/download")
    ).status_code == 404
