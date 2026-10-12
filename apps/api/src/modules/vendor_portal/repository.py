"""All portal reads are constrained by the verified vendor context."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.audit.models import AuditLog
from src.modules.finance.models import (
    OPEN_INVOICE_STATUSES,
    Payment,
    VendorInvoice,
)
from src.modules.procurement.inbound_repository import InboundRepository
from src.modules.procurement.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder
from src.modules.products.models import Item, ItemCategory, UnitOfMeasure
from src.modules.vendors.models import Transporter, Vendor, VendorCategory
from src.modules.warehouses.models import Warehouse

# ASSUMPTION: QC rejection dashboard shows the last thirty UTC days.
# docs/assumptions.md §C1-4.
VENDOR_QC_LOOKBACK_DAYS = 30


class VendorPortalRepository:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant

    async def vendor(self) -> Vendor | None:
        return await self.session.scalar(
            select(Vendor).where(
                Vendor.company_id == self.tenant.company_id,
                Vendor.id == self.tenant.vendor_id,
                Vendor.deleted_at.is_(None),
            )
        )

    async def order_projection(self, order_id: uuid.UUID) -> Any:
        return (
            await self.session.execute(
                InboundRepository(self.session, self.tenant)
                .projection()
                .where(PurchaseOrder.id == order_id)
            )
        ).one()

    async def invoice_summary(self, order_id: uuid.UUID) -> dict[str, Any]:
        row = (
            await self.session.execute(
                select(
                    func.count().label("count"),
                    func.coalesce(func.sum(VendorInvoice.total_amount), 0).label("total"),
                    func.coalesce(func.sum(VendorInvoice.paid_amount), 0).label("paid"),
                    func.coalesce(
                        func.sum(VendorInvoice.total_amount - VendorInvoice.paid_amount), 0
                    ).label("balance"),
                ).where(
                    VendorInvoice.company_id == self.tenant.company_id,
                    VendorInvoice.vendor_id == self.tenant.vendor_id,
                    VendorInvoice.purchase_order_id == order_id,
                    VendorInvoice.status != "CANCELLED",
                )
            )
        ).one()
        return dict(row._mapping)

    async def shipment_count(self, order_id: uuid.UUID) -> int:
        from src.modules.procurement.inbound_models import PurchaseOrderShipment

        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(PurchaseOrderShipment)
                .where(
                    PurchaseOrderShipment.company_id == self.tenant.company_id,
                    PurchaseOrderShipment.purchase_order_id == order_id,
                )
            )
            or 0
        )

    async def payment_page(self, params: PageParams) -> Any:
        from src.modules.finance.models import PaymentAllocation
        from src.modules.finance.repository import PaymentAllocationRepository, PaymentRepository

        repo = PaymentRepository(self.session, self.tenant)
        rows, pagination = await repo.list_page(
            params,
            stmt=repo._scoped().where(
                Payment.status.in_(("APPROVED", "PARTIALLY_ALLOCATED", "ALLOCATED"))
            ),
        )
        allocation_repo = PaymentAllocationRepository(self.session, self.tenant)
        allocations = list(
            (
                await self.session.execute(
                    allocation_repo._scoped().where(
                        PaymentAllocation.payment_id.in_([row.id for row in rows])
                    )
                )
            ).scalars()
        )
        codes = await allocation_repo.invoice_codes([row.vendor_invoice_id for row in allocations])
        return rows, allocations, codes, pagination

    async def categories(self) -> list[Any]:
        stmt = (
            select(ItemCategory.id, ItemCategory.code, ItemCategory.name)
            .join(
                VendorCategory,
                and_(
                    VendorCategory.item_category_id == ItemCategory.id,
                    VendorCategory.company_id == ItemCategory.company_id,
                ),
            )
            .where(
                ItemCategory.company_id == self.tenant.company_id,
                VendorCategory.vendor_id == self.tenant.vendor_id,
                ItemCategory.deleted_at.is_(None),
            )
        )
        if self.tenant.allowed_category_ids:
            stmt = stmt.where(ItemCategory.id.in_(self.tenant.allowed_category_ids))
        return list((await self.session.execute(stmt.order_by(ItemCategory.code))).all())

    async def dashboard(self, as_of: date) -> Any:
        source = InboundRepository(self.session, self.tenant).projection().subquery()

        # Includes every counter as a scalar aggregate in one database statement.
        def count(where: Any) -> Any:
            return func.count().filter(where)

        invoices = (
            select(func.count())
            .where(
                VendorInvoice.company_id == self.tenant.company_id,
                VendorInvoice.vendor_id == self.tenant.vendor_id,
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES),
                VendorInvoice.total_amount > VendorInvoice.paid_amount,
            )
            .scalar_subquery()
        )
        rejected = (
            select(func.count())
            .select_from(GoodsReceiptLine)
            .join(
                GoodsReceipt,
                and_(
                    GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id,
                    GoodsReceipt.company_id == GoodsReceiptLine.company_id,
                ),
            )
            .where(
                GoodsReceipt.company_id == self.tenant.company_id,
                GoodsReceipt.vendor_id == self.tenant.vendor_id,
                GoodsReceipt.status == "POSTED",
                GoodsReceipt.receipt_date >= as_of - timedelta(days=VENDOR_QC_LOOKBACK_DAYS),
                GoodsReceiptLine.rejected_quantity > 0,
                GoodsReceipt.purchase_order_id.in_(select(source.c.id)),
            )
            .scalar_subquery()
        )
        return (
            await self.session.execute(
                select(
                    count(source.c.status == "SENT").label("to_accept"),
                    count(
                        and_(
                            source.c.status == "ACKNOWLEDGED",
                            source.c.derived_tracking_status == "NOT_DISPATCHED",
                        )
                    ).label("accepted"),
                    count(source.c.derived_tracking_status == "READY_FOR_DISPATCH").label("ready"),
                    count(source.c.derived_tracking_status.in_(("DISPATCHED", "IN_TRANSIT"))).label(
                        "in_transit"
                    ),
                    count(
                        source.c.derived_tracking_status.in_(
                            ("DELIVERED", "RECEIVED", "QC_PENDING", "GRN_COMPLETED")
                        )
                    ).label("delivered"),
                    invoices.label("payment_pending"),
                    rejected.label("qc_rejected"),
                ).select_from(source)
            )
        ).one()

    async def line_labels(self, order_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
        from src.modules.procurement.models import PurchaseOrderLine

        stmt = (
            select(
                PurchaseOrderLine.id,
                Item.code,
                Item.name,
                Item.category_id,
                UnitOfMeasure.code.label("uom_code"),
            )
            .join(
                Item,
                and_(
                    Item.id == PurchaseOrderLine.item_id,
                    Item.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .outerjoin(
                UnitOfMeasure,
                and_(
                    UnitOfMeasure.id == PurchaseOrderLine.uom_id,
                    UnitOfMeasure.company_id == Item.company_id,
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrderLine.purchase_order_id == order_id,
            )
        )
        return {r.id: dict(r._mapping) for r in (await self.session.execute(stmt)).all()}

    async def warehouse(self, warehouse_id: uuid.UUID) -> Warehouse | None:
        return await self.session.scalar(
            select(Warehouse).where(
                Warehouse.company_id == self.tenant.company_id, Warehouse.id == warehouse_id
            )
        )

    async def timeline(self, order_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.company_id == self.tenant.company_id,
                        AuditLog.entity_type == "purchase_order",
                        AuditLog.entity_id == order_id,
                        AuditLog.action.in_(
                            ("STATUS_CHANGE", "PURCHASE_APPROVAL", "CANCEL", "REJECT")
                        ),
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [
            {"id": r.id, "created_at": r.created_at, "summary": r.summary, "action": r.action}
            for r in rows
        ]

    async def receipts(
        self, params: PageParams, order_id: uuid.UUID | None = None
    ) -> tuple[list[GoodsReceipt], OffsetPagination]:
        visible = (
            InboundRepository(self.session, self.tenant)
            ._scoped()
            .with_only_columns(PurchaseOrder.id)
        )
        stmt = select(GoodsReceipt).where(
            GoodsReceipt.company_id == self.tenant.company_id,
            GoodsReceipt.vendor_id == self.tenant.vendor_id,
            GoodsReceipt.status == "POSTED",
            GoodsReceipt.purchase_order_id.in_(visible),
        )
        if order_id:
            stmt = stmt.where(GoodsReceipt.purchase_order_id == order_id)
        from src.core.pagination import paginate

        return await paginate(
            self.session,
            stmt.order_by(GoodsReceipt.created_at.desc(), GoodsReceipt.id.desc()),
            params,
        )

    async def receipt_lines(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
        rows = (
            (
                await self.session.execute(
                    select(GoodsReceiptLine)
                    .where(
                        GoodsReceiptLine.company_id == self.tenant.company_id,
                        GoodsReceiptLine.goods_receipt_id.in_(ids),
                    )
                    .order_by(GoodsReceiptLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r.goods_receipt_id, []).append(
                {
                    key: getattr(r, key)
                    for key in (
                        "id",
                        "item_id",
                        "purchase_order_line_id",
                        "received_quantity",
                        "accepted_quantity",
                        "rejected_quantity",
                        "rejection_reason",
                        "qc_status",
                    )
                }
            )
        return grouped

    async def receipt_inspections(
        self, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[dict[str, Any]]]:
        from src.modules.quality.service import inspections_for_reference

        rows = await inspections_for_reference(self.session, self.tenant, "goods_receipt", ids)
        grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for r in rows:
            if r.reference_id:
                grouped.setdefault(r.reference_id, []).append(
                    {
                        key: getattr(r, key)
                        for key in (
                            "id",
                            "code",
                            "status",
                            "inspected_quantity",
                            "accepted_quantity",
                            "rejected_quantity",
                        )
                    }
                )
        return grouped

    async def transporters(self, params: PageParams) -> tuple[list[Transporter], OffsetPagination]:
        from src.core.pagination import paginate

        return await paginate(
            self.session,
            select(Transporter)
            .where(
                Transporter.company_id == self.tenant.company_id,
                Transporter.is_active.is_(True),
                Transporter.deleted_at.is_(None),
            )
            .order_by(Transporter.code),
            params,
        )

    async def payment_summary(self, as_of: date) -> list[Any]:
        balance = func.greatest(VendorInvoice.total_amount - VendorInvoice.paid_amount, 0)
        inv = (
            select(
                VendorInvoice.currency.label("currency"),
                func.sum(balance).label("outstanding"),
                func.sum(case((VendorInvoice.due_date < as_of, balance), else_=0)).label("overdue"),
                func.sum(
                    case(
                        (VendorInvoice.due_date.between(as_of, as_of + timedelta(days=7)), balance),
                        else_=0,
                    )
                ).label("due_next_seven_days"),
            )
            .where(
                VendorInvoice.company_id == self.tenant.company_id,
                VendorInvoice.vendor_id == self.tenant.vendor_id,
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES),
            )
            .group_by(VendorInvoice.currency)
            .subquery()
        )
        pay = (
            select(
                Payment.currency.label("currency"),
                func.sum(
                    case((Payment.payment_date >= as_of.replace(day=1), Payment.amount), else_=0)
                ).label("paid_this_month"),
            )
            .where(
                Payment.company_id == self.tenant.company_id,
                Payment.party_id == self.tenant.vendor_id,
                Payment.party_type == "VENDOR",
                Payment.status.in_(("APPROVED", "PARTIALLY_ALLOCATED", "ALLOCATED")),
                Payment.payment_date <= as_of,
            )
            .group_by(Payment.currency)
            .subquery()
        )
        latest = (
            select(Payment.payment_date, Payment.amount)
            .where(
                Payment.company_id == self.tenant.company_id,
                Payment.party_id == self.tenant.vendor_id,
                Payment.party_type == "VENDOR",
                Payment.currency == func.coalesce(inv.c.currency, pay.c.currency),
                Payment.status.in_(("APPROVED", "PARTIALLY_ALLOCATED", "ALLOCATED")),
                Payment.payment_date <= as_of,
            )
            .order_by(Payment.payment_date.desc(), Payment.id.desc())
            .limit(1)
            .lateral()
        )
        from sqlalchemy import true

        return list(
            (
                await self.session.execute(
                    select(
                        func.coalesce(inv.c.currency, pay.c.currency).label("currency"),
                        func.coalesce(inv.c.outstanding, 0).label("outstanding"),
                        func.coalesce(inv.c.overdue, 0).label("overdue"),
                        func.coalesce(inv.c.due_next_seven_days, 0).label("due_next_seven_days"),
                        func.coalesce(pay.c.paid_this_month, 0).label("paid_this_month"),
                        latest.c.payment_date.label("last_payment_date"),
                        latest.c.amount.label("last_payment_amount"),
                    )
                    .select_from(inv.join(pay, inv.c.currency == pay.c.currency, full=True))
                    .outerjoin(latest, true())
                )
            ).all()
        )
