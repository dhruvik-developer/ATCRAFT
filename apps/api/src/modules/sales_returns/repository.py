from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import and_, func, or_, select

from src.common.repository import BaseRepository
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.dispatch.models import DispatchLine
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.sales_returns.models import (
    SalesReturn,
    SalesReturnLine,
    SalesReturnStatus,
)


class SalesReturnRepository(BaseRepository[SalesReturn]):
    model = SalesReturn
    entity_label = "Sales return"
    searchable_fields = ("code", "notes")

    async def dispatch_cost_movements(
        self,
        dispatch_id: uuid.UUID,
        *,
        ordinary_item_ids: set[uuid.UUID],
        serial_ids: set[uuid.UUID],
    ) -> list[InventoryTransaction]:
        """Original issue values, including reservation-consumed movements.

        A dispatch line's anchor may name only the first serial movement, and
        reserved lines need not have an anchor. The ledger's source reference
        is authoritative. Limit serial reads to the units actually returning.
        """
        if not ordinary_item_ids and not serial_ids:
            return []
        stmt = (
            select(InventoryTransaction)
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.reference_type == "dispatch",
                InventoryTransaction.reference_id == dispatch_id,
                InventoryTransaction.transaction_type == TransactionType.DISPATCH,
                or_(
                    and_(
                        InventoryTransaction.serial_id.is_(None),
                        InventoryTransaction.item_id.in_(ordinary_item_ids),
                    ),
                    InventoryTransaction.serial_id.in_(serial_ids),
                ),
            )
            .order_by(
                InventoryTransaction.occurred_at,
                InventoryTransaction.created_at,
                InventoryTransaction.id,
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def dispatch_cost_lines(
        self, dispatch_id: uuid.UUID, item_ids: set[uuid.UUID]
    ) -> list[DispatchLine]:
        """Counter snapshots for the original fungible item/batch cost queues.

        The caller already holds the dispatch header lock, which serializes
        receipts from that dispatch. Refresh previously loaded counter values.
        """
        if not item_ids:
            return []
        stmt = (
            select(DispatchLine)
            .where(
                DispatchLine.company_id == self.tenant.company_id,
                DispatchLine.dispatch_id == dispatch_id,
                DispatchLine.item_id.in_(item_ids),
            )
            .order_by(DispatchLine.created_at, DispatchLine.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def claimed_quantities(
        self, dispatch_line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        """What open returns already claim against each dispatch line, in one statement.

        Every return that is not cancelled counts - a DRAFT or RECEIVED request
        consumes the returnable pool before ``returned_quantity`` moves, which
        happens only on receipt. A line nothing has claimed is absent.
        """
        wanted = list(dict.fromkeys(dispatch_line_ids))
        if not wanted:
            return {}
        stmt = (
            select(SalesReturnLine.dispatch_line_id, func.sum(SalesReturnLine.quantity))
            .join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id)
            .where(
                SalesReturnLine.company_id == self.tenant.company_id,
                SalesReturnLine.dispatch_line_id.in_(wanted),
                SalesReturn.company_id == self.tenant.company_id,
                SalesReturn.status != SalesReturnStatus.CANCELLED,
            )
            .group_by(SalesReturnLine.dispatch_line_id)
        )
        return {row[0]: Decimal(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def claimed_serials(
        self, dispatch_line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        """The serials open returns already name, per dispatch line, in one statement."""
        wanted = list(dict.fromkeys(dispatch_line_ids))
        if not wanted:
            return {}
        stmt = (
            select(SalesReturnLine.dispatch_line_id, SalesReturnLine.serial_ids)
            .join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id)
            .where(
                SalesReturnLine.company_id == self.tenant.company_id,
                SalesReturnLine.dispatch_line_id.in_(wanted),
                SalesReturn.company_id == self.tenant.company_id,
                SalesReturn.status != SalesReturnStatus.CANCELLED,
            )
        )
        claimed: dict[uuid.UUID, set[uuid.UUID]] = {}
        for dispatch_line_id, serial_ids in (await self.session.execute(stmt)).all():
            claimed.setdefault(dispatch_line_id, set()).update(serial_ids or ())
        return claimed

    async def lines(self, return_id: uuid.UUID) -> list[SalesReturnLine]:
        stmt = (
            select(SalesReturnLine)
            .where(
                SalesReturnLine.company_id == self.tenant.company_id,
                SalesReturnLine.sales_return_id == return_id,
            )
            .order_by(SalesReturnLine.created_at, SalesReturnLine.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page_for_tenant(
        self, params: PageParams
    ) -> tuple[list[SalesReturn], OffsetPagination]:
        return await self.list_page(params)
