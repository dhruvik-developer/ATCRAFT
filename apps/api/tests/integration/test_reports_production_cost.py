"""Tenant-scoped production cost report query."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.organization.models import CostCentre, CostCentreType
from src.modules.production.models import ProductionOrder, ProductionOrderStatus, WorkOrder
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import ProductionCostRow, ReportFilters, ReportPeriod
from tests import seed_support

pytestmark = pytest.mark.integration


async def test_production_cost_period_and_work_centre_cost_centre_filters(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    company_id = company.id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    item = await seed_support.seed_item(
        session, company_id=company_id, code=f"ITEM-{uuid.uuid4().hex[:8]}"
    )
    cost_centres = [
        CostCentre(
            company_id=company_id,
            code=f"CC-{uuid.uuid4().hex[:8]}",
            name=f"Production {index}",
            cost_centre_type=CostCentreType.PRODUCTION,
        )
        for index in range(2)
    ]
    session.add_all(cost_centres)
    await session.flush()
    work_centres = [
        await seed_support.seed_work_center(
            session, company_id=company_id, code=f"WC-{uuid.uuid4().hex[:8]}"
        )
        for _ in range(2)
    ]
    closed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    orders = []
    for index in range(2):
        order = ProductionOrder(
            company_id=company_id,
            code=f"PROD-{uuid.uuid4().hex[:8]}",
            item_id=item.id,
            planned_quantity=Decimal("10"),
            produced_quantity=Decimal("10"),
            warehouse_id=warehouse.id,
            cost_centre_id=cost_centres[index].id,
            planned_start_date=date(2026, 9, 1),
            planned_end_date=date(2026, 9, 10),
            actual_end_date=date(2026, 9, 10),
            status=ProductionOrderStatus.CLOSED,
            approved_by_id=tenant.user_id,
            approved_at=closed_at,
            total_material_cost=Decimal("70"),
            total_operation_cost=Decimal("20"),
            actual_subcontract_cost=Decimal("10"),
            total_cost=Decimal("100"),
            standard_unit_cost=Decimal("9"),
            actual_unit_cost=Decimal("10"),
            material_variance=Decimal("0"),
            operation_variance=Decimal("0"),
            subcontract_variance=Decimal("10"),
            total_variance=Decimal("10"),
            updated_at=closed_at,
        )
        session.add(order)
        await session.flush()
        session.add(
            WorkOrder(
                company_id=company_id,
                code=f"WO-{uuid.uuid4().hex[:8]}",
                production_order_id=order.id,
                sequence=1,
                operation_code="CUT",
                operation_name="Cutting",
                work_center_id=work_centres[index].id,
                planned_quantity=Decimal("10"),
            )
        )
        orders.append(order)
    await session.flush()

    repository = ReportQueryRepository(session, tenant)
    period = ReportPeriod(date_from=date(2026, 9, 10), date_to=date(2026, 9, 10))
    all_rows = (
        await session.execute(repository.production_cost(period, ReportFilters()))
    ).mappings()
    assert len(all_rows.all()) == 2

    selected = (
        (
            await session.execute(
                repository.production_cost(
                    period,
                    ReportFilters(
                        item_id=item.id,
                        work_center_id=work_centres[0].id,
                        cost_centre_id=cost_centres[0].id,
                    ),
                )
            )
        )
        .mappings()
        .one()
    )
    row = ProductionCostRow.model_validate(selected)

    assert row.production_order_id == orders[0].id
    assert row.cost_centre_code == cost_centres[0].code
    assert row.produced_quantity == Decimal("10")
    assert row.actual_unit_cost == Decimal("10")
    assert row.standard_unit_cost == Decimal("9")
    assert row.total_variance == Decimal("10")
