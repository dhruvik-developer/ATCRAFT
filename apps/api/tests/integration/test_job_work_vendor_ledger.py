"""Job-work vendor ledger: GET /job-work/vendor-ledger (E7b/P10.M10)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
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
from src.modules.job_work.service import vendor_ledger
from tests import seed_support

pytestmark = pytest.mark.integration


async def test_vendor_ledger_opening_issued_consumed_and_closing(
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
        status=JobWorkOrderStatus.PARTIALLY_RECEIVED,
    )
    session.add(order)
    await session.flush()
    line = JobWorkOrderLine(
        company_id=company_id,
        job_work_order_id=order.id,
        line_number=1,
        item_id=item.id,
        uom_id=item.base_uom_id,
        quantity_to_issue=Decimal("150"),
        issued_quantity=Decimal("150"),
        received_quantity=Decimal("60"),
        expected_output_item_id=item.id,
        expected_output_quantity=Decimal("150"),
        received_output_quantity=Decimal("55"),
        rejected_quantity=Decimal("0"),
        scrap_quantity=Decimal("2"),
    )
    session.add(line)
    await session.flush()

    for code, on in (("JWC-BEFORE", date(2026, 8, 20)), ("JWC-PERIOD", date(2026, 9, 5))):
        challan = JobWorkChallan(
            company_id=company_id,
            code=f"{code}-{uuid.uuid4().hex[:6]}",
            job_work_order_id=order.id,
            vendor_id=vendor.id,
            challan_date=on,
            expected_return_date=date(2026, 9, 20),
            total_quantity=Decimal("100" if on.month == 8 else "50"),
            total_value=Decimal("1000"),
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
                quantity=Decimal("100" if on.month == 8 else "50"),
                unit_value=Decimal("10"),
                total_value=Decimal("1000" if on.month == 8 else "500"),
            )
        )
    await session.flush()

    receipt = JobWorkReceipt(
        company_id=company_id,
        code=f"{order.code}/R1",
        receipt_number=1,
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        receipt_date=date(2026, 9, 10),
        total_output_quantity=Decimal("55"),
        total_rejected_quantity=Decimal("0"),
        total_returned_unprocessed=Decimal("3"),
        total_scrap_quantity=Decimal("2"),
        total_input_consumed=Decimal("60"),
        total_charges=Decimal("550"),
        vendor_debit_amount=Decimal("30"),
        reason="First lot back",
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
            output_quantity=Decimal("55"),
            rejected_quantity=Decimal("0"),
            rejection_action=RejectionAction.RETURN_TO_STOCK,
            returned_unprocessed_quantity=Decimal("3"),
            scrap_quantity=Decimal("2"),
            excess_loss_quantity=Decimal("0"),
            input_consumed_quantity=Decimal("60"),
            charges=Decimal("550"),
            vendor_debit_amount=Decimal("30"),
            invoiced_amount=Decimal("200"),
            debit_posted_amount=Decimal("0"),
        )
    )
    await session.flush()

    report = await vendor_ledger(
        session,
        tenant,
        vendor_id=vendor.id,
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
    )

    assert report.vendor_id == vendor.id
    assert len(report.items) == 1
    row = report.items[0]
    assert row.item_id == item.id
    assert row.opening_quantity == Decimal("100")
    assert row.issued_quantity == Decimal("50")
    assert row.input_consumed_quantity == Decimal("60")
    assert row.received_output_quantity == Decimal("55")
    assert row.returned_quantity == Decimal("3")
    assert row.scrap_quantity == Decimal("2")
    assert row.closing_quantity == Decimal("90")
    assert row.charges_accrued == Decimal("550")
    assert row.charges_billed == Decimal("200")
    assert row.vendor_debits == Decimal("30")


async def test_vendor_ledger_rejects_an_unknown_vendor_and_a_reversed_period(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    vendor = await seed_support.seed_vendor(
        session, company_id=company.id, code=f"VEN-{uuid.uuid4().hex[:8]}"
    )
    with pytest.raises(NotFoundError):
        await vendor_ledger(
            session,
            tenant,
            vendor_id=uuid.uuid4(),
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
        )
    with pytest.raises(ValidationError):
        await vendor_ledger(
            session,
            tenant,
            vendor_id=vendor.id,
            date_from=date(2026, 9, 30),
            date_to=date(2026, 9, 1),
        )
