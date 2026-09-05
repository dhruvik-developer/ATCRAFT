"""The URLs a 202 body hands back point at routes that exist, under the prefix
the routes are mounted on.

Two things went wrong here before and neither was caught by a test. The packing
list and the valuation export answered ``202`` with ``/api/v1/jobs/{id}``, a
path no router serves, so every poll was a 404 and the UI called every export
failed. And the report templates baked ``/api/v1`` in, so an operator who set
``API_V1_PREFIX`` would have had every ``status_url`` and ``download_url``
pointing at a path that no longer existed. Both are asserted here against the
routers themselves - ``url_path_for`` is the router's own statement of where a
route lives - with the default prefix and with a changed one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI

import src.modules.finance.router as finance_router
import src.modules.packing.router as packing_router
from src.core.config import settings
from src.modules.documents.router import router as documents_routes
from src.modules.finance import service as finance_service
from src.modules.packing import service as packing_service
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.reports.router import router as reports_routes

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)


def _application(prefix: str) -> FastAPI:
    # A private application holding only the two routers whose paths the
    # helpers describe - the process-global app is not used, see
    # tests/unit/test_inventory_query_binding.py for why.
    application = FastAPI()
    application.include_router(reports_routes, prefix=prefix)
    application.include_router(documents_routes, prefix=prefix)
    return application


# -----------------------------------------------------------------------------
# The helpers resolve against the routes, whatever the prefix
# -----------------------------------------------------------------------------


class TestUrlHelpers:
    @pytest.mark.parametrize("prefix", [settings.api_v1_prefix, "/backend/v2"])
    def test_the_job_status_url_is_the_job_route(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", prefix)
        application = _application(prefix)
        job_id = uuid.uuid4()

        assert reports_service.job_status_url(job_id) == str(
            application.url_path_for("get_job", job_id=str(job_id))
        )

    @pytest.mark.parametrize("prefix", [settings.api_v1_prefix, "/backend/v2"])
    def test_the_download_url_is_the_download_route(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", prefix)
        application = _application(prefix)
        document_id = uuid.uuid4()

        assert reports_service.document_download_url(document_id) == str(
            application.url_path_for("download_document", document_id=str(document_id))
        )

    def test_a_trailing_slash_on_the_prefix_does_not_double_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", "/api/v1/")
        job_id = uuid.uuid4()
        assert reports_service.job_status_url(job_id) == f"/api/v1/reports/jobs/{job_id}"

    def test_the_prefix_is_read_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = uuid.uuid4()
        before = reports_service.job_status_url(job_id)
        monkeypatch.setattr(settings, "api_v1_prefix", "/elsewhere")
        assert reports_service.job_status_url(job_id) == f"/elsewhere/reports/jobs/{job_id}"
        assert reports_service.job_status_url(job_id) != before

    def test_a_completed_job_carries_the_download_link_and_a_queued_one_none(self) -> None:
        document_id = uuid.uuid4()
        completed = _snapshot(status=ReportJobStatus.COMPLETED, document_id=document_id)
        queued = _snapshot(status=ReportJobStatus.QUEUED, document_id=None)

        assert reports_service.job_read(completed).download_url == (
            reports_service.document_download_url(document_id)
        )
        assert reports_service.job_read(queued).download_url is None


def _snapshot(*, status: ReportJobStatus, document_id: uuid.UUID | None) -> ReportSnapshot:
    return ReportSnapshot(
        id=uuid.uuid4(),
        report_key=ReportKey.PACKING_LIST,
        parameters_hash="0" * 64,
        parameters={"report_key": "PACKING_LIST"},
        status=status,
        row_count=0,
        generated_at=NOW if status is ReportJobStatus.COMPLETED else None,
        document_id=document_id,
        created_at=NOW,
    )


# -----------------------------------------------------------------------------
# The two document-only 202s point at the same route every other job uses
# -----------------------------------------------------------------------------


class TestDocumentJobResponses:
    async def test_the_packing_list_202_polls_the_report_job_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = str(uuid.uuid4())

        async def _queue(session: Any, tenant: Any, order_id: uuid.UUID) -> str:
            return job_id

        monkeypatch.setattr(packing_service, "queue_packing_list", _queue)

        response = await packing_router.packing_list(
            session=None,  # type: ignore[arg-type]
            tenant=None,  # type: ignore[arg-type]
            order_id=uuid.uuid4(),
        )

        assert response.data["job_id"] == job_id
        assert response.data["status_url"] == reports_service.job_status_url(job_id)
        assert response.data["status_url"].endswith(f"/reports/jobs/{job_id}")

    async def test_the_valuation_export_202_polls_the_report_job_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = str(uuid.uuid4())

        async def _queue(session: Any, tenant: Any, filters: Any) -> str:
            return job_id

        monkeypatch.setattr(finance_service, "queue_inventory_valuation", _queue)

        response = await finance_router.export_inventory_valuation(
            session=None,  # type: ignore[arg-type]
            tenant=None,  # type: ignore[arg-type]
            warehouse_id=None,
            category_id=None,
        )

        assert response.data["job_id"] == job_id
        assert response.data["status_url"] == reports_service.job_status_url(job_id)
        assert response.data["status_url"].endswith(f"/reports/jobs/{job_id}")
