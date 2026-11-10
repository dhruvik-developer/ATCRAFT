"""Stock, bins, departments and scrap for tests of inventory operations.

``_stock`` makes an item with twenty units at the unbinned position of a new
warehouse, received through the real engine so the cost layers are real;
``_receive`` posts more, ``_bin`` and ``_department`` add the places a movement
names, and ``_scrap`` moves available stock to scrap. ``unique_code`` is the
conftest fixture of that name, passed in.

The service suite and the HTTP suites for inventory operations, routes and
document warehouse scope all build their stock this way; the builders live here
so none of those files is another's library. They keep the names they had there.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.service import change_stock_state, post_movement
from src.modules.organization.models import Department
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests.seed_support import seed_item, seed_warehouse


async def _receive(
    session,
    tenant,
    item,
    warehouse,
    quantity: str,
    cost: str,
    *,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> None:
    await post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        location_id=location_id,
        batch_id=batch_id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="test",
        reference_id=None,
        reference_code=None,
        reason="Opening test stock",
        unit_cost=Decimal(cost),
    )


async def _stock(session, tenant, unique_code, *, cost: str = "12", **item_fields):
    """An item with 20 units at ``cost`` at the unbinned position of a new warehouse."""
    item = await seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("IT"),
        standard_cost=Decimal(cost),
        **item_fields,
    )
    warehouse = await seed_warehouse(session, company_id=tenant.company_id, code=unique_code("WH"))
    await _receive(session, tenant, item, warehouse, "20", cost)
    return item, warehouse


async def _bin(session, company_id: uuid.UUID, warehouse, unique_code, **fields) -> StorageLocation:
    row = StorageLocation(
        company_id=company_id,
        warehouse_id=warehouse.id,
        code=unique_code("BN"),
        name="Test bin",
        location_type=StorageLocationType.BIN,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def _department(session, tenant, unique_code, **fields) -> Department:
    row = Department(company_id=tenant.company_id, code=unique_code("DP"), name="Maint", **fields)
    session.add(row)
    await session.flush()
    return row


async def _scrap(session, tenant, item, warehouse, quantity: str) -> None:
    await change_stock_state(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal(quantity),
        from_state=StockState.AVAILABLE,
        to_state=StockState.SCRAP,
        reason="Damaged beyond repair",
    )


__all__ = ["_bin", "_department", "_receive", "_scrap", "_stock"]
