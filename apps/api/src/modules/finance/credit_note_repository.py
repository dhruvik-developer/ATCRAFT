"""Tenant-scoped credit-note queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.modules.finance.credit_note_rounding import CreditedLineTotals
from src.modules.finance.models import CreditNote, CreditNoteLine, CreditNoteStatus


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
