"""Idempotent portal examples, created through the procurement/finance services."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import today, utcnow
from src.core.demo_data import DemoContext
from src.modules.finance import service as finance
from src.modules.finance.models import VendorInvoice
from src.modules.finance.schemas import VendorInvoiceCreate, VendorInvoiceLineIn
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
                            quantity=Decimal("20"),
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
        route = (
            PurchaseOrderStatus.DRAFT,
            PurchaseOrderStatus.PENDING_APPROVAL,
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.SENT,
        )
        if order.status in route:
            for target in route[route.index(order.status) + 1 :]:
                await procurement.transition_purchase_order(
                    ctx.session,
                    ctx.tenant,
                    ctx.principal,
                    order.id,
                    target,
                    reason="Vendor portal demonstration",
                )
        if final == "SENT":
            continue
        if final == "VENDOR_DECLINED":
            if order.status == "SENT":
                await procurement.transition_purchase_order(
                    ctx.session,
                    ctx.tenant,
                    ctx.principal,
                    order.id,
                    PurchaseOrderStatus.VENDOR_DECLINED,
                    reason="Capacity unavailable in the requested delivery week",
                )
            continue
        if order.status == "SENT":
            await procurement.transition_purchase_order(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                order.id,
                PurchaseOrderStatus.ACKNOWLEDGED,
                reason="Vendor confirmed the delivery week",
            )
        if final == "ACKNOWLEDGED":
            continue
        if final == "READY":
            from dataclasses import replace

            from src.modules.vendor_portal.schemas import VendorPortalReady
            from src.modules.vendor_portal.service import VendorPortalService

            portal_tenant = replace(ctx.tenant, vendor_id=manager.vendor_id, user_id=manager.id)
            if order.ready_for_dispatch_at is None:
                await VendorPortalService(ctx.session, portal_tenant).ready(
                    order.id, VendorPortalReady(packages=2, note="Packed; collection booked")
                )
            continue
        shipment = await ctx.session.scalar(
            select(PurchaseOrderShipment).where(
                PurchaseOrderShipment.company_id == ctx.company_id,
                PurchaseOrderShipment.purchase_order_id == order.id,
            )
        )
        if shipment is None:
            created = await record_inbound_shipment(
                ctx.session,
                ctx.tenant,
                order.id,
                InboundShipmentCreate(
                    courier_name="Blue Dart",
                    tracking_reference=marker,
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
            await mark_delivered(ctx.session, ctx.tenant, order.id, shipment.id)
        receipt = await ctx.session.scalar(
            select(GoodsReceipt).where(
                GoodsReceipt.company_id == ctx.company_id,
                GoodsReceipt.purchase_order_id == order.id,
            )
        )
        detail = await procurement.get_purchase_order_detail(ctx.session, ctx.tenant, order.id)
        if receipt is None:
            receipt, _ = await procurement.create_goods_receipt(
                ctx.session,
                ctx.tenant,
                GoodsReceiptCreate(
                    purchase_order_id=order.id,
                    receipt_date=today(),
                    notes=f"[{marker}-GRN] Portal delivery",
                    lines=[
                        GoodsReceiptLineIn(
                            line_number=1,
                            purchase_order_line_id=detail.lines[0].id,
                            received_quantity=Decimal("20"),
                            accepted_quantity=Decimal("20"),
                            requires_qc=False,
                        )
                    ],
                ),
            )
        if receipt.status == "DRAFT":
            await procurement.post_goods_receipt(ctx.session, ctx.tenant, ctx.principal, receipt.id)
        invoice_number = f"{marker}-BILL"
        invoice = await ctx.session.scalar(
            select(VendorInvoice).where(
                VendorInvoice.company_id == ctx.company_id,
                VendorInvoice.vendor_invoice_number == invoice_number,
                VendorInvoice.vendor_id == manager.vendor_id,
            )
        )
        if invoice is None:
            created_invoice = await finance.create_invoice(
                ctx.session,
                ctx.tenant,
                VendorInvoiceCreate(
                    vendor_id=manager.vendor_id,
                    purchase_order_id=order.id,
                    goods_receipt_id=receipt.id,
                    invoice_date=today(),
                    vendor_invoice_number=invoice_number,
                    lines=[
                        VendorInvoiceLineIn(
                            item_id=item.id,
                            purchase_order_line_id=detail.lines[0].id,
                            quantity=Decimal("20"),
                            unit_price=item.standard_cost,
                            tax_rate=item.gst_rate,
                        )
                    ],
                ),
            )
            await finance.submit_invoice(
                ctx.session, ctx.tenant, created_invoice.id, reason="Vendor demo bill"
            )
