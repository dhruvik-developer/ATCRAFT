"""Vendor-portal reads. Every statement is bounded by the verified vendor context.

Purchase-order visibility is never restated here: whatever touches an order goes
through ``PurchaseOrderRepository._scoped`` (vendor, SENT-and-later, category
scope or assignment) - directly, or as the ``visible_order_ids`` subquery other
tables are filtered by. Finance rows carry their own ``vendor_id``/``party_id``
filter, which the finance repositories also apply for a vendor principal.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, func, select, true

from src.core.deps import TenantContext
from src.core.pagination import PageParams, paginate
from src.core.responses import OffsetPagination
from src.modules.audit.models import AuditLog
from src.modules.finance.models import (
    OPEN_INVOICE_STATUSES,
    PartyType,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    PaymentType,
    VendorInvoice,
    VendorInvoiceStatus,
)
from src.modules.procurement.inbound_repository import RECEIPT_REFERENCE, InboundRepository
from src.modules.procurement.models import (
    VENDOR_VISIBLE_PO_STATUSES,
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from src.modules.products.models import Item, ItemCategory, UnitOfMeasure
from src.modules.vendors.models import Transporter, Vendor, VendorCategory
from src.modules.warehouses.models import Warehouse

# ASSUMPTION: the "QC rejected" tile looks back thirty days - recent enough to
# be actionable, long enough to cover a monthly review. docs/assumptions.md §C1-4.
VENDOR_QC_LOOKBACK_DAYS = 30

#: A purchase order changes status a handful of times; the timeline embedded in
#: the detail is bounded rather than paged.
TIMELINE_LIMIT = 100

#: Payments a vendor is shown: approved money to it, allocated or not.
VISIBLE_PAYMENT_STATUSES: tuple[PaymentStatus, ...] = (
    PaymentStatus.APPROVED,
    PaymentStatus.PARTIALLY_ALLOCATED,
    PaymentStatus.ALLOCATED,
)

#: The statuses a vendor's order timeline shows: from the moment it was sent.
#: What happened before - drafting, approval - is the buyer's process.
TIMELINE_STATUSES: tuple[str, ...] = tuple(
    str(status) for status in (*VENDOR_VISIBLE_PO_STATUSES, PurchaseOrderStatus.CANCELLED)
)


class VendorPortalRepository:
    def __init__(self, session: Any, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant
        self.orders = InboundRepository(session, tenant)

    # --- profile ------------------------------------------------------------

    async def vendor(self) -> Vendor | None:
        return await self.session.scalar(
            select(Vendor).where(
                Vendor.company_id == self.tenant.company_id,
                Vendor.id == self.tenant.vendor_id,
                Vendor.deleted_at.is_(None),
            )
        )

    async def categories(self) -> list[Any]:
        """The account's own category scope, or the vendor's qualified categories."""
        stmt = select(ItemCategory.id, ItemCategory.code, ItemCategory.name).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.deleted_at.is_(None),
        )
        if self.tenant.allowed_category_ids:
            stmt = stmt.where(ItemCategory.id.in_(self.tenant.allowed_category_ids))
        else:
            stmt = stmt.where(
                select(VendorCategory.id)
                .where(
                    VendorCategory.company_id == self.tenant.company_id,
                    VendorCategory.vendor_id == self.tenant.vendor_id,
                    VendorCategory.item_category_id == ItemCategory.id,
                )
                .exists()
            )
        return list((await self.session.execute(stmt.order_by(ItemCategory.code))).all())

    # --- dashboard ----------------------------------------------------------

    async def dashboard(self, as_of: date) -> Any:
        """Every tile in one statement: the order tabs plus the money and QC counts."""
        source, tab_columns = self.orders.tab_counts(as_of)
        payment_pending = (
            select(func.count())
            .select_from(VendorInvoice)
            .where(
                VendorInvoice.company_id == self.tenant.company_id,
                VendorInvoice.vendor_id == self.tenant.vendor_id,
                VendorInvoice.status.in_(OPEN_INVOICE_STATUSES),
                VendorInvoice.total_amount > VendorInvoice.paid_amount,
            )
            .scalar_subquery()
        )
        qc_rejected = (
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
                GoodsReceipt.status == GoodsReceiptStatus.POSTED,
                GoodsReceipt.receipt_date >= as_of - timedelta(days=VENDOR_QC_LOOKBACK_DAYS),
                GoodsReceiptLine.rejected_quantity > 0,
                GoodsReceipt.purchase_order_id.in_(self.orders.visible_order_ids()),
            )
            .scalar_subquery()
        )
        return (
            await self.session.execute(
                select(
                    *tab_columns,
                    payment_pending.label("payment_pending"),
                    qc_rejected.label("qc_rejected"),
                ).select_from(source)
            )
        ).one()

    # --- one order ----------------------------------------------------------

    async def line_labels(self, order_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
        """Item code, name, category and unit code per line - the vendor holds no master rights."""
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
                    UnitOfMeasure.company_id == PurchaseOrderLine.company_id,
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

    async def timeline(self, order_id: uuid.UUID) -> list[Any]:
        """The order's status changes from SENT onwards, oldest first.

        ``created_at`` is transaction time, so changes made in one transaction
        tie; the UUIDv7 id restores their order.
        """
        to_status = AuditLog.new_values["status"].astext
        stmt = (
            select(
                AuditLog.id,
                AuditLog.created_at.label("at"),
                AuditLog.old_values["status"].astext.label("from_status"),
                to_status.label("to_status"),
            )
            .where(
                AuditLog.company_id == self.tenant.company_id,
                AuditLog.entity_type == "purchase_order",
                AuditLog.entity_id == order_id,
                to_status.in_(TIMELINE_STATUSES),
                AuditLog.old_values["status"].astext.is_not(None),
            )
            .order_by(AuditLog.created_at, AuditLog.id)
            .limit(TIMELINE_LIMIT)
        )
        return list((await self.session.execute(stmt)).all())

    async def invoice_totals(self, order_id: uuid.UUID) -> Any:
        return (
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
                    VendorInvoice.status != VendorInvoiceStatus.CANCELLED,
                )
            )
        ).one()

    # --- receipts and QC ----------------------------------------------------

    def _receipts(self, order_id: uuid.UUID | None) -> Select[Any]:
        stmt = select(GoodsReceipt).where(
            GoodsReceipt.company_id == self.tenant.company_id,
            GoodsReceipt.vendor_id == self.tenant.vendor_id,
            GoodsReceipt.status == GoodsReceiptStatus.POSTED,
            GoodsReceipt.purchase_order_id.in_(self.orders.visible_order_ids()),
        )
        if order_id is not None:
            stmt = stmt.where(GoodsReceipt.purchase_order_id == order_id)
        return stmt

    async def receipts(
        self, params: PageParams, order_id: uuid.UUID | None = None
    ) -> tuple[list[GoodsReceipt], OffsetPagination]:
        """Own posted receipts, newest first (``(created_at, id)`` - see ``timeline``)."""
        return await paginate(
            self.session,
            self._receipts(order_id).order_by(
                GoodsReceipt.created_at.desc(), GoodsReceipt.id.desc()
            ),
            params,
        )

    async def order_codes(self, order_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not order_ids:
            return {}
        stmt = select(PurchaseOrder.id, PurchaseOrder.code).where(
            PurchaseOrder.company_id == self.tenant.company_id,
            PurchaseOrder.id.in_(list(dict.fromkeys(order_ids))),
        )
        return {row.id: row.code for row in (await self.session.execute(stmt)).all()}

    async def receipt_lines(
        self, receipt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[GoodsReceiptLine]]:
        if not receipt_ids:
            return {}
        rows = (
            await self.session.execute(
                select(GoodsReceiptLine)
                .where(
                    GoodsReceiptLine.company_id == self.tenant.company_id,
                    GoodsReceiptLine.goods_receipt_id.in_(list(receipt_ids)),
                )
                .order_by(GoodsReceiptLine.goods_receipt_id, GoodsReceiptLine.line_number)
            )
        ).scalars()
        grouped: dict[uuid.UUID, list[GoodsReceiptLine]] = {}
        for line in rows:
            grouped.setdefault(line.goods_receipt_id, []).append(line)
        return grouped

    async def receipt_inspections(
        self, receipt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[Any]]:
        """Quality's verdicts on these receipts, pulled through its read facade."""
        if not receipt_ids:
            return {}
        from src.modules.quality.service import inspections_for_reference

        grouped: dict[uuid.UUID, list[Any]] = {}
        for inspection in await inspections_for_reference(
            self.session, self.tenant, RECEIPT_REFERENCE, list(receipt_ids)
        ):
            if inspection.reference_id is not None:
                grouped.setdefault(inspection.reference_id, []).append(inspection)
        return grouped

    # --- references a bill may name -----------------------------------------

    async def own_receipt(self, receipt_id: uuid.UUID, order_id: uuid.UUID) -> GoodsReceipt | None:
        """One of the vendor's receipts against ``order_id`` (any non-cancelled status)."""
        return await self.session.scalar(
            select(GoodsReceipt).where(
                GoodsReceipt.company_id == self.tenant.company_id,
                GoodsReceipt.vendor_id == self.tenant.vendor_id,
                GoodsReceipt.id == receipt_id,
                GoodsReceipt.purchase_order_id == order_id,
                GoodsReceipt.status != GoodsReceiptStatus.CANCELLED,
            )
        )

    async def order_lines(self, order_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
        """``{order line id: item id}`` for one order."""
        stmt = select(PurchaseOrderLine.id, PurchaseOrderLine.item_id).where(
            PurchaseOrderLine.company_id == self.tenant.company_id,
            PurchaseOrderLine.purchase_order_id == order_id,
        )
        return {row.id: row.item_id for row in (await self.session.execute(stmt)).all()}

    async def order_receipt_lines(self, order_id: uuid.UUID) -> set[uuid.UUID]:
        """Ids of every receipt line raised against ``order_id``."""
        stmt = (
            select(GoodsReceiptLine.id)
            .join(
                GoodsReceipt,
                and_(
                    GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id,
                    GoodsReceipt.company_id == GoodsReceiptLine.company_id,
                ),
            )
            .where(
                GoodsReceipt.company_id == self.tenant.company_id,
                GoodsReceipt.purchase_order_id == order_id,
            )
        )
        return set((await self.session.execute(stmt)).scalars())

    # --- payments -----------------------------------------------------------

    def _payments(self) -> Select[Any]:
        return select(Payment).where(
            Payment.company_id == self.tenant.company_id,
            Payment.party_type == PartyType.VENDOR,
            Payment.party_id == self.tenant.vendor_id,
            Payment.payment_type == PaymentType.PAYMENT,
            Payment.status.in_(VISIBLE_PAYMENT_STATUSES),
        )

    async def payment_page(
        self, params: PageParams
    ) -> tuple[list[Payment], list[PaymentAllocation], dict[uuid.UUID, tuple[str, str]], Any]:
        rows, pagination = await paginate(
            self.session,
            self._payments().order_by(Payment.payment_date.desc(), Payment.id.desc()),
            params,
        )
        if not rows:
            return [], [], {}, pagination
        allocations = list(
            (
                await self.session.execute(
                    select(PaymentAllocation)
                    .where(
                        PaymentAllocation.company_id == self.tenant.company_id,
                        PaymentAllocation.payment_id.in_([row.id for row in rows]),
                    )
                    .order_by(PaymentAllocation.created_at, PaymentAllocation.id)
                )
            ).scalars()
        )
        invoice_ids = list(dict.fromkeys(a.vendor_invoice_id for a in allocations))
        codes: dict[uuid.UUID, tuple[str, str]] = {}
        if invoice_ids:
            codes = {
                row.id: (row.code, row.vendor_invoice_number)
                for row in (
                    await self.session.execute(
                        select(
                            VendorInvoice.id,
                            VendorInvoice.code,
                            VendorInvoice.vendor_invoice_number,
                        ).where(
                            VendorInvoice.company_id == self.tenant.company_id,
                            VendorInvoice.vendor_id == self.tenant.vendor_id,
                            VendorInvoice.id.in_(invoice_ids),
                        )
                    )
                ).all()
            }
        return rows, allocations, codes, pagination

    async def payment_summary(self, as_of: date) -> list[Any]:
        """Outstanding, overdue, due in seven days, paid this month and the last payment."""
        balance = func.greatest(VendorInvoice.total_amount - VendorInvoice.paid_amount, 0)
        invoices = (
            select(
                VendorInvoice.currency.label("currency"),
                func.sum(balance).label("outstanding"),
                func.sum(case((VendorInvoice.due_date < as_of, balance), else_=0)).label("overdue"),
                func.sum(
                    case(
                        (
                            and_(
                                VendorInvoice.due_date >= as_of,
                                VendorInvoice.due_date < as_of + timedelta(days=7),
                            ),
                            balance,
                        ),
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
        paid = self._payments().where(Payment.payment_date <= as_of).subquery()
        payments = (
            select(
                paid.c.currency.label("currency"),
                func.sum(
                    case((paid.c.payment_date >= as_of.replace(day=1), paid.c.amount), else_=0)
                ).label("paid_this_month"),
            )
            .group_by(paid.c.currency)
            .subquery()
        )
        currency = func.coalesce(invoices.c.currency, payments.c.currency)
        latest = (
            select(paid.c.payment_date, paid.c.amount)
            .where(paid.c.currency == currency)
            .order_by(paid.c.payment_date.desc(), paid.c.id.desc())
            .limit(1)
            .lateral("last_payment")
        )
        return list(
            (
                await self.session.execute(
                    select(
                        currency.label("currency"),
                        func.coalesce(invoices.c.outstanding, 0).label("outstanding"),
                        func.coalesce(invoices.c.overdue, 0).label("overdue"),
                        func.coalesce(invoices.c.due_next_seven_days, 0).label(
                            "due_next_seven_days"
                        ),
                        func.coalesce(payments.c.paid_this_month, 0).label("paid_this_month"),
                        latest.c.payment_date.label("last_payment_date"),
                        latest.c.amount.label("last_payment_amount"),
                    )
                    .select_from(
                        invoices.join(
                            payments, invoices.c.currency == payments.c.currency, full=True
                        )
                    )
                    .outerjoin(latest, true())
                    .order_by(currency)
                )
            ).all()
        )

    # --- pickers ------------------------------------------------------------

    async def transporters(self, params: PageParams) -> tuple[list[Transporter], OffsetPagination]:
        return await paginate(
            self.session,
            select(Transporter)
            .where(
                Transporter.company_id == self.tenant.company_id,
                Transporter.is_active.is_(True),
                Transporter.deleted_at.is_(None),
            )
            .order_by(Transporter.code, Transporter.id),
            params,
        )


__all__ = [
    "TIMELINE_LIMIT",
    "TIMELINE_STATUSES",
    "VENDOR_QC_LOOKBACK_DAYS",
    "VISIBLE_PAYMENT_STATUSES",
    "VendorPortalRepository",
]
