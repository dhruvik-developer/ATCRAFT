"""Shortfall planning, settings and the fulfilment trail on real PostgreSQL.

WP E1b. The behaviour under test is not expressible without the database: the
planner nets reservations whose open status is decided by a partial index, it
writes DRAFT production orders and requisitions through their owning services in
the confirming transaction, and the trail reads every module's rows with one
statement per family.

The scenario is K02/K08's own: a thousand units wanted, a hundred and twenty in
stock, and an item master that says MAKE or BUY. The plan must cover exactly the
balance, stay idempotent across replans, follow an edit and disappear when the
order is cancelled.
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
from sqlalchemy import func, select

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.customers.models import Customer
from src.modules.inventory.models import TransferSourceType, TransferStatus
from src.modules.inventory.schemas import StockTransferCreate, StockTransferLineCreate
from src.modules.inventory.service import StockTransferService
from src.modules.notifications.models import Notification, NotificationType
from src.modules.procurement.models import (
    PurchaseRequisition,
    PurchaseRequisitionLine,
    PurchaseSourceType,
)
from src.modules.production.models import (
    ProductionOrder,
    ProductionOrderStatus,
    ProductionSourceType,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure
from src.modules.sales import fulfilment as fulfilment_service
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales.schemas import (
    SalesOrderCreate,
    SalesOrderLineIn,
    SalesOrderUpdate,
    SalesSettingsUpdate,
)
from src.modules.users.models import Role, User
from src.modules.warehouses.models import Warehouse, WarehouseType
from src.workers.tasks import maintenance
from tests import seed_support

pytestmark = pytest.mark.integration

D = Decimal


@dataclass
class Bench:
    session: Any
    tenant: TenantContext
    principal: Principal
    uom: UnitOfMeasure
    kit: Item
    component: Item
    bought: Item
    job_item: Item
    warehouse: Warehouse
    spare: Warehouse
    customer: Customer


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _line(item: Item, quantity: str, number: int = 1) -> SalesOrderLineIn:
    return SalesOrderLineIn(line_number=number, item_id=item.id, quantity=D(quantity))


@pytest.fixture
async def bench(
    session: Any, tenant: TenantContext, principal: Principal, company: Any, user: Any
) -> Bench:
    company.state_code = "27"
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=_code("WH"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    spare = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=_code("WH-SPARE"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)

    def item(code: str, **kwargs: Any) -> Item:
        defaults: dict[str, Any] = {
            "company_id": tenant.company_id,
            "code": code,
            "name": f"{code} description",
            "item_group": ItemGroup.COMPONENT,
            "base_uom_id": uom.id,
            "production_type": ProductionType.BUY,
            "can_purchase": True,
            "can_sell": True,
            "sale_price": D("100"),
            "gst_rate": D("18"),
            "default_warehouse_id": warehouse.id,
            "is_active": True,
        }
        defaults.update(kwargs)
        return Item(**defaults)

    kit = item(
        _code("KIT"),
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
        lead_time_days=10,
        sale_price=D("100"),
    )
    component = item(
        _code("COMP"),
        can_be_bom_component=True,
        lead_time_days=21,
        sale_price=D("20"),
    )
    bought = item(
        _code("BUY"),
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.BUY,
        lead_time_days=14,
        sale_price=D("50"),
    )
    job_item = item(
        _code("JOB"),
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.JOB_WORK,
        can_purchase=False,
        can_manufacture=False,
        can_job_work=True,
        sale_price=D("75"),
    )
    session.add_all([kit, component, bought, job_item])
    await session.flush()

    bom = Bom(
        company_id=tenant.company_id,
        code=_code("BOM"),
        item_id=kit.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.PRODUCTION,
        is_default=True,
        output_quantity=D("1"),
        base_uom_id=uom.id,
        effective_from=today() - timedelta(days=30),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    session.add(bom)
    await session.flush()
    session.add(
        BomLine(
            company_id=tenant.company_id,
            bom_id=bom.id,
            parent_item_id=kit.id,
            line_number=1,
            component_item_id=component.id,
            quantity_per=D("2"),
            uom_id=uom.id,
        )
    )
    await session.flush()

    # The planner roles the confirmation notifies. The shared fixture's user
    # carries only TEST_ADMIN, so without this the notification fans out to
    # nobody and "the planner was told" would be untested.
    planner = Role(
        company_id=tenant.company_id,
        code="PLANNER",
        name="Planner",
        permissions=[],
        is_system=False,
        is_active=True,
    )
    session.add(planner)
    await session.flush()
    account = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    account.roles.append(planner)
    await session.flush()

    return Bench(
        session,
        tenant,
        principal,
        uom,
        kit,
        component,
        bought,
        job_item,
        warehouse,
        spare,
        customer,
    )


async def receive(
    b: Bench, quantity: str, *, item: Item, warehouse: Warehouse | None = None
) -> None:
    from src.modules.inventory.models import TransactionType
    from src.modules.inventory.service import post_movement

    await post_movement(
        b.session,
        b.tenant,
        item_id=item.id,
        warehouse_id=(warehouse or b.warehouse).id,
        quantity=D(quantity),
        transaction_type=TransactionType.PURCHASE_RECEIPT,
        reference_type="goods_receipt",
        reference_id=uuid.uuid4(),
        reference_code=_code("GRN"),
        reason="Stock received",
        unit_cost=D("60"),
    )


async def make_order(
    b: Bench, item: Item, quantity: str, *, confirm: bool = True
) -> tuple[SalesOrder, SalesOrderLine, Any]:
    order, lines = await sales.create_order(
        b.session,
        b.tenant,
        b.principal,
        SalesOrderCreate(
            customer_id=b.customer.id,
            warehouse_id=b.warehouse.id,
            required_date=today() + timedelta(days=30),
            priority=5,
            lines=[_line(item, quantity)],
        ),
    )
    result = None
    if confirm:
        result = await sales.confirm_order(
            b.session, b.tenant, b.principal, order.id, reason="Customer order accepted"
        )
        order = await b.session.get(SalesOrder, order.id)
    return order, lines[0], result


def _documents(result: Any, kind: str) -> list[Any]:
    return [document for document in result.documents if document.kind == kind]


# -----------------------------------------------------------------------------
# K02 - the shortfall becomes a DRAFT production order
# -----------------------------------------------------------------------------


async def test_k02_a_short_confirmation_plans_the_balance_as_a_draft(bench: Bench) -> None:
    b = bench
    await receive(b, "120", item=b.kit)

    order, line, result = await make_order(b, b.kit, "500")

    assert order.status is SalesOrderStatus.CONFIRMED
    assert result is not None
    assert result.allocation.total_shortfall_lines == 1
    assert result.planned is not None
    documents = _documents(result.planned, "PRODUCTION_ORDER")
    assert len(documents) == 1
    assert documents[0].quantity == D("380.0000")
    assert documents[0].line_id == line.id

    production = (
        await b.session.execute(
            select(ProductionOrder).where(ProductionOrder.source_id == order.id)
        )
    ).scalar_one()
    assert production.status is ProductionOrderStatus.DRAFT
    assert production.source_type is ProductionSourceType.SALES_ORDER
    assert production.source_line_id == line.id
    assert production.planned_quantity == D("380.0000")
    assert production.planned_start_date >= today()
    assert production.planned_end_date >= production.planned_start_date

    # The planner roles, not the salesperson, get the alert.
    alert = (
        await b.session.execute(
            select(Notification).where(
                Notification.entity_id == order.id,
                Notification.notification_type == NotificationType.STOCK_SHORTAGE,
            )
        )
    ).scalar_one()
    assert alert.severity == "WARNING"
    assert order.code in alert.title
    assert alert.action_url == f"/sales-orders/{order.id}"


async def test_a_replan_is_idempotent_and_an_edit_resizes_the_draft(bench: Bench) -> None:
    b = bench
    await receive(b, "120", item=b.kit)
    order, _, _ = await make_order(b, b.kit, "500")

    first = await sales.plan_shortfall(b.session, b.tenant, order.id, reason="Planner opened it")
    assert first.documents[0].id is not None
    second = await sales.plan_shortfall(b.session, b.tenant, order.id, reason="Planner re-ran it")
    assert [document.id for document in second.documents] == [
        document.id for document in first.documents
    ]
    count = await b.session.scalar(
        select(func.count())
        .select_from(ProductionOrder)
        .where(ProductionOrder.source_id == order.id)
    )
    assert count == 1

    # An edit changes the shortfall: 600 wanted, 120 held, 480 to make.
    await sales.update_order(
        b.session,
        b.tenant,
        b.principal,
        order.id,
        SalesOrderUpdate(lines=[_line(b.kit, "600")]),
    )
    draft = (
        await b.session.execute(
            select(ProductionOrder)
            .where(ProductionOrder.source_id == order.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert draft.planned_quantity == D("480.0000")

    # Shrinking the order keeps the same draft, resized.
    await sales.update_order(
        b.session,
        b.tenant,
        b.principal,
        order.id,
        SalesOrderUpdate(lines=[_line(b.kit, "200")]),
    )
    draft = (
        await b.session.execute(
            select(ProductionOrder).where(ProductionOrder.source_id == order.id)
        )
    ).scalar_one()
    assert draft.planned_quantity == D("80.0000")


async def test_cancelling_the_order_withdraws_the_still_draft_supply(bench: Bench) -> None:
    b = bench
    await receive(b, "120", item=b.kit)
    order, _, _ = await make_order(b, b.kit, "500")

    await sales.cancel_order(b.session, b.tenant, order.id, reason="Customer withdrew")

    draft = (
        await b.session.execute(
            select(ProductionOrder).where(ProductionOrder.source_id == order.id)
        )
    ).scalar_one()
    assert draft.status is ProductionOrderStatus.CANCELLED


# -----------------------------------------------------------------------------
# BUY, JOB_WORK and one requisition per order
# -----------------------------------------------------------------------------


async def test_a_buy_shortfall_creates_one_requisition_pegged_to_the_line(bench: Bench) -> None:
    b = bench
    order, line, result = await make_order(b, b.bought, "80")

    assert result is not None and result.planned is not None
    documents = _documents(result.planned, "PURCHASE_REQUISITION")
    assert len(documents) == 1
    assert documents[0].quantity == D("80.0000")

    header = (
        await b.session.execute(
            select(PurchaseRequisition).where(PurchaseRequisition.source_id == order.id)
        )
    ).scalar_one()
    assert header.source_type == PurchaseSourceType.SALES_ORDER.value
    assert header.status == "DRAFT"
    line_row = (
        await b.session.execute(
            select(PurchaseRequisitionLine).where(
                PurchaseRequisitionLine.requisition_id == header.id
            )
        )
    ).scalar_one()
    assert line_row.quantity == D("80.0000")
    assert line_row.source_demand_refs == [
        {
            "demand_type": "SALES_ORDER",
            "demand_id": str(order.id),
            "demand_line_id": str(line.id),
        }
    ]

    # Replanning keeps the single document and the single line it owns.
    again = await sales.plan_shortfall(b.session, b.tenant, order.id, reason="Replanned")
    assert [document.id for document in again.documents] == [documents[0].id]
    count = await b.session.scalar(
        select(func.count())
        .select_from(PurchaseRequisitionLine)
        .where(PurchaseRequisitionLine.requisition_id == header.id)
    )
    assert count == 1


async def test_a_job_work_shortfall_is_reported_and_creates_nothing(bench: Bench) -> None:
    b = bench
    order, line, result = await make_order(b, b.job_item, "15")

    assert result is not None and result.planned is not None
    assert result.planned.documents == []
    assert [need.line_id for need in result.planned.needs_job_work] == [line.id]
    assert result.planned.needs_job_work[0].item_code == b.job_item.code
    assert result.planned.needs_job_work[0].quantity == D("15.0000")

    assert (
        await b.session.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.source_id == order.id)
        )
        == 0
    )
    assert (
        await b.session.scalar(
            select(func.count())
            .select_from(PurchaseRequisition)
            .where(PurchaseRequisition.source_id == order.id)
        )
        == 0
    )


# -----------------------------------------------------------------------------
# The settings switch
# -----------------------------------------------------------------------------


async def test_the_setting_defaults_on_can_be_switched_off_and_restored(
    bench: Bench,
) -> None:
    b = bench
    effective = await sales.get_sales_settings(b.session, b.tenant)
    assert effective.auto_plan_on_confirm is True
    assert effective.is_default is True

    saved = await sales.update_sales_settings(
        b.session, b.tenant, SalesSettingsUpdate(auto_plan_on_confirm=False)
    )
    assert saved.auto_plan_on_confirm is False
    assert saved.is_default is False
    assert await sales.auto_plan_on_confirm(b.session, b.tenant) is False

    audit = (
        await b.session.execute(select(AuditLog).where(AuditLog.entity_type == "sales_settings"))
    ).scalar_one()
    assert audit.action == AuditAction.UPDATE.value
    assert audit.old_values == {"auto_plan_on_confirm": None}
    assert audit.new_values == {"auto_plan_on_confirm": False}

    # Explicit null is the documented way back to the code default.
    restored = await sales.update_sales_settings(
        b.session, b.tenant, SalesSettingsUpdate(auto_plan_on_confirm=None)
    )
    assert restored.auto_plan_on_confirm is True
    assert restored.is_default is True

    # An omitted field leaves the stored decision alone.
    await sales.update_sales_settings(
        b.session, b.tenant, SalesSettingsUpdate(auto_plan_on_confirm=False)
    )
    untouched = await sales.update_sales_settings(b.session, b.tenant, SalesSettingsUpdate())
    assert untouched.auto_plan_on_confirm is False


async def test_with_the_setting_off_confirmation_leaves_planning_to_the_planner(
    bench: Bench,
) -> None:
    b = bench
    await sales.update_sales_settings(
        b.session, b.tenant, SalesSettingsUpdate(auto_plan_on_confirm=False)
    )
    order, _, result = await make_order(b, b.kit, "50")

    assert result is not None
    assert result.planned is None
    assert (
        await b.session.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.source_id == order.id)
        )
        == 0
    )

    # The on-demand action still works with the setting off.
    plan = await sales.plan_shortfall(b.session, b.tenant, order.id, reason="Plan it now")
    assert _documents(plan, "PRODUCTION_ORDER")


# -----------------------------------------------------------------------------
# The fulfilment trail
# -----------------------------------------------------------------------------


async def test_the_fulfilment_trail_reports_lines_reservations_and_planned_supply(
    bench: Bench,
) -> None:
    b = bench
    await receive(b, "120", item=b.kit)
    order, lines = await sales.create_order(
        b.session,
        b.tenant,
        b.principal,
        SalesOrderCreate(
            customer_id=b.customer.id,
            warehouse_id=b.warehouse.id,
            required_date=today() + timedelta(days=30),
            priority=5,
            lines=[_line(b.kit, "500", 1), _line(b.bought, "30", 2)],
        ),
    )
    await sales.confirm_order(
        b.session, b.tenant, b.principal, order.id, reason="Customer order accepted"
    )
    line = lines[0]

    trail = await fulfilment_service.order_fulfilment(b.session, b.tenant, order.id)

    assert trail.order_id == order.id
    assert trail.code == order.code
    assert len(trail.lines) == 2
    kit_line = next(row for row in trail.lines if row.line_id == line.id)
    assert kit_line.item_code == b.kit.code
    assert kit_line.ordered_quantity == D("500.0000")
    assert kit_line.allocated_quantity == D("120.0000")
    assert kit_line.reserved_quantity == D("120.0000")
    assert kit_line.shortfall_quantity == D("380.0000")
    assert kit_line.reservation_id is not None

    assert len(trail.production_orders) == 1
    production = trail.production_orders[0]
    assert production.kind == "PRODUCTION_ORDER"
    assert production.quantity == D("380.0000")
    assert production.line_id == line.id
    assert production.url == f"/production/{production.id}"

    assert len(trail.requisitions) == 1
    requisition = trail.requisitions[0]
    assert requisition.kind == "PURCHASE_REQUISITION"
    assert requisition.quantity == D("30.0000")
    assert requisition.line_id is not None

    # Families with no document are empty lists, not missing keys: the timeline
    # renders one panel per family and a null would be a second shape to handle.
    assert trail.dispatches == []
    assert trail.invoices == []


# -----------------------------------------------------------------------------
# K10 - other warehouses and the transfer path
# -----------------------------------------------------------------------------


def _run_in(session: Any) -> Any:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Any]:
        yield session

    return scope


async def test_k10_stock_elsewhere_is_offered_and_a_pegged_transfer_fills_the_order(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench
    await receive(b, "40", item=b.kit, warehouse=b.spare)

    order, line, result = await make_order(b, b.kit, "40")
    assert result is not None and result.allocation.total_shortfall_lines == 1
    assert order.status is SalesOrderStatus.CONFIRMED

    view = await sales.allocation_status(b.session, b.tenant, order.id)
    elsewhere = view.lines[0].available_elsewhere
    assert [(row.warehouse_id, row.quantity) for row in elsewhere] == [(b.spare.id, D("40.0000"))]

    transfers = StockTransferService(b.session, b.tenant)
    created = await transfers.create(
        StockTransferCreate(
            from_warehouse_id=b.spare.id,
            to_warehouse_id=b.warehouse.id,
            reason="Stock for a short sales order",
            source_type=TransferSourceType.SALES_ORDER,
            source_id=order.id,
            lines=[
                StockTransferLineCreate(
                    item_id=b.kit.id,
                    quantity=D("40"),
                    source_line_id=line.id,
                )
            ],
        )
    )
    assert created.source_type is TransferSourceType.SALES_ORDER
    await transfers.approve(created.id)
    await transfers.dispatch(created.id, reason="Sent to the order's warehouse")
    received = await transfers.receive(created.id, reason="Arrived")
    assert received.status is TransferStatus.RECEIVED

    # E1a's top-up is what turns stock into a reservation; it is a sweep, not a
    # side effect of receiving, so run the sweep the scheduler would.
    monkeypatch.setattr(maintenance, "session_scope", _run_in(b.session))
    outcome = await maintenance.top_up_short_orders()
    assert outcome["failed"] == 0

    allocated = await sales.get_order_detail(b.session, b.tenant, order.id)
    assert allocated.status is SalesOrderStatus.ALLOCATED
    assert allocated.lines[0].allocated_quantity == D("40.0000")

    # The draft raised while the order was short is withdrawn once the transfer
    # has covered it - the plan follows the reservation, not the other way round.
    draft = (
        await b.session.execute(
            select(ProductionOrder)
            .where(ProductionOrder.source_id == order.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert draft.status is ProductionOrderStatus.CANCELLED

    trail = await fulfilment_service.order_fulfilment(b.session, b.tenant, order.id)
    assert [document.id for document in trail.transfers] == [created.id]
    assert trail.transfers[0].quantity == D("40.0000")
