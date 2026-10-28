"""Consignments for tests that hand packed goods to dispatch.

A packing hand-over loads the boxes onto a real consignment: the dispatch module
reads it through its tenant-scoped repository, refuses one that does not exist,
has left or belongs to somebody else, and matches the sealed cartons to its
lines. A test that hands over to ``uuid4()`` therefore exercises nothing but the
refusal - so this raises the consignment the way the dispatch screen does,
through the dispatch service, naming no packing order and no cartons.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import TenantContext
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.models import DispatchType
from src.modules.dispatch.schemas import DispatchCreate, DispatchLineIn


def consignment_line(
    item_id: uuid.UUID,
    quantity: str | int | Decimal,
    *,
    serial_ids: Sequence[uuid.UUID] = (),
    batch_id: uuid.UUID | None = None,
    sales_order_line_id: uuid.UUID | None = None,
) -> DispatchLineIn:
    """One line of a consignment, carrying no cartons - the hand-over assigns them."""
    return DispatchLineIn(
        item_id=item_id,
        quantity=Decimal(str(quantity)),
        serial_ids=list(serial_ids),
        batch_id=batch_id,
        sales_order_line_id=sales_order_line_id,
    )


async def raise_consignment(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    customer_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lines: Sequence[DispatchLineIn],
    sales_order_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A SALES consignment in NOT_DISPATCHED, open for loading. Returns its id."""
    detail = await dispatch_service.create_dispatch(
        session,
        tenant,
        DispatchCreate(
            dispatch_type=DispatchType.SALES,
            warehouse_id=warehouse_id,
            customer_id=customer_id,
            sales_order_id=sales_order_id,
            dispatch_date=today(),
            lines=list(lines),
        ),
    )
    return detail.id


__all__ = ["consignment_line", "raise_consignment"]
