"""Warehouse scope applies to new destinations and documents raised before restriction."""

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.core.deps import get_current_principal
from src.core.errors import AuthorizationError
from src.main import app
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import InventoryBalance, InventoryTransaction, TransactionType
from src.modules.inventory.repository import BalanceGrain
from src.modules.inventory.service import MovementEngine, MovementRequest
from src.modules.procurement.models import GoodsReceipt, PurchaseOrder
from src.modules.production.models import ProductionOrder
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
async def setting(session, tenant):
    allowed = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    outside = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    other = await seed_support.seed_company(session)
    foreign = await seed_support.seed_warehouse(
        session, company_id=other.id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"ITEM-{uuid.uuid4().hex[:8]}",
        can_purchase=True,
        can_manufacture=True,
    )
    return allowed.id, outside.id, foreign.id, item.id


def production_payload(warehouse_id, item_id):
    return {
        "item_id": str(item_id),
        "warehouse_id": str(warehouse_id),
        "planned_quantity": "5",
        "planned_start_date": today().isoformat(),
        "planned_end_date": today().isoformat(),
    }


@pytest.mark.parametrize("module", ["procurement", "production"])
async def test_create_destination_must_be_assigned_and_in_tenant(
    api_client, principal, setting, module
):
    allowed, outside, foreign, item = setting
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=(allowed,)
    )

    async def create(warehouse_id):
        if module == "production":
            return await api_client.post("/production", json=production_payload(warehouse_id, item))
        return await api_client.post(
            "/procurement/purchase-requisitions",
            json={
                "warehouse_id": str(warehouse_id),
                "required_date": today().isoformat(),
                "lines": [{"line_number": 1, "item_id": str(item), "quantity": "5"}],
            },
        )

    assert (await create(outside)).status_code == 404
    assert (await create(foreign)).status_code == 404
    assert (await create(allowed)).status_code == 201


async def test_legacy_production_completion_is_refused_before_status_or_stock_changes(
    api_client, session, principal, setting
):
    allowed, outside, _, item = setting
    created = await api_client.post("/production", json=production_payload(outside, item))
    assert created.status_code == 201
    order_id = created.json()["data"]["id"]
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=(allowed,)
    )
    before = {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (InventoryBalance, InventoryTransaction, AuditLog)
    }
    status_before = await session.scalar(
        select(ProductionOrder.status).where(ProductionOrder.id == uuid.UUID(order_id))
    )
    response = await api_client.post(f"/production/{order_id}/complete", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SCOPE_DENIED"
    assert (
        await session.scalar(
            select(ProductionOrder.status).where(ProductionOrder.id == uuid.UUID(order_id))
        )
        == status_before
    )
    for model, count in before.items():
        assert await session.scalar(select(func.count()).select_from(model)) == count


async def test_legacy_receipt_post_is_refused_before_any_write(
    api_client, session, tenant, principal, setting
):
    allowed, outside, _, _ = setting
    vendor = await seed_support.seed_vendor(session, company_id=tenant.company_id)
    order = PurchaseOrder(
        company_id=tenant.company_id,
        code=f"PO-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor.id,
        warehouse_id=outside,
        po_date=today(),
    )
    session.add(order)
    await session.flush()
    receipt = GoodsReceipt(
        company_id=tenant.company_id,
        code=f"GR-{uuid.uuid4().hex[:8]}",
        purchase_order_id=order.id,
        vendor_id=vendor.id,
        warehouse_id=outside,
        receipt_date=today(),
    )
    session.add(receipt)
    await session.flush()
    receipt_id = receipt.id
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=(allowed,)
    )
    before = await session.scalar(select(func.count()).select_from(AuditLog))
    result = await api_client.post(f"/procurement/goods-receipts/{receipt_id}/post")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "SCOPE_DENIED"
    await session.refresh(receipt)
    assert receipt.status == "DRAFT"
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == before


@pytest.mark.parametrize("outside_position", ["movement", "extra", "prelocked"])
async def test_movement_engine_refuses_every_unassigned_position_before_writing(
    session, tenant, setting, outside_position
):
    allowed, outside, _, item = setting
    scoped = replace(tenant, allowed_warehouse_ids=(allowed,))
    movement = MovementRequest(
        item_id=item,
        warehouse_id=outside if outside_position == "movement" else allowed,
        quantity=Decimal("1"),
        unit_cost=Decimal("2"),
        transaction_type=TransactionType.PURCHASE_RECEIPT,
        reference_type="scope_test",
        reference_id=uuid.uuid4(),
        reference_code="SCOPE-PROBE",
        reason="Warehouse scope regression",
    )
    grain = BalanceGrain(item_id=item, warehouse_id=outside)
    before = {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (InventoryBalance, InventoryTransaction, AuditLog)
    }
    with pytest.raises(AuthorizationError):
        await MovementEngine(session, scoped).post_with_balances(
            [movement],
            extra_grains=[grain] if outside_position == "extra" else None,
            prelocked={grain: InventoryBalance()} if outside_position == "prelocked" else None,
        )
    for model, count in before.items():
        assert await session.scalar(select(func.count()).select_from(model)) == count


@pytest.mark.parametrize("superuser,restricted", [(False, True), (False, False), (True, True)])
async def test_authorized_and_trusted_movement_contexts_still_post(
    session, tenant, setting, superuser, restricted
):
    allowed, outside, _, item = setting
    scoped = replace(
        tenant,
        allowed_warehouse_ids=(allowed,) if restricted else (),
        is_superuser=superuser,
    )
    warehouse = allowed if restricted and not superuser else outside
    movement = MovementRequest(
        item_id=item,
        warehouse_id=warehouse,
        quantity=Decimal("1"),
        unit_cost=Decimal("2"),
        transaction_type=TransactionType.PURCHASE_RECEIPT,
        reference_type="scope_test",
        reference_id=uuid.uuid4(),
        reference_code="SCOPE-ALLOWED",
        reason="Authorized movement regression",
    )
    posted = await MovementEngine(session, scoped).post([movement])
    assert len(posted) == 1
    assert posted[0].warehouse_id == warehouse
    assert posted[0].quantity == Decimal("1")
