"""The seed's stock reports are populated, reusable and isolated by company."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.demo_data import summaries
from src.core.deps import system_tenant
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.inventory.service import post_movement
from src.modules.reports import service as reports_service
from src.modules.reports.models import InventorySummary
from tests.seed_support import seed_item, seed_warehouse

pytestmark = pytest.mark.integration


async def test_seed_summary_refresh_uses_real_stock_and_preserves_other_company_and_reruns(
    session, tenant, other_company, unique_code, monkeypatch
):
    item = await seed_item(session, company_id=tenant.company_id, code=unique_code("STK"))
    warehouse = await seed_warehouse(session, company_id=tenant.company_id, code=unique_code("WH"))
    other_item = await seed_item(session, company_id=other_company.id, code=unique_code("OTHER"))
    other_warehouse = await seed_warehouse(
        session, company_id=other_company.id, code=unique_code("OTHERWH")
    )
    item_id, warehouse_id = item.id, warehouse.id
    other_tenant = system_tenant(other_company.id)

    for scope, item_key, warehouse_key, quantity, unit_cost in (
        (tenant, item_id, warehouse_id, Decimal("10"), Decimal("2")),
        (other_tenant, other_item.id, other_warehouse.id, Decimal("5"), Decimal("3")),
    ):
        await post_movement(
            session,
            scope,
            item_id=item_key,
            warehouse_id=warehouse_key,
            quantity=quantity,
            unit_cost=unit_cost,
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="demo_summary_test",
            reference_id=None,
            reference_code=unique_code("OPEN"),
            reason="Opening stock for local seed summary verification",
        )
    await reports_service.refresh_inventory_summary(session, other_tenant)
    other_summary = (
        await session.scalars(
            select(InventorySummary).where(InventorySummary.company_id == other_company.id)
        )
    ).one()
    other_before = (
        other_summary.id,
        other_summary.on_hand,
        other_summary.valuation_amount,
        other_summary.refreshed_at,
    )

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(summaries, "session_scope", scope)

    result = await summaries.refresh_if_stale(tenant)
    assert result is not None and result.position_count == 1
    summary = (
        await session.scalars(
            select(InventorySummary).where(InventorySummary.company_id == tenant.company_id)
        )
    ).one()
    assert summary.on_hand == Decimal("10")
    assert summary.available == Decimal("10")
    assert summary.valuation_amount == Decimal("20")
    audit_count = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.company_id == tenant.company_id)
    )
    first_refresh = summary.refreshed_at
    assert await summaries.refresh_if_stale(tenant) is None
    assert summary.refreshed_at == first_refresh
    assert (
        await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.company_id == tenant.company_id)
        )
        == audit_count
    )

    await post_movement(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal("3"),
        unit_cost=Decimal("2"),
        transaction_type=TransactionType.ADJUSTMENT_INCREASE,
        reference_type="demo_summary_test",
        reference_id=None,
        reference_code=unique_code("ADJ"),
        reason="Later stock movement must be visible in the seeded reports",
    )
    latest = await session.scalar(
        select(func.max(InventoryTransaction.created_at)).where(
            InventoryTransaction.company_id == tenant.company_id
        )
    )
    # The integration fixture keeps all service commits under one outer database
    # transaction, whose server now() is fixed. Age only this derived summary to
    # reproduce the earlier-refresh condition without modifying immutable stock.
    summary.refreshed_at = latest - timedelta(seconds=1)
    await session.flush()
    session.expunge(summary)
    assert await summaries.refresh_if_stale(tenant) is not None
    summary = (
        await session.scalars(
            select(InventorySummary).where(InventorySummary.company_id == tenant.company_id)
        )
    ).one()
    assert summary.on_hand == Decimal("13")
    assert summary.valuation_amount == Decimal("26")
    await session.refresh(other_summary)
    assert (
        other_summary.id,
        other_summary.on_hand,
        other_summary.valuation_amount,
        other_summary.refreshed_at,
    ) == other_before

    # An unrelated tenant with a stale summary must not trigger work or acquire
    # another company's data when the requested tenant is already current.
    other_summary.refreshed_at = latest - timedelta(days=1)
    await session.flush()
    assert await summaries.refresh_if_stale(tenant) is None
    await session.refresh(other_summary)
    assert other_summary.refreshed_at == latest - timedelta(days=1)
