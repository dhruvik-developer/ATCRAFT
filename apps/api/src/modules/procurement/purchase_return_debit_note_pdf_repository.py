"""Tenant-scoped, batched values for a purchase-return debit-note PDF."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.modules.organization.models import Company
from src.modules.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseReturnLine,
)
from src.modules.procurement.repository import (
    PurchaseReturnLineRepository,
    PurchaseReturnRepository,
)
from src.modules.products.models import Item, UnitOfMeasure
from src.modules.vendors.models import Vendor


async def purchase_return_debit_note_pdf_data(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> dict[str, Any]:
    """Load the source document and all printable references in bounded queries."""
    purchase_return = await PurchaseReturnRepository(session, tenant).get(return_id)
    if purchase_return is None:
        raise NotFoundError("Purchase return", return_id)

    company = await session.scalar(select(Company).where(Company.id == tenant.company_id))
    vendor = await session.scalar(
        select(Vendor).where(
            Vendor.company_id == tenant.company_id,
            Vendor.id == purchase_return.vendor_id,
        )
    )
    receipt = await session.scalar(
        select(GoodsReceipt).where(
            GoodsReceipt.company_id == tenant.company_id,
            GoodsReceipt.id == purchase_return.goods_receipt_id,
        )
    )
    order = (
        await session.scalar(
            select(PurchaseOrder).where(
                PurchaseOrder.company_id == tenant.company_id,
                PurchaseOrder.id == purchase_return.purchase_order_id,
            )
        )
        if purchase_return.purchase_order_id is not None
        else None
    )
    if company is None or vendor is None or receipt is None:
        raise NotFoundError("Purchase return references", return_id)

    lines = await PurchaseReturnLineRepository(session, tenant).list_for_return(return_id)
    item_facts = (
        await session.execute(
            select(PurchaseReturnLine.id, Item.code, Item.name, UnitOfMeasure.code)
            .join(
                Item,
                (Item.id == PurchaseReturnLine.item_id) & (Item.company_id == tenant.company_id),
            )
            .join(
                UnitOfMeasure,
                (UnitOfMeasure.id == PurchaseReturnLine.uom_id)
                & (UnitOfMeasure.company_id == tenant.company_id),
            )
            .where(
                PurchaseReturnLine.company_id == tenant.company_id,
                PurchaseReturnLine.purchase_return_id == return_id,
            )
            .order_by(PurchaseReturnLine.line_number)
        )
    ).all()
    facts_by_line_id = {line_id: (code, name, uom) for line_id, code, name, uom in item_facts}

    def address_block(row: Any) -> dict[str, str | None]:
        return {
            key: getattr(row, key, None)
            for key in (
                "gstin",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "postal_code",
            )
        }

    data: dict[str, Any] = {
        "code": purchase_return.code,
        "debit_note_number": purchase_return.debit_note_number,
        "return_date": purchase_return.return_date,
        "currency": purchase_return.currency,
        "reason": purchase_return.reason,
        "goods_receipt_code": receipt.code,
        "purchase_order_code": order.code if order else None,
        "subtotal": purchase_return.subtotal,
        "tax_amount": purchase_return.tax_amount,
        "total_amount": purchase_return.total_amount,
        "company": {
            "name": company.legal_name or company.name,
            **address_block(company),
        },
        "vendor": {
            "name": vendor.legal_name or vendor.name,
            **address_block(vendor),
        },
        "lines": [],
    }
    for line in lines:
        item_code, item_name, uom_code = facts_by_line_id[line.id]
        data["lines"].append(
            {
                "line_number": line.line_number,
                "item_code": item_code,
                "description": line.description or item_name,
                "quantity": line.quantity,
                "uom": uom_code,
                "unit_price": line.unit_price,
                "tax_rate": line.tax_rate,
                "tax_amount": line.tax_amount,
                "line_total": line.line_total,
                "stock_state": line.stock_state,
                "reason": line.reason,
            }
        )
    return data
