"""Delivery-challan PDF data assembly (E7b/P10.M3)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.job_work.models import (
    JobWorkChallan,
    JobWorkChallanLine,
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkProcess,
)
from src.modules.job_work.service import challan_pdf_data
from tests import seed_support

pytestmark = pytest.mark.integration


async def test_challan_pdf_data_names_parties_goods_and_vehicle(
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
        session, company_id=company_id, code=f"RAW-{uuid.uuid4().hex[:8]}"
    )
    order = JobWorkOrder(
        company_id=company_id,
        code=f"JW-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor.id,
        process_type=JobWorkProcess.PVD,
        process_description="PVD, rose gold, 3 micron",
        expected_return_date=date(2026, 9, 20),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        status=JobWorkOrderStatus.ISSUED,
    )
    session.add(order)
    await session.flush()
    order_line = JobWorkOrderLine(
        company_id=company_id,
        job_work_order_id=order.id,
        line_number=1,
        item_id=item.id,
        uom_id=item.base_uom_id,
        quantity_to_issue=Decimal("100"),
        issued_quantity=Decimal("100"),
        expected_output_item_id=item.id,
        expected_output_quantity=Decimal("100"),
    )
    session.add(order_line)
    await session.flush()
    challan = JobWorkChallan(
        company_id=company_id,
        code="JWC/2026-27/00007",
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        challan_date=date(2026, 9, 5),
        expected_return_date=date(2026, 9, 20),
        vehicle_number="GJ01AB1234",
        total_quantity=Decimal("100"),
        total_value=Decimal("10000"),
    )
    session.add(challan)
    await session.flush()
    session.add(
        JobWorkChallanLine(
            company_id=company_id,
            challan_id=challan.id,
            job_work_order_line_id=order_line.id,
            line_number=1,
            item_id=item.id,
            uom_id=item.base_uom_id,
            quantity=Decimal("100"),
            unit_value=Decimal("100"),
            total_value=Decimal("10000"),
        )
    )
    await session.flush()

    data = await challan_pdf_data(session, tenant, challan.id)

    assert data["code"] == "JWC/2026-27/00007"
    assert data["order_code"] == order.code
    assert data["purpose"] == "Sent for job work \u2014 not for sale"
    assert data["consignee"]["name"] == vendor.name
    assert data["vehicle_number"] == "GJ01AB1234"
    assert len(data["lines"]) == 1
    assert data["lines"][0]["item_code"] == item.code
    assert Decimal(data["total_value"]) == Decimal("10000")
