"""Assigned warehouses constrain whole documents, pagination and direct actions."""

import uuid
from dataclasses import replace

import pytest

from src.core.clock import utcnow
from src.core.deps import get_current_principal
from src.main import app
from src.modules.inventory.models import (
    AdjustmentReason,
    PhysicalCount,
    StockAdjustment,
    StockTransfer,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
async def documents(session, tenant):
    allowed = [
        await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=f"S-{uuid.uuid4().hex[:8]}"
        )
        for _ in range(2)
    ]
    outside = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    other = await seed_support.seed_company(session)
    foreign = await seed_support.seed_warehouse(
        session, company_id=other.id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    foreign_destination = await seed_support.seed_warehouse(
        session, company_id=other.id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    groups = {"transfers": [], "adjustments": [], "physical-counts": []}
    for company_id, source, destination in [
        (tenant.company_id, allowed[0].id, allowed[1].id),
        (tenant.company_id, allowed[0].id, outside.id),
        (tenant.company_id, outside.id, allowed[0].id),
        (other.id, foreign.id, foreign_destination.id),
    ]:
        common = {
            "company_id": company_id,
            "code": f"DOC-{uuid.uuid4().hex[:8]}",
            "reason": "Warehouse scope regression",
        }
        rows = [
            StockTransfer(
                **common,
                from_warehouse_id=source,
                to_warehouse_id=destination,
                transfer_date=utcnow().date(),
            ),
            StockAdjustment(
                **common,
                warehouse_id=destination,
                adjustment_date=utcnow().date(),
                reason_code=AdjustmentReason.CORRECTION,
            ),
            PhysicalCount(**common, warehouse_id=destination, count_date=utcnow().date()),
        ]
        session.add_all(rows)
        await session.flush()
        for key, row in zip(groups, rows, strict=True):
            groups[key].append(row.id)
    return tuple(row.id for row in allowed), groups


@pytest.mark.parametrize("kind", ["transfers", "adjustments", "physical-counts"])
async def test_list_pagination_and_direct_documents_obey_scope(
    api_client, principal, documents, kind
):
    allowed, groups = documents
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=allowed
    )
    response = await api_client.get(f"/inventory/{kind}", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    # Single-store documents can see the return-to-allowed row; transfers need both endpoints.
    visible = [groups[kind][0]] if kind == "transfers" else [groups[kind][0], groups[kind][2]]
    assert body["pagination"]["total"] == len(visible)
    pages = []
    for page in range(1, len(visible) + 1):
        result = await api_client.get(f"/inventory/{kind}", params={"limit": 1, "page": page})
        pages.extend(row["id"] for row in result.json()["data"])
    assert set(pages) == {str(value) for value in visible}
    assert (await api_client.get(f"/inventory/{kind}/{visible[0]}")).status_code == 200
    hidden = [groups[kind][1], groups[kind][3]]
    if kind == "transfers":
        hidden.append(groups[kind][2])
    for document_id in hidden:
        assert (await api_client.get(f"/inventory/{kind}/{document_id}")).status_code == 404
        assert (
            await api_client.post(
                f"/inventory/{kind}/{document_id}/cancel", json={"reason": "Scope probe"}
            )
        ).status_code == 404
    if kind == "physical-counts":
        assert (await api_client.get(f"/inventory/{kind}/{hidden[0]}/lines")).status_code == 404


@pytest.mark.parametrize("superuser", [False, True])
async def test_unrestricted_and_superuser_see_own_company_only(
    api_client, principal, documents, superuser
):
    allowed, groups = documents
    scope = allowed if superuser else ()
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=scope, is_superuser=superuser
    )
    for kind, ids in groups.items():
        result = await api_client.get(f"/inventory/{kind}")
        assert result.status_code == 200
        assert {row["id"] for row in result.json()["data"]} == {str(value) for value in ids[:3]}
        assert (await api_client.get(f"/inventory/{kind}/{ids[3]}")).status_code == 404
