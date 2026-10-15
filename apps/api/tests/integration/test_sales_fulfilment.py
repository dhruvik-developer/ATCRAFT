"""Sales-order fulfilment on real PostgreSQL: K01, K08 and the allocation top-up.

WP E1a. Everything goes through the real sales, allocation, inventory and
dispatch services, because what is proved here is the hand-off between them:

* a consignment raised the way the dispatch screen raises it consumes the
  reservation it was raised for, moves the order line and walks the order (K01) -
  the defect was that it shipped free stock beside the reservation instead;
* a partly reserved order ships what it has reserved and waits for the rest,
  which stock arriving later then covers (K08, the proposal's 500/200 case);
* the scheduled top-up reserves arrived stock for waiting orders, most urgent
  first, bounded, with one order's failure isolated from the rest;
* ALLOCATED keeps meaning "fully reserved" when a quantity grows or shrinks;
* a reservation released elsewhere is no reservation, on every path.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError, ValidationError
from src.core.ids import SYSTEM_ACTOR_ID
from src.modules.allocation import service as allocation
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import Customer
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest, DispatchResult
from src.modules.inventory import service as inventory
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    SerialStatus,
    StockSerial,
    TransactionType,
)
from src.modules.inventory.schemas import StockSerialCreate
from src.modules.products.models import Item, ItemGroup, UnitOfMeasure
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn, SalesOrderUpdate
from src.modules.warehouses.models import Warehouse, WarehouseType
from src.workers.tasks import maintenance
from tests import seed_support

pytestmark = pytest.mark.integration


# -----------------------------------------------------------------------------
# Bench
# -----------------------------------------------------------------------------


@dataclass
class Bench:
    session: AsyncSession
    tenant: TenantContext
    principal: Principal
    uom: UnitOfMeasure
    item: Item
    warehouse: Warehouse
    customer: Customer


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
async def bench(
    session: AsyncSession, tenant: TenantContext, principal: Principal, company: Any
) -> Bench:
    # The seller's GST state: a taxed order cannot be priced without it.
    company.state_code = "27"
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("FG"),
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
        sale_price=Decimal("100"),
        gst_rate=Decimal("18"),
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=_code("WH"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    return Bench(session, tenant, principal, uom, item, warehouse, customer)


async def receive(b: Bench, quantity: str, *, item: Item | None = None) -> None:
    """Stock arriving the way a goods receipt books it."""
    await inventory.post_movement(
        b.session,
        b.tenant,
        item_id=(item or b.item).id,
        warehouse_id=b.warehouse.id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.PURCHASE_RECEIPT,
        reference_type="goods_receipt",
        reference_id=uuid.uuid4(),
        reference_code=_code("GRN"),
        reason="Goods received against a purchase order",
        unit_cost=Decimal("60"),
    )


async def raise_order(
    b: Bench, quantity: str, *, item: Item | None = None, priority: int = 5
) -> tuple[SalesOrder, SalesOrderLine]:
    """A priced order, confirmed - which reserves whatever is free."""
    order, lines = await sales.create_order(
        b.session,
        b.tenant,
        b.principal,
        SalesOrderCreate(
            customer_id=b.customer.id,
            warehouse_id=b.warehouse.id,
            required_date=today() + timedelta(days=10),
            priority=priority,
            lines=[
                SalesOrderLineIn(
                    line_number=1,
                    item_id=(item or b.item).id,
                    quantity=Decimal(quantity),
                    uom_id=b.uom.id,
                )
            ],
        ),
    )
    await sales.confirm_order(
        b.session, b.tenant, b.principal, order.id, reason="Customer order accepted"
    )
    return order, lines[0]


def ui_payload(
    b: Bench, order: SalesOrder, lines: list[tuple[SalesOrderLine, str, list[str]]]
) -> DispatchCreate:
    """The body CreateDispatchDialog sends, field for field, as JSON carries it."""
    return DispatchCreate.model_validate(
        {
            "dispatch_type": "SALES",
            "warehouse_id": str(b.warehouse.id),
            "customer_id": str(b.customer.id),
            "vendor_id": None,
            "sales_order_id": str(order.id),
            "dispatch_date": today().isoformat(),
            "expected_delivery_date": None,
            "transporter_id": None,
            "courier_name": None,
            "transport_name": None,
            "tracking_id": None,
            "lr_number": None,
            "vehicle_number": "MH12AB1234",
            "driver_name": None,
            "driver_mobile": None,
            "eway_bill_number": None,
            "total_boxes": None,
            "total_weight_kg": None,
            "freight_amount": None,
            "freight_paid_by": None,
            "notes": None,
            "lines": [
                {
                    "item_id": str(line.item_id),
                    "sales_order_line_id": str(line.id),
                    "quantity": quantity,
                    "serial_ids": serials,
                    "carton_ids": [],
                }
                for line, quantity, serials in lines
            ],
        }
    )


async def ship(b: Bench, payload: DispatchCreate) -> DispatchResult:
    detail = await dispatch_service.create_dispatch(b.session, b.tenant, payload)
    await dispatch_service.mark_ready(b.session, b.tenant, detail.id, reason="Loaded")
    return await dispatch_service.dispatch_consignment(
        b.session, b.tenant, detail.id, DispatchRequest(reason="Handed to the carrier")
    )


async def fresh(b: Bench, model: Any, row_id: uuid.UUID) -> Any:
    stmt = select(model).where(model.id == row_id).execution_options(populate_existing=True)
    return (await b.session.execute(stmt)).scalar_one()


async def balance(b: Bench, item: Item | None = None) -> InventoryBalance:
    stmt = (
        select(InventoryBalance)
        .where(
            InventoryBalance.company_id == b.tenant.company_id,
            InventoryBalance.item_id == (item or b.item).id,
            InventoryBalance.warehouse_id == b.warehouse.id,
            InventoryBalance.batch_id.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    return (await b.session.execute(stmt)).scalar_one()


# -----------------------------------------------------------------------------
# K01 - a direct finished-goods sale, through the screen's payload
# -----------------------------------------------------------------------------


async def test_k01_a_ui_shaped_consignment_consumes_the_reservation_it_was_raised_for(
    bench: Bench,
) -> None:
    b = bench
    # Exactly the stock the order reserves, and no more: the old defect shipped
    # free stock beside the reservation and so was refused here outright.
    await receive(b, "100")
    order, line = await raise_order(b, "100")
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED
    reservation_id = line.reservation_id

    result = await ship(b, ui_payload(b, order, [(line, "100", [])]))

    assert result.consumed_reservation_ids == [reservation_id]
    assert result.sales_order_status == SalesOrderStatus.DISPATCHED
    reservation = await fresh(b, StockReservation, reservation_id)
    assert reservation.status is ReservationStatus.CONSUMED
    assert reservation.consumed_quantity == Decimal("100.0000")
    shipped = await fresh(b, SalesOrderLine, line.id)
    assert shipped.dispatched_quantity == Decimal("100.0000")
    assert shipped.allocated_quantity == Decimal("0.0000")
    stock = await balance(b)
    assert stock.on_hand_quantity == Decimal("0.0000")
    assert stock.reserved_quantity == Decimal("0.0000")


# -----------------------------------------------------------------------------
# K08 - partial dispatch of a partly reserved order (500 ordered, 200 reserved)
# -----------------------------------------------------------------------------


async def test_k08_a_partly_reserved_order_ships_what_it_has_and_the_balance_after_a_receipt(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "200")
    order, line = await raise_order(b, "500")
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.CONFIRMED

    view = await sales.allocation_status(b.session, b.tenant, order.id)
    assert view.lines[0].reserved_quantity == Decimal("200.0000")
    assert view.lines[0].dispatchable_quantity == Decimal("200.0000")
    assert view.lines[0].shortfall_quantity == Decimal("300.0000")

    first = await ship(b, ui_payload(b, order, [(line, "200", [])]))
    assert first.sales_order_status == SalesOrderStatus.PARTIALLY_DISPATCHED
    assert (await fresh(b, SalesOrderLine, line.id)).dispatched_quantity == Decimal("200.0000")

    # The balance of 300 is still open, and nothing more may leave until it is
    # reserved: shipping unreserved stock against the order is not allowed.
    view = await sales.allocation_status(b.session, b.tenant, order.id)
    assert view.lines[0].dispatchable_quantity == Decimal("0.0000")
    assert view.lines[0].shortfall_quantity == Decimal("300.0000")
    with pytest.raises(ValidationError, match="no live stock reservation"):
        await dispatch_service.create_dispatch(
            b.session, b.tenant, ui_payload(b, order, [(line, "1", [])])
        )

    # The receipt arrives; the top-up reserves it for the waiting order.
    await receive(b, "300")
    view = await sales.allocate_order(b.session, b.tenant, order.id, reason="Stock arrived")
    assert view.lines[0].reserved_quantity == Decimal("300.0000")
    assert view.lines[0].dispatchable_quantity == Decimal("300.0000")
    assert view.fully_allocated is True
    # Part-shipped stays part-shipped; its next step is the consignment.
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.PARTIALLY_DISPATCHED

    second = await ship(b, ui_payload(b, order, [(line, "300", [])]))
    assert second.sales_order_status == SalesOrderStatus.DISPATCHED
    assert (await fresh(b, SalesOrderLine, line.id)).dispatched_quantity == Decimal("500.0000")


async def test_a_manual_top_up_with_nothing_newly_free_changes_nothing_and_audits_nothing(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "30")
    order, _ = await raise_order(b, "50")
    before = (
        await b.session.execute(select(AuditLog.id).where(AuditLog.entity_id == order.id))
    ).all()

    view = await sales.allocate_order(b.session, b.tenant, order.id, reason="Anything new?")

    assert view.lines[0].reserved_quantity == Decimal("30.0000")
    assert view.lines[0].shortfall_quantity == Decimal("20.0000")
    after = (
        await b.session.execute(select(AuditLog.id).where(AuditLog.entity_id == order.id))
    ).all()
    assert after == before


async def test_a_top_up_is_refused_on_an_order_that_cannot_ship(bench: Bench) -> None:
    b = bench
    order, _ = await sales.create_order(
        b.session,
        b.tenant,
        b.principal,
        SalesOrderCreate(
            customer_id=b.customer.id,
            warehouse_id=b.warehouse.id,
            required_date=today() + timedelta(days=5),
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=b.item.id, quantity=Decimal("5"), uom_id=b.uom.id
                )
            ],
        ),
    )
    with pytest.raises(BusinessRuleError, match="DRAFT"):
        await sales.allocate_order(b.session, b.tenant, order.id, reason="Too early")


# -----------------------------------------------------------------------------
# The scheduled top-up
# -----------------------------------------------------------------------------


def _run_in(session: AsyncSession) -> Any:
    """``session_scope`` replaced by the test's own session (and its rollback)."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield session

    return scope


async def test_the_scheduled_top_up_reserves_a_goods_receipt_for_the_waiting_order(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench
    order, line = await raise_order(b, "50")
    assert line.reservation_id is None
    await receive(b, "80")
    monkeypatch.setattr(maintenance, "session_scope", _run_in(b.session))

    outcome = await maintenance.top_up_short_orders()

    assert outcome["failed"] == 0
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED
    topped = await fresh(b, SalesOrderLine, line.id)
    assert topped.allocated_quantity == Decimal("50.0000")
    reservation = await fresh(b, StockReservation, topped.reservation_id)
    # Made by the system: no person's name on it, and no foreign-key violation.
    assert reservation.created_by_id is None
    top_ups = await _top_up_audit(b, order.id)
    assert [row.created_by_id for row in top_ups] == [None]

    # A second tick finds nothing short for this order and writes nothing.
    await maintenance.top_up_short_orders()
    assert len(await _top_up_audit(b, order.id)) == 1


async def _top_up_audit(b: Bench, order_id: uuid.UUID) -> list[AuditLog]:
    rows = await b.session.execute(
        select(AuditLog).where(
            AuditLog.entity_id == order_id,
            AuditLog.action == AuditAction.RESERVATION_CHANGE,
            AuditLog.summary.like("Allocated newly available stock%"),
        )
    )
    return list(rows.scalars().all())


async def test_k02_a_partly_reserved_line_is_topped_up_by_the_system_actor(
    bench: Bench,
) -> None:
    """The release-and-re-reserve path, run as the scheduler runs it.

    120 of 500 reserved at confirmation; 380 arrive. The line's one reservation
    is released and retaken for the whole 500 - the path that wrote the nil
    actor into ``released_by_id`` and failed on its foreign key.
    """
    b = bench
    await receive(b, "120")
    order, line = await raise_order(b, "500")
    first_reservation = line.reservation_id
    await receive(b, "380")
    system = TenantContext(company_id=b.tenant.company_id, user_id=SYSTEM_ACTOR_ID)

    view = await sales.allocate_order(b.session, system, order.id, reason="Scheduled top-up")

    assert view.fully_allocated is True
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED
    released = await fresh(b, StockReservation, first_reservation)
    assert released.status is ReservationStatus.RELEASED
    assert released.released_by_id is None
    current = await fresh(b, SalesOrderLine, line.id)
    assert current.reservation_id != first_reservation
    assert current.allocated_quantity == Decimal("500.0000")
    assert (await fresh(b, StockReservation, current.reservation_id)).quantity == Decimal(
        "500.0000"
    )
    stock = await balance(b)
    assert stock.reserved_quantity == Decimal("500.0000")


async def test_the_sweep_is_bounded_most_urgent_first_and_isolates_a_failure(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench
    urgent, _ = await raise_order(b, "10", priority=1)
    failing, _ = await raise_order(b, "10", priority=2)
    unreached, _ = await raise_order(b, "10", priority=3)
    await receive(b, "30")

    calls: list[uuid.UUID] = []
    real = sales.allocate_order

    async def recording(
        session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
    ) -> Any:
        if tenant.company_id == b.tenant.company_id:
            calls.append(order_id)
            if order_id == failing.id:
                raise RuntimeError("a row this order cannot read")
        return await real(session, tenant, order_id, reason=reason)

    monkeypatch.setattr(maintenance, "session_scope", _run_in(b.session))
    monkeypatch.setattr(maintenance, "MAX_TOPUP_ORDERS", 2)
    monkeypatch.setattr(sales, "allocate_order", recording)

    outcome = await maintenance.top_up_short_orders()

    # Most urgent first, at most two for this company this tick.
    assert calls == [urgent.id, failing.id]
    assert outcome["failed"] >= 1
    assert (await fresh(b, SalesOrder, urgent.id)).status == SalesOrderStatus.ALLOCATED
    # The failure undid nothing but its own order...
    assert (await fresh(b, SalesOrder, failing.id)).status == SalesOrderStatus.CONFIRMED
    # ...and the bound left the least urgent one for the next tick.
    assert (await fresh(b, SalesOrder, unreached.id)).status == SalesOrderStatus.CONFIRMED


# -----------------------------------------------------------------------------
# ALLOCATED keeps its meaning
# -----------------------------------------------------------------------------


async def test_raising_a_quantity_past_free_stock_takes_an_allocated_order_back_to_confirmed(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "100")
    order, line = await raise_order(b, "100")
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED

    await sales.update_order(
        b.session,
        b.tenant,
        b.principal,
        order.id,
        SalesOrderUpdate(
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=b.item.id, quantity=Decimal("150"), uom_id=b.uom.id
                )
            ]
        ),
    )

    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.CONFIRMED
    assert (await fresh(b, SalesOrderLine, line.id)).allocated_quantity == Decimal("100.0000")
    # The edge itself is audited.
    statuses = (
        (
            await b.session.execute(
                select(AuditLog.new_values).where(
                    AuditLog.entity_id == order.id,
                    AuditLog.action == AuditAction.STATUS_CHANGE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {"status": "CONFIRMED"} in statuses


async def test_lowering_a_quantity_to_what_is_reserved_makes_a_confirmed_order_allocated(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "60")
    order, _ = await raise_order(b, "100")
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.CONFIRMED

    await sales.update_order(
        b.session,
        b.tenant,
        b.principal,
        order.id,
        SalesOrderUpdate(
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=b.item.id, quantity=Decimal("60"), uom_id=b.uom.id
                )
            ]
        ),
    )

    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED


# -----------------------------------------------------------------------------
# A reservation closed elsewhere is no reservation
# -----------------------------------------------------------------------------


async def test_a_reservation_released_elsewhere_counts_as_none_everywhere(bench: Bench) -> None:
    b = bench
    await receive(b, "100")
    order, line = await raise_order(b, "100")
    closed = line.reservation_id
    await allocation.release(
        b.session, b.tenant, reservation_id=closed, reason="Released from the allocation screen"
    )

    view = await sales.allocation_status(b.session, b.tenant, order.id)
    assert view.lines[0].reserved_quantity == Decimal("0.0000")
    assert view.lines[0].dispatchable_quantity == Decimal("0.0000")
    assert view.lines[0].shortfall_quantity == Decimal("100.0000")
    assert view.fully_allocated is False

    with pytest.raises(ValidationError, match="no live stock reservation"):
        await dispatch_service.create_dispatch(
            b.session, b.tenant, ui_payload(b, order, [(line, "10", [])])
        )

    # The top-up re-reserves it - the stock is free again - and never touches
    # the closed one.
    view = await sales.allocate_order(b.session, b.tenant, order.id, reason="Re-reserve")
    assert view.lines[0].reserved_quantity == Decimal("100.0000")
    current = await fresh(b, SalesOrderLine, line.id)
    assert current.reservation_id not in (None, closed)
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED


async def test_an_allocated_order_that_lost_its_stock_goes_back_to_confirmed(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "100")
    order, line = await raise_order(b, "100")
    await allocation.release(
        b.session, b.tenant, reservation_id=line.reservation_id, reason="Given to a rush order"
    )
    # Someone else takes the freed stock before the top-up runs.
    rival, _ = await raise_order(b, "100")
    assert (await fresh(b, SalesOrder, rival.id)).status == SalesOrderStatus.ALLOCATED

    await sales.allocate_order(b.session, b.tenant, order.id, reason="Top-up")

    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.CONFIRMED
    assert (await fresh(b, SalesOrderLine, line.id)).reservation_id is None


async def test_cancelling_an_order_whose_reservation_was_already_closed_still_succeeds(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "40")
    order, line = await raise_order(b, "40")
    await allocation.release(
        b.session, b.tenant, reservation_id=line.reservation_id, reason="Released elsewhere"
    )

    cancelled = await sales.cancel_order(b.session, b.tenant, order.id, reason="Withdrawn")

    assert cancelled.status == SalesOrderStatus.CANCELLED
    # Nothing was released twice: the balance holds no reservation either way.
    assert (await balance(b)).reserved_quantity == Decimal("0.0000")


# -----------------------------------------------------------------------------
# Serialised goods consume their reservation unit by unit
# -----------------------------------------------------------------------------


async def test_a_serial_tracked_order_line_consumes_its_reservation_one_unit_at_a_time(
    bench: Bench,
) -> None:
    b = bench
    lock = await seed_support.seed_item(
        b.session,
        company_id=b.tenant.company_id,
        code=_code("LOCK"),
        base_uom_id=b.uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
        sale_price=Decimal("4500"),
        gst_rate=Decimal("18"),
        is_serial_tracked=True,
    )
    units: list[uuid.UUID] = []
    for _ in range(3):
        serial = await inventory.TraceabilityService(b.session, b.tenant).create_serial(
            StockSerialCreate(item_id=lock.id, serial_number=_code("SN"), unit_cost=Decimal("3000"))
        )
        await inventory.post_movement(
            b.session,
            b.tenant,
            item_id=lock.id,
            warehouse_id=b.warehouse.id,
            quantity=Decimal("1"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="Serialised opening stock",
            serial_id=serial.id,
            unit_cost=Decimal("3000"),
        )
        units.append(serial.id)
    order, line = await raise_order(b, "3", item=lock)
    assert (await fresh(b, SalesOrder, order.id)).status == SalesOrderStatus.ALLOCATED

    result = await ship(b, ui_payload(b, order, [(line, "2", [str(u) for u in units[:2]])]))

    assert result.sales_order_status == SalesOrderStatus.PARTIALLY_DISPATCHED
    reservation = await fresh(b, StockReservation, line.reservation_id)
    assert reservation.status is ReservationStatus.PARTIALLY_CONSUMED
    assert reservation.consumed_quantity == Decimal("2.0000")
    rows = (
        (
            await b.session.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.reference_id == result.dispatch.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(row.serial_id for row in rows) == sorted(units[:2])
    assert all(row.quantity == Decimal("-1.0000") for row in rows)
    for unit in units[:2]:
        assert (await fresh(b, StockSerial, unit)).status is SerialStatus.DISPATCHED
    assert (await fresh(b, StockSerial, units[2])).status is SerialStatus.IN_STOCK
    stock = await balance(b, lock)
    assert stock.on_hand_quantity == Decimal("1.0000")
    assert stock.reserved_quantity == Decimal("1.0000")
