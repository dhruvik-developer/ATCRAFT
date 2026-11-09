"""Vendor-portal job work and consignment routes over HTTP.

``tests/api/test_vendor_portal_scope_http.py`` proves the portal admits only
vendor accounts and that each vendor role holds only its own rights;
``test_vendor_portal_http.py`` walks the purchase-order actions. The three
routes here had never had their bodies executed:

* ``GET /vendor-portal/job-work/{id}`` - one of the vendor's job-work orders
  with its lines, challans, receipts and the progress it has reported;
* ``POST /vendor-portal/job-work/{id}/status`` - a progress report that is
  recorded and audited but moves no stock and no status;
* ``GET /vendor-portal/shipments`` - consignments across every order the vendor
  can see, narrowed by delivery state.

Another vendor's order, the vendor's own draft and another company's order are
all "not found" - confirming that one exists would itself be the disclosure.

Job-work orders are raised, approved and issued through the job-work service
with the worked example of ``tests/integration/test_job_work_service.py`` (raw
handles out for PVD). Purchase orders are raised and sent through procurement
as the buyer; consignments are recorded through the inbound service.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inventory
from src.modules.job_work import service as job_work
from src.modules.job_work.models import JobWorkOrderStatus, JobWorkVendorUpdate
from src.modules.procurement import inbound_service
from src.modules.procurement.inbound_models import PurchaseOrderShipment
from src.modules.procurement.inbound_schemas import InboundShipmentCreate
from src.modules.products.models import Item
from src.modules.vendors.models import Vendor
from tests import seed_support
from tests.api.vendor_portal_support import (
    approved_vendor,
    code,
    portal_user,
    raise_order,
    send,
    sign_in,
    vendor_principal,
)
from tests.integration.test_job_work_service import (
    PvdJob,
    issued_order,
    seed_item,
    seed_uom,
    seed_vendor,
    seed_warehouse,
    stock_in,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code_: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code_, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def vendor_account(
    session: AsyncSession, tenant: TenantContext, staff: Principal, vendor_id: uuid.UUID
) -> Principal:
    """A VENDOR_OWNER account of this vendor, as its portal token resolves."""
    vendor = await session.get(Vendor, vendor_id)
    assert vendor is not None
    user = await portal_user(session, tenant, vendor)
    return vendor_principal(staff, vendor_id, user_id=user.id)


def progress(**overrides: Any) -> dict[str, Any]:
    return {
        "update_type": "IN_PROCESS",
        "event_time": (utcnow() - timedelta(hours=1)).isoformat(),
        "expected_date": (today() + timedelta(days=4)).isoformat(),
        "note": "Surface preparation is underway",
        **overrides,
    }


async def plating_job(
    session: AsyncSession, company_id: uuid.UUID, vendor_id: uuid.UUID | None = None
) -> PvdJob:
    uom_id = await seed_uom(session, company_id)
    return PvdJob(
        uom_id=uom_id,
        raw_item_id=await seed_item(session, company_id, uom_id, code=code("RAW")),
        coated_item_id=await seed_item(
            session, company_id, uom_id, code=code("PVD"), standard_cost="130"
        ),
        warehouse_id=await seed_warehouse(session, company_id),
        vendor_id=vendor_id or await seed_vendor(session, company_id),
    )


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
async def works(session, tenant, principal) -> SimpleNamespace:
    """Our plater with an issued order and an approved one; a rival with an issued one."""
    ours = await plating_job(session, tenant.company_id)
    rival = await plating_job(session, tenant.company_id)
    issued, _ = await issued_order((session, tenant), ours)
    rivals, _ = await issued_order((session, tenant), rival)
    # Approving reserves what the order will send, so it needs stock of its own.
    await stock_in(session, tenant, ours.raw_item_id, ours.warehouse_id, "20")
    approved, _ = await job_work.create_order(session, tenant, ours.payload(quantity="20"))
    approved_id = approved.id
    await job_work.approve_order(session, tenant, approved_id, reason="Rate agreed with vendor")
    draft, _ = await job_work.create_order(session, tenant, ours.payload(quantity="10"))
    output_code = await session.scalar(select(Item.code).where(Item.id == ours.coated_item_id))
    return SimpleNamespace(
        job=ours,
        output_code=output_code,
        issued_id=issued.id,
        approved_id=approved_id,
        draft_id=draft.id,
        rival_issued_id=rivals.id,
        account=await vendor_account(session, tenant, principal, ours.vendor_id),
    )


@pytest.fixture
async def foreign_order(session, other_company) -> uuid.UUID:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    tenant = TenantContext(company_id=other_company.id, user_id=owner.id)
    order, _ = await issued_order((session, tenant), await plating_job(session, other_company.id))
    return order.id


# -----------------------------------------------------------------------------
# GET /vendor-portal/job-work/{order_id}
# -----------------------------------------------------------------------------


async def test_the_vendor_reads_its_issued_order_with_lines_and_challan(
    api_client, works, decimal_equal
):
    sign_in(works.account)

    data = ok(await api_client.get(f"/vendor-portal/job-work/{works.issued_id}"))

    assert (data["id"], data["status"], data["process_type"]) == (
        str(works.issued_id),
        "ISSUED",
        "PVD",
    )
    decimal_equal(data["issued_quantity"], "100")
    decimal_equal(data["pending_quantity"], "100")
    [line] = data["lines"]
    # The line names what the vendor is to send back: the coated handle.
    assert line["item_code"] == works.output_code
    decimal_equal(line["issued_quantity"], "100")
    decimal_equal(line["received_quantity"], "0")
    [challan] = data["challans"]
    decimal_equal(challan["quantity"], "100")
    assert challan["vehicle_number"] == "GJ01AB1234"
    assert (data["receipts"], data["updates"]) == ([], [])


async def test_orders_the_vendor_may_not_see_are_not_found(api_client, works, foreign_order):
    sign_in(works.account)

    for order_id in (works.rival_issued_id, works.draft_id, foreign_order):
        refused(await api_client.get(f"/vendor-portal/job-work/{order_id}"), 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /vendor-portal/job-work/{order_id}/status
# -----------------------------------------------------------------------------


async def test_progress_is_recorded_and_audited_but_moves_no_stock_or_status(
    api_client, session, tenant, works, decimal_equal
):
    before = await inventory.get_balance(
        session, tenant, works.job.raw_item_id, works.job.warehouse_id
    )
    assert before is not None
    held_at_vendor = Decimal(before.job_work_quantity)
    sign_in(works.account)

    reported = await api_client.post(
        f"/vendor-portal/job-work/{works.issued_id}/status",
        json=progress(note="  Surface preparation is underway  "),
    )
    data = ok(reported, 201)

    assert reported.json()["message"] == "Job work progress recorded"

    assert (data["update_type"], data["note"]) == ("IN_PROCESS", "Surface preparation is underway")
    update_id = uuid.UUID(data["id"])
    stored = await session.get(JobWorkVendorUpdate, update_id)
    assert stored is not None
    assert (stored.job_work_order_id, stored.vendor_id, stored.created_by_id) == (
        works.issued_id,
        works.job.vendor_id,
        works.account.user_id,
    )
    [entry] = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "job_work_vendor_update", AuditLog.entity_id == update_id
            )
        )
    ).all()
    assert (entry.action, entry.created_by_id) == (AuditAction.CREATE, works.account.user_id)
    assert entry.new_values["job_work_order_id"] == str(works.issued_id)

    detail = ok(await api_client.get(f"/vendor-portal/job-work/{works.issued_id}"))
    assert [row["id"] for row in detail["updates"]] == [str(update_id)]
    assert detail["status"] == JobWorkOrderStatus.ISSUED
    after = await inventory.get_balance(
        session, tenant, works.job.raw_item_id, works.job.warehouse_id
    )
    assert after is not None
    decimal_equal(after.job_work_quantity, str(held_at_vendor))


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"event_time": (utcnow() + timedelta(days=1)).isoformat()}, None),
        ({"expected_date": (today() - timedelta(days=1)).isoformat()}, None),
        ({"event_time": "2026-11-01T10:00:00"}, "event_time"),
        ({"note": "   "}, "note"),
        ({"update_type": "LOST_IN_TRANSIT"}, "update_type"),
        ({"invoice_number": "INV-9"}, "invoice_number"),
    ],
    ids=["future-event", "past-expected-date", "naive-time", "blank-note", "unknown-kind", "extra"],
)
async def test_a_progress_report_outside_the_rules_is_refused_and_records_nothing(
    api_client, session, works, overrides, field
):
    sign_in(works.account)

    error = refused(
        await api_client.post(
            f"/vendor-portal/job-work/{works.issued_id}/status", json=progress(**overrides)
        ),
        422,
        "VALIDATION_ERROR",
    )

    if field is not None:
        assert field in error["details"]["fields"]
    count = await session.scalar(
        select(func.count())
        .select_from(JobWorkVendorUpdate)
        .where(JobWorkVendorUpdate.job_work_order_id == works.issued_id)
    )
    assert count == 0


async def test_progress_is_refused_on_an_order_whose_material_has_not_left(
    api_client, session, works
):
    sign_in(works.account)

    error = refused(
        await api_client.post(
            f"/vendor-portal/job-work/{works.approved_id}/status", json=progress()
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )

    assert error["details"]["status"] == JobWorkOrderStatus.APPROVED
    count = await session.scalar(select(func.count()).select_from(JobWorkVendorUpdate))
    assert count == 0


async def test_progress_on_an_order_the_vendor_may_not_see_is_not_found(
    api_client, session, works, foreign_order
):
    sign_in(works.account)

    for order_id in (works.rival_issued_id, works.draft_id, foreign_order):
        refused(
            await api_client.post(f"/vendor-portal/job-work/{order_id}/status", json=progress()),
            404,
            "NOT_FOUND",
        )
    count = await session.scalar(select(func.count()).select_from(JobWorkVendorUpdate))
    assert count == 0


# -----------------------------------------------------------------------------
# GET /vendor-portal/shipments
# -----------------------------------------------------------------------------


@pytest.fixture
async def freight(api_client, session, tenant, principal) -> SimpleNamespace:
    """Two accepted orders of ours (one delivered, one moving) and one of a rival's."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=code("WH")
    )
    ours = await approved_vendor(session, tenant, "Our Fasteners")
    rival = await approved_vendor(session, tenant, "Somebody Else's Fasteners")
    accounts = {
        ours.id: await vendor_account(session, tenant, principal, ours.id),
        rival.id: await vendor_account(session, tenant, principal, rival.id),
    }

    async def consignment(vendor: Vendor, *, shipped_hours_ago: int) -> uuid.UUID:
        order = await raise_order(
            session, tenant, principal, vendor=vendor, warehouse_id=warehouse.id, uom_id=uom.id
        )
        order_id = order.id
        await send(session, tenant, principal, order_id)
        sign_in(accounts[vendor.id])
        ok(await api_client.post(f"/vendor-portal/purchase-orders/{order_id}/accept", json={}))
        shipment = await inbound_service.record_inbound_shipment(
            session,
            tenant,
            order_id,
            InboundShipmentCreate(
                courier_name="Blue Dart",
                tracking_reference=code("BD"),
                shipped_at=utcnow() - timedelta(hours=shipped_hours_ago),
            ),
        )
        return shipment.id

    delivered = await consignment(ours, shipped_hours_ago=48)
    moving = await consignment(ours, shipped_hours_ago=1)
    rivals = await consignment(rival, shipped_hours_ago=2)
    delivered_order = await session.scalar(
        select(PurchaseOrderShipment.purchase_order_id).where(PurchaseOrderShipment.id == delivered)
    )
    await inbound_service.mark_delivered(session, tenant, delivered_order, delivered)
    sign_in(accounts[ours.id])
    return SimpleNamespace(delivered=str(delivered), moving=str(moving), rivals=str(rivals))


@pytest.mark.parametrize(
    ("status", "expected"),
    [("all", ["moving", "delivered"]), ("in_transit", ["moving"]), ("delivered", ["delivered"])],
)
async def test_the_vendor_lists_only_its_own_consignments_by_delivery_state(
    api_client, freight, status, expected
):
    response = await api_client.get("/vendor-portal/shipments", params={"status": status})

    rows = ok(response)
    assert [row["id"] for row in rows] == [getattr(freight, name) for name in expected]
    assert freight.rivals not in {row["id"] for row in rows}
    assert response.json()["pagination"]["total"] == len(expected)


async def test_the_consignment_list_is_paginated_newest_first(api_client, freight):
    response = await api_client.get("/vendor-portal/shipments", params={"limit": 1})

    assert [row["id"] for row in ok(response)] == [freight.moving]
    assert response.json()["pagination"]["has_next"] is True


async def test_an_unknown_delivery_state_is_a_field_error(api_client, freight):
    assert "query.status" in field_errors(
        await api_client.get("/vendor-portal/shipments", params={"status": "lost"})
    )
