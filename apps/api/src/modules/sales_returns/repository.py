from __future__ import annotations

import uuid

from sqlalchemy import select

from src.common.repository import BaseRepository
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine


class SalesReturnRepository(BaseRepository[SalesReturn]):
    model = SalesReturn
    entity_label = "Sales return"
    searchable_fields = ("code", "notes")

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
