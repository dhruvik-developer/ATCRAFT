"""Receipt cost capitalisation end to end (E7b/P13.M5).

Stock in at 100, job-worked at 12.50 a piece, received in full: the processed
output must be booked at 112.50 a unit - the input's frozen FIFO value plus the
charge - and not at the output item's standard cost. The JOB_WORK_COST report
reads the same figures back.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.inventory import service as inv
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.job_work import service
from src.modules.job_work.models import JobWorkProcess, RateBasis
from src.modules.job_work.schemas import (
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import JobWorkCostRow, ReportFilters, ReportPeriod
from tests import seed_support

pytestmark = pytest.mark.integration


def _principal(tenant: TenantContext) -> Principal:
    return Principal(
        user_id=tenant.user_id,
        company_id=tenant.company_id,
        email="operator@atcraft.test",
        full_name="Operator",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
    )


async def test_receipt_books_output_at_input_cost_plus_charge(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    company_id = company.id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
    )
    vendor = await seed_support.seed_vendor(
        session,
        company_id=company_id,
        code=f"VEN-{uuid.uuid4().hex[:8]}",
        is_approved=True,
        approved_at=utcnow(),
    )
    raw_item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=f"RAW-{uuid.uuid4().hex[:8]}",
        can_job_work=True,
        standard_cost=Decimal("100"),
    )
    # A deliberately different standard cost: booking at standard cost would
    # show 130, and the test would fail.
    coated_item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=f"PVD-{uuid.uuid4().hex[:8]}",
        standard_cost=Decimal("130"),
    )

    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=raw_item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("100"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="costing fixture opening stock",
            unit_cost=Decimal("100"),
        )

    order, _ = await service.create_order(
        session,
        tenant,
        JobWorkOrderCreate(
            vendor_id=vendor.id,
            process_type=JobWorkProcess.PVD,
            expected_return_date=today() + timedelta(days=14),
            warehouse_id=warehouse.id,
            rate_per_unit=Decimal("12.50"),
            rate_basis=RateBasis.PER_PIECE,
            lines=[
                JobWorkOrderLineCreate(
                    line_number=1,
                    item_id=raw_item.id,
                    quantity_to_issue=Decimal("100"),
                    expected_output_item_id=coated_item.id,
                    expected_output_quantity=Decimal("100"),
                )
            ],
        ),
    )
    await service.approve_order(session, tenant, order.id, reason="Rate agreed with vendor")
    await service.issue_order(
        session,
        tenant,
        order.id,
        IssueRequest(reason="Handed to the vendor's vehicle"),
    )
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="Full lot back",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"))],
        ),
    )

    assert result.receipt.total_charges == Decimal("1250")

    posted = (
        (
            await session.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.company_id == tenant.company_id,
                    InventoryTransaction.item_id == coated_item.id,
                    InventoryTransaction.transaction_type == TransactionType.JOB_WORK_RECEIPT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(posted) == 1
    # (100 units of input at its frozen 100 issue cost + 1250 charge) / 100.
    assert Decimal(posted[0].unit_cost) == Decimal("112.500000")

    repository = ReportQueryRepository(session, tenant)
    period = ReportPeriod(
        date_from=today() - timedelta(days=1), date_to=today() + timedelta(days=30)
    )
    selected = (
        (await session.execute(repository.job_work_cost(period, ReportFilters()))).mappings().one()
    )
    row = JobWorkCostRow.model_validate(selected)
    assert row.code == order.code
    assert row.output_item_code == coated_item.code
    assert row.input_cost == Decimal("10000")
    assert row.charges == Decimal("1250")
    assert row.debits == Decimal("0")
    assert row.output_quantity == Decimal("100")
    assert row.cost_per_unit == Decimal("112.5")
