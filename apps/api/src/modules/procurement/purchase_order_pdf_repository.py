"""Tenant-scoped batched data for the purchase-order PDF renderer."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from src.core.deps import TenantContext
from src.modules.organization.models import Company
from src.modules.procurement.models import PurchaseOrderLine
from src.modules.procurement.repository import PurchaseOrderLineRepository, PurchaseOrderRepository
from src.modules.products.models import Item, ItemVendorLink, UnitOfMeasure
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse


async def purchase_order_pdf_data(
    session: Any, tenant: TenantContext, order_id: uuid.UUID
) -> dict[str, Any]:
    """Plain values for the renderer; all reference reads are tenant constrained."""
    order = await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
    company = await session.scalar(select(Company).where(Company.id == tenant.company_id))
    vendor = await session.scalar(
        select(Vendor).where(Vendor.company_id == tenant.company_id, Vendor.id == order.vendor_id)
    )
    warehouse = await session.scalar(
        select(Warehouse).where(
            Warehouse.company_id == tenant.company_id, Warehouse.id == order.warehouse_id
        )
    )
    lines = await PurchaseOrderLineRepository(session, tenant).list_for_order(order_id)
    items = (
        await session.execute(
            select(PurchaseOrderLine.id, Item, ItemVendorLink.vendor_item_code, UnitOfMeasure.code)
            .join(
                Item,
                (Item.id == PurchaseOrderLine.item_id) & (Item.company_id == tenant.company_id),
            )
            .outerjoin(
                ItemVendorLink,
                (ItemVendorLink.item_id == Item.id)
                & (ItemVendorLink.vendor_id == order.vendor_id)
                & (ItemVendorLink.company_id == tenant.company_id)
                & (ItemVendorLink.deleted_at.is_(None)),
            )
            .outerjoin(
                UnitOfMeasure,
                (UnitOfMeasure.id == PurchaseOrderLine.uom_id)
                & (UnitOfMeasure.company_id == tenant.company_id),
            )
            .where(
                PurchaseOrderLine.company_id == tenant.company_id,
                PurchaseOrderLine.purchase_order_id == order_id,
            )
        )
    ).all()
    lookup = {r[0]: r[1:] for r in items}

    def block(row: Any) -> dict[str, Any]:
        return {
            key: getattr(row, key, None)
            for key in (
                "name",
                "gstin",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "postal_code",
            )
        }

    data = {
        key: getattr(order, key)
        for key in (
            "code",
            "po_date",
            "status",
            "expected_delivery_date",
            "vendor_committed_date",
            "delivery_address",
            "terms",
            "subtotal",
            "discount",
            "tax_amount",
            "freight_amount",
            "total_amount",
            "currency",
        )
    }
    if not data["delivery_address"] and warehouse:
        data["delivery_address"] = ", ".join(
            str(v) for k, v in block(warehouse).items() if v and k != "gstin"
        )
    data.update(company=block(company), vendor=block(vendor), lines=[])
    if company is not None and company.legal_name:
        # The buyer prints its registered name on a document it signs.
        data["company"]["name"] = company.legal_name
    for line in lines:
        item, vendor_code, uom = lookup[line.id]
        data["lines"].append(
            {
                "code": item.code,
                "vendor_item_code": vendor_code,
                "description": line.description or item.name,
                "hsn": item.hsn_code,
                "quantity": line.quantity,
                "uom": uom,
                "unit_price": line.unit_price,
                "discount": line.discount,
                "tax": line.tax_amount,
                "total": line.line_total,
            }
        )
    return data
