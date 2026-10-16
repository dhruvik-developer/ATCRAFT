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
from src.modules.audit.models import AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    StockState,
    TransactionType,
)
from src.modules.job_work import service
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderStatus,
    JobWorkProcess,
    RateBasis,
    RejectionAction,
)
from src.modules.job_work.schemas import (
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.products.models import Item
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import JobWorkCostRow, ReportFilters, ReportPeriod
from src.modules.warehouses.models import Warehouse
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


async def _issued_lot(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    input_quantity: Decimal = Decimal("100"),
    output_quantity: Decimal = Decimal("100"),
    input_unit_cost: Decimal = Decimal("100"),
    rate: Decimal = Decimal("12.50"),
    line_count: int = 1,
) -> tuple[JobWorkOrder, Item, Item, Warehouse]:
    company_id = tenant.company_id
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
        standard_cost=input_unit_cost,
        can_scrap=True,
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
            quantity=input_quantity * line_count,
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="costing fixture opening stock",
            unit_cost=input_unit_cost,
        )

    order, _ = await service.create_order(
        session,
        tenant,
        JobWorkOrderCreate(
            vendor_id=vendor.id,
            process_type=JobWorkProcess.PVD,
            expected_return_date=today() + timedelta(days=14),
            warehouse_id=warehouse.id,
            rate_per_unit=rate,
            rate_basis=RateBasis.PER_PIECE,
            lines=[
                JobWorkOrderLineCreate(
                    line_number=number,
                    item_id=raw_item.id,
                    quantity_to_issue=input_quantity,
                    expected_output_item_id=coated_item.id,
                    expected_output_quantity=output_quantity,
                )
                for number in range(1, line_count + 1)
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
    return order, raw_item, coated_item, warehouse


async def test_receipt_books_output_at_input_cost_plus_charge(
    session: AsyncSession, tenant: TenantContext
) -> None:
    order, _, coated_item, _ = await _issued_lot(session, tenant)
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


@pytest.mark.parametrize(
    ("input_quantity", "expected_output", "good", "rejected"),
    [("100", "100", "90", "10"), ("10", "100", "80", "20"), ("100", "10", "8", "2")],
)
@pytest.mark.parametrize("action", list(RejectionAction))
async def test_mixed_output_conserves_material_value_and_charges_only_good_units(
    session: AsyncSession,
    tenant: TenantContext,
    input_quantity: str,
    expected_output: str,
    good: str,
    rejected: str,
    action: RejectionAction,
) -> None:
    raw_quantity, expected, good_quantity, rejected_quantity = map(
        Decimal, (input_quantity, expected_output, good, rejected)
    )
    order, raw_item, coated_item, warehouse = await _issued_lot(
        session, tenant, input_quantity=raw_quantity, output_quantity=expected
    )
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="Good output and a separately valued rejected share",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=good_quantity,
                    rejected_quantity=rejected_quantity,
                    rejection_action=action,
                    is_final=action is not RejectionAction.REWORK_AT_VENDOR,
                )
            ],
        ),
    )
    balances = {
        row.item_id: row
        for row in (
            await session.scalars(
                select(InventoryBalance).where(
                    InventoryBalance.company_id == tenant.company_id,
                    InventoryBalance.warehouse_id == warehouse.id,
                )
            )
        ).all()
    }
    transactions = (
        await session.scalars(
            select(InventoryTransaction).where(
                InventoryTransaction.company_id == tenant.company_id,
                InventoryTransaction.reference_id == order.id,
                InventoryTransaction.transaction_type != TransactionType.JOB_WORK_ISSUE,
            )
        )
    ).all()
    output_rows = [row for row in transactions if row.item_id == coated_item.id]
    good_row = next(row for row in output_rows if row.stock_state is StockState.AVAILABLE)
    material_unit = raw_quantity * Decimal("100") / expected
    assert good_row.unit_cost == material_unit + Decimal("12.50")
    assert result.receipt.total_charges == good_quantity * Decimal("12.50")
    assert result.receipt.vendor_debit_amount == 0
    assert result.receipt.total_scrap_quantity == 0  # process loss only
    assert result.receipt.has_excess_loss is False

    raw_balance, output_balance = balances[raw_item.id], balances[coated_item.id]
    rejected_input = raw_quantity * rejected_quantity / expected
    if action is RejectionAction.RETURN_TO_STOCK:
        assert len(output_rows) == 2
        damaged = next(row for row in output_rows if row.stock_state is StockState.DAMAGED)
        assert damaged.unit_cost == material_unit
        assert output_balance.on_hand_quantity == expected
        assert output_balance.damaged_quantity == rejected_quantity
        assert raw_balance.job_work_quantity == 0
        assert raw_balance.scrap_quantity == 0
        other_material_value = Decimal("0")
    elif action is RejectionAction.SCRAP_AT_VENDOR:
        assert len(output_rows) == 1
        assert output_balance.on_hand_quantity == good_quantity
        assert output_balance.damaged_quantity == 0
        assert raw_balance.job_work_quantity == 0
        assert raw_balance.scrap_quantity == rejected_input
        scrap = next(row for row in transactions if row.transaction_type is TransactionType.SCRAP)
        assert scrap.item_id == raw_item.id
        assert scrap.source_state is StockState.JOB_WORK
        assert -scrap.quantity == rejected_input
        other_material_value = rejected_input * scrap.unit_cost
    else:
        assert len(output_rows) == 1
        assert output_balance.damaged_quantity == 0
        assert raw_balance.job_work_quantity == rejected_input
        assert raw_balance.scrap_quantity == 0
        assert result.order.status is JobWorkOrderStatus.PARTIALLY_RECEIVED
        other_material_value = rejected_input * Decimal("100")
    assert (
        output_balance.stock_value + other_material_value
        == raw_quantity * Decimal("100") + result.receipt.total_charges
    )


@pytest.mark.parametrize("action", list(RejectionAction))
async def test_fractional_yield_preserves_actual_raw_share_and_records_cost_rounding(
    session: AsyncSession, tenant: TenantContext, action: RejectionAction
) -> None:
    order, raw, output, _ = await _issued_lot(
        session,
        tenant,
        input_quantity=Decimal("1"),
        output_quantity=Decimal("3"),
        input_unit_cost=Decimal("10000"),
        rate=Decimal("0"),
    )
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="A fractional raw share must not lose material value",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("2"),
                    rejected_quantity=Decimal("1"),
                    rejection_action=action,
                    is_final=action is not RejectionAction.REWORK_AT_VENDOR,
                )
            ],
        ),
    )
    balances = {
        row.item_id: row
        for row in (
            await session.scalars(
                select(InventoryBalance).where(InventoryBalance.company_id == tenant.company_id)
            )
        ).all()
    }
    audit = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == result.receipt.id,
                AuditLog.entity_type == service.AUDIT_JOB_WORK_RECEIPT,
            )
        )
    ).one()
    assert audit.new_values is not None
    if action is RejectionAction.RETURN_TO_STOCK:
        assert balances[output.id].stock_value == Decimal("10000")
        cost = audit.new_values["costing"]["1"]
        rejected_cost = audit.new_values["rejected_costing"]["1"]
        assert Decimal(cost["unit_cost"]) == Decimal("3333.333333")
        assert Decimal(rejected_cost["unit_cost"]) == Decimal("3333.333333")
        assert Decimal(cost["rounding_residual"]) + Decimal(
            rejected_cost["rounding_residual"]
        ) == Decimal("0.000001")
    else:
        # Quantity4dp gives 0.6667 raw units to good output and leaves 0.3333
        # at the vendor (for rework), or moves precisely that share to scrap.
        assert balances[output.id].stock_value == Decimal("6667")
        raw_balance = balances[raw.id]
        assert raw_balance.job_work_quantity + raw_balance.scrap_quantity == Decimal("0.3333")
        assert balances[output.id].stock_value + (
            raw_balance.job_work_quantity + raw_balance.scrap_quantity
        ) * Decimal("10000") == Decimal("10000")
        if action is RejectionAction.SCRAP_AT_VENDOR:
            assert audit.new_values["rejected_scrap_input"] == {"1": "0.3333"}


@pytest.mark.parametrize(
    "action", [RejectionAction.RETURN_TO_STOCK, RejectionAction.SCRAP_AT_VENDOR]
)
async def test_rejected_only_output_has_no_fee_or_automatic_vendor_debit(
    session: AsyncSession, tenant: TenantContext, action: RejectionAction
) -> None:
    order, raw, output, _ = await _issued_lot(session, tenant)
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="Every piece rejected; no processing fee earned",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("0"),
                    rejected_quantity=Decimal("100"),
                    rejection_action=action,
                    is_final=True,
                )
            ],
        ),
    )
    assert result.receipt.total_charges == 0
    assert result.receipt.vendor_debit_amount == 0
    assert result.receipt.total_scrap_quantity == 0
    assert result.receipt.total_rejected_quantity == 100
    assert result.order.status is JobWorkOrderStatus.RECEIVED
    rows = (
        await session.scalars(
            select(InventoryTransaction).where(
                InventoryTransaction.reference_id == order.id,
                InventoryTransaction.transaction_type != TransactionType.JOB_WORK_ISSUE,
            )
        )
    ).all()
    if action is RejectionAction.RETURN_TO_STOCK:
        returned = next(row for row in rows if row.item_id == output.id)
        assert returned.stock_state is StockState.DAMAGED
        assert returned.unit_cost == Decimal("100")
    else:
        assert len(rows) == 1
        assert rows[0].item_id == raw.id
        assert rows[0].transaction_type is TransactionType.SCRAP
        assert rows[0].quantity == Decimal("-100")


async def test_charge_rounding_remainder_stays_with_good_output_lines(
    session: AsyncSession, tenant: TenantContext
) -> None:
    order, _, output, warehouse = await _issued_lot(
        session,
        tenant,
        input_quantity=Decimal("1"),
        output_quantity=Decimal("1"),
        rate=Decimal("0.004"),
        line_count=4,
    )
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="Last line is rejected and cannot earn the rounding remainder",
            lines=[
                ReceiptLineIn(
                    line_number=number,
                    output_quantity=Decimal("1") if number < 4 else Decimal("0"),
                    rejected_quantity=Decimal("0") if number < 4 else Decimal("1"),
                    is_final=True,
                )
                for number in range(1, 5)
            ],
        ),
    )
    charges = {line.line_number: line.charges for line in result.receipt.lines}
    assert result.receipt.total_charges == Decimal("0.01")
    assert charges[4] == 0
    assert all(charge >= 0 for charge in charges.values())
    assert sum(charges.values()) == Decimal("0.01")
    output_balance = (
        await session.scalars(
            select(InventoryBalance).where(
                InventoryBalance.item_id == output.id,
                InventoryBalance.warehouse_id == warehouse.id,
            )
        )
    ).one()
    assert output_balance.stock_value == Decimal("400.01")


async def test_rejected_scrap_is_separate_from_process_loss_recovery(
    session: AsyncSession, tenant: TenantContext
) -> None:
    order, raw, output, warehouse = await _issued_lot(session, tenant)
    result = await service.receive(
        session,
        tenant,
        _principal(tenant),
        order.id,
        ReceiveRequest(
            reason="Ten rejected pieces scrapped; a further ten inputs lost",
            approve_excess_loss=True,
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("80"),
                    rejected_quantity=Decimal("10"),
                    rejection_action=RejectionAction.SCRAP_AT_VENDOR,
                    is_final=True,
                )
            ],
        ),
    )
    assert result.receipt.total_rejected_quantity == 10
    assert result.receipt.total_scrap_quantity == 10
    assert result.receipt.vendor_debit_amount == Decimal("1000")
    raw_balance = (
        await session.scalars(
            select(InventoryBalance).where(
                InventoryBalance.item_id == raw.id,
                InventoryBalance.warehouse_id == warehouse.id,
            )
        )
    ).one()
    assert raw_balance.scrap_quantity == 20
    assert raw_balance.job_work_quantity == 0
    output_rows = (
        await session.scalars(
            select(InventoryTransaction).where(InventoryTransaction.item_id == output.id)
        )
    ).all()
    assert len(output_rows) == 1
    assert output_rows[0].stock_state is StockState.AVAILABLE
    assert output_rows[0].unit_cost == Decimal("112.50")
