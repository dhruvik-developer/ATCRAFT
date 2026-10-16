"""Good-output report costs exclude rejected, returned and scrapped material."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

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
    JobWorkReceipt,
    JobWorkReceiptLine,
    RejectionAction,
)
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import JobWorkCostRow, ReportFilters, ReportPeriod
from tests import seed_support

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("action", "input_cost", "unit_cost"),
    [
        (RejectionAction.RETURN_TO_STOCK, "14823.5294", "108.823529"),
        (RejectionAction.SCRAP_AT_VENDOR, "14823.5300", "108.823533"),
    ],
)
async def test_cost_report_allocates_only_good_input_within_each_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    company,
    action: RejectionAction,
    input_cost: str,
    unit_cost: str,
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
        expected_return_date=date(2026, 10, 30),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        status=JobWorkOrderStatus.RECEIVED,
    )
    session.add(order)
    await session.flush()
    line = JobWorkOrderLine(
        company_id=company_id,
        job_work_order_id=order.id,
        line_number=1,
        item_id=item.id,
        uom_id=item.base_uom_id,
        quantity_to_issue=Decimal("225"),
        issued_quantity=Decimal("225"),
        received_quantity=Decimal("225"),
        expected_output_item_id=item.id,
        expected_output_quantity=Decimal("225"),
        received_output_quantity=Decimal("150"),
        rejected_quantity=Decimal("52"),
        scrap_quantity=Decimal("10"),
    )
    session.add(line)
    await session.flush()
    challan = JobWorkChallan(
        company_id=company_id,
        code=f"JWC-{uuid.uuid4().hex[:8]}",
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        challan_date=date(2026, 10, 1),
        expected_return_date=date(2026, 10, 30),
        total_quantity=Decimal("225"),
        total_value=Decimal("22500"),
    )
    session.add(challan)
    await session.flush()
    session.add(
        JobWorkChallanLine(
            company_id=company_id,
            challan_id=challan.id,
            job_work_order_line_id=line.id,
            line_number=1,
            item_id=item.id,
            uom_id=item.base_uom_id,
            quantity=Decimal("225"),
            unit_value=Decimal("100"),
            total_value=Decimal("22500"),
        )
    )

    # First lot: 80 processed input split 60 good / 20 rejected, with
    # 10 unprocessed and 10 scrapped. Second lot: 102 output from 100 input,
    # accepted inside the 2% tolerance, split 90 good / 12 rejected. The
    # different capped input shares must be preserved rather than averaged
    # across the order. All-rejected and all-unprocessed lots contribute zero.
    for number, (consumed, good, rejected, returned, scrap, charges) in enumerate(
        [
            ("100", "60", "20", "10", "10", "600"),
            ("100", "90", "12", "0", "0", "900"),
            ("20", "0", "20", "0", "0", "0"),
            ("5", "0", "0", "5", "0", "0"),
        ],
        start=1,
    ):
        receipt = JobWorkReceipt(
            company_id=company_id,
            code=f"{order.code}/R{number}",
            receipt_number=number,
            job_work_order_id=order.id,
            vendor_id=vendor.id,
            receipt_date=date(2026, 10, number + 1),
            total_output_quantity=Decimal(good),
            total_rejected_quantity=Decimal(rejected),
            total_returned_unprocessed=Decimal(returned),
            total_scrap_quantity=Decimal(scrap),
            total_input_consumed=Decimal(consumed),
            total_charges=Decimal(charges),
            reason="Processed lot returned",
        )
        session.add(receipt)
        await session.flush()
        session.add(
            JobWorkReceiptLine(
                company_id=company_id,
                receipt_id=receipt.id,
                job_work_order_line_id=line.id,
                line_number=1,
                output_item_id=item.id,
                output_quantity=Decimal(good),
                rejected_quantity=Decimal(rejected),
                rejection_action=action,
                returned_unprocessed_quantity=Decimal(returned),
                scrap_quantity=Decimal(scrap),
                input_consumed_quantity=Decimal(consumed),
                charges=Decimal(charges),
            )
        )
    await session.flush()

    query = ReportQueryRepository(session, tenant).job_work_cost(
        ReportPeriod(date_from=date(2026, 10, 1), date_to=date(2026, 10, 31)),
        ReportFilters(),
    )
    selected = (await session.execute(query)).mappings().one()
    row = JobWorkCostRow.model_validate(selected)
    assert row.input_cost.quantize(Decimal("0.0001"), ROUND_HALF_UP) == Decimal(input_cost)
    assert row.charges == Decimal("1500")
    assert row.output_quantity == Decimal("150")
    assert row.cost_per_unit is not None
    assert row.cost_per_unit.quantize(Decimal("0.000001"), ROUND_HALF_UP) == Decimal(unit_cost)
