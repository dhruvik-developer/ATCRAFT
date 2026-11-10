"""Costed stock shipped to a customer, and the goods that come back.

``shipped_stock`` posts opening layers at the costs a test names - one serial
per layer when it asks for serialised stock - and dispatches them in one
consignment, split across lines if it says so; ``receive_goods`` raises a return
against a dispatch line and receives it. Every return then has a known original
value to be costed back at. The return-costing suite and the packing, dispatch
and returns HTTP suite both use them; they live here so neither is the other's
library.
"""

from __future__ import annotations

from decimal import Decimal

from src.core.clock import today
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchLineIn, DispatchRequest
from src.modules.inventory.models import TransactionType
from src.modules.inventory.service import MovementRequest, post_movements
from src.modules.sales_returns import service
from src.modules.sales_returns.models import SalesReturnStatus
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn
from tests import seed_support


async def shipped_stock(session, tenant, costs, *, split_quantities=None, serialized=False):
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code="RETURN-FIFO"
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code="RETURN-FIFO",
        can_sell=True,
        is_serial_tracked=serialized,
        standard_cost=Decimal(costs[0][1]),
    )
    total = sum((Decimal(quantity) for quantity, _ in costs), Decimal("0"))
    serial_ids = []
    if serialized:
        for _ in costs:
            serial = await seed_support.seed_serial(
                session, company_id=tenant.company_id, item_id=item.id, warehouse_id=warehouse.id
            )
            serial_ids.append(serial.id)
    await post_movements(
        session,
        tenant,
        [
            MovementRequest(
                item_id=item.id,
                warehouse_id=warehouse.id,
                quantity=Decimal(quantity),
                unit_cost=Decimal(unit_cost),
                serial_id=serial_ids[number] if serialized else None,
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="return_cost_fixture",
                reference_code=str(number),
                reason="Costed stock for customer return regression",
            )
            for number, (quantity, unit_cost) in enumerate(costs)
        ],
    )
    dispatch = await dispatch_service.create_dispatch(
        session,
        tenant,
        DispatchCreate(
            warehouse_id=warehouse.id,
            customer_id=customer.id,
            dispatch_date=today(),
            lines=[
                DispatchLineIn(item_id=item.id, quantity=Decimal(quantity), serial_ids=serial_ids)
                for quantity in (split_quantities or [total])
            ],
        ),
    )
    await dispatch_service.mark_ready(session, tenant, dispatch.id, reason="Loaded for delivery")
    await dispatch_service.dispatch_consignment(
        session, tenant, dispatch.id, DispatchRequest(reason="Goods dispatched to customer")
    )
    return dispatch, item, warehouse


async def receive_goods(session, tenant, dispatch, quantity, *, line_index=0, serial_ids=None):
    returned = await service.create_return(
        session,
        tenant,
        SalesReturnCreate(
            dispatch_id=dispatch.id,
            lines=[
                SalesReturnLineIn(
                    dispatch_line_id=dispatch.lines[line_index].id,
                    quantity=Decimal(quantity),
                    reason_code="SURPLUS",
                    requires_qc=bool(serial_ids),
                    serial_ids=serial_ids or [],
                )
            ],
        ),
    )
    await service.transition_return(session, tenant, returned.id, SalesReturnStatus.RECEIVED)
    return returned


__all__ = ["receive_goods", "shipped_stock"]
