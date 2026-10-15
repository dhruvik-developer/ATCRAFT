"""Tenant-scoped issued invoice queries and batched source snapshots."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.clock import today
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.customers.models import CustomerAddress
from src.modules.dispatch.models import Dispatch, DispatchLine
from src.modules.finance.models import Payment, PaymentAllocation, SalesInvoice, SalesInvoiceLine
from src.modules.finance.sales_invoice_schemas import SalesInvoiceFilters, SalesInvoicePaymentRead
from src.modules.organization.models import Branch, Company
from src.modules.products.models import Item, UnitOfMeasure
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.vendors.models import Transporter

SALES_INVOICE_ENTITY = "Sales invoice"


class SalesInvoiceRepository(BaseRepository[SalesInvoice]):
    model = SalesInvoice
    entity_label = SALES_INVOICE_ENTITY
    searchable_fields = ("code", "buyer_name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": SalesInvoice.code,
        "invoice_date": SalesInvoice.invoice_date,
        "due_date": SalesInvoice.due_date,
        "total_amount": SalesInvoice.total_amount,
        "paid_amount": SalesInvoice.paid_amount,
        "status": SalesInvoice.status,
    }

    async def live_for_dispatch(self, dispatch_id: uuid.UUID) -> SalesInvoice | None:
        stmt = self._scoped().where(
            SalesInvoice.dispatch_id == dispatch_id, SalesInvoice.status != "CANCELLED"
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_many(self, ids: Sequence[uuid.UUID]) -> list[SalesInvoice]:
        if not ids:
            return []
        stmt = (
            self._scoped()
            .where(SalesInvoice.id.in_(ids))
            .order_by(SalesInvoice.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_header(self, invoice_id: uuid.UUID) -> SalesInvoice:
        from src.core.errors import NotFoundError

        rows = await self.lock_many([invoice_id])
        if not rows:
            raise NotFoundError(SALES_INVOICE_ENTITY, invoice_id)
        return rows[0]

    async def list_filtered(
        self, filters: SalesInvoiceFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[SalesInvoice], OffsetPagination]:
        stmt = self._apply_search(self._scoped(), filters.search)
        for field in ("status", "customer_id", "sales_order_id", "dispatch_id"):
            value = getattr(filters, field)
            if value is not None:
                stmt = stmt.where(getattr(SalesInvoice, field) == value)
        if filters.date_from:
            stmt = stmt.where(SalesInvoice.invoice_date >= filters.date_from)
        if filters.date_to:
            stmt = stmt.where(SalesInvoice.invoice_date <= filters.date_to)
        if filters.open_only:
            stmt = stmt.where(
                SalesInvoice.status.in_(("ISSUED", "PARTIALLY_PAID")),
                SalesInvoice.paid_amount < SalesInvoice.total_amount,
            )
        if filters.overdue_only:
            stmt = stmt.where(
                SalesInvoice.due_date < today(),
                SalesInvoice.paid_amount < SalesInvoice.total_amount,
                SalesInvoice.status != "CANCELLED",
            )
        return await self.list_page(
            params,
            stmt=stmt,
            order_by=sort.resolve(self.sortable_fields, SalesInvoice.invoice_date.desc()),
        )

    async def lines(self, invoice_id: uuid.UUID) -> list[SalesInvoiceLine]:
        stmt = (
            select(SalesInvoiceLine)
            .where(
                SalesInvoiceLine.company_id == self.tenant.company_id,
                SalesInvoiceLine.sales_invoice_id == invoice_id,
            )
            .order_by(SalesInvoiceLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def payments(self, invoice_id: uuid.UUID) -> list[SalesInvoicePaymentRead]:
        stmt = (
            select(
                Payment.id.label("payment_id"),
                Payment.code.label("payment_code"),
                Payment.payment_date,
                PaymentAllocation.amount,
            )
            .join(
                PaymentAllocation,
                and_(
                    PaymentAllocation.payment_id == Payment.id,
                    PaymentAllocation.company_id == Payment.company_id,
                ),
            )
            .where(
                Payment.company_id == self.tenant.company_id,
                PaymentAllocation.sales_invoice_id == invoice_id,
            )
            .order_by(Payment.payment_date, Payment.id)
        )
        return [
            SalesInvoicePaymentRead(**row._mapping)
            for row in (await self.session.execute(stmt)).all()
        ]

    async def allocation_ids(self, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not ids:
            return set()
        stmt = (
            select(PaymentAllocation.sales_invoice_id)
            .where(
                PaymentAllocation.company_id == self.tenant.company_id,
                PaymentAllocation.sales_invoice_id.in_(ids),
            )
            .distinct()
        )
        return {
            value
            for value in (await self.session.execute(stmt)).scalars().all()
            if value is not None
        }

    async def credited_ids(self, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        # A3 owns this table; use a read-only seam when its model exists.
        table = Base.metadata.tables.get("credit_notes")
        if table is None or not ids:
            return set()
        stmt = select(table.c.sales_invoice_id).where(
            table.c.company_id == self.tenant.company_id, table.c.sales_invoice_id.in_(ids)
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def closed_order_ids(self, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Invoice IDs whose source order has completed the final close step."""
        if not ids:
            return set()
        stmt = (
            select(SalesInvoice.id)
            .join(
                SalesOrder,
                and_(
                    SalesOrder.id == SalesInvoice.sales_order_id,
                    SalesOrder.company_id == SalesInvoice.company_id,
                ),
            )
            .where(
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.id.in_(ids),
                SalesOrder.status == SalesOrderStatus.CLOSED,
            )
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def previous_discount(self, line_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        if not line_ids:
            return {}
        stmt = (
            select(SalesInvoiceLine.sales_order_line_id, func.sum(SalesInvoiceLine.discount_amount))
            .join(
                SalesInvoice,
                and_(
                    SalesInvoice.id == SalesInvoiceLine.sales_invoice_id,
                    SalesInvoice.company_id == SalesInvoiceLine.company_id,
                ),
            )
            .where(
                SalesInvoiceLine.company_id == self.tenant.company_id,
                SalesInvoiceLine.sales_order_line_id.in_(line_ids),
                SalesInvoice.status != "CANCELLED",
            )
            .group_by(SalesInvoiceLine.sales_order_line_id)
        )
        return {row[0]: Decimal(row[1]) for row in (await self.session.execute(stmt)).all()}


class SalesInvoiceSourceRepository(BaseRepository[Dispatch]):
    model = Dispatch
    entity_label = "Dispatch"

    async def dispatch(self, dispatch_id: uuid.UUID, *, lock: bool = False) -> Dispatch:
        from src.core.errors import NotFoundError

        stmt = self._scoped().where(Dispatch.id == dispatch_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Dispatch", dispatch_id)
        return row

    async def dispatch_lines(self, dispatch_id: uuid.UUID) -> list[DispatchLine]:
        stmt = (
            select(DispatchLine)
            .where(
                DispatchLine.company_id == self.tenant.company_id,
                DispatchLine.dispatch_id == dispatch_id,
            )
            .order_by(DispatchLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def order(self, order_id: uuid.UUID, *, lock: bool = False) -> SalesOrder:
        from src.core.errors import NotFoundError

        stmt = select(SalesOrder).where(
            SalesOrder.company_id == self.tenant.company_id, SalesOrder.id == order_id
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Sales order", order_id)
        return row

    async def order_lines(self, order_id: uuid.UUID, *, lock: bool = False) -> list[SalesOrderLine]:
        stmt = (
            select(SalesOrderLine)
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrderLine.sales_order_id == order_id,
            )
            .order_by(SalesOrderLine.id)
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return list((await self.session.execute(stmt)).scalars().all())

    async def seller(self, branch_id: uuid.UUID | None) -> tuple[Company, Branch | None]:
        company = (
            await self.session.execute(select(Company).where(Company.id == self.tenant.company_id))
        ).scalar_one()
        branch = None
        if branch_id:
            branch = (
                await self.session.execute(
                    select(Branch).where(
                        Branch.id == branch_id, Branch.company_id == self.tenant.company_id
                    )
                )
            ).scalar_one_or_none()
        return company, branch

    async def addresses(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, CustomerAddress]:
        stmt = select(CustomerAddress).where(
            CustomerAddress.company_id == self.tenant.company_id, CustomerAddress.id.in_(ids)
        )
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def item_facts(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str | None]]:
        stmt = select(Item.id, Item.name, Item.hsn_code).where(
            Item.company_id == self.tenant.company_id, Item.id.in_(ids)
        )
        return {
            row.id: (row.name, row.hsn_code) for row in (await self.session.execute(stmt)).all()
        }

    async def units(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        stmt = select(UnitOfMeasure.id, UnitOfMeasure.code).where(
            UnitOfMeasure.company_id == self.tenant.company_id, UnitOfMeasure.id.in_(ids)
        )
        return {row.id: row.code for row in (await self.session.execute(stmt)).all()}

    async def transporter_name(self, transporter_id: uuid.UUID | None) -> str | None:
        if transporter_id is None:
            return None
        stmt = select(Transporter.name).where(
            Transporter.company_id == self.tenant.company_id, Transporter.id == transporter_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
