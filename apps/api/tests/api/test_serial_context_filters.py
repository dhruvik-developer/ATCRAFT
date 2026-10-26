"""Scoped serial selectors preserve history while identifying vendor custody."""

import uuid
from dataclasses import replace

import pytest

from src.core.deps import get_current_principal
from src.modules.job_work import service as job_work
from tests.integration.test_serial_job_work import _issued, _receive
from tests.integration.test_serial_job_work import kit as kit

pytestmark = [pytest.mark.api, pytest.mark.integration]


async def test_serial_context_filters_keep_history_separate_from_vendor_outstanding_units(
    kit, principal, api_client, other_company
):
    from src.main import app

    k = kit
    order = await _issued(k, convert=True, qc=True)
    detail = await job_work.get_order_detail(k.session, k.tenant, order.id)
    line_id = str(detail.lines[0].id)
    filters = {"job_work_order_line_id": line_id, "status": "ISSUED", "page_size": 2}
    first = await api_client.get("/inventory/serials", params=filters)
    second = await api_client.get("/inventory/serials", params={**filters, "page": 2})
    assert first.status_code == second.status_code == 200
    assert first.json()["pagination"]["total"] == 4
    assert {row["id"] for row in first.json()["data"] + second.json()["data"]} == {
        str(serial_id) for serial_id in k.ids
    }
    result = await _receive(k, principal, order.id, output_quantity=1, output_serial_ids=k.ids[:1])
    source_id, output_id = next(iter(result.receipt.lines[0].converted_serial_ids.items()))
    history = await api_client.get("/inventory/serials", params=filters)
    assert history.json()["pagination"]["total"] == 4
    outstanding = await api_client.get(
        "/inventory/serials",
        params={
            **filters,
            "job_work_outstanding_only": "true",
            "page_size": 10,
        },
    )
    assert outstanding.status_code == 200, outstanding.text
    assert outstanding.json()["pagination"]["total"] == 3
    assert source_id not in {row["id"] for row in outstanding.json()["data"]}
    context = await api_client.get(
        "/inventory/serials",
        params={
            "qc_inspection_id": str(result.receipt.qc_inspection_id),
        },
    )
    assert [row["id"] for row in context.json()["data"]] == [output_id]
    searched = await api_client.get(
        "/inventory/serials",
        params={
            "qc_inspection_id": str(result.receipt.qc_inspection_id),
            "search": "NOT-A-SERIAL",
        },
    )
    assert searched.json()["data"] == []
    missing_context = await api_client.get(
        "/inventory/serials",
        params={
            "job_work_outstanding_only": "true",
        },
    )
    assert missing_context.status_code == 422
    for key in ("qc_inspection_id", "job_work_order_line_id"):
        unknown = await api_client.get("/inventory/serials", params={key: str(uuid.uuid4())})
        assert unknown.status_code == 200 and unknown.json()["data"] == []
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, company_id=other_company.id
    )
    foreign = await api_client.get("/inventory/serials", params={"job_work_order_line_id": line_id})
    assert foreign.status_code == 200 and foreign.json()["data"] == []
