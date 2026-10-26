"""The serial demo leaves actionable workflows and reruns without ledger drift."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, literal, select, union_all

from src.core.clock import utcnow
from src.core.demo_data import DemoContext
from src.core.demo_data import serial_workflows as demo
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import (
    InventoryTransaction,
    StockSerial,
    StockSerialConversion,
)
from src.modules.job_work.models import JobWorkOrder, JobWorkOrderStatus, JobWorkReceipt
from src.modules.packing.models import Carton, PackingOrder, PackingOrderStatus
from src.modules.products.models import ItemGroup
from src.modules.quality.models import InspectionStatus, QcInspection, QcInspectionDecision
from src.modules.vendors.models import VendorType
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_vendor, seed_warehouse

pytestmark = pytest.mark.integration


async def test_serial_demo_keeps_labels_tracks_qc_and_is_idempotent(session, tenant, principal):
    ctx = DemoContext(
        session,
        replace(tenant, is_superuser=True),
        replace(principal, is_superuser=True),
        tenant.company_id,
        tenant.user_id,
    )
    uom = await seed_uom(session, company_id=tenant.company_id, code="NOS")
    await seed_warehouse(session, company_id=tenant.company_id, code="WH-FG")
    await seed_vendor(
        session,
        company_id=tenant.company_id,
        code="DM-VEN-PLATE",
        vendor_types=(VendorType.JOB_WORK,),
        is_approved=True,
        approved_at=utcnow(),
    )
    await seed_customer(session, company_id=tenant.company_id, code="DM-DLR-VASTU")
    await seed_item(
        session,
        company_id=tenant.company_id,
        code="DM-PK-BOX",
        base_uom_id=uom.id,
        item_group=ItemGroup.PACKING_MATERIAL,
    )

    await demo.seed(ctx)
    assert ctx.skipped == []
    same = await session.get(JobWorkOrder, ctx.id_of("job_work_order", "DM-JW-SERIAL-SAME"))
    converted = await session.get(JobWorkOrder, ctx.id_of("job_work_order", "DM-JW-SERIAL-CONVERT"))
    packed = await session.get(PackingOrder, ctx.id_of("packing_order", "DM-PACK-SERIAL-FINAL"))
    assert same.status == JobWorkOrderStatus.PARTIALLY_RECEIVED
    assert converted.status == JobWorkOrderStatus.COMPLETED
    assert packed.status == PackingOrderStatus.PACKED
    links = (await session.scalars(select(StockSerialConversion))).all()
    assert len(links) == 4
    units = {unit.id: unit for unit in (await session.scalars(select(StockSerial))).all()}
    for link in links:
        assert (
            units[link.source_serial_id].serial_number == units[link.output_serial_id].serial_number
        )
        assert units[link.source_serial_id].item_id != units[link.output_serial_id].item_id
    inspections = (await session.scalars(select(QcInspection))).all()
    assert sorted(row.status for row in inspections) == [
        InspectionStatus.ACCEPTED,
        InspectionStatus.ACCEPTED,
        InspectionStatus.HOLD,
    ]
    assert sum(len(row.serial_ids) for row in inspections) == 8
    models = [
        InventoryTransaction,
        StockSerial,
        StockSerialConversion,
        JobWorkOrder,
        JobWorkReceipt,
        PackingOrder,
        Carton,
        QcInspection,
        QcInspectionDecision,
        AuditLog,
    ]
    statement = union_all(
        *[
            select(literal(model.__tablename__), func.count())
            .select_from(model)
            .where(model.company_id == ctx.company_id)
            for model in models
        ]
    )
    before = dict((await session.execute(statement)).all())
    await demo.seed(ctx)
    assert dict((await session.execute(statement)).all()) == before
    assert ctx.skipped == []
