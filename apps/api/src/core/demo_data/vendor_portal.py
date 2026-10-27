"""Vendor-portal demonstration orders, one per vendor-app tab (WP C1a).

Each order is raised, approved and sent by the buyer through the procurement
service, then taken forward *as the vendor* through the portal service - the
same code the vendor app calls - so the demo carries what a real vendor would
leave behind: a committed delivery date separate from the planning date, a
decline reason, a ready flag, a numbered consignment, a delivery mark and a
submitted bill with no cost centre.

Idempotent by identity: an order is found again by the ``[DM-PORTAL-...]``
marker in its notes, a consignment by its order, a receipt by its order and a
bill by its vendor invoice number - never by a date. A re-run only advances
what an earlier run left part-way.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import today, utcnow
from src.core.demo_data import DemoContext
from src.modules.finance.models import VendorInvoice
from src.modules.procurement import service as procurement
from src.modules.procurement.inbound_models import PurchaseOrderShipment
from src.modules.procurement.inbound_schemas import InboundShipmentCreate
from src.modules.procurement.inbound_service import mark_delivered, record_inbound_shipment
from src.modules.procurement.models import GoodsReceipt, PurchaseOrder, PurchaseOrderStatus
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderCreate,
    PurchaseOrderLineIn,
)
from src.modules.products.models import Item
from src.modules.users.models import User
from src.modules.vendor_portal.schemas import (
    VendorPortalAccept,
    VendorPortalInvoiceCreate,
    VendorPortalInvoiceLineIn,
    VendorPortalReady,
)
from src.modules.vendor_portal.service import VendorPortalService

#: (marker suffix, vendor manager, item, warehouse, where the order ends up).
#: Every manager gets one SENT order of its own; the rest walk the tabs.
EXAMPLES = (
    ("LOCKS-SENT", "VEN-LOCKS", "BO-LOCKBODY", "WH-CMP", "SENT"),
    ("HANDLES-SENT", "VEN-HANDLES", "BO-HANDLE", "WH-CMP", "SENT"),
    ("PACKING-SENT", "VEN-PACKING", "PK-BOX", "WH-PKG", "SENT"),
    ("ACCEPTED", "VEN-LOCKS", "BO-LOCKBODY", "WH-CMP", "ACKNOWLEDGED"),
    ("READY", "VEN-LOCKS", "BO-LOCKBODY", "WH-CMP", "READY"),
    ("SHIPPED", "VEN-HANDLES", "BO-HANDLE", "WH-CMP", "SHIPPED"),
    ("DECLINED", "VEN-HANDLES", "BO-HANDLE", "WH-CMP", "VENDOR_DECLINED"),
    ("DELIVERED", "VEN-PACKING", "PK-BOX", "WH-PKG", "DELIVERED"),
)

#: The order quantity every example carries.
ORDER_QUANTITY = Decimal("20")
#: How many days later than the buyer's date the vendor commits to.
COMMITMENT_SLIP_DAYS = 3

_SEND_ROUTE = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
)


async def seed(ctx: DemoContext) -> None:
    managers = {
        row.id: row
        for row in (
            await ctx.session.execute(
                select(User).where(
                    User.company_id == ctx.company_id,
                    User.vendor_id.is_not(None),
                    User.deleted_at.is_(None),
                )
            )
        ).scalars()
    }
    items = {
        row.id: row
        for row in (
            await ctx.session.execute(
                select(Item).where(Item.company_id == ctx.company_id, Item.deleted_at.is_(None))
            )
        ).scalars()
    }
    orders = {
        row.notes.split("]", 1)[0][1:]: row
        for row in (
            await ctx.session.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.company_id == ctx.company_id,
                    PurchaseOrder.notes.like("[DM-PORTAL-%"),
                )
            )
        ).scalars()
        if row.notes
    }
    for index, (suffix, manager_code, item_code, warehouse_code, final) in enumerate(EXAMPLES):
        manager_id = ctx.maybe_id("user", ctx.code("USER", manager_code))
        item_id = ctx.maybe_id("item", ctx.code(item_code))
        warehouse_id = ctx.maybe_id("warehouse", warehouse_code)
        if manager_id not in managers or item_id not in items or warehouse_id is None:
            ctx.skipped.append(
                ("procurement", f"Portal {suffix}: demo manager/item/warehouse missing")
            )
            continue
        manager, item = managers[manager_id], items[item_id]
        assert manager.vendor_id is not None
        marker = ctx.code("PORTAL", suffix)
        order = orders.get(marker)
        if order is None:
            order, _, _ = await procurement.create_purchase_order(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                PurchaseOrderCreate(
                    vendor_id=manager.vendor_id,
                    assigned_vendor_user_id=manager.id,
                    warehouse_id=warehouse_id,
                    po_date=today() - timedelta(days=3),
                    expected_delivery_date=today() + timedelta(days=40 + index * 20),
                    notes=f"[{marker}] Vendor portal demonstration",
                    lines=[
                        PurchaseOrderLineIn(
                            line_number=1,
                            item_id=item.id,
                            quantity=ORDER_QUANTITY,
                            unit_price=item.standard_cost,
                            requires_qc=False,
                        )
                    ],
                ),
            )
            ctx.note("procurement", created=1)
        else:
            ctx.note("procurement", existing=1)
        ctx.remember("purchase_order", marker, order.id)
        order_id = order.id

        if order.status in _SEND_ROUTE:
            for target in _SEND_ROUTE[_SEND_ROUTE.index(order.status) + 1 :]:
                await procurement.transition_purchase_order(
                    ctx.session,
                    ctx.tenant,
                    ctx.principal,
                    order_id,
                    target,
                    reason="Vendor portal demonstration",
                    confirm_duplicate=bool(order.duplicate_acknowledged),
                )
        if final == "SENT":
            continue

        # From here on the vendor acts, through the portal service.
        vendor_tenant = replace(
            ctx.tenant,
            vendor_id=manager.vendor_id,
            user_id=manager.id,
            allowed_category_ids=tuple(manager.allowed_category_ids or ()),
        )
        portal = VendorPortalService(ctx.session, vendor_tenant)
        if final == "VENDOR_DECLINED":
            if order.status == PurchaseOrderStatus.SENT:
                await portal.decline(
                    ctx.principal, order_id, "Capacity unavailable in the requested delivery week"
                )
            continue
        if order.status == PurchaseOrderStatus.SENT:
            committed = order.expected_delivery_date or today()
            await portal.accept(
                ctx.principal,
                order_id,
                VendorPortalAccept(
                    committed_delivery_date=committed + timedelta(days=COMMITMENT_SLIP_DAYS),
                    vendor_reference=f"{marker}-SO",
                    note="Confirmed against our production plan",
                ),
            )
        if final == "ACKNOWLEDGED":
            continue
        if final == "READY":
            if order.ready_for_dispatch_at is None:
                await portal.ready(
                    order_id, VendorPortalReady(packages=2, note="Packed; collection booked")
                )
            continue

        shipment = await ctx.session.scalar(
            select(PurchaseOrderShipment).where(
                PurchaseOrderShipment.company_id == ctx.company_id,
                PurchaseOrderShipment.purchase_order_id == order_id,
            )
        )
        if shipment is None:
            created = await record_inbound_shipment(
                ctx.session,
                vendor_tenant,
                order_id,
                InboundShipmentCreate(
                    courier_name="Blue Dart",
                    tracking_reference=marker,
                    lr_number=f"LR-{index:04d}",
                    shipped_at=utcnow() - timedelta(days=1),
                    packages=2,
                    weight_kg=Decimal("12"),
                    expected_delivery_date=today() + timedelta(days=2),
                ),
            )
            shipment = await ctx.session.get(PurchaseOrderShipment, created.id)
        assert shipment is not None
        if final != "DELIVERED":
            continue
        if shipment.delivered_at is None:
            await mark_delivered(ctx.session, vendor_tenant, order_id, shipment.id)

        # The buyer's store receives the goods ...
        receipt = await ctx.session.scalar(
            select(GoodsReceipt).where(
                GoodsReceipt.company_id == ctx.company_id,
                GoodsReceipt.purchase_order_id == order_id,
            )
        )
        detail = await procurement.get_purchase_order_detail(ctx.session, ctx.tenant, order_id)
        if receipt is None:
            receipt, _ = await procurement.create_goods_receipt(
                ctx.session,
                ctx.tenant,
                GoodsReceiptCreate(
                    purchase_order_id=order_id,
                    receipt_date=today(),
                    notes=f"[{marker}-GRN] Portal delivery",
                    lines=[
                        GoodsReceiptLineIn(
                            line_number=1,
                            purchase_order_line_id=detail.lines[0].id,
                            received_quantity=ORDER_QUANTITY,
                            accepted_quantity=ORDER_QUANTITY,
                            requires_qc=False,
                        )
                    ],
                ),
            )
        if receipt.status == "DRAFT":
            await procurement.post_goods_receipt(ctx.session, ctx.tenant, ctx.principal, receipt.id)

        # ... and the vendor bills it from the portal (no cost centre: staff tag it).
        invoice_number = f"{marker}-BILL"
        invoice = await ctx.session.scalar(
            select(VendorInvoice).where(
                VendorInvoice.company_id == ctx.company_id,
                VendorInvoice.vendor_invoice_number == invoice_number,
                VendorInvoice.vendor_id == manager.vendor_id,
            )
        )
        if invoice is None:
            await portal.create_invoice(
                VendorPortalInvoiceCreate(
                    purchase_order_id=order_id,
                    goods_receipt_id=receipt.id,
                    invoice_date=today(),
                    vendor_invoice_number=invoice_number,
                    lines=[
                        VendorPortalInvoiceLineIn(
                            item_id=item.id,
                            purchase_order_line_id=detail.lines[0].id,
                            quantity=ORDER_QUANTITY,
                            unit_price=item.standard_cost,
                            tax_rate=item.gst_rate,
                        )
                    ],
                )
            )
