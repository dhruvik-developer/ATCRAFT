"""Tenant-scoped reads for purchase-order revisions and source allocations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.pagination import CursorParams, paginate_keyset
from src.core.responses import CursorPagination
from src.modules.audit.models import AuditLog
from src.modules.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
)


class SourceAllocationRepository(BaseRepository[PurchaseOrderSourceAllocation]):
    model = PurchaseOrderSourceAllocation

    async def list_for_lines(
        self, line_ids: Sequence[uuid.UUID], *, active_only: bool = False
    ) -> list[PurchaseOrderSourceAllocation]:
        if not line_ids:
            return []
        stmt = self._scoped().where(
            PurchaseOrderSourceAllocation.purchase_order_line_id.in_(set(line_ids))
        )
        if active_only:
            stmt = stmt.where(PurchaseOrderSourceAllocation.is_active.is_(True))
        return list(
            (await self.session.execute(stmt.order_by(PurchaseOrderSourceAllocation.id))).scalars()
        )

    async def source_ids_for_order(self, order_id: uuid.UUID) -> frozenset[uuid.UUID]:
        allocation_ids = (
            select(PurchaseOrderSourceAllocation.requisition_line_id)
            .join(
                PurchaseOrderLine,
                and_(
                    PurchaseOrderLine.id == PurchaseOrderSourceAllocation.purchase_order_line_id,
                    PurchaseOrderLine.company_id == PurchaseOrderSourceAllocation.company_id,
                ),
            )
            .where(
                PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
                PurchaseOrderLine.purchase_order_id == order_id,
                PurchaseOrderSourceAllocation.is_active.is_(True),
            )
        )
        legacy_ids = select(PurchaseOrderLine.requisition_line_id).where(
            PurchaseOrderLine.company_id == self.tenant.company_id,
            PurchaseOrderLine.purchase_order_id == order_id,
            PurchaseOrderLine.requisition_line_id.is_not(None),
        )
        values: Sequence[uuid.UUID | None] = (
            (await self.session.execute(union_all(allocation_ids, legacy_ids))).scalars().all()
        )
        return frozenset(value for value in values if value is not None)

    async def ordered_quantities(
        self, requisition_line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        """Count allocations once, with legacy scalar links as a compatibility fallback.

        A line with any durable allocation rows never falls back to its scalar
        projection, including after all its allocations have been released.
        This supports historical imports without counting a merged line twice.
        """
        if not requisition_line_ids:
            return {}
        allocation_rows = select(
            PurchaseOrderSourceAllocation.requisition_line_id.label("source_id"),
            PurchaseOrderSourceAllocation.quantity.label("quantity"),
        ).where(
            PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
            PurchaseOrderSourceAllocation.requisition_line_id.in_(set(requisition_line_ids)),
            PurchaseOrderSourceAllocation.is_active.is_(True),
        )
        has_mapping = exists(
            select(PurchaseOrderSourceAllocation.id).where(
                PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
                PurchaseOrderSourceAllocation.purchase_order_line_id == PurchaseOrderLine.id,
            )
        )
        legacy_rows = (
            select(
                PurchaseOrderLine.requisition_line_id.label("source_id"),
                PurchaseOrderLine.quantity.label("quantity"),
            )
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrderLine.requisition_line_id.in_(set(requisition_line_ids)),
                PurchaseOrder.status.not_in(
                    (PurchaseOrderStatus.CANCELLED, PurchaseOrderStatus.REJECTED)
                ),
                ~has_mapping,
            )
        )
        quantities = union_all(allocation_rows, legacy_rows).subquery()
        stmt = select(quantities.c.source_id, func.sum(quantities.c.quantity)).group_by(
            quantities.c.source_id
        )
        return {
            source_id: Decimal(quantity) for source_id, quantity in await self.session.execute(stmt)
        }

    async def labels(
        self, requisition_line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        if not requisition_line_ids:
            return {}
        stmt = (
            select(
                PurchaseRequisitionLine.id,
                PurchaseRequisitionLine.line_number,
                PurchaseRequisition.code,
            )
            .join(
                PurchaseRequisition,
                and_(
                    PurchaseRequisition.id == PurchaseRequisitionLine.requisition_id,
                    PurchaseRequisition.company_id == PurchaseRequisitionLine.company_id,
                ),
            )
            .where(
                PurchaseRequisitionLine.company_id == self.tenant.company_id,
                PurchaseRequisitionLine.id.in_(set(requisition_line_ids)),
            )
        )
        return {
            row.id: {"requisition_code": row.code, "requisition_line_number": row.line_number}
            for row in await self.session.execute(stmt)
        }


async def has_receipt_history(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> bool:
    """Draft and cancelled GRNs are history too; quantities alone are insufficient."""
    stmt = select(
        exists(
            select(GoodsReceipt.id).where(
                GoodsReceipt.company_id == tenant.company_id,
                GoodsReceipt.purchase_order_id == order_id,
            )
        )
    )
    return bool(await session.scalar(stmt))


async def amendment_history(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    params: CursorParams,
) -> tuple[list[AuditLog], CursorPagination]:
    stmt = select(AuditLog).where(
        AuditLog.company_id == tenant.company_id,
        AuditLog.entity_type == "purchase_order",
        AuditLog.entity_id == order_id,
        AuditLog.context["event"].astext == "purchase_order_amendment",
    )
    return await paginate_keyset(
        session, stmt, params, created_at_col=AuditLog.created_at, id_col=AuditLog.id
    )
