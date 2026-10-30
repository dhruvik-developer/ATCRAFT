"""Batched, tenant-scoped resolution of procurement's causal demand identities."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.mrp.models import MrpDemand, MrpDemandSource, MrpRun
from src.modules.sales.models import SalesOrder, SalesOrderLine


class DemandReferenceRepository:
    """Resolve document ownership without loading commercial terms or ORM graphs."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def sales_orders(self, order_ids: Collection[uuid.UUID]) -> frozenset[uuid.UUID]:
        if not order_ids:
            return frozenset()
        stmt = select(SalesOrder.id).where(
            SalesOrder.company_id == self.tenant.company_id,
            SalesOrder.id.in_(sorted(set(order_ids))),
        )
        return frozenset((await self.session.execute(stmt)).scalars())

    async def sales_order_lines(
        self, line_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        if not line_ids:
            return {}
        stmt = select(SalesOrderLine.id, SalesOrderLine.sales_order_id).where(
            SalesOrderLine.company_id == self.tenant.company_id,
            SalesOrderLine.id.in_(sorted(set(line_ids))),
        )
        return dict((await self.session.execute(stmt)).tuples().all())

    async def manual_mrp_demands(self, demand_ids: Collection[uuid.UUID]) -> frozenset[uuid.UUID]:
        if not demand_ids:
            return frozenset()
        stmt = (
            select(MrpDemand.id)
            .join(
                MrpRun,
                and_(MrpRun.id == MrpDemand.run_id, MrpRun.company_id == MrpDemand.company_id),
            )
            .where(
                MrpDemand.company_id == self.tenant.company_id,
                MrpRun.company_id == self.tenant.company_id,
                MrpDemand.id.in_(sorted(set(demand_ids))),
                MrpDemand.demand_source == MrpDemandSource.MANUAL,
            )
        )
        return frozenset((await self.session.execute(stmt)).scalars())

    async def items(self, item_ids: Collection[uuid.UUID]) -> frozenset[uuid.UUID]:
        """Items named by a reorder-point demand reference."""
        if not item_ids:
            return frozenset()
        from src.modules.products.models import Item

        stmt = select(Item.id).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(sorted(set(item_ids))),
        )
        return frozenset((await self.session.execute(stmt)).scalars())
