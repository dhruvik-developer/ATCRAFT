"""Report documentation names the same rows that the service returns.

The report handler deliberately bypasses a fixed response model: small results
are pages, while large results become jobs. Both need concrete OpenAPI schemas
so frontend types can describe the actual response without inventing row shapes.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from src.modules.reports.models import ReportKey
from src.modules.reports.router import router
from src.modules.reports.service import REPORT_CATALOGUE

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def report_spec() -> dict[str, Any]:
    application = FastAPI()
    application.include_router(router)
    return application.openapi()


@pytest.mark.parametrize("key", list(REPORT_CATALOGUE))
def test_report_schemas_match_the_catalogue(report_spec: dict[str, Any], key: ReportKey) -> None:
    path = f"/reports/{key.value.lower().replace('_', '-')}"
    responses = report_spec["paths"][path]["get"]["responses"]
    inline_ref = responses["200"]["content"]["application/json"]["schema"]["$ref"]
    inline = report_spec["components"]["schemas"][inline_ref.rsplit("/", 1)[1]]

    assert {"success", "data", "message", "pagination"} <= inline["properties"].keys()
    row_ref = inline["properties"]["data"]["items"]["$ref"]
    assert row_ref == f"#/components/schemas/{REPORT_CATALOGUE[key].row_model.__name__}"

    queued = responses["202"]["content"]["application/json"]["schema"]
    assert queued == {"$ref": "#/components/schemas/AcceptedJobResponse"}
