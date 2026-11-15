"""Tenant-scoped credit-note queries.

Also the two reads that tie a credit to a sales return - which invoice lines
bill the returned dispatch lines, and how much of each the return's notes
already credit - because both answer a crediting question in finance's tables.
The return's own rows are locked through
:class:`~src.modules.sales_returns.repository.SalesReturnRepository`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.modules.finance.credit_note_rounding import CreditedLineTotals
from src.modules.finance.models import (
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
)
from src.modules.sales_returns.models import SalesReturn


class CreditNoteRepository(BaseRepository[CreditNote]):
    model = CreditNote
    entity_label = "Credit note"
    searchable_fields = ("code", "buyer_name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": CreditNote.code,
        "note_date": CreditNote.note_date,
        "total_amount": CreditNote.total_amount,
        "status": CreditNote.status,
    }

    async def lines(self, note_id: uuid.UUID) -> list[CreditNoteLine]:
        """A note's lines in the order they were written."""
        stmt = (
            select(CreditNoteLine)
            .where(
                CreditNoteLine.company_id == self.tenant.company_id,
                CreditNoteLine.credit_note_id == note_id,
            )
            .order_by(CreditNoteLine.created_at, CreditNoteLine.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_update(self, note_id: uuid.UUID) -> CreditNote | None:
        """Row-lock a note and re-read it before cancelling it."""
        stmt = (
            self._scoped()
            .where(CreditNote.id == note_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def issued_return_quantities(self, return_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
        """Quantity the return's issued notes credit, per dispatch line, in one statement.

        A dispatch line no issued note credits is absent.
        """
        stmt = (
            select(SalesInvoiceLine.dispatch_line_id, func.sum(CreditNoteLine.quantity))
            .join(CreditNoteLine, CreditNoteLine.sales_invoice_line_id == SalesInvoiceLine.id)
            .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNoteLine.company_id == self.tenant.company_id,
                SalesInvoiceLine.company_id == self.tenant.company_id,
                CreditNote.sales_return_id == return_id,
                CreditNote.status == CreditNoteStatus.ISSUED,
            )
            .group_by(SalesInvoiceLine.dispatch_line_id)
        )
        return {
            dispatch_line_id: Decimal(quantity or 0)
            for dispatch_line_id, quantity in (await self.session.execute(stmt)).all()
        }

    async def return_credited_by_invoice_line(
        self, return_id: uuid.UUID
    ) -> dict[uuid.UUID, Decimal]:
        """Quantity the return's live notes already credit, per invoice line.

        An amount-only line credits no quantity and counts as zero. An invoice
        line no live note credits is absent.
        """
        stmt = (
            select(
                CreditNoteLine.sales_invoice_line_id,
                func.coalesce(func.sum(CreditNoteLine.quantity), 0),
            )
            .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNote.sales_return_id == return_id,
                CreditNote.status != CreditNoteStatus.CANCELLED,
            )
            .group_by(CreditNoteLine.sales_invoice_line_id)
        )
        return {
            invoice_line_id: Decimal(quantity)
            for invoice_line_id, quantity in (await self.session.execute(stmt)).all()
        }

    async def creditable_invoice_lines(
        self, return_id: uuid.UUID, dispatch_line_ids: Iterable[uuid.UUID]
    ) -> list[SalesInvoiceLine]:
        """The live invoice lines that bill these dispatch lines of a return.

        Only an invoice issued no later than the return was received: goods
        received earlier were already left out of the billed quantity when that
        invoice was issued, so there is nothing of theirs to credit. Nothing
        named, nothing read.
        """
        wanted = list(dict.fromkeys(dispatch_line_ids))
        if not wanted:
            return []
        stmt = (
            select(SalesInvoiceLine)
            .join(SalesInvoice, SalesInvoice.id == SalesInvoiceLine.sales_invoice_id)
            .join(SalesReturn, SalesReturn.dispatch_id == SalesInvoice.dispatch_id)
            .where(
                SalesInvoiceLine.company_id == self.tenant.company_id,
                SalesInvoiceLine.dispatch_line_id.in_(wanted),
                SalesInvoice.company_id == self.tenant.company_id,
                SalesInvoice.status != SalesInvoiceStatus.CANCELLED,
                SalesReturn.company_id == self.tenant.company_id,
                SalesReturn.id == return_id,
                SalesReturn.received_at >= SalesInvoice.issued_at,
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def issued_line_totals(
        self, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, CreditedLineTotals]:
        if not ids:
            return {}
        stmt = (
            select(
                CreditNoteLine.sales_invoice_line_id,
                func.coalesce(func.sum(CreditNoteLine.quantity), 0),
                func.sum(CreditNoteLine.taxable_amount),
                func.sum(CreditNoteLine.cgst_amount),
                func.sum(CreditNoteLine.sgst_amount),
                func.sum(CreditNoteLine.igst_amount),
            )
            .join(
                CreditNote,
                (CreditNote.id == CreditNoteLine.credit_note_id)
                & (CreditNote.company_id == CreditNoteLine.company_id),
            )
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNote.status == CreditNoteStatus.ISSUED,
                CreditNoteLine.sales_invoice_line_id.in_(ids),
            )
            .group_by(CreditNoteLine.sales_invoice_line_id)
        )
        return {
            row[0]: CreditedLineTotals(*row[1:]) for row in (await self.session.execute(stmt)).all()
        }

    async def issued_invoice_totals(self, invoice_id: uuid.UUID) -> tuple[Decimal, Decimal]:
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(CreditNote.total_amount), 0),
                    func.coalesce(func.sum(CreditNote.round_off_amount), 0),
                ).where(
                    CreditNote.company_id == self.tenant.company_id,
                    CreditNote.sales_invoice_id == invoice_id,
                    CreditNote.status == CreditNoteStatus.ISSUED,
                )
            )
        ).one()
        return row[0], row[1]
