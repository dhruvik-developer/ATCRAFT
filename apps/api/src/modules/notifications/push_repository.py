"""Device persistence, ownership transfer and lifecycle cleanup."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.modules.notifications.models import PushSubscription


class PushSubscriptionRepository(BaseRepository[PushSubscription]):
    model = PushSubscription

    def own(self) -> Any:
        return self._scoped().where(PushSubscription.user_id == self.tenant.user_id)

    async def upsert(self, values: dict[str, Any]) -> PushSubscription:
        statement = insert(PushSubscription).values(values)
        mutable = {
            key: getattr(statement.excluded, key)
            for key in values
            if key not in {"id", "created_at"}
        }
        # A plant PC's endpoint may transfer even across tenants. Return no former owner data.
        returning = (
            statement.on_conflict_do_update(index_elements=["endpoint_hash"], set_=mutable)
            .returning(PushSubscription)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(returning)).scalar_one()

    async def remove(
        self, *, subscription_id: uuid.UUID | None = None, endpoint_hash: str | None = None
    ) -> bool:
        statement = delete(PushSubscription).where(
            PushSubscription.company_id == self.tenant.company_id,
            PushSubscription.user_id == self.tenant.user_id,
        )
        if subscription_id is not None:
            statement = statement.where(PushSubscription.id == subscription_id)
        if endpoint_hash is not None:
            statement = statement.where(PushSubscription.endpoint_hash == endpoint_hash)
        return affected_rows(await self.session.execute(statement)) > 0


async def remove_user_push_devices(
    session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.company_id == company_id, PushSubscription.user_id == user_id
        )
    )
