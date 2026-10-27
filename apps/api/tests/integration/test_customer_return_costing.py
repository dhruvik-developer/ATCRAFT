"""Customer returns retain the original dispatch's FIFO value."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.models import DispatchLine
from src.modules.dispatch.schemas import DispatchCreate, DispatchLineIn, DispatchRequest
from src.modules.inventory.models import InventoryBalance, InventoryTransaction, TransactionType
from src.modules.inventory.repository import CostConsumptionRepository
from src.modules.inventory.service import MovementRequest, post_movements
from src.modules.sales_returns import service
from src.modules.sales_returns.models import SalesReturn, SalesReturnStatus
from src.modules.sales_returns.repository import SalesReturnRepository
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn
from tests import seed_support

pytestmark = pytest.mark.integration


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


@pytest.mark.parametrize("original_cost", ["10", "0"])
async def test_return_ignores_a_later_standard_cost_change(session, tenant, original_cost):
    dispatch, item, warehouse = await shipped_stock(session, tenant, [("10", original_cost)])
    item_id, warehouse_id = item.id, warehouse.id
    item.standard_cost = Decimal("99")
    await session.flush()
    returned = await receive_goods(session, tenant, dispatch, "10")
    value = await session.scalar(
        select(InventoryTransaction.total_cost).where(
            InventoryTransaction.company_id == tenant.company_id,
            InventoryTransaction.reference_type == "sales_return",
            InventoryTransaction.reference_id == returned.id,
            InventoryTransaction.transaction_type == TransactionType.CUSTOMER_RETURN,
        )
    )
    assert value == Decimal(original_cost) * Decimal("10")
    balance = (
        await session.execute(
            select(InventoryBalance).where(
                InventoryBalance.company_id == tenant.company_id,
                InventoryBalance.item_id == item_id,
                InventoryBalance.warehouse_id == warehouse_id,
            )
        )
    ).scalar_one()
    assert balance.stock_value == Decimal(original_cost) * Decimal("10")
    assert balance.on_hand_quantity == Decimal("10")


async def test_partial_returns_restore_each_original_layer_once(session, tenant):
    dispatch, item, warehouse = await shipped_stock(session, tenant, [("10", "10"), ("10", "20")])
    item_id, warehouse_id = item.id, warehouse.id
    item.standard_cost = Decimal("99")
    await session.flush()
    await receive_goods(session, tenant, dispatch, "5")
    first_value = await session.scalar(
        select(InventoryBalance.stock_value).where(
            InventoryBalance.company_id == tenant.company_id,
            InventoryBalance.item_id == item_id,
            InventoryBalance.warehouse_id == warehouse_id,
        )
    )
    assert first_value == Decimal("50")
    await receive_goods(session, tenant, dispatch, "15")
    final_value = await session.scalar(
        select(InventoryBalance.stock_value).where(
            InventoryBalance.company_id == tenant.company_id,
            InventoryBalance.item_id == item_id,
            InventoryBalance.warehouse_id == warehouse_id,
        )
    )
    assert final_value == Decimal("300")


@pytest.mark.parametrize("second_anchor_missing", [False, True])
async def test_identical_items_on_separate_lines_keep_their_known_movement_costs(
    session, tenant, second_anchor_missing
):
    dispatch, item, warehouse = await shipped_stock(
        session, tenant, [("10", "10"), ("10", "20")], split_quantities=["10", "10"]
    )
    item_id, warehouse_id = item.id, warehouse.id
    if second_anchor_missing:
        # Older reservation-based dispatches did not persist an anchor. The
        # other line's known movement must remain outside this fallback queue.
        source = await session.get(DispatchLine, dispatch.lines[1].id)
        source.inventory_transaction_id = None
    item.standard_cost = Decimal("99")
    await session.flush()
    returned = await receive_goods(session, tenant, dispatch, "10", line_index=1)
    # The second source line consumed the second layer; it must not restore
    # the first line's cheaper units just because the item and batch match.
    assert await session.scalar(
        select(InventoryTransaction.total_cost).where(
            InventoryTransaction.reference_type == "sales_return",
            InventoryTransaction.reference_id == returned.id,
            InventoryTransaction.transaction_type == TransactionType.CUSTOMER_RETURN,
        )
    ) == Decimal("200")
    assert await session.scalar(
        select(InventoryBalance.stock_value).where(
            InventoryBalance.item_id == item_id, InventoryBalance.warehouse_id == warehouse_id
        )
    ) == Decimal("200")


async def test_unanchored_lines_share_only_their_original_fifo_queue(session, tenant):
    dispatch, item, warehouse = await shipped_stock(
        session, tenant, [("10", "10"), ("10", "20")], split_quantities=["10", "10"]
    )
    item_id, warehouse_id = item.id, warehouse.id
    source_lines = (
        (await session.execute(select(DispatchLine).where(DispatchLine.dispatch_id == dispatch.id)))
        .scalars()
        .all()
    )
    for source in source_lines:
        source.inventory_transaction_id = None
    item.standard_cost = Decimal("99")
    await session.flush()
    await receive_goods(session, tenant, dispatch, "5")
    await receive_goods(session, tenant, dispatch, "10", line_index=1)
    await receive_goods(session, tenant, dispatch, "5")
    assert await session.scalar(
        select(InventoryBalance.stock_value).where(
            InventoryBalance.item_id == item_id, InventoryBalance.warehouse_id == warehouse_id
        )
    ) == Decimal("300")


@pytest.mark.parametrize("selected,expected_cost", [(0, "0"), (1, "20")])
async def test_serial_returns_restore_the_selected_units_cost_including_zero(
    session, tenant, selected, expected_cost
):
    dispatch, item, _ = await shipped_stock(
        session, tenant, [("1", "0"), ("1", "20")], serialized=True
    )
    item.standard_cost = Decimal("99")
    await session.flush()
    returned = await receive_goods(
        session, tenant, dispatch, "1", serial_ids=[dispatch.lines[0].serial_ids[selected]]
    )
    assert await session.scalar(
        select(InventoryTransaction.total_cost).where(
            InventoryTransaction.company_id == tenant.company_id,
            InventoryTransaction.reference_type == "sales_return",
            InventoryTransaction.reference_id == returned.id,
            InventoryTransaction.transaction_type == TransactionType.CUSTOMER_RETURN,
        )
    ) == Decimal(expected_cost)


@pytest.mark.parametrize("original_cost", ["0", "10"])
async def test_legacy_without_layer_breakdown_uses_the_original_ledger_cost(
    session, tenant, monkeypatch, original_cost
):
    dispatch, item, _ = await shipped_stock(session, tenant, [("10", original_cost)])
    item.standard_cost = Decimal("99")
    await session.flush()

    async def legacy_breakdown(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(CostConsumptionRepository, "by_transaction", legacy_breakdown)
    returned = await receive_goods(session, tenant, dispatch, "10")
    assert await session.scalar(
        select(InventoryTransaction.total_cost).where(
            InventoryTransaction.reference_type == "sales_return",
            InventoryTransaction.reference_id == returned.id,
            InventoryTransaction.transaction_type == TransactionType.CUSTOMER_RETURN,
        )
    ) == Decimal(original_cost) * Decimal("10")


async def test_missing_dispatch_history_refuses_without_posting_or_advancing_state(
    session, tenant, monkeypatch
):
    dispatch, _, _ = await shipped_stock(session, tenant, [("10", "10")])
    before = await session.scalar(select(func.count()).select_from(InventoryTransaction))

    async def missing_history(*_args, **_kwargs):
        return []

    monkeypatch.setattr(SalesReturnRepository, "dispatch_cost_movements", missing_history)
    with pytest.raises(BusinessRuleError, match="original dispatch cost could not be verified"):
        await receive_goods(session, tenant, dispatch, "10")
    assert await session.scalar(select(func.count()).select_from(InventoryTransaction)) == before
    assert (
        await session.scalar(
            select(SalesReturn.status).where(SalesReturn.dispatch_id == dispatch.id)
        )
        == SalesReturnStatus.DRAFT
    )


async def test_a_received_return_cannot_restore_its_cost_a_second_time(session, tenant):
    dispatch, _, _ = await shipped_stock(session, tenant, [("10", "10")])
    returned = await receive_goods(session, tenant, dispatch, "10")
    before = await session.scalar(select(func.count()).select_from(InventoryTransaction))
    with pytest.raises(InvalidStateTransitionError):
        await service.transition_return(session, tenant, returned.id, SalesReturnStatus.RECEIVED)
    assert await session.scalar(select(func.count()).select_from(InventoryTransaction)) == before
