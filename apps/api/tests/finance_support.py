"""A vendor's purchase received in full, and the invoices and returns against it.

``seed`` builds a vendor, an item, a purchase order and the goods receipt that
took it in, straight through the ORM; ``invoice_payload`` is a vendor invoice
matching that receipt, ``approved_invoice`` records, submits and approves one,
and ``posted_purchase_return`` posts a return with its debit note. The finance
service suite and the finance HTTP suite both build on them; they live here so
neither is the other's library.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, TenantContext
from src.modules.finance import service
from src.modules.finance.models import VendorInvoice
from src.modules.finance.schemas import InvoiceApproval, VendorInvoiceCreate, VendorInvoiceLineIn
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnStatus,
)
from src.modules.products.models import Item, ItemGroup, UnitOfMeasure, UomCategory
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Fixture:
    """One vendor, one item and a purchase order received in full."""

    def __init__(self) -> None:
        self.vendor: Vendor
        self.item: Item
        self.order: PurchaseOrder
        self.order_line: PurchaseOrderLine
        self.receipt: GoodsReceipt
        self.receipt_line: GoodsReceiptLine
        self.uom: UnitOfMeasure


async def seed(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    ordered_quantity: str = "100",
    unit_price: str = "25",
    accepted_quantity: str = "100",
    line_discount: str = "0",
) -> Fixture:
    """A vendor, an item, an order for it and the receipt that took it in."""
    fixture = Fixture()

    fixture.uom = UnitOfMeasure(
        company_id=company_id, code=_code("U"), name="Numbers", uom_category=UomCategory.COUNT
    )
    fixture.vendor = Vendor(
        company_id=company_id,
        code=_code("V"),
        name="Hardware Supplies",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER],
        payment_terms_days=30,
    )
    warehouse = Warehouse(
        company_id=company_id,
        code=_code("W"),
        name="Raw store",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    session.add_all([fixture.uom, fixture.vendor, warehouse])
    await session.flush()

    fixture.item = Item(
        company_id=company_id,
        code=_code("RM"),
        name="Brass rod 12mm",
        item_group=ItemGroup.RAW_MATERIAL,
        base_uom_id=fixture.uom.id,
        hsn_code="7407",
        gst_rate=Decimal("18"),
    )
    session.add(fixture.item)
    await session.flush()

    fixture.order = PurchaseOrder(
        company_id=company_id,
        code=_code("PO"),
        vendor_id=fixture.vendor.id,
        po_date=date(2026, 4, 1),
        warehouse_id=warehouse.id,
    )
    session.add(fixture.order)
    await session.flush()

    fixture.order_line = PurchaseOrderLine(
        company_id=company_id,
        purchase_order_id=fixture.order.id,
        line_number=1,
        item_id=fixture.item.id,
        quantity=Decimal(ordered_quantity),
        uom_id=fixture.uom.id,
        unit_price=Decimal(unit_price),
        discount=Decimal(line_discount),
        received_quantity=Decimal(accepted_quantity),
        accepted_quantity=Decimal(accepted_quantity),
    )
    fixture.receipt = GoodsReceipt(
        company_id=company_id,
        code=_code("GRN"),
        purchase_order_id=fixture.order.id,
        vendor_id=fixture.vendor.id,
        warehouse_id=warehouse.id,
        receipt_date=date(2026, 4, 5),
    )
    session.add_all([fixture.order_line, fixture.receipt])
    await session.flush()

    fixture.receipt_line = GoodsReceiptLine(
        company_id=company_id,
        goods_receipt_id=fixture.receipt.id,
        line_number=1,
        purchase_order_line_id=fixture.order_line.id,
        item_id=fixture.item.id,
        received_quantity=Decimal(accepted_quantity),
        accepted_quantity=Decimal(accepted_quantity),
        uom_id=fixture.uom.id,
        unit_cost=Decimal(unit_price),
    )
    session.add(fixture.receipt_line)
    await session.flush()
    return fixture


def invoice_payload(
    fixture: Fixture,
    *,
    quantity: str = "100",
    unit_price: str = "25",
    number: str | None = None,
    invoice_date: date = date(2026, 4, 6),
    link_receipt: bool = True,
    currency: str = "INR",
) -> VendorInvoiceCreate:
    return VendorInvoiceCreate(
        vendor_id=fixture.vendor.id,
        vendor_invoice_number=number or _code("INV"),
        invoice_date=invoice_date,
        currency=currency,
        purchase_order_id=fixture.order.id,
        goods_receipt_id=fixture.receipt.id if link_receipt else None,
        lines=[
            VendorInvoiceLineIn(
                item_id=fixture.item.id,
                purchase_order_line_id=fixture.order_line.id,
                goods_receipt_line_id=fixture.receipt_line.id if link_receipt else None,
                quantity=Decimal(quantity),
                uom_id=fixture.uom.id,
                unit_price=Decimal(unit_price),
                tax_rate=Decimal("18"),
            )
        ],
    )


async def approved_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    fixture: Fixture,
    **kwargs: object,
) -> VendorInvoice:
    """Record, submit and approve one matching invoice."""
    detail = await service.create_invoice(
        session,
        tenant,
        invoice_payload(fixture, **kwargs),  # type: ignore[arg-type]
    )
    await service.submit_invoice(session, tenant, detail.id, reason=None)
    await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())
    return await session.get(VendorInvoice, detail.id)  # type: ignore[return-value]


async def posted_purchase_return(
    session: AsyncSession,
    tenant: TenantContext,
    fixture: Fixture,
    *,
    total_amount: str,
    currency: str = "INR",
) -> PurchaseReturn:
    total = Decimal(total_amount)
    purchase_return = PurchaseReturn(
        company_id=tenant.company_id,
        code=_code("PRTN"),
        vendor_id=fixture.vendor.id,
        goods_receipt_id=fixture.receipt.id,
        purchase_order_id=fixture.order.id,
        warehouse_id=fixture.receipt.warehouse_id,
        return_date=date(2026, 5, 6),
        status=PurchaseReturnStatus.POSTED,
        reason="Damaged goods returned",
        currency=currency,
        subtotal=total,
        tax_amount=Decimal("0"),
        total_amount=total,
        posted_at=utcnow(),
        posted_by_id=tenant.user_id,
    )
    session.add(purchase_return)
    await session.flush()
    await service.post_purchase_return_debit_note(
        session,
        tenant,
        purchase_return_id=purchase_return.id,
        code=purchase_return.code,
        vendor_id=purchase_return.vendor_id,
        entry_date=purchase_return.return_date,
        currency=purchase_return.currency,
        subtotal=total,
        tax_amount=Decimal("0"),
        total_amount=total,
    )
    return purchase_return


__all__ = [
    "Fixture",
    "_code",
    "approved_invoice",
    "invoice_payload",
    "posted_purchase_return",
    "seed",
]
