"""Vendor isolation over HTTP (WP C1a, P0).

A vendor account reaches the ERP through exactly one door. The rules that keep
it there are settled before a handler runs, so they are tested where they live:

* **The route allow-list** (``core.deps.vendor_route_allowed``, applied in
  ``get_current_principal``): a vendor principal may call ``/vendor-portal/**``,
  its own ``/notifications/**`` and auth self-service - every other route is 403
  ``SCOPE_DENIED``, whatever permissions its roles carry. Tested through the
  real dependency chain with a real token, because ``api_client`` overrides
  ``get_current_principal`` and would skip the gate.
* **The portal refuses staff** (``require_vendor_user``): an administrator holds
  every permission string and must not act "as" a vendor.
* **Ownership is "not found"**: another vendor's order, file, receipt, job or
  bill answers 404 - confirming it exists would itself be the disclosure. A
  vendor's own unsent order is "not found" too (docs/assumptions.md §56).
* **The vendor sees allow-listed fields**: buyer notes, duplicate-override
  reasons, approval comments and internal cost centres stay internal.
* **Shared files only**: what a buyer filed against the order from the generic
  Documents screen is not the vendor's to list or download.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import ROLE_PRESETS
from src.modules.procurement import service as procurement
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.vendors.models import Vendor
from src.workers.tasks.reports import render_purchase_order_pdf
from tests import seed_support
from tests.api.vendor_portal_support import (
    approved_vendor,
    code,
    order_line,
    portal_user,
    raise_order,
    receive,
    send,
    sign_in,
    vendor_principal,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

PDF = render_purchase_order_pdf({"code": "TEST", "lines": []})


@pytest.fixture
async def world(session: AsyncSession, tenant: TenantContext, principal: Principal) -> Any:
    """Two approved vendors with one DRAFT order each, raised by the buyer."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=code("WH")
    )
    ours = await approved_vendor(session, tenant, "Our Fasteners")
    theirs = await approved_vendor(session, tenant, "Somebody Else's Fasteners")

    async def order_for(vendor: Vendor, **kwargs: Any) -> Any:
        return await raise_order(
            session,
            tenant,
            principal,
            vendor=vendor,
            warehouse_id=warehouse.id,
            uom_id=uom.id,
            **kwargs,
        )

    return {
        "ours": ours,
        "theirs": theirs,
        "our_order": await order_for(ours, notes="Internal: vendor is our second choice"),
        "their_order": await order_for(theirs),
        "order_for": order_for,
        "warehouse": warehouse,
        "uom": uom,
    }


# -----------------------------------------------------------------------------
# The route allow-list, through the real dependency chain
# -----------------------------------------------------------------------------


def _real_chain(app: Any) -> None:
    from src.core.deps import get_current_principal

    app.dependency_overrides.pop(get_current_principal, None)


@pytest.mark.parametrize(
    "path",
    [
        "/procurement/purchase-orders",
        "/procurement/inbound-tracking",
        "/dispatch",
        "/finance/vendor-invoices",
        "/finance/payments",
        "/documents",
        "/job-work",
        "/quality/inspections",
        "/reports/catalogue",
        "/users",
        "/vendors/options",
    ],
)
async def test_a_vendor_token_is_refused_on_every_staff_route(
    api_client, session, tenant, principal, world, path
):
    from src.core.security import create_access_token
    from src.main import app

    vendor_user = await portal_user(session, tenant, world["ours"])
    _real_chain(app)
    token, _ = create_access_token(vendor_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


async def test_a_vendor_token_is_refused_even_when_a_role_grants_staff_rights(
    api_client, session, tenant, principal, world
):
    """The gate is structural: a vendor role mistakenly granted po.view changes nothing."""
    from src.core.security import create_access_token
    from src.main import app
    from tests.api.vendor_portal_support import role

    leaky = await role(
        session,
        tenant,
        role_code=code("LEAKY"),
        permissions=["purchase_order.view", "dispatch.view", "vendor_portal.access"],
        vendor_role=True,
    )
    vendor_user = await portal_user(session, tenant, world["ours"], with_role=leaky)
    _real_chain(app)
    token, _ = create_access_token(vendor_user.id, principal.company_id)
    for path in ("/procurement/purchase-orders", "/dispatch"):
        result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 403, (path, result.text)
        assert result.json()["error"]["code"] == "SCOPE_DENIED"


@pytest.mark.parametrize(
    "path",
    [
        "/vendor-portal/me",
        "/vendor-portal/dashboard",
        "/vendor-portal/purchase-orders",
        "/vendor-portal/receipts",
        "/vendor-portal/invoices",
        "/vendor-portal/payments",
        "/vendor-portal/payments/summary",
        "/vendor-portal/rating",
        "/vendor-portal/transporters/options",
        "/notifications/unread-count",
        "/auth/me",
    ],
)
async def test_a_vendor_token_reaches_the_portal(
    api_client, session, tenant, principal, world, path
):
    from src.core.security import create_access_token
    from src.main import app

    vendor_user = await portal_user(session, tenant, world["ours"])
    _real_chain(app)
    token, _ = create_access_token(vendor_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize(
    ("preset", "path", "allowed"),
    [
        ("VENDOR_QC_CONTACT", "/vendor-portal/invoices", False),
        ("VENDOR_QC_CONTACT", "/vendor-portal/receipts", True),
        ("VENDOR_DISPATCH_MANAGER", "/vendor-portal/transporters/options", True),
        ("VENDOR_DISPATCH_MANAGER", "/vendor-portal/payments", False),
        ("VENDOR_ACCOUNT_MANAGER", "/vendor-portal/payments", True),
        ("VENDOR_ACCOUNT_MANAGER", "/vendor-portal/transporters/options", False),
    ],
)
async def test_each_vendor_role_holds_only_its_portal_rights(
    api_client, session, tenant, principal, world, preset, path, allowed
):
    from src.core.security import create_access_token
    from src.main import app

    vendor_user = await portal_user(session, tenant, world["ours"], preset=preset)
    _real_chain(app)
    token, _ = create_access_token(vendor_user.id, principal.company_id)
    result = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == (200 if allowed else 403), result.text


# -----------------------------------------------------------------------------
# The portal refuses staff
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/vendor-portal/dashboard"),
        ("GET", "/vendor-portal/purchase-orders"),
        ("GET", "/vendor-portal/invoices"),
        ("POST", "/vendor-portal/purchase-orders/{order}/accept"),
        ("POST", "/vendor-portal/purchase-orders/{order}/decline"),
    ],
)
async def test_staff_cannot_impersonate_a_vendor(api_client, world, method, path):
    url = path.format(order=world["our_order"].id)
    body = {"reason": "Attempt by an administrator"} if "decline" in url else {}
    result = await api_client.request(method, url, json=body if method == "POST" else None)
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "SCOPE_DENIED"


# -----------------------------------------------------------------------------
# Ownership is "not found"
# -----------------------------------------------------------------------------


async def test_the_portal_hides_foreign_and_unsent_orders(
    api_client, session, tenant, principal, world
):
    ours, our_order, their_order = world["ours"], world["our_order"], world["their_order"]
    await send(session, tenant, principal, their_order.id)
    sign_in(vendor_principal(principal, ours.id))

    for row in (our_order, their_order):  # ours is still DRAFT
        result = await api_client.get(f"/vendor-portal/purchase-orders/{row.id}")
        assert result.status_code == 404, result.text

    await send(session, tenant, principal, our_order.id)
    listed = await api_client.get("/vendor-portal/purchase-orders")
    assert listed.status_code == 200, listed.text
    assert {row["id"] for row in listed.json()["data"]} == {str(our_order.id)}

    for suffix in ("", "/shipments", "/documents", "/receipts"):
        result = await api_client.get(f"/vendor-portal/purchase-orders/{their_order.id}{suffix}")
        assert result.status_code == 404, (suffix, result.text)
    for action, body in (
        ("accept", {}),
        ("decline", {"reason": "Not ours to decline"}),
        ("ready-for-dispatch", {}),
        ("delivery-date", {"date": today().isoformat(), "reason": "Not ours"}),
    ):
        result = await api_client.post(
            f"/vendor-portal/purchase-orders/{their_order.id}/{action}", json=body
        )
        assert result.status_code == 404, (action, result.text)
    queued = await api_client.post(f"/vendor-portal/purchase-orders/{their_order.id}/pdf")
    assert queued.status_code == 404, queued.text


async def test_a_category_scoped_account_sees_its_categories_and_its_assignments(
    api_client, session, tenant, principal, world
):
    locks = await seed_support.seed_category(session, company_id=tenant.company_id, code=code("L"))
    handles = await seed_support.seed_category(
        session, company_id=tenant.company_id, code=code("H")
    )
    lever = await seed_support.seed_category(
        session, company_id=tenant.company_id, code=code("LV"), parent=handles
    )
    ours = world["ours"]
    lock_order = await world["order_for"](ours, category=locks)
    lever_order = await world["order_for"](ours, category=lever)
    for order in (lock_order, lever_order):
        await send(session, tenant, principal, order.id)

    sign_in(vendor_principal(principal, ours.id, allowed_category_ids=(handles.id,)))
    listed = await api_client.get("/vendor-portal/purchase-orders")
    # A scope on Handles covers Lever Handles (subtree); Lock Bodies is someone else's.
    assert {row["id"] for row in listed.json()["data"]} == {str(lever_order.id)}
    assert (
        await api_client.get(f"/vendor-portal/purchase-orders/{lock_order.id}")
    ).status_code == 404


async def test_another_vendors_files_bills_and_jobs_are_not_found(
    api_client, session, tenant, principal, world, monkeypatch, tmp_path
):
    from src.core.config import settings
    from src.workers import celery_app as celery_module

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    monkeypatch.setattr(
        celery_module.celery_app,
        "send_task",
        lambda *args, **kwargs: type("Task", (), {"id": uuid.uuid4().hex})(),
    )
    ours, our_order = world["ours"], world["our_order"]
    await send(session, tenant, principal, our_order.id)
    sign_in(vendor_principal(principal, ours.id))
    assert (
        await api_client.post(f"/vendor-portal/purchase-orders/{our_order.id}/accept", json={})
    ).status_code == 200
    uploaded = await api_client.post(
        f"/vendor-portal/purchase-orders/{our_order.id}/documents",
        data={"kind": "INVOICE"},
        files={"file": ("bill.pdf", PDF, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    line = await order_line(session, our_order.id)
    billed = await api_client.post(
        "/vendor-portal/invoices",
        json={
            "purchase_order_id": str(our_order.id),
            "vendor_invoice_number": code("BILL"),
            "invoice_document_id": uploaded.json()["data"]["id"],
            "invoice_date": today().isoformat(),
            "lines": [
                {
                    "purchase_order_line_id": str(line.id),
                    "item_id": str(line.item_id),
                    "quantity": "1",
                    "unit_price": "50",
                }
            ],
        },
    )
    assert billed.status_code == 201, billed.text
    job = await api_client.post(f"/vendor-portal/purchase-orders/{our_order.id}/pdf")
    assert job.status_code == 202, job.text
    job_id = job.json()["data"]["job_id"]

    # Another vendor, and another account of the same vendor.
    sign_in(vendor_principal(principal, world["theirs"].id))
    assert (
        await api_client.get(f"/vendor-portal/documents/{uploaded.json()['data']['id']}/download")
    ).status_code == 404
    assert (
        await api_client.get(f"/vendor-portal/invoices/{billed.json()['data']['id']}")
    ).status_code == 404
    assert (await api_client.get(f"/vendor-portal/jobs/{job_id}")).status_code == 404
    assert (await api_client.get("/vendor-portal/invoices")).json()["data"] == []

    sign_in(vendor_principal(principal, ours.id, user_id=uuid.uuid4()))
    assert (await api_client.get(f"/vendor-portal/jobs/{job_id}")).status_code == 404


# -----------------------------------------------------------------------------
# Allow-listed fields
# -----------------------------------------------------------------------------


async def test_the_vendor_reads_no_internal_fields(api_client, session, tenant, principal, world):
    ours, our_order = world["ours"], world["our_order"]
    await send(session, tenant, principal, our_order.id)
    sign_in(vendor_principal(principal, ours.id))

    detail = (await api_client.get(f"/vendor-portal/purchase-orders/{our_order.id}")).json()["data"]
    for internal in (
        "notes",
        "duplicate_acknowledged",
        "duplicate_acknowledgement_reason",
        "rejection_reason",
        "cancellation_reason",
        "approved_by_id",
        "source_type",
        "source_id",
        "branch_id",
    ):
        assert internal not in detail, internal
    assert "Internal" not in str(detail)
    line = detail["lines"][0]
    assert "requisition_line_id" not in line and "notes" not in line
    assert line["item_code"].startswith("RM-")
    # The timeline starts when the order was sent; drafting and approval are internal.
    assert [event["to_status"] for event in detail["timeline"]] == ["SENT"]

    row = (await api_client.get("/vendor-portal/purchase-orders")).json()["data"][0]
    assert "source_id" not in row and "notes" not in row


async def test_a_bill_names_no_cost_centre_and_no_foreign_reference(
    api_client, session, tenant, principal, world
):
    ours, our_order, their_order = world["ours"], world["our_order"], world["their_order"]
    for order in (our_order, their_order):
        await send(session, tenant, principal, order.id)
    await procurement.transition_purchase_order(
        session, tenant, principal, their_order.id, PurchaseOrderStatus.ACKNOWLEDGED, reason="ok"
    )
    foreign_receipt = await receive(
        session, tenant, principal, their_order.id, received=Decimal("2")
    )
    sign_in(vendor_principal(principal, ours.id))
    line = await order_line(session, our_order.id)
    body = {
        "purchase_order_id": str(our_order.id),
        "vendor_invoice_number": code("BILL"),
        "invoice_date": today().isoformat(),
        "lines": [{"description": "Bolts", "quantity": "1", "unit_price": "10"}],
    }

    for extra in ({"cost_centre_id": str(uuid.uuid4())}, {"vendor_id": str(uuid.uuid4())}):
        assert (
            await api_client.post("/vendor-portal/invoices", json={**body, **extra})
        ).status_code == 422
    smuggled_line = {**body["lines"][0], "cost_centre_id": str(uuid.uuid4())}
    assert (
        await api_client.post("/vendor-portal/invoices", json={**body, "lines": [smuggled_line]})
    ).status_code == 422

    # Another vendor's receipt is "not found" - never "belongs to another vendor <code>".
    refused = await api_client.post(
        "/vendor-portal/invoices", json={**body, "goods_receipt_id": str(foreign_receipt.id)}
    )
    assert refused.status_code == 404, refused.text
    assert foreign_receipt.code not in refused.text
    foreign_line = await order_line(session, their_order.id)
    for line_ref in (
        {"purchase_order_line_id": str(foreign_line.id)},
        {"item_id": str(foreign_line.item_id)},
    ):
        result = await api_client.post(
            "/vendor-portal/invoices",
            json={**body, "lines": [{**body["lines"][0], **line_ref}]},
        )
        assert result.status_code == 404, (line_ref, result.text)
    assert (
        await api_client.post(
            "/vendor-portal/invoices", json={**body, "purchase_order_id": str(their_order.id)}
        )
    ).status_code == 404

    billed = await api_client.post(
        "/vendor-portal/invoices",
        json={
            **body,
            "lines": [
                {
                    "purchase_order_line_id": str(line.id),
                    "item_id": str(line.item_id),
                    "quantity": "1",
                    "unit_price": "50",
                }
            ],
        },
    )
    assert billed.status_code == 201, billed.text
    bill = billed.json()["data"]
    assert bill["status"] == "PENDING_APPROVAL"
    for internal in ("vendor_id", "cost_centre_id", "variance_override_reason", "approved_by_id"):
        assert internal not in bill, internal
    assert "cost_centre_id" not in bill["lines"][0]


@pytest.mark.parametrize("category", ["DOCUMENT", "REPORT"])
async def test_files_filed_internally_stay_internal(
    api_client, session, tenant, principal, world, monkeypatch, tmp_path, category
):
    import io

    from starlette.datastructures import Headers, UploadFile

    from src.core.config import settings
    from src.modules.documents.models import DocumentCategory
    from src.modules.documents.schemas import DocumentUploadRequest
    from src.modules.documents.service import DocumentService

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    ours, our_order = world["ours"], world["our_order"]
    await send(session, tenant, principal, our_order.id)
    memo = await DocumentService(session, tenant).upload(
        UploadFile(
            io.BytesIO(PDF),
            filename="quote-comparison.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        ),
        DocumentUploadRequest(
            category=DocumentCategory(category),
            entity_type="purchase_order",
            entity_id=our_order.id,
        ),
    )
    staff_view = await api_client.get(f"/procurement/purchase-orders/{our_order.id}/documents")
    assert [row["id"] for row in staff_view.json()["data"]] == [str(memo.id)]

    sign_in(vendor_principal(principal, ours.id))
    listed = await api_client.get(f"/vendor-portal/purchase-orders/{our_order.id}/documents")
    assert listed.json()["data"] == []
    assert (await api_client.get(f"/vendor-portal/documents/{memo.id}/download")).status_code == 404
    detail = await api_client.get(f"/vendor-portal/purchase-orders/{our_order.id}")
    assert detail.json()["data"]["documents_count"] == 0

    # A file the vendor shares through the order's documents route is listed.
    shared = await api_client.post(
        f"/vendor-portal/purchase-orders/{our_order.id}/documents",
        data={"kind": "DELIVERY_CHALLAN"},
        files={"file": ("challan.pdf", PDF, "application/pdf")},
    )
    assert shared.status_code == 201, shared.text
    listed = await api_client.get(f"/vendor-portal/purchase-orders/{our_order.id}/documents")
    assert [(row["id"], row["kind"]) for row in listed.json()["data"]] == [
        (shared.json()["data"]["id"], "DELIVERY_CHALLAN")
    ]


async def test_generated_order_pdf_is_shared_without_an_upload_link(
    api_client, session, tenant, principal, world, monkeypatch, tmp_path
):
    import io

    from starlette.datastructures import Headers, UploadFile

    from src.core.config import settings
    from src.modules.documents.models import DocumentCategory
    from src.modules.documents.schemas import DocumentUploadRequest
    from src.modules.documents.service import DocumentService
    from src.modules.reports import service as reports
    from src.modules.reports.models import ReportJobStatus, ReportKey

    monkeypatch.setattr(settings, "storage_path", tmp_path)
    ours, order = world["ours"], world["our_order"]
    await send(session, tenant, principal, order.id)
    pdf = await DocumentService(session, tenant).upload(
        UploadFile(
            io.BytesIO(PDF),
            filename="purchase-order.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        ),
        DocumentUploadRequest(
            category=DocumentCategory.REPORT, entity_type="purchase_order", entity_id=order.id
        ),
    )
    snapshot = reports.new_job_snapshot(
        ReportKey.PURCHASE_ORDER, {"purchase_order_id": str(order.id)}, now=utcnow()
    )
    snapshot.company_id = tenant.company_id
    snapshot.document_id = pdf.id
    snapshot.status = ReportJobStatus.COMPLETED
    snapshot.generated_at = utcnow()
    session.add(snapshot)
    await session.flush()

    sign_in(vendor_principal(principal, ours.id))
    listed = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}/documents")
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["data"]] == [str(pdf.id)]
    download = await api_client.get(f"/vendor-portal/documents/{pdf.id}/download")
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    detail = await api_client.get(f"/vendor-portal/purchase-orders/{order.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["documents_count"] == 1


def test_every_vendor_preset_is_exercised() -> None:
    """The role matrix above names every vendor preset there is."""
    assert {code for code, spec in ROLE_PRESETS.items() if spec.get("is_vendor_role")} == {
        "VENDOR_OWNER",
        "VENDOR_MANAGER",
        "VENDOR_DISPATCH_MANAGER",
        "VENDOR_QC_CONTACT",
        "VENDOR_ACCOUNT_MANAGER",
    }


async def test_a_vendor_does_not_see_its_order_until_it_is_sent_again(
    api_client, session, tenant, principal, world
):
    """A cancelled order stays visible only when it had been sent first."""
    ours, our_order = world["ours"], world["our_order"]
    draft = await world["order_for"](ours)
    await procurement.transition_purchase_order(
        session, tenant, principal, draft.id, PurchaseOrderStatus.CANCELLED, reason="Not needed"
    )
    await send(session, tenant, principal, our_order.id)
    await procurement.transition_purchase_order(
        session,
        tenant,
        principal,
        our_order.id,
        PurchaseOrderStatus.CANCELLED,
        reason="Bought elsewhere",
    )
    sign_in(vendor_principal(principal, ours.id))
    listed = await api_client.get("/vendor-portal/purchase-orders")
    assert {row["id"] for row in listed.json()["data"]} == {str(our_order.id)}
    assert listed.json()["data"][0]["status"] == "CANCELLED"
    assert (await api_client.get(f"/vendor-portal/purchase-orders/{draft.id}")).status_code == 404
