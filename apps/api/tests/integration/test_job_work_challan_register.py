"""Company-wide challan register: GET /job-work/challans (E7b/P10.M3)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.pagination import PageParams, SortParams
from src.modules.job_work.models import (
    JobWorkChallan,
    JobWorkOrder,
    JobWorkOrderStatus,
    JobWorkProcess,
)
from src.modules.job_work.schemas import ChallanFilters
from src.modules.job_work.service import list_challan_register
from tests import seed_support

pytestmark = pytest.mark.integration


async def _order(
    session: AsyncSession,
    company_id,
    vendor,
    warehouse,
    item,
    *,
    code: str,
    status: JobWorkOrderStatus = JobWorkOrderStatus.ISSUED,
) -> JobWorkOrder:
    order = JobWorkOrder(
        company_id=company_id,
        code=code,
        vendor_id=vendor.id,
        process_type=JobWorkProcess.POLISHING,
        expected_return_date=date(2026, 9, 10),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        status=status,
    )
    session.add(order)
    await session.flush()
    return order


async def _challan(
    session: AsyncSession, company_id, order, vendor, *, code: str, on: date
) -> JobWorkChallan:
    challan = JobWorkChallan(
        company_id=company_id,
        code=code,
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        challan_date=on,
        expected_return_date=date(2026, 9, 20),
        total_quantity=Decimal("10"),
        total_value=Decimal("100"),
    )
    session.add(challan)
    await session.flush()
    return challan


async def test_challan_register_lists_across_orders_with_filters(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    company_id = company.id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    vendor_a = await seed_support.seed_vendor(
        session, company_id=company_id, code=f"VA-{uuid.uuid4().hex[:8]}"
    )
    vendor_b = await seed_support.seed_vendor(
        session, company_id=company_id, code=f"VB-{uuid.uuid4().hex[:8]}"
    )
    item = await seed_support.seed_item(
        session, company_id=company_id, code=f"ITEM-{uuid.uuid4().hex[:8]}"
    )
    order_a = await _order(
        session, company_id, vendor_a, warehouse, item, code=f"JW-{uuid.uuid4().hex[:8]}"
    )
    order_b = await _order(
        session, company_id, vendor_b, warehouse, item, code=f"JW-{uuid.uuid4().hex[:8]}"
    )
    await _challan(session, company_id, order_a, vendor_a, code="JWC-1", on=date(2026, 9, 1))
    await _challan(session, company_id, order_b, vendor_b, code="JWC-2", on=date(2026, 9, 5))

    params = PageParams(page=1, limit=20)
    sort = SortParams()

    all_rows, _ = await list_challan_register(session, tenant, ChallanFilters(), params, sort)
    assert {row.code for row in all_rows} == {"JWC-1", "JWC-2"}
    # Newest despatch first.
    assert [row.code for row in all_rows] == ["JWC-2", "JWC-1"]

    vendor_rows, _ = await list_challan_register(
        session, tenant, ChallanFilters(vendor_id=vendor_a.id), params, sort
    )
    assert [row.code for row in vendor_rows] == ["JWC-1"]

    order_rows, _ = await list_challan_register(
        session, tenant, ChallanFilters(job_work_order_id=order_b.id), params, sort
    )
    assert [row.code for row in order_rows] == ["JWC-2"]

    date_rows, _ = await list_challan_register(
        session,
        tenant,
        ChallanFilters(date_from=date(2026, 9, 3), date_to=date(2026, 9, 6)),
        params,
        sort,
    )
    assert [row.code for row in date_rows] == ["JWC-2"]
