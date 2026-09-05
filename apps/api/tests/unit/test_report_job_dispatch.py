"""Background jobs: the row that is polled, and the publish that must not block.

Three rules, none of which an integration run would notice breaking:

* **A job id is a snapshot row.** The packing list and the valuation export
  used to return a bare Celery task id, which ``GET /reports/jobs/{id}`` could
  never resolve. Both now create the same ``report_snapshots`` row every other
  background report uses, in the same transaction as their audit entry, and
  return *its* id.
* **The publish runs off the event loop.** ``send_task`` opens a socket to the
  broker with connect-and-retry; inline, an unreachable broker stalls every
  request the process is serving for the length of the timeout. The call is
  asserted to run on another thread, with its kwargs assembled beforehand.
* **A refused publish fails the job.** Otherwise a client polls a QUEUED job
  that no worker will ever pick up.

Repositories, the transaction boundary and the broker are stubbed; nothing here
opens a connection.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError, ErrorCode
from src.modules.audit.models import AuditAction
from src.modules.finance import service as finance_service
from src.modules.finance.schemas import ValuationFilters
from src.modules.packing import service as packing_service
from src.modules.packing.models import PackingOrderStatus
from src.modules.packing.repository import PackingOrderRepository
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.reports.schemas import ReportFilters
from src.workers.celery_app import celery_app

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)


class _Session:
    """The only session methods the queueing paths call directly."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushes = 0

    def add(self, instance: Any) -> None:
        # The primary key is a Python-side default assigned at flush; a fake
        # session has to stand in for that or nothing downstream has an id.
        if getattr(instance, "id", None) is None:
            instance.id = uuid.uuid4()
        self.added.append(instance)

    async def flush(self) -> None:
        self.flushes += 1


@asynccontextmanager
async def _no_transaction(session: Any) -> AsyncIterator[Any]:
    yield session


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _snapshot() -> ReportSnapshot:
    snapshot = reports_service.new_job_snapshot(
        ReportKey.PACKING_LIST, {"report_key": "PACKING_LIST"}, now=NOW
    )
    snapshot.id = uuid.uuid4()
    return snapshot


# -----------------------------------------------------------------------------
# The job row
# -----------------------------------------------------------------------------


class TestNewJobSnapshot:
    def test_it_starts_queued_with_the_hash_and_the_retention_window(self) -> None:
        parameters = {"report_key": "PACKING_LIST", "packing_order_id": str(uuid.uuid4())}

        snapshot = reports_service.new_job_snapshot(ReportKey.PACKING_LIST, parameters, now=NOW)

        assert snapshot.status is ReportJobStatus.QUEUED
        assert snapshot.report_key is ReportKey.PACKING_LIST
        assert snapshot.parameters == parameters
        assert snapshot.parameters_hash == reports_service.parameters_hash(parameters)
        assert snapshot.expires_at == NOW + timedelta(
            hours=reports_service.REPORT_SNAPSHOT_TTL_HOURS
        )
        assert snapshot.celery_task_id is None
        assert snapshot.document_id is None


# -----------------------------------------------------------------------------
# Publishing
# -----------------------------------------------------------------------------


class TestDispatchJob:
    async def test_the_publish_runs_off_the_loop_thread_and_records_the_task_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.current_thread()
        seen: dict[str, Any] = {}

        def _send_task(name: str, *, kwargs: dict[str, Any], queue: str) -> Any:
            seen["thread"] = threading.current_thread()
            seen["name"] = name
            seen["kwargs"] = kwargs
            seen["queue"] = queue
            return SimpleNamespace(id="celery-task-1")

        monkeypatch.setattr(celery_app, "send_task", _send_task)
        monkeypatch.setattr(reports_service, "transaction", _no_transaction)
        session = _Session()
        tenant = _tenant()
        snapshot = _snapshot()

        task_id = await reports_service.dispatch_job(
            session,  # type: ignore[arg-type]
            tenant,
            snapshot,
            task_name="reports.generate_packing_list",
            queue="reports",
            task_kwargs={"packing_order_id": "abc"},
        )

        assert task_id == "celery-task-1"
        assert snapshot.celery_task_id == "celery-task-1"
        assert snapshot.updated_by_id == tenant.user_id
        # The socket is opened on a worker thread, never on the loop.
        assert seen["thread"] is not loop_thread
        assert seen["name"] == "reports.generate_packing_list"
        assert seen["queue"] == "reports"
        # The worker is told which job row to drive, and the caller's own
        # arguments travel with it; nothing is read on the worker thread.
        assert seen["kwargs"] == {
            "packing_order_id": "abc",
            "snapshot_id": str(snapshot.id),
            "company_id": str(tenant.company_id),
            "user_id": str(tenant.user_id),
            "request_id": None,
        }

    async def test_the_job_row_wins_over_a_caller_argument_of_the_same_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _send_task(name: str, *, kwargs: dict[str, Any], queue: str) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(id="t")

        monkeypatch.setattr(celery_app, "send_task", _send_task)
        monkeypatch.setattr(reports_service, "transaction", _no_transaction)
        snapshot = _snapshot()

        await reports_service.dispatch_job(
            _Session(),  # type: ignore[arg-type]
            _tenant(),
            snapshot,
            task_name="x",
            queue="reports",
            task_kwargs={"snapshot_id": "forged"},
        )

        assert seen["snapshot_id"] == str(snapshot.id)

    async def test_a_refused_publish_fails_the_job_and_answers_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _send_task(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("broker unreachable")

        failed: list[tuple[uuid.UUID, str]] = []

        async def _fail(session: Any, tenant: Any, *, snapshot_id: uuid.UUID, error: str) -> None:
            failed.append((snapshot_id, error))

        monkeypatch.setattr(celery_app, "send_task", _send_task)
        monkeypatch.setattr(reports_service, "fail_snapshot", _fail)
        snapshot = _snapshot()

        with pytest.raises(AppError) as exc:
            await reports_service.dispatch_job(
                _Session(),  # type: ignore[arg-type]
                _tenant(),
                snapshot,
                task_name="reports.generate_packing_list",
                queue="reports",
                subject="The packing list",
            )

        assert exc.value.status_code == 503
        assert exc.value.code is ErrorCode.SERVICE_UNAVAILABLE
        assert exc.value.message == (
            "The packing list could not be queued for background generation."
        )
        # The job is marked FAILED with the same sentence the caller saw, so a
        # client that polls anyway is told rather than left waiting.
        assert failed == [(snapshot.id, exc.value.message)]
        assert snapshot.celery_task_id is None


# -----------------------------------------------------------------------------
# The packing list queues a job row
# -----------------------------------------------------------------------------


def _order(status: PackingOrderStatus) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), code="PK/2026-27/00007", status=status, updated_at=NOW)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[ReportSnapshot, dict[str, Any]]]:
    calls: list[tuple[ReportSnapshot, dict[str, Any]]] = []

    async def _dispatch(session: Any, tenant: Any, snapshot: ReportSnapshot, **kw: Any) -> str:
        calls.append((snapshot, kw))
        return "celery-task-1"

    monkeypatch.setattr(reports_service, "dispatch_job", _dispatch)
    return calls


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _record_audit(session: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    for module in (packing_service, finance_service):
        monkeypatch.setattr(module, "record_audit", _record_audit)
        monkeypatch.setattr(module, "transaction", _no_transaction)
    return calls


class TestQueuePackingList:
    async def test_the_job_is_a_snapshot_row_created_with_its_audit_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatched: list[tuple[ReportSnapshot, dict[str, Any]]],
        audits: list[dict[str, Any]],
    ) -> None:
        order = _order(PackingOrderStatus.IN_PROGRESS)

        async def _get_or_404(self: Any, order_id: uuid.UUID) -> Any:
            return order

        monkeypatch.setattr(PackingOrderRepository, "get_or_404", _get_or_404)
        session = _Session()
        tenant = _tenant()

        job_id = await packing_service.queue_packing_list(
            session,  # type: ignore[arg-type]
            tenant,
            order.id,
        )

        [snapshot] = session.added
        assert isinstance(snapshot, ReportSnapshot)
        # The id handed back is the row the status route reads - never the
        # Celery task id.
        assert job_id == str(snapshot.id)
        assert job_id != "celery-task-1"
        assert snapshot.report_key is ReportKey.PACKING_LIST
        assert snapshot.status is ReportJobStatus.QUEUED
        assert snapshot.company_id == tenant.company_id
        assert snapshot.parameters == packing_service.packing_list_parameters(order)
        assert snapshot.parameters["packing_order_id"] == str(order.id)
        assert snapshot.parameters["packing_order_code"] == order.code
        assert session.flushes == 1

        # The worker is told the job row and the order, on the reports queue.
        assert dispatched == [
            (
                snapshot,
                {
                    "task_name": packing_service.PACKING_LIST_TASK_NAME,
                    "queue": packing_service.PACKING_LIST_QUEUE,
                    "task_kwargs": {"packing_order_id": str(order.id)},
                    "subject": "The packing list",
                },
            )
        ]

        # The audit entry stays against the packing order and names the job.
        [audit] = audits
        assert audit["action"] is AuditAction.JOB_STARTED
        assert audit["entity_type"] == packing_service.AUDIT_PACKING_ORDER
        assert audit["entity_id"] == order.id
        assert audit["entity_code"] == order.code
        assert audit["context"]["job_id"] == job_id

    async def test_a_changed_order_hashes_to_a_different_job(self) -> None:
        # ``as_of`` is the order's ``updated_at``: the hash says which version
        # of the order the PDF describes.
        order = _order(PackingOrderStatus.IN_PROGRESS)
        first = reports_service.parameters_hash(packing_service.packing_list_parameters(order))
        order.updated_at = NOW + timedelta(minutes=1)
        second = reports_service.parameters_hash(packing_service.packing_list_parameters(order))
        assert first != second

    @pytest.mark.parametrize("status", [PackingOrderStatus.DRAFT, PackingOrderStatus.CANCELLED])
    async def test_an_order_that_never_started_packing_queues_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatched: list[tuple[ReportSnapshot, dict[str, Any]]],
        audits: list[dict[str, Any]],
        status: PackingOrderStatus,
    ) -> None:
        order = _order(status)

        async def _get_or_404(self: Any, order_id: uuid.UUID) -> Any:
            return order

        monkeypatch.setattr(PackingOrderRepository, "get_or_404", _get_or_404)
        session = _Session()

        with pytest.raises(BusinessRuleError) as exc:
            await packing_service.queue_packing_list(
                session,  # type: ignore[arg-type]
                _tenant(),
                order.id,
            )

        assert order.code in exc.value.message
        assert session.added == []
        assert dispatched == []
        assert audits == []


# -----------------------------------------------------------------------------
# The valuation export queues a job row
# -----------------------------------------------------------------------------


class TestQueueInventoryValuation:
    async def test_the_job_is_a_snapshot_row_created_with_its_audit_entry(
        self,
        dispatched: list[tuple[ReportSnapshot, dict[str, Any]]],
        audits: list[dict[str, Any]],
    ) -> None:
        warehouse_id = uuid.uuid4()
        filters = ValuationFilters(warehouse_id=warehouse_id)
        session = _Session()
        tenant = _tenant()

        job_id = await finance_service.queue_inventory_valuation(
            session,  # type: ignore[arg-type]
            tenant,
            filters,
        )

        [snapshot] = session.added
        assert isinstance(snapshot, ReportSnapshot)
        assert job_id == str(snapshot.id)
        assert job_id != "celery-task-1"
        assert snapshot.report_key is ReportKey.INVENTORY_VALUATION
        assert snapshot.status is ReportJobStatus.QUEUED
        assert snapshot.company_id == tenant.company_id
        assert snapshot.parameters == finance_service.valuation_export_parameters(filters)
        assert snapshot.parameters["filters"] == {"warehouse_id": str(warehouse_id)}

        assert dispatched == [
            (
                snapshot,
                {
                    "task_name": finance_service.VALUATION_TASK_NAME,
                    "queue": finance_service.VALUATION_QUEUE,
                    "task_kwargs": {"warehouse_id": str(warehouse_id), "category_id": None},
                    "subject": "The inventory valuation",
                },
            )
        ]

        [audit] = audits
        assert audit["action"] is AuditAction.JOB_STARTED
        assert audit["entity_type"] == "inventory_valuation"
        assert audit["entity_id"] == snapshot.id
        assert audit["context"]["job_id"] == job_id

    def test_the_export_never_shares_a_snapshot_with_the_tabular_report(self) -> None:
        # ``_queue_report`` reuses a live snapshot by ``(report_key, hash)``. The
        # workbook and the paginated grid have different columns, so the export
        # over the same filters must hash apart from the catalogue report.
        warehouse_id = uuid.uuid4()
        export = reports_service.parameters_hash(
            finance_service.valuation_export_parameters(ValuationFilters(warehouse_id=warehouse_id))
        )
        definition = reports_service.definition_for(ReportKey.INVENTORY_VALUATION)
        ctx = reports_service.build_context(
            definition, None, ReportFilters(warehouse_id=warehouse_id), now=NOW
        )
        tabular = reports_service.parameters_hash(reports_service.parameters_of(definition, ctx))
        assert export != tabular

    def test_unset_filters_are_dropped_from_the_stored_parameters(self) -> None:
        parameters = finance_service.valuation_export_parameters(ValuationFilters())
        assert parameters["filters"] == {}
        assert parameters["report_key"] == "INVENTORY_VALUATION"
        assert parameters["task"] == finance_service.VALUATION_TASK_NAME
