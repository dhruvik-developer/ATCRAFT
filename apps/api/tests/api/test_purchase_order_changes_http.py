"""D1 boundary checks: permissions, typed revisions, conflicts and safe validation."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.core.deps import get_current_principal
from src.core.permissions import Permission
from src.main import app
from src.modules.procurement import purchase_order_changes, service
from src.modules.procurement.models import PurchaseOrderStatus, PurchaseRequisition
from src.modules.procurement.schemas import PurchaseOrderAmendLineIn, PurchaseOrderAmendRequest
from src.modules.sales.models import SalesOrder, SalesOrderLine
from tests import seed_support
from tests.api.test_procurement_conversion_http import setting as setting
from tests.api.test_vendor_portal_http import as_owner, sent_order
from tests.api.test_vendor_portal_http import world as world
from tests.api.vendor_portal_support import order_line, portal_user, vendor_principal
from tests.integration.test_procurement_service import make_order, send_order
from tests.integration.test_purchase_order_changes import manual_payload, manual_resolution

pytestmark = [pytest.mark.api, pytest.mark.integration]


async def test_derived_amount_overflow_returns_422_without_creating_a_document(
    api_client, session, setting
):
    from src.modules.procurement.models import PurchaseOrder

    before = await session.scalar(select(func.count()).select_from(PurchaseOrder))
    response = await api_client.post(
        "/procurement/purchase-orders",
        json={
            "vendor_id": str(setting["vendor"].id),
            "warehouse_id": str(setting["warehouse"].id),
            "lines": [
                {
                    "line_number": 1,
                    "item_id": str(setting["lock_body"].id),
                    "quantity": "1000000000000",
                    "unit_price": "100000000000",
                }
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "amount is too large" in response.json()["error"]["message"]
    assert await session.scalar(select(func.count()).select_from(PurchaseOrder)) == before


@pytest.fixture
async def draft(session, tenant, principal, setting):
    masters = {
        "vendor": setting["vendor"],
        "warehouse": setting["warehouse"],
        "item": setting["lock_body"],
    }
    order, lines, _ = await make_order(session, tenant, principal, masters)
    return order, lines[0], masters


def sign_in(principal):
    app.dependency_overrides[get_current_principal] = lambda: principal


def body(order, masters, **overrides):
    return manual_resolution(order, manual_payload(masters), **overrides).model_dump(mode="json")


async def test_duplicate_resolution_returns_specific_success_envelope(api_client, draft):
    order, line, masters = draft
    response = await api_client.post(
        "/procurement/purchase-orders/duplicate-resolution", json=body(order, masters)
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["action"] == "MERGE"
    assert order.code in payload["message"]
    detail = await api_client.get(f"/procurement/purchase-orders/{order.id}")
    updated = detail.json()["data"]
    assert updated["lines"][0]["id"] == str(line.id)
    assert Decimal(updated["lines"][0]["quantity"]) == Decimal("125")


async def test_resolution_checks_its_action_permission(api_client, principal, draft):
    order, _, masters = draft
    restricted = dataclasses.replace(
        principal, permissions=frozenset({str(Permission.PURCHASE_ORDER_VIEW)})
    )
    sign_in(restricted)
    response = await api_client.post(
        "/procurement/purchase-orders/duplicate-resolution", json=body(order, masters)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_vendor_principal_cannot_invoke_staff_resolution(api_client, principal, draft):
    order, _, masters = draft
    vendor = dataclasses.replace(principal, vendor_id=masters["vendor"].id)
    sign_in(vendor)
    response = await api_client.post(
        "/procurement/purchase-orders/duplicate-resolution", json=body(order, masters)
    )
    assert response.status_code == 403


async def test_stale_resolution_has_conflict_envelope(api_client, draft):
    order, _, masters = draft
    response = await api_client.post(
        "/procurement/purchase-orders/duplicate-resolution",
        json=body(order, masters, version=order.version + 1),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "Traceback" not in response.text
    assert "sqlalchemy" not in response.text


async def test_amendment_body_requires_version_and_non_null_existing_line_terms(api_client, draft):
    order, line, _ = draft
    path = f"/procurement/purchase-orders/{order.id}/amend"
    missing_version = await api_client.post(
        path,
        json={
            "reason": "Need additional units",
            "lines": [{"line_id": str(line.id), "quantity": "110"}],
        },
    )
    assert missing_version.status_code == 422
    null_price = await api_client.post(
        path,
        json={
            "expected_version": order.version,
            "reason": "Need additional units",
            "lines": [{"line_id": str(line.id), "quantity": "110", "unit_price": None}],
        },
    )
    assert null_price.status_code == 422
    assert null_price.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "pydantic_core" not in null_price.text


async def test_amendment_and_history_need_only_po_permissions(
    api_client, session, tenant, principal, draft
):
    order, line, _ = draft
    await send_order(session, tenant, principal, order)
    scoped = dataclasses.replace(
        principal,
        permissions=frozenset(
            {str(Permission.PURCHASE_ORDER_VIEW), str(Permission.PURCHASE_ORDER_UPDATE)}
        ),
    )
    sign_in(scoped)
    response = await api_client.post(
        f"/procurement/purchase-orders/{order.id}/amend",
        json={
            "expected_version": order.version,
            "reason": "Customer changed the quantity",
            "lines": [{"line_id": str(line.id), "quantity": "110"}],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "PENDING_APPROVAL"
    assert data["amendment_number"] == 1
    assert data["lines"][0]["id"] == str(line.id)
    history = await api_client.get(f"/procurement/purchase-orders/{order.id}/amendments?limit=1")
    assert history.status_code == 200, history.text
    assert len(history.json()["data"]) == 1
    assert history.json()["pagination"]["has_next"] is False
    assert Decimal(history.json()["data"][0]["old_values"]["lines"][0]["quantity"]) == Decimal(
        "100"
    )


async def test_duplicate_approval_requires_explicit_confirmation_over_http(
    api_client, session, tenant, principal, draft
):
    order, _, masters = draft
    request = body(order, masters)
    request["action"] = "CREATE_WITH_APPROVAL"
    request.pop("candidate_purchase_order_id")
    request.pop("expected_candidate_version")
    response = await api_client.post(
        "/procurement/purchase-orders/duplicate-resolution", json=request
    )
    assert response.status_code == 200, response.text
    identifier = uuid.UUID(response.json()["data"]["purchase_orders"][0]["id"])
    await service.transition_purchase_order(
        session,
        tenant,
        principal,
        identifier,
        PurchaseOrderStatus.PENDING_APPROVAL,
        reason="Review the separate demand",
    )
    sign_in(dataclasses.replace(principal, is_superuser=True))
    refused = await api_client.post(
        f"/procurement/purchase-orders/{identifier}/approve", json={"reason": "Approve"}
    )
    assert refused.status_code == 422, refused.text
    approved = await api_client.post(
        f"/procurement/purchase-orders/{identifier}/approve",
        json={"reason": "Reviewed the separate requirement", "confirm_duplicate": True},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "APPROVED"


async def test_history_permission_is_not_broadened_to_all_authenticated_users(
    api_client, principal, draft
):
    order, _, _ = draft
    sign_in(
        dataclasses.replace(
            principal, permissions=frozenset({str(Permission.PURCHASE_ORDER_CREATE)})
        )
    )
    response = await api_client.get(f"/procurement/purchase-orders/{order.id}/amendments")
    assert response.status_code == 403


@pytest.mark.parametrize("reapproved", [False, True])
async def test_vendor_pending_revision_is_status_only_until_resent(
    api_client, session, tenant, principal, world, reapproved
):
    order = await sent_order(session, tenant, principal, world)
    order_id = order.id
    line = await order_line(session, order_id)
    revised_price = line.unit_price + Decimal("11")
    await purchase_order_changes.amend_purchase_order(
        session,
        tenant,
        order_id,
        PurchaseOrderAmendRequest(
            expected_version=order.version,
            reason="Private buyer negotiation reason",
            terms="Private unapproved revised terms",
            lines=[
                PurchaseOrderAmendLineIn(
                    line_id=line.id, quantity=Decimal("110"), unit_price=revised_price
                )
            ],
        ),
    )
    if reapproved:
        await service.transition_purchase_order(
            session,
            tenant,
            dataclasses.replace(principal, is_superuser=True),
            order_id,
            PurchaseOrderStatus.APPROVED,
            reason="Approved revised commercial terms",
        )
    as_owner(principal, world)
    response = await api_client.get(f"/vendor-portal/purchase-orders/{order_id}")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {
        "id",
        "code",
        "status",
        "amendment_number",
        "last_amended_at",
        "pending_amendment",
        "message",
    }
    assert data["pending_amendment"] is True
    assert data["amendment_number"] == 1
    assert data["status"] == ("APPROVED" if reapproved else "PENDING_APPROVAL")
    assert "Private buyer negotiation" not in response.text
    assert "Private unapproved" not in response.text
    pending = await api_client.get("/vendor-portal/purchase-orders/pending-amendments?limit=1")
    assert pending.status_code == 200, pending.text
    assert [row["id"] for row in pending.json()["data"]] == [str(order_id)]
    assert pending.json()["pagination"]["limit"] == 1
    normal_list = await api_client.get("/vendor-portal/purchase-orders")
    assert normal_list.status_code == 200
    assert str(order_id) not in {row["id"] for row in normal_list.json()["data"]}
    cannot_accept = await api_client.post(
        f"/vendor-portal/purchase-orders/{order_id}/accept", json={}
    )
    assert cannot_accept.status_code == 404, cannot_accept.text
    if reapproved:
        await service.transition_purchase_order(
            session,
            tenant,
            principal,
            order_id,
            PurchaseOrderStatus.SENT,
            reason="Send approved revised document",
        )
        resent = await api_client.get(f"/vendor-portal/purchase-orders/{order_id}")
        assert resent.status_code == 200, resent.text
        assert resent.json()["data"]["pending_amendment"] is False
        assert resent.json()["data"]["amendment_number"] == 1
        assert Decimal(resent.json()["data"]["lines"][0]["unit_price"]) == revised_price


async def test_pending_vendor_feed_refuses_another_vendor_and_category_scope(
    api_client, session, tenant, principal, world
):
    order = await sent_order(session, tenant, principal, world)
    order_id = order.id
    line = await order_line(session, order_id)
    await purchase_order_changes.amend_purchase_order(
        session,
        tenant,
        order_id,
        PurchaseOrderAmendRequest(
            expected_version=order.version,
            reason="Review outstanding demand",
            lines=[PurchaseOrderAmendLineIn(line_id=line.id, quantity=Decimal("110"))],
        ),
    )
    foreign = vendor_principal(principal, world["theirs"].id, user_id=world["owner"].id)
    sign_in(foreign)
    response = await api_client.get(f"/vendor-portal/purchase-orders/{order_id}")
    assert response.status_code == 404
    feed = await api_client.get("/vendor-portal/purchase-orders/pending-amendments")
    assert feed.json()["data"] == []
    category = await seed_support.seed_category(session, company_id=tenant.company_id)
    second_user = await portal_user(
        session, tenant, world["ours"], allowed_category_ids=[category.id]
    )
    limited = vendor_principal(
        principal,
        world["ours"].id,
        user_id=second_user.id,
        allowed_category_ids=(category.id,),
    )
    sign_in(limited)
    denied = await api_client.get(f"/vendor-portal/purchase-orders/{order_id}")
    assert denied.status_code == 404


@pytest.mark.parametrize("invalid_reference", ["foreign_order", "wrong_line", "missing_manual"])
async def test_demand_ownership_failures_return_controlled_errors_without_writes(
    api_client, session, tenant, setting, other_company, invalid_reference
):
    if invalid_reference == "missing_manual":
        reference = {"demand_type": "MRP_DEMAND", "demand_id": str(uuid.uuid4())}
        expected_status = 404
    else:
        source_company = (
            other_company.id if invalid_reference == "foreign_order" else tenant.company_id
        )
        customer = await seed_support.seed_customer(session, company_id=source_company)
        warehouse = await seed_support.seed_warehouse(
            session, company_id=source_company, code=f"DEMAND-{uuid.uuid4().hex[:8]}"
        )
        first = SalesOrder(
            company_id=source_company,
            code=f"DEMAND-{uuid.uuid4().hex[:8]}",
            customer_id=customer.id,
            warehouse_id=warehouse.id,
            order_date=today(),
            required_date=today(),
        )
        session.add(first)
        await session.flush()
        reference = {"demand_type": "SALES_ORDER", "demand_id": str(first.id)}
        expected_status = 404
        if invalid_reference == "wrong_line":
            second = SalesOrder(
                company_id=tenant.company_id,
                code=f"DEMAND-{uuid.uuid4().hex[:8]}",
                customer_id=customer.id,
                warehouse_id=warehouse.id,
                order_date=today(),
                required_date=today(),
            )
            session.add(second)
            await session.flush()
            line = SalesOrderLine(
                company_id=tenant.company_id,
                sales_order_id=second.id,
                line_number=1,
                item_id=setting["lock_body"].id,
                quantity=Decimal("1"),
                uom_id=setting["lock_body"].base_uom_id,
                unit_price=Decimal("50"),
            )
            session.add(line)
            await session.flush()
            reference["demand_line_id"] = str(line.id)
            expected_status = 422
    before = await session.scalar(select(func.count()).select_from(PurchaseRequisition))
    response = await api_client.post(
        "/procurement/purchase-requisitions",
        json={
            "required_date": today().isoformat(),
            "warehouse_id": str(setting["warehouse"].id),
            "lines": [
                {
                    "line_number": 1,
                    "item_id": str(setting["lock_body"].id),
                    "quantity": "5",
                    "source_demand_refs": [reference],
                }
            ],
        },
    )
    assert response.status_code == expected_status, response.text
    assert response.json()["success"] is False
    assert "Traceback" not in response.text
    assert "sqlalchemy" not in response.text
    assert await session.scalar(select(func.count()).select_from(PurchaseRequisition)) == before
