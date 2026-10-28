"""Batched tenant-scoped receipt and previous credit-cost evidence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.finance.models import (
    CreditNote,
    CreditNoteCostAllocation,
    CreditNoteLine,
    CreditNoteReason,
    CreditNoteStatus,
    SalesInvoiceLine,
)
from src.modules.inventory.models import (
    InventoryCostLayer,
    InventoryTransaction,
    TransactionType,
)
from src.modules.quality.models import QcInspection


class CreditCostRepository:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def receipts(self, return_id: uuid.UUID) -> list[InventoryTransaction]:
        return list(
            (
                await self.session.scalars(
                    select(InventoryTransaction)
                    .where(
                        InventoryTransaction.company_id == self.tenant.company_id,
                        InventoryTransaction.transaction_type == TransactionType.CUSTOMER_RETURN,
                        InventoryTransaction.reference_type == "sales_return",
                        InventoryTransaction.reference_id == return_id,
                    )
                    .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
                )
            ).all()
        )

    async def layers(self, receipt_ids: Sequence[uuid.UUID]) -> list[InventoryCostLayer]:
        if not receipt_ids:
            return []
        return list(
            (
                await self.session.scalars(
                    select(InventoryCostLayer)
                    .where(
                        InventoryCostLayer.company_id == self.tenant.company_id,
                        InventoryCostLayer.source_transaction_id.in_(receipt_ids),
                    )
                    .order_by(InventoryCostLayer.received_at, InventoryCostLayer.id)
                )
            ).all()
        )

    async def inspections(self, ids: Sequence[uuid.UUID]) -> list[QcInspection]:
        if not ids:
            return []
        return list(
            (
                await self.session.scalars(
                    select(QcInspection).where(
                        QcInspection.company_id == self.tenant.company_id,
                        QcInspection.id.in_(ids),
                    )
                )
            ).all()
        )

    async def occupied_ranges(
        self, return_id: uuid.UUID, *, exclude_note_id: uuid.UUID
    ) -> list[CreditNoteCostAllocation]:
        return list(
            (
                await self.session.scalars(
                    select(CreditNoteCostAllocation)
                    .join(
                        CreditNoteLine,
                        CreditNoteLine.id == CreditNoteCostAllocation.credit_note_line_id,
                    )
                    .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
                    .where(
                        CreditNoteCostAllocation.company_id == self.tenant.company_id,
                        CreditNoteLine.company_id == self.tenant.company_id,
                        CreditNote.company_id == self.tenant.company_id,
                        CreditNote.sales_return_id == return_id,
                        CreditNote.status == CreditNoteStatus.ISSUED,
                        CreditNote.id != exclude_note_id,
                    )
                )
            ).all()
        )

    async def unallocated_dispatch_lines(
        self, return_id: uuid.UUID, *, exclude_note_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """An older unallocated credit may already own any of the receipt pieces.

        Excluding the just-created note is essential: its lines have been
        flushed but their immutable allocations have not yet been written.
        """
        allocated = (
            select(
                CreditNoteCostAllocation.credit_note_line_id.label("line_id"),
                func.sum(CreditNoteCostAllocation.quantity).label("quantity"),
            )
            .where(CreditNoteCostAllocation.company_id == self.tenant.company_id)
            .group_by(CreditNoteCostAllocation.credit_note_line_id)
            .subquery()
        )
        rows = await self.session.execute(
            select(SalesInvoiceLine.dispatch_line_id)
            .select_from(CreditNoteLine)
            .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
            .join(SalesInvoiceLine, SalesInvoiceLine.id == CreditNoteLine.sales_invoice_line_id)
            .outerjoin(allocated, allocated.c.line_id == CreditNoteLine.id)
            .where(
                CreditNote.company_id == self.tenant.company_id,
                CreditNoteLine.company_id == self.tenant.company_id,
                SalesInvoiceLine.company_id == self.tenant.company_id,
                CreditNote.sales_return_id == return_id,
                CreditNote.reason_type == CreditNoteReason.RETURN,
                CreditNote.status == CreditNoteStatus.ISSUED,
                CreditNote.id != exclude_note_id,
                func.coalesce(allocated.c.quantity, Decimal("0")) != CreditNoteLine.quantity,
            )
        )
        return set(rows.scalars().all())


__all__ = ["CreditCostRepository"]
