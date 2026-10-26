"""Repeatable physical-unit job work, conversion, QC and packing demonstrations.

Dedicated labels and items keep these examples independent from the ordinary
sales demo. Each workflow is atomic; a rerun finds the original notes/labels and
resumes its real lifecycle without opening stock or issuing a unit twice.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import today
from src.core.database import transaction
from src.core.demo_data import DemoContext, find_by_code
from src.modules.customers.models import Customer
from src.modules.inventory import service as inventory
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.inventory.repository import StockSerialRepository
from src.modules.inventory.schemas import StockSerialBulkCreate
from src.modules.job_work import service as job_work
from src.modules.job_work.models import JobWorkOrder, JobWorkOrderStatus, RejectionAction
from src.modules.job_work.schemas import (
    IssueLineIn,
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.packing import service as packing
from src.modules.packing.models import PackingOrder, PackingOrderStatus
from src.modules.packing.repository import CartonRepository
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    CompleteRequest,
    PackingOrderCreate,
    PackingOrderLineIn,
    PackingSerialSelection,
    SealCartonRequest,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure
from src.modules.products.schemas import ItemCreate
from src.modules.products.service import create_item
from src.modules.quality import service as quality
from src.modules.quality.models import InspectionStatus, QcInspection
from src.modules.quality.repository import QcTemplateParameterRepository
from src.modules.quality.schemas import DecideRequest, RecordResultsRequest
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse

STAGE = "operations"


async def _item(ctx: DemoContext, code: str, *, output: bool, uom_id: uuid.UUID) -> uuid.UUID:
    existing = await find_by_code(ctx.session, Item, ctx.company_id, code)
    if existing is not None:
        ctx.remember("item", code, existing.id)
        return existing.id
    row = await create_item(
        ctx.session,
        ctx.tenant,
        ItemCreate(
            code=code,
            name="Serial brass escutcheon - PVD finish"
            if output
            else "Serial brass escutcheon - unfinished",
            description=(
                "Dedicated named-unit job-work and QC demonstration; "
                "physical labels survive finishing."
            ),
            item_group=ItemGroup.FINISHED_GOOD,
            production_type=ProductionType.JOB_WORK,
            can_manufacture=False,
            base_uom_id=uom_id,
            is_serial_tracked=True,
            can_job_work=True,
            can_sell=True,
            can_scrap=True,
            requires_final_qc=output,
            standard_cost=Decimal("100"),
        ),
    )
    ctx.note(STAGE, created=1)
    return ctx.remember("item", code, row.id)


async def _units(
    ctx: DemoContext, *, item_id: uuid.UUID, warehouse_id: uuid.UUID, tag: str
) -> list[uuid.UUID]:
    labels = [f"{tag}-{number:03d}" for number in range(1, 5)]
    found = await StockSerialRepository(ctx.session, ctx.tenant).by_numbers(item_id, labels)
    missing = [label for label in labels if label not in found]
    if missing:
        await inventory.TraceabilityService(ctx.session, ctx.tenant).bulk_create_serials(
            StockSerialBulkCreate(item_id=item_id, serial_numbers=missing)
        )
        found = await StockSerialRepository(ctx.session, ctx.tenant).by_numbers(item_id, labels)
    opened = await ctx.session.scalar(
        select(InventoryTransaction.id)
        .where(
            InventoryTransaction.company_id == ctx.company_id,
            InventoryTransaction.reference_code == tag,
            InventoryTransaction.transaction_type == TransactionType.OPENING_BALANCE,
        )
        .limit(1)
    )
    ids = [found[label].id for label in labels]
    if opened is None:
        await inventory.post_movements(
            ctx.session,
            ctx.tenant,
            [
                inventory.MovementRequest(
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    serial_id=serial_id,
                    quantity=Decimal("1"),
                    unit_cost=Decimal("100"),
                    transaction_type=TransactionType.OPENING_BALANCE,
                    reference_type="demo_serial_opening",
                    reference_code=tag,
                    reason="Opening individually labelled job-work demo units",
                )
                for serial_id in ids
            ],
        )
    for label, serial_id in zip(labels, ids, strict=True):
        ctx.remember("stock_serial", label, serial_id)
    return ids


async def _job(
    ctx: DemoContext,
    *,
    tag: str,
    item_id: uuid.UUID,
    output_id: uuid.UUID,
    vendor_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    ids: list[uuid.UUID],
) -> JobWorkOrder:
    order = await ctx.session.scalar(
        select(JobWorkOrder)
        .where(
            JobWorkOrder.company_id == ctx.company_id,
            JobWorkOrder.notes.like(f"{tag} -%"),
        )
        .order_by(JobWorkOrder.id)
        .limit(1)
    )
    if order is None:
        order, _ = await job_work.create_order(
            ctx.session,
            ctx.tenant,
            JobWorkOrderCreate(
                vendor_id=vendor_id,
                warehouse_id=warehouse_id,
                return_warehouse_id=warehouse_id,
                process_type="PVD",
                expected_return_date=today() + timedelta(days=7),
                requires_qc=True,
                rate_per_unit=Decimal("12.50"),
                notes=f"{tag} - Named physical labels retained through outsourced finishing",
                lines=[
                    JobWorkOrderLineCreate(
                        line_number=1,
                        item_id=item_id,
                        expected_output_item_id=output_id,
                        quantity_to_issue=4,
                    )
                ],
            ),
        )
        ctx.note(STAGE, created=1)
    ctx.remember("job_work_order", tag, order.id)
    if order.status == JobWorkOrderStatus.DRAFT:
        await job_work.approve_order(
            ctx.session, ctx.tenant, order.id, reason="Serial demo finishing approved"
        )
    if order.status == JobWorkOrderStatus.APPROVED:
        await job_work.issue_order(
            ctx.session,
            ctx.tenant,
            order.id,
            IssueRequest(
                reason="Named demonstration units sent to the finisher",
                lines=[IssueLineIn(line_number=1, issued_quantity=4, serial_ids=ids)],
            ),
        )
    return order


async def _decide(
    ctx: DemoContext, inspection_id: uuid.UUID, ids: list[uuid.UUID], *, hold_last: bool = False
) -> None:
    inspection = await ctx.session.get(QcInspection, inspection_id)
    if inspection is None:
        return
    ctx.remember("qc_inspection", inspection.code, inspection.id)
    if inspection.status not in (
        InspectionStatus.PENDING,
        InspectionStatus.IN_PROGRESS,
    ):
        return
    if inspection.template_id is not None:
        # Reuse the ordinary demo's in-spec readings when a site has a stage
        # default template. Named-unit selection never bypasses its check sheet.
        from src.core.demo_data.operations import _measurement

        parameters = await QcTemplateParameterRepository(ctx.session, ctx.tenant).list_for_template(
            inspection.template_id
        )
        measurements = [
            reading
            for parameter in parameters
            if (reading := _measurement(parameter, fail=False)) is not None
        ]
        if measurements:
            await quality.record_results(
                ctx.session,
                ctx.tenant,
                inspection_id,
                RecordResultsRequest(
                    measurements=measurements,
                ),
            )
    await quality.decide_inspection(
        ctx.session,
        ctx.tenant,
        inspection_id,
        DecideRequest(
            accepted_quantity=len(ids) - int(hold_last),
            hold_quantity=int(hold_last),
            serial_dispositions={
                serial_id: "HOLD" if hold_last and index == len(ids) - 1 else "ACCEPTED"
                for index, serial_id in enumerate(ids)
            },
            reason="Named-unit demonstration finish inspection",
        ),
    )


async def _receive(
    ctx: DemoContext, order: JobWorkOrder, ids: list[uuid.UUID], *, conversion: bool
) -> list[uuid.UUID]:
    receipts = await job_work.list_receipts(ctx.session, ctx.tenant, order.id)
    if not receipts:
        await job_work.receive(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            ReceiveRequest(
                reason="Individually identified demonstration goods received",
                lines=[
                    ReceiptLineIn(
                        line_number=1,
                        output_quantity=4 if conversion else 2,
                        output_serial_ids=ids if conversion else ids[:2],
                        rejected_quantity=0 if conversion else 1,
                        rejected_serial_ids=[] if conversion else ids[3:],
                        rejection_action=RejectionAction.REWORK_AT_VENDOR,
                        returned_unprocessed_quantity=0 if conversion else 1,
                        returned_unprocessed_serial_ids=[] if conversion else ids[2:3],
                    )
                ],
            ),
        )
        receipts = await job_work.list_receipts(ctx.session, ctx.tenant, order.id)
    receipt = receipts[0]
    # Receipt arrays retain input identities. Conversion maps resolve the actual
    # output identities that QC and packing must select.
    line = receipt.lines[0]
    output = [
        uuid.UUID(line.converted_serial_ids.get(str(serial_id), str(serial_id)))
        for serial_id in line.output_serial_ids
    ]
    if receipt.qc_inspection_id is not None:
        await _decide(ctx, receipt.qc_inspection_id, output, hold_last=not conversion)
    ctx.remember(
        "job_work_receipt",
        "DM-JW-SERIAL-CONVERT" if conversion else "DM-JW-SERIAL-SAME",
        receipt.id,
    )
    return output


async def _pack(
    ctx: DemoContext,
    *,
    customer_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    item_id: uuid.UUID,
    uom_id: uuid.UUID,
    box_id: uuid.UUID,
    ids: list[uuid.UUID],
) -> None:
    tag = ctx.code("PACK", "SERIAL-FINAL")
    order = await ctx.session.scalar(
        select(PackingOrder)
        .where(
            PackingOrder.company_id == ctx.company_id,
            PackingOrder.notes.like(f"{tag} -%"),
        )
        .order_by(PackingOrder.id)
        .limit(1)
    )
    if order is None:
        order, _ = await packing.create_order(
            ctx.session,
            ctx.tenant,
            PackingOrderCreate(
                customer_id=customer_id,
                warehouse_id=warehouse_id,
                planned_date=today(),
                notes=f"{tag} - Same-label converted units pass FINAL QC before sealing",
                lines=[PackingOrderLineIn(item_id=item_id, quantity_to_pack=2, uom_id=uom_id)],
            ),
        )
    ctx.remember("packing_order", tag, order.id)
    detail = await packing.get_order_detail(ctx.session, ctx.tenant, order.id)
    line = detail.lines[0]
    if order.status == PackingOrderStatus.DRAFT:
        await packing.start_packing(
            ctx.session,
            ctx.tenant,
            order.id,
            reason="Start FINAL inspection of converted serial units",
            serial_selections=[PackingSerialSelection(line_id=line.id, serial_ids=ids[:2])],
        )
        detail = await packing.get_order_detail(ctx.session, ctx.tenant, order.id)
        line = detail.lines[0]
    if line.qc_inspection_id is not None:
        await _decide(ctx, line.qc_inspection_id, ids[:2])
    if order.status != PackingOrderStatus.IN_PROGRESS:
        return
    cartons = await CartonRepository(ctx.session, ctx.tenant).list_for_order(order.id)
    if not cartons:
        marker = f"{tag}-BOX"
        opened = await ctx.session.scalar(
            select(InventoryTransaction.id)
            .where(
                InventoryTransaction.company_id == ctx.company_id,
                InventoryTransaction.reference_code == marker,
            )
            .limit(1)
        )
        if opened is None:
            await inventory.post_movement(
                ctx.session,
                ctx.tenant,
                item_id=box_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                unit_cost=Decimal("14.60"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="demo_serial_opening",
                reference_id=None,
                reference_code=marker,
                reason="Carton staged for the serial packing demonstration",
            )
        await packing.add_carton(
            ctx.session,
            ctx.tenant,
            order.id,
            CartonCreate(
                packing_material_item_id=box_id,
                contents=[
                    CartonContentIn(
                        line_number=1, quantity=2, serial_ids=ids[:2], net_weight_kg=Decimal("0.5")
                    )
                ],
            ),
        )
        cartons = await CartonRepository(ctx.session, ctx.tenant).list_for_order(order.id)
    for carton in cartons:
        if not carton.is_sealed:
            await packing.seal_carton(
                ctx.session,
                ctx.tenant,
                order.id,
                carton.id,
                SealCartonRequest(reason="FINAL-approved physical units checked and sealed"),
            )
    await packing.complete_packing(
        ctx.session,
        ctx.tenant,
        order.id,
        CompleteRequest(reason="Serial demonstration carton ready for dispatch"),
    )


async def seed(ctx: DemoContext) -> None:
    warehouse = await find_by_code(ctx.session, Warehouse, ctx.company_id, "WH-FG")
    vendor = await find_by_code(ctx.session, Vendor, ctx.company_id, ctx.code("VEN", "PLATE"))
    customer = await find_by_code(ctx.session, Customer, ctx.company_id, ctx.code("DLR", "VASTU"))
    uom = await find_by_code(ctx.session, UnitOfMeasure, ctx.company_id, "NOS")
    box = await find_by_code(ctx.session, Item, ctx.company_id, ctx.code("PK", "BOX"))
    if warehouse is None or vendor is None or customer is None or uom is None or box is None:
        ctx.skipped.append(
            (
                STAGE,
                "Serial workflows require the seeded finishing vendor, customer, "
                "NOS, carton and WH-FG",
            )
        )
        return
    async with transaction(ctx.session):
        source = await _item(ctx, ctx.code("FG", "SERIAL-BODY"), output=False, uom_id=uom.id)
        output = await _item(ctx, ctx.code("FG", "SERIAL-PVD"), output=True, uom_id=uom.id)
        same_ids = await _units(
            ctx, item_id=source, warehouse_id=warehouse.id, tag=ctx.code("SERIAL", "SAME")
        )
        same = await _job(
            ctx,
            tag=ctx.code("JW", "SERIAL-SAME"),
            item_id=source,
            output_id=source,
            vendor_id=vendor.id,
            warehouse_id=warehouse.id,
            ids=same_ids,
        )
        await _receive(ctx, same, same_ids, conversion=False)
        convert_ids = await _units(
            ctx, item_id=source, warehouse_id=warehouse.id, tag=ctx.code("SERIAL", "CONVERT")
        )
        converted = await _job(
            ctx,
            tag=ctx.code("JW", "SERIAL-CONVERT"),
            item_id=source,
            output_id=output,
            vendor_id=vendor.id,
            warehouse_id=warehouse.id,
            ids=convert_ids,
        )
        output_ids = await _receive(ctx, converted, convert_ids, conversion=True)
        await _pack(
            ctx,
            customer_id=customer.id,
            warehouse_id=warehouse.id,
            item_id=output,
            uom_id=uom.id,
            box_id=box.id,
            ids=output_ids,
        )
