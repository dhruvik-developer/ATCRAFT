"""Queries for sales returns and the dispatch, items and warehouse they come back to.

The service decides whether a return may be raised, received, dispositioned or
closed; this file decides how its rows and its source dispatch's rows are read
and locked. Every lock re-reads (``populate_existing``): a value served from the
identity map is the one loaded before the lock, and the lock would protect it
for nothing.

Lock order, which every writer of a return follows: the return header, then its
lines, then the source dispatch header, then the dispatch lines. Finance's
credit-note issue and cancel take the first two through this repository too, so
a manual credit cannot deadlock a receipt of the same return.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Row

from src.common.repository import BaseRepository
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.dispatch.models import Dispatch, DispatchLine
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.products.models import Item
from src.modules.sales.models import SalesOrder, SalesOrderLine
from src.modules.sales_returns.models import (
    SalesReturn,
    SalesReturnLine,
    SalesReturnStatus,
)
from src.modules.warehouses.models import Warehouse


class SalesReturnRepository(BaseRepository[SalesReturn]):
    model = SalesReturn
    entity_label = "Sales return"
    searchable_fields = ("code", "notes")

    async def lock_for_update(self, return_id: uuid.UUID) -> SalesReturn | None:
        """Row-lock a return's header and re-read it.

        The header is the serialisation point for the whole document: receiving,
        dispositioning units, closing and crediting all pass through it before
        they touch its lines.
        """
        stmt = (
            self._scoped()
            .where(SalesReturn.id == return_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_lines(self, return_id: uuid.UUID) -> list[SalesReturnLine]:
        """Row-lock every line of a return and re-read them, after its header.

        ``(created_at, id)`` is the acquisition order - lines written in one
        transaction share ``created_at``, and the UUIDv7 id recovers the order
        they were written in - which is also the order costing reads them in.
        """
        stmt = (
            select(SalesReturnLine)
            .where(
                SalesReturnLine.company_id == self.tenant.company_id,
                SalesReturnLine.sales_return_id == return_id,
            )
            .order_by(SalesReturnLine.created_at, SalesReturnLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_dispatch(self, dispatch_id: uuid.UUID) -> Dispatch | None:
        """Row-lock the dispatch goods come back from, and re-read it.

        The dispatch header serialises everything that reads or moves its lines'
        returnable pool: a new return claims from it, and a receipt raises
        ``returned_quantity``. Taken before :meth:`lock_dispatch_lines`.
        """
        stmt = (
            select(Dispatch)
            .where(Dispatch.company_id == self.tenant.company_id, Dispatch.id == dispatch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_dispatch_lines(
        self, dispatch_id: uuid.UUID, line_ids: Iterable[uuid.UUID]
    ) -> list[DispatchLine]:
        """Row-lock the named lines of one dispatch, in id order, and re-read them.

        A line of another dispatch is absent from the answer, which is how a
        caller learns it was named. Nothing named, nothing read.
        """
        wanted = list(dict.fromkeys(line_ids))
        if not wanted:
            return []
        stmt = (
            select(DispatchLine)
            .where(
                DispatchLine.company_id == self.tenant.company_id,
                DispatchLine.dispatch_id == dispatch_id,
                DispatchLine.id.in_(wanted),
            )
            .order_by(DispatchLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def items(self, item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Item]:
        """The items dispatch lines carry, by id - retired items included.

        An item retired after it shipped is still the item coming back, so there
        is deliberately no ``deleted_at`` filter. Nothing named, nothing read.
        """
        wanted = list(dict.fromkeys(item_ids))
        if not wanted:
            return {}
        stmt = select(Item).where(Item.company_id == self.tenant.company_id, Item.id.in_(wanted))
        return {item.id: item for item in (await self.session.execute(stmt)).scalars().all()}

    async def order_line_units(self, order_line_ids: Iterable[uuid.UUID]) -> list[Row[Any]]:
        """The unit of each named sales-order line, beside its item's stock unit.

        Each row: ``line_number``, ``uom_id``, ``order_code``, ``item_code`` and
        ``stock_uom_id`` - what the stock-unit rule reads about the order lines
        a dispatch shipped (docs/assumptions.md §UOM-1). One statement, in
        order-line id order; nothing named, nothing read.
        """
        wanted = list(dict.fromkeys(order_line_ids))
        if not wanted:
            return []
        stmt = (
            select(
                SalesOrderLine.line_number,
                SalesOrderLine.uom_id,
                SalesOrder.code.label("order_code"),
                Item.code.label("item_code"),
                Item.base_uom_id.label("stock_uom_id"),
            )
            .join(
                SalesOrder,
                and_(
                    SalesOrder.id == SalesOrderLine.sales_order_id,
                    SalesOrder.company_id == SalesOrderLine.company_id,
                ),
            )
            .join(
                Item,
                and_(
                    Item.id == SalesOrderLine.item_id, Item.company_id == SalesOrderLine.company_id
                ),
            )
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrderLine.id.in_(wanted),
            )
            .order_by(SalesOrderLine.id)
        )
        return list((await self.session.execute(stmt)).all())

    async def active_warehouse_exists(self, warehouse_id: uuid.UUID) -> bool:
        """Whether this company has an active warehouse with this id."""
        stmt = select(
            select(Warehouse.id)
            .where(
                Warehouse.company_id == self.tenant.company_id,
                Warehouse.id == warehouse_id,
                Warehouse.is_active.is_(True),
            )
            .exists()
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    async def awaiting_quality_verdict_ids(
        self, *, after: uuid.UUID | None, limit: int
    ) -> list[uuid.UUID]:
        """Received returns with an inspection, in id order after ``after``.

        One keyset page of the quality owner sweep's candidates; the next page
        starts after the last id this one returned.
        """
        stmt = (
            select(SalesReturn.id)
            .join(SalesReturnLine, SalesReturnLine.sales_return_id == SalesReturn.id)
            .where(
                SalesReturn.company_id == self.tenant.company_id,
                SalesReturn.status == SalesReturnStatus.RECEIVED,
                SalesReturnLine.qc_inspection_id.is_not(None),
            )
            .distinct()
            .order_by(SalesReturn.id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(SalesReturn.id > after)
        return list((await self.session.execute(stmt)).scalars().all())

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
