"""Two 202 routes, followed through to the document they file.

``POST /finance/inventory-valuation/export`` and ``GET /packing/{id}/packing-list``
each hand the work to Celery and answer with a job id that ``GET
/reports/jobs/{id}`` resolves. This walks the journey a client actually takes:
queue, poll and read QUEUED, let the worker run, poll again and read COMPLETED
with a download link, and find the document row the link points at. A route
that returned a job id nothing could resolve, or a worker that finished without
completing the job row, passes every service-level test and leaves the client
polling for ever.

Committed for real, in a private schema: the worker opens its own session, so
rows left inside the ``session`` fixture's rolled-back transaction would be
invisible to it. The task is run the way Celery's ``AsyncTask`` base runs it -
driving a fresh event loop with ``run_until_complete`` - which cannot happen on
the loop this test is already on, so it runs on a worker thread; the engine is
``NullPool``, so the connections it opens belong to that loop alone. The worker
is pointed at the private schema by replacing its ``session_scope``, and
``send_task`` is stubbed: the broker is not what is under test.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import get_db, transaction
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.models import Base
from src.core.permissions import Permission
from src.modules.customers.models import Customer, CustomerType
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.service import resolve_storage_path
from src.modules.inventory import service as inventory_service
from src.modules.inventory.models import TransactionType
from src.modules.packing import service as packing_service
from src.modules.packing.models import PackingOrderStatus
from src.modules.packing.schemas import PackingOrderCreate, PackingOrderLineIn
from src.modules.products.models import ItemGroup, PriceType
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.warehouses.models import WarehouseType
from src.workers import celery_app as celery_module
from src.workers.tasks import reports as report_tasks
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = [pytest.mark.api, pytest.mark.integration]

# ``create_all`` resolves foreign keys against the metadata, so every module has
# to be imported first - see tests/schema_support.
prepare_metadata()


def _schema_engine(schema: str):
    """Engine pinned to one schema, one connection per session (NullPool)."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


def _factory(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """A private schema with one company and one user, dropped afterwards."""
    schema = f"it_report_jobs_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            async with _factory(engine)() as session, session.begin():
                await seed_support.seed_company(session, company_id=company_id, prefix="RPT")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"reports-{uuid.uuid4().hex[:8]}@atcraft.test",
                )
        finally:
            await engine.dispose()

    async def teardown() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def engine(_schema: tuple[str, uuid.UUID, uuid.UUID]) -> AsyncIterator[Any]:
    schema, _, _ = _schema
    eng = _schema_engine(schema)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def tenant(_schema: tuple[str, uuid.UUID, uuid.UUID]) -> TenantContext:
    _, company_id, user_id = _schema
    return TenantContext(company_id=company_id, user_id=user_id)


@pytest.fixture
def principal(_schema: tuple[str, uuid.UUID, uuid.UUID]) -> Principal:
    _, company_id, user_id = _schema
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email="reports@atcraft.test",
        full_name="Reports Tester",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("TEST_ADMIN",),
    )


@pytest_asyncio.fixture
async def client(
    engine: Any, principal: Principal, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncIterator[AsyncClient]:
    """The app over ASGI, its request sessions and the worker's sessions both
    bound to the private schema, the broker stubbed and files kept in tmp_path."""
    from src.main import app

    factory = _factory(engine)

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    @asynccontextmanager
    async def worker_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def accepted_by_the_broker(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id=f"stub-{uuid.uuid4().hex}")

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted_by_the_broker)
    monkeypatch.setattr(report_tasks, "session_scope", worker_session)
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[get_current_principal] = lambda: principal
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _prefix() -> str:
    return settings.api_v1_prefix.rstrip("/")


async def _run_worker(task: Any, **kwargs: str) -> dict[str, Any]:
    """Run a Celery task inline, off the test's own event loop."""
    return await asyncio.to_thread(task, **kwargs)


async def _job(client: AsyncClient, status_url: str) -> dict[str, Any]:
    response = await client.get(status_url)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _filed_document(engine: Any, document_id: str) -> Document:
    async with _factory(engine)() as reader:
        document = await reader.get(Document, uuid.UUID(document_id))
    assert document is not None, "the job reports a document that was never filed"
    return document


def _assert_queued(job: dict[str, Any], *, job_id: str, report_key: str) -> None:
    assert job["id"] == job_id
    assert job["status"] == "QUEUED"
    assert job["report_key"] == report_key
    assert job["document_id"] is None
    assert job["download_url"] is None


def _assert_completed(job: dict[str, Any], *, job_id: str, report_key: str) -> str:
    assert job["id"] == job_id
    assert job["status"] == "COMPLETED", job
    assert job["report_key"] == report_key
    assert job["error_message"] is None
    document_id = job["document_id"]
    assert document_id is not None
    assert job["download_url"] == (
        reports_service.job_download_url(job_id)
        if report_key == "INVENTORY_VALUATION"
        else reports_service.document_download_url(document_id)
    )
    return str(document_id)


async def _stock_a_position(engine: Any, tenant: TenantContext) -> None:
    """One valued position, so the workbook has a row to carry."""
    async with _factory(engine)() as session, transaction(session):
        warehouse = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WH")
        )
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            gst_rate=Decimal("18"),
            standard_cost=Decimal("10"),
        )
        await inventory_service.post_movement(
            session,
            tenant,
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("25"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="report job fixture opening stock",
            unit_cost=Decimal("10"),
        )


async def _packing_in_progress(engine: Any, tenant: TenantContext) -> uuid.UUID:
    """A packing order on the bench - the earliest state a packing list means anything."""
    async with _factory(engine)() as session:
        async with transaction(session):
            uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
            warehouse = await seed_support.seed_warehouse(
                session,
                company_id=tenant.company_id,
                code=_code("FG"),
                warehouse_type=WarehouseType.FINISHED_GOODS,
            )
            goods = await seed_support.seed_item(
                session,
                company_id=tenant.company_id,
                code=_code("LOCK"),
                base_uom_id=uom.id,
                item_group=ItemGroup.FINISHED_GOOD,
                gst_rate=Decimal("18"),
                standard_cost=Decimal("60"),
                sale_price=Decimal("100"),
                can_sell=True,
            )
            customer = Customer(
                company_id=tenant.company_id,
                code=_code("CU"),
                name="Hardware Distributors",
                customer_type=CustomerType.CUSTOMER,
                price_list_type=PriceType.SALE,
                credit_days=30,
                is_active=True,
                is_blocked=False,
            )
            session.add(customer)
            await session.flush()

        order, _ = await packing_service.create_order(
            session,
            tenant,
            PackingOrderCreate(
                customer_id=customer.id,
                warehouse_id=warehouse.id,
                planned_date=date(2026, 9, 1),
                lines=[
                    PackingOrderLineIn(
                        item_id=goods.id, quantity_to_pack=Decimal("10"), uom_id=uom.id
                    )
                ],
            ),
        )
        started, _ = await packing_service.start_packing(
            session, tenant, order.id, reason="Bench is free"
        )
        assert PackingOrderStatus(started.status) is PackingOrderStatus.IN_PROGRESS
        return order.id


# -----------------------------------------------------------------------------
# The journeys
# -----------------------------------------------------------------------------


async def test_the_inventory_valuation_export_is_a_job_that_files_a_workbook(
    client: AsyncClient, engine: Any, tenant: TenantContext
) -> None:
    await _stock_a_position(engine, tenant)

    accepted = await client.post(f"{_prefix()}/finance/inventory-valuation/export")
    assert accepted.status_code == 202, accepted.text
    handed_off = accepted.json()["data"]
    job_id = handed_off["job_id"]
    assert handed_off["status_url"] == reports_service.job_status_url(job_id)

    _assert_queued(
        await _job(client, handed_off["status_url"]),
        job_id=job_id,
        report_key="INVENTORY_VALUATION",
    )

    outcome = await _run_worker(
        report_tasks.inventory_valuation,
        company_id=str(tenant.company_id),
        user_id=str(tenant.user_id),
        snapshot_id=job_id,
    )
    assert outcome["status"] == "COMPLETED", outcome

    document_id = _assert_completed(
        await _job(client, handed_off["status_url"]),
        job_id=job_id,
        report_key="INVENTORY_VALUATION",
    )
    document = await _filed_document(engine, document_id)
    assert document.company_id == tenant.company_id
    assert document.category == DocumentCategory.REPORT
    assert document.file_name.endswith(".xlsx")
    assert document.size_bytes > 0
    assert resolve_storage_path(document.storage_key).is_file()


async def test_restricted_valuation_exporter_can_watch_and_download_only_its_own_export(
    client: AsyncClient, engine: Any, tenant: TenantContext, principal: Principal
) -> None:
    from src.main import app

    exporter = replace(
        principal,
        permissions=frozenset(
            {str(Permission.INVENTORY_VALUATION_VIEW), str(Permission.REPORT_EXPORT)}
        ),
    )
    app.dependency_overrides[get_current_principal] = lambda: exporter
    await _stock_a_position(engine, tenant)
    accepted = await client.post(f"{_prefix()}/finance/inventory-valuation/export")
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["data"]["job_id"]
    status_url = accepted.json()["data"]["status_url"]
    download_url = reports_service.job_download_url(job_id)
    assert (await client.get(status_url)).status_code == 200
    assert (await client.get(download_url)).status_code == 404  # no file until completion

    outcome = await _run_worker(
        report_tasks.inventory_valuation,
        company_id=str(tenant.company_id),
        user_id=str(tenant.user_id),
        snapshot_id=job_id,
    )
    assert outcome["status"] == "COMPLETED", outcome
    job = await _job(client, status_url)
    assert job["download_url"] == download_url
    downloaded = await client.get(download_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content.startswith(b"PK")  # actual generated workbook
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert int(downloaded.headers["content-length"]) == len(downloaded.content)
    assert "attachment" in downloaded.headers["content-disposition"]

    # The exceptional right is bound to this server-issued job, not broad file
    # or catalogue access; arbitrary document ids cannot replace that binding.
    assert (await client.get(f"{_prefix()}/reports/catalogue")).status_code == 403
    assert (
        await client.get(f"{_prefix()}/documents/{job['document_id']}/download")
    ).status_code == 403
    substituted = await client.get(download_url, params={"document_id": str(uuid.uuid4())})
    assert substituted.status_code == 200
    assert substituted.content == downloaded.content

    for intruder in (
        replace(exporter, user_id=uuid.uuid4()),
        replace(exporter, company_id=uuid.uuid4()),
        replace(exporter, permissions=frozenset({str(Permission.INVENTORY_VALUATION_VIEW)})),
    ):
        app.dependency_overrides[get_current_principal] = lambda user=intruder: user
        assert (await client.get(status_url)).status_code == 404
        assert (await client.get(download_url)).status_code == 404

    # Even broad grants do not make a vendor an internal report principal.
    vendor = replace(principal, vendor_id=uuid.uuid4())
    app.dependency_overrides[get_current_principal] = lambda: vendor
    assert (await client.get(status_url)).status_code == 403
    assert (await client.get(download_url)).status_code == 403

    # Preserve the existing internal report/document-reader access, including
    # shared jobs generated by a different colleague.
    reader = replace(
        principal,
        user_id=uuid.uuid4(),
        permissions=frozenset({str(Permission.REPORT_VIEW), str(Permission.DOCUMENT_VIEW)}),
    )
    app.dependency_overrides[get_current_principal] = lambda: reader
    assert (await client.get(status_url)).status_code == 200
    assert (await client.get(download_url)).status_code == 200
    app.dependency_overrides[get_current_principal] = lambda: replace(
        reader, permissions=frozenset({str(Permission.REPORT_VIEW)})
    )
    assert (await client.get(status_url)).status_code == 200
    assert (await client.get(download_url)).status_code == 403


async def test_valuation_permission_does_not_open_catalogue_jobs_or_other_document_jobs(
    client: AsyncClient, engine: Any, tenant: TenantContext, principal: Principal
) -> None:
    from src.core.clock import utcnow
    from src.main import app

    ids = []
    async with _factory(engine)() as session, transaction(session):
        repo = ReportSnapshotRepository(session, tenant)
        for key, params in (
            (ReportKey.INVENTORY_VALUATION, {"filters": {}}),  # same key, different origin
            (ReportKey.PACKING_LIST, {"packing_order_id": str(uuid.uuid4())}),
            (ReportKey.STOCK_AVAILABILITY, {"task": "reports.generate_inventory_valuation"}),
        ):
            snapshot = reports_service.new_job_snapshot(key, params, now=utcnow())
            repo.add(snapshot)
            await session.flush()
            ids.append(snapshot.id)
    exporter = replace(
        principal,
        permissions=frozenset(
            {str(Permission.INVENTORY_VALUATION_VIEW), str(Permission.REPORT_EXPORT)}
        ),
    )
    app.dependency_overrides[get_current_principal] = lambda: exporter
    for job_id in ids:
        assert (await client.get(reports_service.job_status_url(job_id))).status_code == 404
        assert (await client.get(reports_service.job_download_url(job_id))).status_code == 404

    # Generic report access still works for the original catalogue job types.
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, permissions=frozenset({str(Permission.REPORT_VIEW)})
    )
    for job_id in ids:
        assert (await client.get(reports_service.job_status_url(job_id))).status_code == 200


async def test_the_packing_list_is_a_job_that_files_a_pdf(
    client: AsyncClient, engine: Any, tenant: TenantContext
) -> None:
    order_id = await _packing_in_progress(engine, tenant)

    accepted = await client.get(f"{_prefix()}/packing/{order_id}/packing-list")
    assert accepted.status_code == 202, accepted.text
    handed_off = accepted.json()["data"]
    job_id = handed_off["job_id"]
    assert handed_off["status_url"] == reports_service.job_status_url(job_id)

    _assert_queued(
        await _job(client, handed_off["status_url"]), job_id=job_id, report_key="PACKING_LIST"
    )

    outcome = await _run_worker(
        report_tasks.generate_packing_list,
        packing_order_id=str(order_id),
        snapshot_id=job_id,
        company_id=str(tenant.company_id),
        user_id=str(tenant.user_id),
    )
    assert outcome["status"] == "COMPLETED", outcome

    document_id = _assert_completed(
        await _job(client, handed_off["status_url"]), job_id=job_id, report_key="PACKING_LIST"
    )
    document = await _filed_document(engine, document_id)
    assert document.company_id == tenant.company_id
    assert document.category == DocumentCategory.REPORT
    assert document.entity_type == report_tasks.PACKING_LIST_ENTITY
    assert document.entity_id == order_id
    assert document.file_name.endswith(".pdf")
    assert resolve_storage_path(document.storage_key).is_file()
