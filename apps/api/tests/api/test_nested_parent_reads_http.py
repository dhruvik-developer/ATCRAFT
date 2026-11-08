"""A nested read addresses its parent, so the parent must be the caller's.

``GET /warehouses/{id}/locations``, ``/mrp/runs/{id}/suggestions`` and twelve
more read one record's children or figures. Until November 2026 each answered
200 for a parent that did not exist - an empty page, a zeroed quality report, a
0-day lead time - so a deleted, mistyped or other-company id read as "nothing
here". Each now resolves its parent first, through the tenant-scoped lookup its
detail route uses, while its flat sibling still reads the same id as a filter.

``test_route_contract_http.py`` pins the 404 for an unknown id on every one of
them. This file pins the other two sides: a real parent still answers, with its
rows where they are cheap to build, and another company's parent is the same
404 as one that never existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import TenantContext
from src.modules.mrp.models import MrpRun, MrpRunMode, MrpRunStatus
from src.modules.production.models import (
    ProductionOrder,
    ProductionOrderStatus,
    ProductionSourceType,
)
from src.modules.sales.models import SalesOrder, SalesOrderStatus
from src.modules.vendors.models import VendorContactRole
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's rows, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


def ok(response: Response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def not_found(response: Response) -> None:
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["success"] is False and body["error"]["code"] == "NOT_FOUND", body


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# -----------------------------------------------------------------------------
# Parents with children cheap enough to build: the rows still list
# -----------------------------------------------------------------------------


async def test_a_warehouse_lists_its_locations_and_not_anothers(
    api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, foreign: TenantContext
) -> None:
    ours = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    theirs = await seed_support.seed_warehouse(
        session, company_id=foreign.company_id, code=_code("WH")
    )
    location = StorageLocation(
        company_id=tenant.company_id,
        warehouse_id=ours.id,
        code=_code("BIN"),
        name="Bin A",
        location_type=StorageLocationType.BIN,
    )
    session.add(location)
    await session.flush()
    ours_id, theirs_id, location_id = ours.id, theirs.id, location.id

    rows = ok(await api_client.get(f"/warehouses/{ours_id}/locations"))

    assert [row["id"] for row in rows] == [str(location_id)]
    not_found(await api_client.get(f"/warehouses/{theirs_id}/locations"))


async def test_a_vendor_lists_its_contacts_and_categories(
    api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
) -> None:
    vendor = await seed_support.seed_vendor(session, company_id=tenant.company_id)
    category = await seed_support.seed_category(session, company_id=tenant.company_id)
    vendor_id, category_id = vendor.id, category.id
    contact = await api_client.post(
        f"/vendors/{vendor_id}/contacts",
        json={
            "name": "Rajesh Kumar",
            "contact_role": VendorContactRole.ACCOUNTS,
            "email": "accounts@vendor-qa.com",
        },
    )
    link = await api_client.post(
        f"/vendors/{vendor_id}/categories", json={"item_category_id": str(category_id)}
    )
    assert contact.status_code == 201 and link.status_code == 201, (contact.text, link.text)

    contacts = ok(await api_client.get(f"/vendors/{vendor_id}/contacts"))
    categories = ok(await api_client.get(f"/vendors/{vendor_id}/categories"))

    assert [row["id"] for row in contacts] == [contact.json()["data"]["id"]]
    assert [row["id"] for row in categories] == [link.json()["data"]["id"]]


# -----------------------------------------------------------------------------
# Item- and vendor-addressed reads: a real one answers, another company's is 404
# -----------------------------------------------------------------------------

ITEM_READS = [
    pytest.param("/bom/where-used/{id}", lambda data: data == [], id="bom where-used"),
    pytest.param("/bom/items/{id}/versions", lambda data: data == [], id="bom versions"),
    pytest.param("/routing/items/{id}/versions", lambda data: data == [], id="routing versions"),
    # The documented answer for a real item with no effective routing.
    pytest.param(
        "/routing/items/{id}/lead-time",
        lambda data: data["routing_id"] is None and data["lead_time_days"] == 0,
        id="routing lead time",
    ),
    pytest.param(
        "/quality/reports/item/{id}",
        lambda data: data["inspection_count"] == 0,
        id="item quality history",
    ),
]


@pytest.mark.parametrize(("path", "answers"), ITEM_READS)
async def test_an_item_read_answers_for_a_real_item_only(
    path: str,
    answers: Callable[[Any], bool],
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    foreign: TenantContext,
) -> None:
    ours = await seed_support.seed_item(session, company_id=tenant.company_id, code=_code("IT"))
    theirs = await seed_support.seed_item(session, company_id=foreign.company_id, code=_code("IT"))
    ours_id, theirs_id = ours.id, theirs.id

    assert answers(ok(await api_client.get(path.format(id=ours_id))))
    not_found(await api_client.get(path.format(id=theirs_id)))


VENDOR_READS = [
    pytest.param("/vendors/{id}/contacts", lambda data: data == [], id="vendor contacts"),
    pytest.param("/vendors/{id}/categories", lambda data: data == [], id="vendor categories"),
    # Never delivered anything inspectable: the rating engine's "no evidence".
    pytest.param(
        "/quality/reports/vendor/{id}",
        lambda data: data["has_evidence"] is False,
        id="vendor quality summary",
    ),
]


@pytest.mark.parametrize(("path", "answers"), VENDOR_READS)
async def test_a_vendor_read_answers_for_a_real_vendor_only(
    path: str,
    answers: Callable[[Any], bool],
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    foreign: TenantContext,
) -> None:
    ours = await seed_support.seed_vendor(session, company_id=tenant.company_id)
    theirs = await seed_support.seed_vendor(session, company_id=foreign.company_id)
    ours_id, theirs_id = ours.id, theirs.id

    assert answers(ok(await api_client.get(path.format(id=ours_id))))
    not_found(await api_client.get(path.format(id=theirs_id)))


async def test_a_serial_in_no_carton_traces_to_none_and_anothers_is_not_found(
    api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, foreign: TenantContext
) -> None:
    item = await seed_support.seed_item(session, company_id=tenant.company_id, code=_code("IT"))
    other = await seed_support.seed_item(session, company_id=foreign.company_id, code=_code("IT"))
    ours = await seed_support.seed_serial(session, company_id=tenant.company_id, item_id=item.id)
    theirs = await seed_support.seed_serial(
        session, company_id=foreign.company_id, item_id=other.id
    )
    ours_id, theirs_id = ours.id, theirs.id

    assert ok(await api_client.get(f"/packing/trace/serial/{ours_id}")) == []
    not_found(await api_client.get(f"/packing/trace/serial/{theirs_id}"))


# -----------------------------------------------------------------------------
# Planning and production parents
# -----------------------------------------------------------------------------


async def _completed_run(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    finished = utcnow()
    run = MrpRun(
        company_id=company_id,
        code=_code("MRP"),
        status=MrpRunStatus.COMPLETED,
        mode=MrpRunMode.SYNC,
        started_at=finished,
        completed_at=finished,
    )
    session.add(run)
    await session.flush()
    return run.id


async def test_a_run_with_nothing_to_propose_lists_no_suggestions(
    api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, foreign: TenantContext
) -> None:
    ours = await _completed_run(session, tenant.company_id)
    theirs = await _completed_run(session, foreign.company_id)

    assert ok(await api_client.get(f"/mrp/runs/{ours}/suggestions")) == []
    not_found(await api_client.get(f"/mrp/runs/{theirs}/suggestions"))


async def test_an_order_no_plan_has_seen_has_no_expected_availability(
    api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
) -> None:
    """No completed run having seen the order is a normal state, not a 404."""
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    order = SalesOrder(
        company_id=tenant.company_id,
        code=_code("SO"),
        customer_id=customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=30),
        warehouse_id=warehouse.id,
        status=SalesOrderStatus.CONFIRMED,
        priority=5,
        currency="INR",
    )
    session.add(order)
    await session.flush()
    order_id = order.id

    data = ok(await api_client.get(f"/mrp/sales-orders/{order_id}/expected-availability"))

    assert data["run_id"] is None and data["lines"] == []


@pytest.mark.parametrize("child", ["entries", "work-orders"])
async def test_a_draft_job_lists_no_bookings_or_operations_yet(
    child: str, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
) -> None:
    item = await seed_support.seed_item(session, company_id=tenant.company_id, code=_code("FG"))
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    order = ProductionOrder(
        company_id=tenant.company_id,
        code=_code("PO"),
        item_id=item.id,
        planned_quantity=Decimal("10"),
        warehouse_id=warehouse.id,
        planned_start_date=today(),
        planned_end_date=today() + timedelta(days=5),
        priority=5,
        status=ProductionOrderStatus.DRAFT,
        source_type=ProductionSourceType.MANUAL,
    )
    session.add(order)
    await session.flush()
    order_id = order.id

    assert ok(await api_client.get(f"/production/{order_id}/{child}")) == []
