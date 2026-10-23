"""Demo document registers post real movements and remain stable on rerun."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.demo_data import DemoContext
from src.core.demo_data import inventory_ops as demo
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import (
    AdjustmentStatus,
    InventoryBalance,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
    InventoryTransaction,
    PhysicalCount,
    PhysicalCountStatus,
    StockAdjustment,
    StockTransfer,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.schemas import (
    PhysicalCountCreate,
    PhysicalCountEntry,
    PhysicalCountEntryRequest,
)
from src.modules.inventory.service import PhysicalCountService, post_movement
from src.modules.warehouses.models import StorageLocation, StorageLocationType, WarehouseType
from tests.seed_support import seed_item, seed_warehouse

pytestmark = pytest.mark.integration


@pytest.fixture
async def stocked_registers(session, tenant, principal, unique_code):
    tenant = replace(tenant, is_superuser=True)
    context = DemoContext(
        session=session,
        tenant=tenant,
        principal=replace(principal, is_superuser=True),
        company_id=tenant.company_id,
        user_id=tenant.user_id,
    )
    store = await seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=unique_code("CNS"),
        warehouse_type=WarehouseType.CONSUMABLES,
    )
    destination = await seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=unique_code("WIP"),
        warehouse_type=WarehouseType.WIP,
    )
    location = StorageLocation(
        company_id=tenant.company_id,
        warehouse_id=store.id,
        code=unique_code("BIN"),
        name="Consumables rack",
        location_type=StorageLocationType.BIN,
    )
    session.add(location)
    await session.flush()
    item = await seed_item(
        session,
        company_id=tenant.company_id,
        code=context.code("CN", "BUFF-COMPOUND"),
        standard_cost=Decimal("320"),
        can_purchase=True,
    )
    store_id, bin_id, item_id, destination_id = store.id, location.id, item.id, destination.id
    context.remember("item", context.code("CN", "BUFF-COMPOUND"), item_id)
    context.remember("warehouse", "WH-WIP", destination_id)
    await post_movement(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=store_id,
        location_id=bin_id,
        quantity=Decimal("120"),
        unit_cost=Decimal("320"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="demo_test",
        reference_id=None,
        reference_code="DEMO-REGISTER-OPENING",
        reason="Opening consumables for the demo register integration test",
    )
    return context, store_id, bin_id, item_id, destination_id


async def _row_counts(context: DemoContext) -> dict[str, int]:
    counts = {}
    for model in (
        StockTransfer,
        StockAdjustment,
        PhysicalCount,
        InventoryTransaction,
        InventoryBalance,
        InventoryCostLayer,
        InventoryCostLayerConsumption,
        AuditLog,
    ):
        counts[model.__tablename__] = (
            await context.session.execute(
                select(func.count())
                .select_from(model)
                .where(model.company_id == context.company_id)
            )
        ).scalar_one()
    return counts


async def test_registers_have_actionable_states_real_stock_and_idempotent_history(
    stocked_registers,
):
    context, store_id, bin_id, item_id, destination_id = stocked_registers
    await demo._movement_registers(context, store_id, bin_id)
    assert context.skipped == []

    transfers = (
        (
            await context.session.execute(
                select(StockTransfer).where(StockTransfer.company_id == context.company_id)
            )
        )
        .scalars()
        .all()
    )
    assert {transfer.status for transfer in transfers} == {
        TransferStatus.DRAFT,
        TransferStatus.IN_TRANSIT,
        TransferStatus.RECEIVED,
    }
    adjustments = (
        (
            await context.session.execute(
                select(StockAdjustment).where(StockAdjustment.company_id == context.company_id)
            )
        )
        .scalars()
        .all()
    )
    assert {adjustment.status for adjustment in adjustments} == {
        AdjustmentStatus.DRAFT,
        AdjustmentStatus.POSTED,
    }
    counts = (
        (
            await context.session.execute(
                select(PhysicalCount).where(PhysicalCount.company_id == context.company_id)
            )
        )
        .scalars()
        .all()
    )
    assert {count.status for count in counts} == {
        PhysicalCountStatus.DRAFT,
        PhysicalCountStatus.CONFIRMED,
    }

    balances = (
        (
            await context.session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.company_id == context.company_id,
                    InventoryBalance.item_id == item_id,
                )
            )
        )
        .scalars()
        .all()
    )
    positions = {balance.warehouse_id: balance for balance in balances}
    # Ten bars moved out, two found, one missing on the actual count. Drafts
    # move nothing. The four in transit are still company-owned at destination.
    assert positions[store_id].on_hand_quantity == Decimal("111")
    assert positions[destination_id].on_hand_quantity == Decimal("6")
    assert positions[destination_id].in_transit_quantity == Decimal("4")
    before = await _row_counts(context)
    assert before["stock_transfers"] == 3
    assert before["stock_adjustments"] == 2
    assert before["physical_counts"] == 2
    assert before["inventory_transactions"] == 6

    await demo._movement_registers(context, store_id, bin_id)
    assert await _row_counts(context) == before
    assert context.skipped == []
    await context.session.refresh(positions[store_id])
    assert positions[store_id].on_hand_quantity == Decimal("111")


async def test_missing_registered_item_is_reported_without_using_other_stock(stocked_registers):
    context, store_id, bin_id, _, _ = stocked_registers
    context.refs.pop("item")
    before = await _row_counts(context)
    await demo._movement_registers(context, store_id, bin_id)
    assert await _row_counts(context) == before
    assert context.skipped == [
        ("inventory_ops", "no buffing compound for the transfer, adjustment and count registers")
    ]


async def test_one_complete_count_entry_becomes_counted_and_can_post(stocked_registers):
    context, store_id, bin_id, item_id, _ = stocked_registers
    service = PhysicalCountService(context.session, context.tenant)
    draft = await service.create(
        PhysicalCountCreate(
            warehouse_id=store_id,
            location_id=bin_id,
            item_ids=[item_id],
            reason="One-line count completion regression",
        )
    )
    await service.start(draft.id)
    lines, _ = await service.list_lines(draft.id, PageParams(limit=1))
    counted = await service.enter_counts(
        draft.id,
        PhysicalCountEntryRequest(
            entries=[PhysicalCountEntry(line_id=lines[0].id, counted_quantity=Decimal("119"))]
        ),
    )
    assert counted.status is PhysicalCountStatus.COUNTED
    confirmed = await service.confirm(draft.id)
    assert confirmed.status is PhysicalCountStatus.CONFIRMED
    movement = (
        await context.session.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.company_id == context.company_id,
                InventoryTransaction.reference_id == draft.id,
            )
        )
    ).scalar_one()
    assert movement.transaction_type is TransactionType.ADJUSTMENT_DECREASE
    assert movement.quantity == Decimal("-1")


async def test_an_existing_operator_count_is_preserved_and_reported(stocked_registers):
    context, store_id, bin_id, item_id, _ = stocked_registers
    manual = await PhysicalCountService(context.session, context.tenant).create(
        PhysicalCountCreate(
            warehouse_id=store_id,
            location_id=bin_id,
            item_ids=[item_id],
            reason="Operator's count already in progress",
            notes="An operator's sheet must not be cancelled or replaced by the demo seed",
        )
    )
    await demo._physical_counts(context, item_id, store_id, bin_id)
    remaining = await PhysicalCountService(context.session, context.tenant).get_count(manual.id)
    assert remaining.status is PhysicalCountStatus.DRAFT
    assert (await _row_counts(context))["physical_counts"] == 1
    assert len(context.skipped) == 2
    assert all("already open" in reason for _, reason in context.skipped)
