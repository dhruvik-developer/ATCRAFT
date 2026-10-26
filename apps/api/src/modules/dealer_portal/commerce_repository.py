"""Dealer financial reads always constrain both tenant and verified customer."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.core.pagination import PageParams, paginate
from src.core.responses import OffsetPagination
from src.modules.dispatch.models import Dispatch, DispatchLine, DispatchStatus, DispatchType
from src.modules.finance.models import CreditNote, CreditNoteStatus, SalesInvoice
from src.modules.products.models import Item
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine, SalesReturnStatus

RETURNABLE_DISPATCH_STATUSES = (
    DispatchStatus.DISPATCHED,
    DispatchStatus.IN_TRANSIT,
    DispatchStatus.DELIVERED,
    DispatchStatus.RECEIVED,
    DispatchStatus.QC_PENDING,
    DispatchStatus.QC_COMPLETED,
    DispatchStatus.GRN_COMPLETED,
)


class DealerCommerceRepository:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant

    def owned(self, model: Any) -> Any:
        return select(model).where(
            model.company_id == self.tenant.company_id,
            model.customer_id == self.tenant.customer_id,
        )

    async def require_owned(self, model: Any, identity: uuid.UUID, label: str) -> Any:
        row = await self.session.scalar(self.owned(model).where(model.id == identity))
        if row is None:
            raise NotFoundError(label, identity)
        return row

    async def credit_notes(
        self, params: PageParams, *, invoice_id: uuid.UUID | None = None
    ) -> tuple[list[CreditNote], OffsetPagination]:
        stmt = self.owned(CreditNote)
        if invoice_id is not None:
            stmt = stmt.where(CreditNote.sales_invoice_id == invoice_id)
        return await paginate(
            self.session, stmt.order_by(CreditNote.note_date.desc(), CreditNote.id.desc()), params
        )

    async def returns(self, params: PageParams) -> tuple[list[SalesReturn], OffsetPagination]:
        return await paginate(
            self.session,
            self.owned(SalesReturn).order_by(SalesReturn.created_at.desc(), SalesReturn.id.desc()),
            params,
        )

    async def return_credits(self, identities: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        stmt = (
            select(CreditNote.sales_return_id, func.sum(CreditNote.total_amount))
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNote.customer_id == self.tenant.customer_id,
                CreditNote.sales_return_id.in_(identities),
                CreditNote.status == CreditNoteStatus.ISSUED,
            )
            .group_by(CreditNote.sales_return_id)
        )
        return {
            identity: amount
            for identity, amount in (await self.session.execute(stmt)).all()
            if identity is not None
        }

    def _returnable_lines(self) -> Any:
        returned = (
            select(
                SalesReturnLine.dispatch_line_id,
                func.sum(SalesReturnLine.quantity).label("quantity"),
            )
            .join(
                SalesReturn,
                (SalesReturn.id == SalesReturnLine.sales_return_id)
                & (SalesReturn.company_id == SalesReturnLine.company_id),
            )
            .where(
                SalesReturn.company_id == self.tenant.company_id,
                SalesReturn.customer_id == self.tenant.customer_id,
                SalesReturn.status != SalesReturnStatus.CANCELLED,
            )
            .group_by(SalesReturnLine.dispatch_line_id)
            .subquery()
        )
        remaining = DispatchLine.quantity - func.coalesce(returned.c.quantity, 0)
        return (
            select(DispatchLine, Item.code, Item.name, remaining.label("remaining"))
            .join(
                Dispatch,
                (Dispatch.id == DispatchLine.dispatch_id)
                & (Dispatch.company_id == DispatchLine.company_id),
            )
            .join(
                Item,
                (Item.id == DispatchLine.item_id) & (Item.company_id == DispatchLine.company_id),
            )
            .outerjoin(returned, returned.c.dispatch_line_id == DispatchLine.id)
            .where(
                Dispatch.company_id == self.tenant.company_id,
                Dispatch.customer_id == self.tenant.customer_id,
                Dispatch.dispatch_type == DispatchType.SALES,
                Dispatch.status.in_(RETURNABLE_DISPATCH_STATUSES),
                Item.is_serial_tracked.is_(False),
                remaining > 0,
            )
        )

    async def eligible_dispatches(self, params: PageParams) -> tuple[list[Any], OffsetPagination]:
        eligible_ids = self._returnable_lines().with_only_columns(DispatchLine.dispatch_id)
        rows, page = await paginate(
            self.session,
            self.owned(Dispatch)
            .where(Dispatch.id.in_(eligible_ids))
            .order_by(Dispatch.created_at.desc(), Dispatch.id.desc()),
            params,
        )
        return rows, page

    async def eligible_lines(self, dispatch_ids: list[uuid.UUID]) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    self._returnable_lines()
                    .where(DispatchLine.dispatch_id.in_(dispatch_ids))
                    .order_by(DispatchLine.id)
                )
            ).all()
        )

    async def overdue_invoices(self, on_date: Any) -> list[Any]:
        credits = (
            select(CreditNote.sales_invoice_id, func.sum(CreditNote.total_amount).label("amount"))
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNote.customer_id == self.tenant.customer_id,
                CreditNote.status == CreditNoteStatus.ISSUED,
            )
            .group_by(CreditNote.sales_invoice_id)
            .subquery()
        )
        balance = (
            SalesInvoice.total_amount
            - SalesInvoice.paid_amount
            - func.coalesce(credits.c.amount, 0)
        )
        stmt = (
            select(SalesInvoice, balance.label("balance"))
            .outerjoin(credits, credits.c.sales_invoice_id == SalesInvoice.id)
            .where(
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.customer_id == self.tenant.customer_id,
                SalesInvoice.status != "CANCELLED",
                SalesInvoice.due_date < on_date,
                balance > 0,
            )
            .order_by(SalesInvoice.due_date, SalesInvoice.id)
            .limit(100)
        )
        return list((await self.session.execute(stmt)).all())
