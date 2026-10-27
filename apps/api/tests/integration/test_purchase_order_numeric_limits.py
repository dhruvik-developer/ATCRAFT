"""Calculated limits fail atomically at service and HTTP boundaries."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.core.errors import ValidationError
from src.modules.procurement import purchase_order_changes as changes
from src.modules.procurement import service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseRequisitionLine,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    PurchaseOrderLineIn,
    RequisitionCreate,
    RequisitionLineIn,
)
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import approver as approver
from tests.integration.test_procurement_service import buyer as buyer
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import tenant as tenant
from tests.integration.test_purchase_order_changes import manual_payload, manual_resolution

pytestmark = pytest.mark.integration


async def test_pooled_source_counter_overflow_refuses_all_order_writes(
    db, tenant, buyer, approver, masters
):
    header, sources = await service.create_requisition(
        db,
        tenant,
        buyer,
        RequisitionCreate(
            required_date=today(),
            warehouse_id=masters["warehouse"].id,
            lines=[
                RequisitionLineIn(
                    line_number=1,
                    item_id=masters["item"].id,
                    quantity=Decimal("60000000000000"),
                    estimated_unit_price=Decimal("0"),
                )
            ],
        ),
    )
    header_id, source_id = header.id, sources[0].id
    for status in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await service.transition_requisition(
            db, tenant, approver, header_id, status, reason="Demand approved for numeric boundary"
        )
    before_orders = await db.scalar(select(func.count()).select_from(PurchaseOrder))
    before_allocations = await db.scalar(
        select(func.count()).select_from(PurchaseOrderSourceAllocation)
    )
    payload = manual_payload(
        masters,
        lines=[
            PurchaseOrderLineIn(
                line_number=number,
                item_id=masters["item"].id,
                quantity=Decimal("60000000000000"),
                unit_price=Decimal("0"),
                requisition_line_id=source_id,
            )
            for number in (1, 2)
        ],
    )
    with pytest.raises(ValidationError, match="total ordered quantity is too large"):
        await service.create_purchase_order(db, tenant, buyer, payload)
    assert await db.scalar(select(func.count()).select_from(PurchaseOrder)) == before_orders
    assert (
        await db.scalar(select(func.count()).select_from(PurchaseOrderSourceAllocation))
        == before_allocations
    )
    assert await db.scalar(
        select(PurchaseRequisitionLine.ordered_quantity).where(
            PurchaseRequisitionLine.id == source_id
        )
    ) == Decimal("0")


async def test_merge_quantity_overflow_preserves_the_original_free_order(
    db, tenant, buyer, masters
):
    masters["item"].gst_rate = Decimal("0")
    await db.flush()

    def proposed(quantity):
        return manual_payload(
            masters,
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=masters["item"].id,
                    quantity=Decimal(quantity),
                    unit_price=Decimal("0"),
                )
            ],
        )

    order, lines, _ = await service.create_purchase_order(
        db, tenant, buyer, proposed("90000000000000")
    )
    identifier, line_id, version = order.id, lines[0].id, order.version
    with pytest.raises(ValidationError, match="merged quantity or discount is too large"):
        await changes.resolve_duplicate(
            db, tenant, buyer, manual_resolution(order, proposed("20000000000000"))
        )
    assert (
        await db.scalar(select(PurchaseOrder.version).where(PurchaseOrder.id == identifier))
        == version
    )
    assert await db.scalar(
        select(PurchaseOrderLine.quantity).where(PurchaseOrderLine.id == line_id)
    ) == Decimal("90000000000000")
