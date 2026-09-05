"""Every URL a body hands back is built under the mounted prefix, at call time.

``api_path`` is the one place a service turns a route-relative path into the
absolute path a client can fetch. The report and MRP helpers build on it, so
each of them is pinned here against the prefix the routers are mounted under -
the default, a changed one, and one changed *after* import - and against the
router's own statement of where the route lives.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from src.core.config import settings
from src.core.responses import api_path
from src.modules.mrp import service as mrp_service
from src.modules.mrp.router import router as mrp_routes
from src.modules.reports import service as reports_service

pytestmark = pytest.mark.unit


class TestApiPath:
    def test_a_relative_path_is_placed_under_the_prefix(self) -> None:
        prefix = settings.api_v1_prefix.rstrip("/")
        assert api_path("reports/jobs/1") == f"{prefix}/reports/jobs/1"

    def test_a_leading_slash_does_not_double_up(self) -> None:
        assert api_path("/reports/jobs/1") == api_path("reports/jobs/1")

    def test_a_trailing_slash_on_the_prefix_does_not_double_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", "/api/v1/")
        assert api_path("/mrp/runs/1") == "/api/v1/mrp/runs/1"

    def test_the_prefix_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        before = api_path("x")
        monkeypatch.setattr(settings, "api_v1_prefix", "/elsewhere")
        assert api_path("x") == "/elsewhere/x"
        assert api_path("x") != before


class TestEveryTemplateStartsWithThePrefix:
    """The three helpers that put a URL in a body all honour the prefix."""

    @pytest.mark.parametrize("prefix", [settings.api_v1_prefix, "/backend/v2"])
    def test_each_built_url_starts_with_the_mounted_prefix(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", prefix)
        identifier = uuid.uuid4()
        built = [
            reports_service.job_status_url(identifier),
            reports_service.document_download_url(identifier),
            mrp_service.status_url(identifier),
        ]
        for url in built:
            assert url.startswith(prefix.rstrip("/") + "/"), url

    def test_the_templates_themselves_are_prefix_relative(self) -> None:
        """A prefix baked into a template is what made ``API_V1_PREFIX`` a lie."""
        for template in (
            reports_service.JOB_STATUS_URL_TEMPLATE,
            reports_service.DOCUMENT_DOWNLOAD_URL_TEMPLATE,
            mrp_service.STATUS_URL_TEMPLATE,
        ):
            assert template.startswith("/"), template
            assert not template.startswith(settings.api_v1_prefix.rstrip("/") + "/"), template

    @pytest.mark.parametrize("prefix", [settings.api_v1_prefix, "/backend/v2"])
    def test_the_mrp_status_url_is_the_run_route(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        monkeypatch.setattr(settings, "api_v1_prefix", prefix)
        application = FastAPI()
        application.include_router(mrp_routes, prefix=prefix)
        run_id = uuid.uuid4()

        assert mrp_service.status_url(run_id) == str(
            application.url_path_for("get_run", run_id=str(run_id))
        )
