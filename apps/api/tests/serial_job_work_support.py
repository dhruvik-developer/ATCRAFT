"""Four named serial units ready to go out to a plating vendor, and their trip.

``make_serial_kit`` stocks four serialised units of a job-work item, a second
serialised item they can come back as, a source and a destination store and an
approved job-work vendor; ``kit`` is the same as a fixture over the conftest
``session`` and ``tenant``. ``_issued`` raises, approves and issues an order that
sends all four units out under one challan - converting them, asking for QC or
returning them to the second store when told to - and ``_receive`` records one
receipt line against it.

The serial job-work suite and the serial context-filter HTTP suite both use
them; they live here so neither is the other's library. They keep the names
they had there.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.clock import today, utcnow
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.inventory.schemas import StockSerialBulkCreate
from src.modules.job_work import service as job_work
from src.modules.job_work.schemas import (
    IssueLineIn,
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.vendors.models import VendorType
from tests.seed_support import seed_item, seed_uom, seed_vendor, seed_warehouse


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


__all__ = ["_issued", "_receive", "kit", "make_serial_kit"]
