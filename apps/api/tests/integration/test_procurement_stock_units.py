"""Purchase documents are stated in the item's stock unit (docs/assumptions.md §UOM-1).

No purchase document converts a unit: a goods receipt posts its line's number
as stock. So an item stocked in NOS and bought in packets of ten was ordered,
received and valued in packets counted as single screws - 100 PKT ordered,
100 NOS put on the shelf at a packet's price each. Until documents convert
units, a line naming no unit is written in the stock unit, a line naming any
other unit is refused, and a posting refuses a source line written in another
unit before the rule existed, rather than post its number unconverted.

The legacy lines are made the only way they can exist now: written through the
service in the stock unit, then moved to the purchase unit through the ORM, as
a line raised before the rule would read.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.modules.inventory.models import InventoryBalance
from src.modules.procurement import service
from src.modules.procurement.models import (
    GoodsReceiptLine,
    PurchaseOrderLine,
    PurchaseRequisitionLine,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderLineIn,
    PurchaseReturnCreate,
    PurchaseReturnLineIn,
    RequisitionConversionRequest,
    RequisitionCreate,
    RequisitionLineIn,
)
from src.modules.products.models import Item, ProductionType, UnitOfMeasure
from tests import seed_support
from tests.procurement_support import _schema as _schema
from tests.procurement_support import (
    approved_request,
    fresh,
    linked_order,
    make_order,
    manual_payload,
    receive,
    send_order,
)
from tests.procurement_support import approver as approver
from tests.procurement_support import buyer as buyer
from tests.procurement_support import db as db
from tests.procurement_support import masters as masters
from tests.procurement_support import other_tenant as other_tenant
from tests.procurement_support import tenant as tenant

pytestmark = pytest.mark.integration


async def bought_in_packets(
    db: AsyncSession, tenant: TenantContext, masters
) -> tuple[Item, uuid.UUID]:
    """An item stocked in the masters' NOS and bought in packets, and the packet unit."""
    packet = await seed_support.seed_uom(
        db, company_id=tenant.company_id, code=f"PKT{uuid.uuid4().hex[:6].upper()}", name="Packet"
    )
    item = await seed_support.seed_item(
        db,
        company_id=tenant.company_id,
        code=f"SCREW-{uuid.uuid4().hex[:6].upper()}",
        base_uom_id=masters["uom"].id,
        purchase_uom_id=packet.id,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("4"),
        production_type=ProductionType.BUY,
        can_purchase=True,
        is_active=True,
    )
    return item, packet.id


async def balance_of(db: AsyncSession, tenant: TenantContext, item_id, warehouse_id):
    stmt = select(InventoryBalance).where(
        InventoryBalance.company_id == tenant.company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
    )
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalars().first()


def requisition_payload(masters, item_id, *, uom_id=None) -> RequisitionCreate:
    return RequisitionCreate(
        required_date=today() + timedelta(days=14),
        warehouse_id=masters["warehouse"].id,
        lines=[
            RequisitionLineIn(
                line_number=1,
                item_id=item_id,
                quantity=Decimal("100"),
                uom_id=uom_id,
                suggested_vendor_id=masters["vendor"].id,
                estimated_unit_price=Decimal("4"),
            )
        ],
    )


# -----------------------------------------------------------------------------
# A line naming no unit is in the stock unit; any other unit is refused
# -----------------------------------------------------------------------------


async def test_a_requisition_line_naming_no_unit_is_in_the_stock_unit(
    db, tenant, buyer, masters
) -> None:
    item, _ = await bought_in_packets(db, tenant, masters)

    _, lines = await service.create_requisition(
        db, tenant, buyer, requisition_payload(masters, item.id)
    )

    assert lines[0].uom_id == masters["uom"].id


async def test_an_order_naming_no_unit_is_received_into_stock_as_ordered(
    db, tenant, buyer, masters
) -> None:
    """The finding's own case: 100 ordered is 100 on the shelf, in the stock unit."""
    item, _ = await bought_in_packets(db, tenant, masters)

    order, lines, _ = await make_order(db, tenant, buyer, masters, item=item, quantity="100")
    assert lines[0].uom_id == masters["uom"].id
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="100")

    assert posted.receipt.lines[0].uom_id == masters["uom"].id
    balance = await balance_of(db, tenant, item.id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("100.0000")


async def test_a_line_in_the_purchase_unit_is_refused_with_both_units_named(
    db, tenant, buyer, masters
) -> None:
    item, packet_id = await bought_in_packets(db, tenant, masters)
    packet_code = (await fresh(db, UnitOfMeasure, packet_id)).code
    item_code, stock_code = item.code, masters["uom"].code

    with pytest.raises(ValidationError) as refused:
        await service.create_purchase_order(
            db,
            tenant,
            buyer,
            manual_payload(
                masters,
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=item.id,
                        quantity=Decimal("10"),
                        uom_id=packet_id,
                        unit_price=Decimal("40"),
                    )
                ],
            ),
        )

    assert refused.value.message == (
        f"Line 1 is in {packet_code}, but item {item_code} is stocked in {stock_code}. "
        f"Documents do not convert units yet, so enter the quantity in {stock_code}."
    )
    assert refused.value.details["uom_id"] == str(packet_id)


async def test_a_requisition_line_in_the_purchase_unit_is_refused(
    db, tenant, buyer, masters
) -> None:
    item, packet_id = await bought_in_packets(db, tenant, masters)

    with pytest.raises(ValidationError, match="Documents do not convert units yet"):
        await service.create_requisition(
            db, tenant, buyer, requisition_payload(masters, item.id, uom_id=packet_id)
        )


async def test_another_companys_unit_is_not_found(db, tenant, other_tenant, buyer, masters) -> None:
    """A foreign key to a UUID is not a tenant check: the line is refused as unknown."""
    foreign = await seed_support.seed_uom(db, company_id=other_tenant.company_id)
    foreign_id = foreign.id

    with pytest.raises(NotFoundError):
        await service.create_requisition(
            db, tenant, buyer, requisition_payload(masters, masters["item"].id, uom_id=foreign_id)
        )


# -----------------------------------------------------------------------------
# A posting refuses a source line written in another unit before the rule
# -----------------------------------------------------------------------------


async def _legacy_order(db, tenant, buyer: Principal, masters):
    """A sent order whose line reads in packets, as one raised before the rule would."""
    item, packet_id = await bought_in_packets(db, tenant, masters)
    order, lines, _ = await make_order(db, tenant, buyer, masters, item=item, quantity="5")
    await send_order(db, tenant, buyer, order)
    order_line = await fresh(db, PurchaseOrderLine, lines[0].id)
    order_line.uom_id = packet_id
    await db.flush()
    return item, order, order_line


async def test_no_receipt_is_taken_against_an_order_line_in_another_unit(
    db, tenant, buyer, masters
) -> None:
    item, order, order_line = await _legacy_order(db, tenant, buyer, masters)
    item_id, order_code, warehouse_id = item.id, order.code, masters["warehouse"].id

    with pytest.raises(ValidationError) as refused:
        await service.create_goods_receipt(
            db,
            tenant,
            GoodsReceiptCreate(
                purchase_order_id=order.id,
                lines=[
                    GoodsReceiptLineIn(
                        line_number=1,
                        purchase_order_line_id=order_line.id,
                        received_quantity=Decimal("5"),
                        accepted_quantity=Decimal("5"),
                        rejected_quantity=Decimal("0"),
                    )
                ],
            ),
        )

    assert refused.value.message.startswith(f"Purchase order {order_code} line 1 is in PKT")
    assert refused.value.message.endswith(
        "so amend that line to the stock unit before receiving against it."
    )
    assert await balance_of(db, tenant, item_id, warehouse_id) is None


async def test_a_draft_receipt_in_another_unit_is_not_posted(db, tenant, buyer, masters) -> None:
    """A draft keyed before the rule: posting it would book five packets as five units."""
    item, packet_id = await bought_in_packets(db, tenant, masters)
    order, lines, _ = await make_order(db, tenant, buyer, masters, item=item, quantity="5")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="5", post=False)
    receipt_line = (
        await db.execute(
            select(GoodsReceiptLine).where(GoodsReceiptLine.goods_receipt_id == receipt.id)
        )
    ).scalar_one()
    receipt_line.uom_id = packet_id
    await db.flush()
    item_id, receipt_id, receipt_code = item.id, receipt.id, receipt.code
    warehouse_id = masters["warehouse"].id

    with pytest.raises(ValidationError) as refused:
        await service.post_goods_receipt(db, tenant, buyer, receipt_id)

    assert refused.value.message.startswith(f"Line 1 of {receipt_code} is in PKT")
    assert refused.value.message.endswith(
        "so cancel the receipt and raise a new purchase order in the stock unit."
    )
    assert await balance_of(db, tenant, item_id, warehouse_id) is None


async def test_goods_received_in_another_unit_are_not_returned_against_the_receipt(
    db, tenant, buyer, masters
) -> None:
    item, packet_id = await bought_in_packets(db, tenant, masters)
    order, lines, _ = await make_order(db, tenant, buyer, masters, item=item, quantity="5")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="5")
    receipt_line = await fresh(db, GoodsReceiptLine, posted.receipt.lines[0].id)
    receipt_line.uom_id = packet_id
    await db.flush()
    receipt_id, receipt_line_id = posted.receipt.id, receipt_line.id

    with pytest.raises(ValidationError, match="it cannot be returned against the receipt"):
        await service.create_purchase_return(
            db,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=receipt_id,
                reason="Wrong finish supplied",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1, goods_receipt_line_id=receipt_line_id, quantity=Decimal("2")
                    )
                ],
            ),
        )


async def test_a_requisition_line_in_another_unit_is_listed_unresolved_by_the_conversion(
    db, tenant, buyer, approver, masters
) -> None:
    """Shown with its reason, so the buyer can leave it out (§51), never ordered as it is."""
    item, packet_id = await bought_in_packets(db, tenant, masters)
    item_code, stock_code = item.code, masters["uom"].code
    header, lines = await service.create_requisition(
        db, tenant, buyer, requisition_payload(masters, item.id)
    )
    for status in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await service.transition_requisition(
            db, tenant, approver, header.id, status, reason="Demand approved"
        )
    requisition_line = await fresh(db, PurchaseRequisitionLine, lines[0].id)
    requisition_line.uom_id = packet_id
    packet_code = (await fresh(db, UnitOfMeasure, packet_id)).code
    await db.flush()
    header_id = header.id

    preview = await service.preview_requisition_conversion(
        db, tenant, [header_id], RequisitionConversionRequest()
    )

    assert preview.groups == []
    [unresolved] = preview.unresolved_lines
    assert unresolved.line_number == 1
    assert (
        f"This line is in {packet_code}, but item {item_code} is stocked in {stock_code}. "
        "Documents do not convert units yet, so leave it out and raise it again in the "
        "stock unit."
    ) in unresolved.reasons


async def test_an_order_line_cannot_draw_on_a_requisition_line_in_another_unit(
    db, tenant, buyer, approver, masters
) -> None:
    """The order line is in the stock unit, so the demand it takes must be too.

    The refusal used to tell the buyer to use the requisition line's unit, which
    the order line may no longer name: a dead end rather than a remedy.
    """
    header, lines = await approved_request(db, tenant, buyer, approver, masters, "20")
    packet = await seed_support.seed_uom(db, company_id=tenant.company_id, name="Packet")
    source = await fresh(db, PurchaseRequisitionLine, lines[0].id)
    source.uom_id = packet.id
    await db.flush()
    source_id, header_code, packet_code = source.id, header.code, packet.code

    with pytest.raises(ValidationError) as refused:
        await linked_order(db, tenant, buyer, masters, [(source_id, "20")])

    assert refused.value.message.startswith(
        f"Requisition {header_code} line 1 is in {packet_code}, but item "
    )
    assert refused.value.message.endswith(
        "so leave it out of this order and raise that demand again in the stock unit."
    )
