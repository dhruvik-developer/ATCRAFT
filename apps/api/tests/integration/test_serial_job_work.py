"""Physical-unit custody, processing costs, conversion lineage and QC on PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.core.clock import today, utcnow
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.inventory import service as inventory
from src.modules.inventory.models import (
    InventoryTransaction,
    SerialStatus,
    StockSerial,
    StockSerialConversion,
    TransactionType,
)
from src.modules.inventory.schemas import StockSerialBulkCreate
from src.modules.job_work import service as job_work
from src.modules.job_work.models import JobWorkOrderStatus, RejectionAction
from src.modules.job_work.schemas import (
    IssueLineIn,
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.quality import service as quality
from src.modules.quality.models import QcInspection
from src.modules.quality.schemas import DecideRequest
from src.modules.vendors.models import VendorType
from tests.seed_support import seed_item, seed_uom, seed_vendor, seed_warehouse

pytestmark = pytest.mark.integration


@pytest.fixture
async def kit(session, tenant):
    return await make_serial_kit(session, tenant)


async def make_serial_kit(session, tenant):
    uom = await seed_uom(session, company_id=tenant.company_id)
    source = await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"JW-IN-{uuid.uuid4().hex[:8]}",
        base_uom_id=uom.id,
        is_serial_tracked=True,
        can_job_work=True,
        can_scrap=True,
        standard_cost=Decimal("100"),
    )
    output = await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"JW-OUT-{uuid.uuid4().hex[:8]}",
        base_uom_id=uom.id,
        is_serial_tracked=True,
        can_job_work=True,
        can_scrap=True,
        standard_cost=Decimal("130"),
    )
    warehouse = await seed_warehouse(
        session, company_id=tenant.company_id, code=f"JW-WH-{uuid.uuid4().hex[:8]}"
    )
    destination = await seed_warehouse(
        session, company_id=tenant.company_id, code=f"JW-DEST-{uuid.uuid4().hex[:8]}"
    )
    vendor = await seed_vendor(
        session,
        company_id=tenant.company_id,
        vendor_types=(VendorType.JOB_WORK,),
        is_approved=True,
        approved_at=utcnow(),
    )
    units = await inventory.TraceabilityService(session, tenant).bulk_create_serials(
        StockSerialBulkCreate(
            item_id=source.id, serial_numbers=[f"JW-{uuid.uuid4().hex[:12]}" for _ in range(4)]
        )
    )
    ids = [unit.id for unit in units]
    await inventory.post_movements(
        session,
        tenant,
        [
            inventory.MovementRequest(
                item_id=source.id,
                warehouse_id=warehouse.id,
                serial_id=serial_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                unit_cost=Decimal("100"),
                reference_type="opening",
                reason="Opening serial stock for vendor processing",
            )
            for serial_id in ids
        ],
    )
    return SimpleNamespace(
        session=session,
        tenant=tenant,
        source=source,
        output=output,
        warehouse=warehouse,
        destination=destination,
        vendor=vendor,
        ids=ids,
    )


async def _issued(kit, *, convert=False, qc=False, destination=False):
    k = kit
    order, _ = await job_work.create_order(
        k.session,
        k.tenant,
        JobWorkOrderCreate(
            vendor_id=k.vendor.id,
            warehouse_id=k.warehouse.id,
            return_warehouse_id=k.destination.id if destination else k.warehouse.id,
            process_type="PVD",
            expected_return_date=today() + timedelta(days=5),
            requires_qc=qc,
            rate_per_unit=Decimal("12.50"),
            lines=[
                JobWorkOrderLineCreate(
                    line_number=1,
                    item_id=k.source.id,
                    quantity_to_issue=Decimal("4"),
                    expected_output_item_id=k.output.id if convert else k.source.id,
                    expected_loss_percentage=Decimal("100"),
                )
            ],
        ),
    )
    await job_work.approve_order(k.session, k.tenant, order.id, reason="Vendor processing approved")
    issued = await job_work.issue_order(
        k.session,
        k.tenant,
        order.id,
        IssueRequest(
            reason="Named units leave under challan",
            lines=[IssueLineIn(line_number=1, issued_quantity=Decimal("4"), serial_ids=k.ids)],
        ),
    )
    assert issued.movement_count == 4
    assert issued.challan.lines[0].serial_ids == k.ids
    return order


async def _receive(kit, principal, order_id, **entry):
    return await job_work.receive(
        kit.session,
        kit.tenant,
        principal,
        order_id,
        ReceiveRequest(
            reason="Vendor delivery inspected and counted",
            lines=[ReceiptLineIn(line_number=1, **entry)],
        ),
    )


async def test_named_same_item_units_can_return_in_lots_with_vendor_rework(kit, principal):
    k = kit
    order = await _issued(k)
    first = await _receive(
        k,
        principal,
        order.id,
        output_quantity=2,
        output_serial_ids=k.ids[:2],
        returned_unprocessed_quantity=1,
        returned_unprocessed_serial_ids=k.ids[2:3],
        rejected_quantity=1,
        rejected_serial_ids=k.ids[3:],
        rejection_action=RejectionAction.REWORK_AT_VENDOR,
    )
    assert first.order.status == JobWorkOrderStatus.PARTIALLY_RECEIVED
    assert first.order.lines[0].received_quantity == 3
    assert (await k.session.get(StockSerial, k.ids[3])).status == SerialStatus.ISSUED
    second = await _receive(k, principal, order.id, output_quantity=1, output_serial_ids=k.ids[3:])
    assert second.order.status == JobWorkOrderStatus.RECEIVED
    position = await inventory.get_balance(k.session, k.tenant, k.source.id, k.warehouse.id)
    assert position.job_work_quantity == 0
    assert position.on_hand_quantity == 4


async def test_final_shortfall_names_the_scrapped_unit_and_preserves_quantity(kit, principal):
    k = kit
    order = await _issued(k)
    result = await _receive(
        k,
        principal,
        order.id,
        output_quantity=3,
        output_serial_ids=k.ids[:3],
        is_final=True,
        scrapped_serial_ids=k.ids[3:],
    )
    assert result.order.status == JobWorkOrderStatus.RECEIVED
    assert result.receipt.total_scrap_quantity == 1
    assert (await k.session.get(StockSerial, k.ids[3])).status == SerialStatus.SCRAPPED
    position = await inventory.get_balance(k.session, k.tenant, k.source.id, k.warehouse.id)
    assert position.on_hand_quantity + position.job_work_quantity + position.scrap_quantity == 4


async def test_serial_receipt_can_relocate_to_another_store_and_capitalizes_processing(
    kit, principal
):
    k = kit
    order = await _issued(k, destination=True)
    await _receive(k, principal, order.id, output_quantity=4, output_serial_ids=k.ids)
    position = await inventory.get_balance(k.session, k.tenant, k.source.id, k.destination.id)
    assert position.on_hand_quantity == 4
    assert position.stock_value == Decimal("450")
    for serial_id in k.ids:
        serial = await k.session.get(StockSerial, serial_id)
        assert serial.warehouse_id == k.destination.id
        assert serial.status == SerialStatus.IN_STOCK


async def test_a_receipt_cannot_consume_another_units_identity_and_failure_is_atomic(
    kit, principal
):
    k = kit
    order = await _issued(k)
    before = await k.session.scalar(select(func.count()).select_from(InventoryTransaction))
    with pytest.raises(BusinessRuleError, match="issued on this job-work line"):
        await _receive(k, principal, order.id, output_quantity=1, output_serial_ids=[uuid.uuid4()])
    assert await k.session.scalar(select(func.count()).select_from(InventoryTransaction)) == before
    assert (await k.session.get(StockSerial, k.ids[0])).status == SerialStatus.ISSUED


async def test_sku_conversion_keeps_labels_and_links_both_immutable_histories(kit, principal):
    k = kit
    order = await _issued(k, convert=True)
    result = await _receive(k, principal, order.id, output_quantity=4, output_serial_ids=k.ids)
    mapping = result.receipt.lines[0].converted_serial_ids
    assert set(mapping) == {str(serial_id) for serial_id in k.ids}
    assert await k.session.scalar(select(func.count()).select_from(StockSerialConversion)) == 4
    for source_id, output_id in mapping.items():
        source = await k.session.get(StockSerial, uuid.UUID(source_id))
        output = await k.session.get(StockSerial, uuid.UUID(output_id))
        assert source.serial_number == output.serial_number
        assert source.item_id == k.source.id and output.item_id == k.output.id
        trace = await inventory.TraceabilityService(k.session, k.tenant).get_serial(output.id)
        assert len(trace.conversions) == 1
        assert {movement.item_id for movement in trace.movements} == {k.source.id, k.output.id}
        assert len(trace.movements) == 4
    with pytest.raises(BusinessRuleError, match="material at the vendor"):
        await _receive(k, principal, order.id, output_quantity=4, output_serial_ids=k.ids)
    assert await k.session.scalar(select(func.count()).select_from(StockSerialConversion)) == 4


async def test_serial_conversion_trace_is_tenant_scoped(kit, principal, other_company):
    k = kit
    order = await _issued(k, convert=True)
    result = await _receive(k, principal, order.id, output_quantity=4, output_serial_ids=k.ids)
    output_id = uuid.UUID(next(iter(result.receipt.lines[0].converted_serial_ids.values())))
    tenant = TenantContext(company_id=other_company.id, user_id=k.tenant.user_id)
    with pytest.raises(NotFoundError):
        await inventory.TraceabilityService(k.session, tenant).get_serial(output_id)


async def test_serial_qc_decides_named_units_and_keeps_prior_dispositions_on_rework(kit, principal):
    k = kit
    order = await _issued(k, qc=True)
    result = await _receive(k, principal, order.id, output_quantity=4, output_serial_ids=k.ids)
    assert result.order.status == JobWorkOrderStatus.QC_PENDING
    inspection = await k.session.get(QcInspection, result.receipt.qc_inspection_id)
    assert set(inspection.serial_ids) == set(k.ids)
    await quality.decide_inspection(
        k.session,
        k.tenant,
        inspection.id,
        DecideRequest(
            accepted_quantity=1,
            rejected_quantity=1,
            rework_quantity=1,
            hold_quantity=1,
            serial_dispositions=dict(
                zip(k.ids, ["ACCEPTED", "REJECTED", "REWORK", "HOLD"], strict=True)
            ),
            rejection_reason="Finish defect on named unit",
            reason="Initial per-unit verdict",
        ),
    )
    settled = await quality.decide_inspection(
        k.session,
        k.tenant,
        inspection.id,
        DecideRequest(
            accepted_quantity=2,
            rejected_quantity=1,
            scrap_quantity=1,
            serial_dispositions=dict(
                zip(k.ids, ["ACCEPTED", "REJECTED", "ACCEPTED", "SCRAP"], strict=True)
            ),
            rejection_reason="One held unit cannot be recovered",
            reason="Rework and hold settled",
        ),
    )
    assert len(settled.inspection.decisions) == 2
    assert settled.inspection.serial_labels == {
        unit.id: unit.serial_number
        for unit in (
            await k.session.scalars(select(StockSerial).where(StockSerial.id.in_(k.ids)))
        ).all()
    }
    assert settled.inspection.decisions[0].serial_dispositions[str(k.ids[2])] == "REWORK"
    assert (await k.session.get(StockSerial, k.ids[3])).status == SerialStatus.SCRAPPED
    position = await inventory.get_balance(k.session, k.tenant, k.source.id, k.warehouse.id)
    assert position.available_quantity == 2
    assert position.damaged_quantity == 1
    assert position.qc_hold_quantity == 0
    completed = await job_work.get_order_detail(k.session, k.tenant, order.id)
    assert completed.status == JobWorkOrderStatus.COMPLETED
