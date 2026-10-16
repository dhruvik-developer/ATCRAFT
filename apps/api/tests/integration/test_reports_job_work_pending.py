"""Tenant-scoped job-work pending report: rejected and scrap columns (E7b/K07)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkProcess,
)
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import JobWorkPendingRow, ReportFilters, ReportPeriod
from src.modules.reports.service import columns_for
from tests import seed_support

pytestmark = pytest.mark.integration


async def test_job_work_pending_row_carries_rejected_and_scrap(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    company_id = company.id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    vendor = await seed_support.seed_vendor(
        session, company_id=company_id, code=f"VEN-{uuid.uuid4().hex[:8]}"
    )
    item = await seed_support.seed_item(
        session, company_id=company_id, code=f"ITEM-{uuid.uuid4().hex[:8]}"
    )
    order = JobWorkOrder(
        company_id=company_id,
        code=f"JW-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor.id,
        process_type=JobWorkProcess.POLISHING,
        expected_return_date=date(2026, 9, 10),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        status=JobWorkOrderStatus.ISSUED,
    )
    session.add(order)
    await session.flush()
    session.add(
        JobWorkOrderLine(
            company_id=company_id,
            job_work_order_id=order.id,
            line_number=1,
            item_id=item.id,
            uom_id=item.base_uom_id,
            quantity_to_issue=Decimal("100"),
            issued_quantity=Decimal("100"),
            received_quantity=Decimal("90"),
            expected_output_item_id=item.id,
            expected_output_quantity=Decimal("100"),
            received_output_quantity=Decimal("80"),
            rejected_quantity=Decimal("5"),
            scrap_quantity=Decimal("5"),
        )
    )
    await session.flush()

    repository = ReportQueryRepository(session, tenant)
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    selected = (
        (await session.execute(repository.job_work_pending(period, ReportFilters())))
        .mappings()
        .one()
    )
    row = JobWorkPendingRow.model_validate(selected)

    assert row.code == order.code
    assert row.issued_quantity == Decimal("100")
    assert row.received_quantity == Decimal("80")
    assert row.rejected_quantity == Decimal("5")
    assert row.scrap_quantity == Decimal("5")


def test_job_work_pending_descriptor_exposes_rejected_and_scrap_columns() -> None:
    names = {column.name for column in columns_for(JobWorkPendingRow)}
    assert {"rejected_quantity", "scrap_quantity"} <= names
