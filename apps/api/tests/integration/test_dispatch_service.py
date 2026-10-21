"""Service-level tests for dispatch against a real PostgreSQL.

What is under test is the sentence master rules sections 22 and 23 turn on:
sending a consignment draws down the promise the sales order was holding, takes
the goods off the shelf, and advances the order by exactly the quantity that
left - no more, no less, and never one of those without the others.

These need PostgreSQL because none of it is expressible without it: the generated
``available_quantity`` column, the CHECK constraints that make an over-dispatched
order line or a delivered consignment with no arrival date impossible,
``SELECT ... FOR UPDATE`` on the consignment header and the order lines, the
``uuid[]`` traceability arrays, the partial index behind the delay sweep, and the
audit rows written inside the same transaction as the movements they describe.

The allocation and inventory engines are the real ones. Substituting either would
make the central assertion here - that ``reserved_quantity`` and
``on_hand_quantity`` fall together and by the same amount - a scripted result
rather than a property of the code that runs in production.

Isolation follows ``test_sales_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import CursorParams, PageParams, SortParams
from src.modules.allocation import service as allocation
from src.modules.allocation.models import DemandType, ReservationStatus, StockReservation
from src.modules.audit.models import AuditLog
from src.modules.customers.models import Customer, CustomerType
from src.modules.dispatch import service
from src.modules.dispatch.models import (
    Dispatch,
    DispatchLine,
    DispatchStatus,
    DispatchTrackingEvent,
    DispatchType,
)
from src.modules.dispatch.repository import CartonLookupRepository
from src.modules.dispatch.schemas import (
    DeliveryConfirmation,
    DispatchCreate,
    DispatchFilters,
    DispatchLineIn,
    DispatchRequest,
    DispatchUpdate,
    TrackingEventIn,
)
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    TransactionType,
)
from src.modules.notifications.models import Notification, NotificationType
from src.modules.organization.models import Company
from src.modules.packing import service as packing
from src.modules.packing.models import PackingOrder
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    PackingOrderCreate,
    PackingOrderLineIn,
    PackingOrderUpdate,
    SealCartonRequest,
)
from src.modules.packing.schemas import (
    CompleteRequest as PackingCompleteRequest,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.sales.models import (
    SalesOrder,
    SalesOrderLine,
    SalesOrderLineStatus,
    SalesOrderStatus,
)
from src.modules.users.models import Role, User
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------


def _schema_engine(schema: str):
    """Engine pinned to one schema. ``public`` stays on the path for pg_trgm."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and one dispatch clerk, built once."""
    prepare_metadata()

    schema = f"it_dispatch_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        eng = _schema_engine(schema)
        try:
            async with eng.begin() as conn:
                # checkfirst=False: the shared conftest already created these
                # tables in `public`, which stays on the search path.
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )

            factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
            async with factory() as setup_session, setup_session.begin():
                for cid in (company_id, other_company_id):
                    setup_session.add(
                        Company(
                            id=cid,
                            code=f"C{uuid.uuid4().hex[:8].upper()}",
                            name="Atcraft Manufacturing",
                            country="India",
                            currency="INR",
                            state_code="27",
                            is_active=True,
                        )
                    )
                await setup_session.flush()
                # A staffed DISPATCH_MANAGER role: the delay sweep resolves its
                # recipients from the role assignment, so a company with nobody
                # in the role would prove nothing about the alert.
                role = Role(
                    company_id=company_id,
                    code="DISPATCH_MANAGER",
                    name="Dispatch manager",
                    permissions=["dispatch.view"],
                    is_active=True,
                )
                setup_session.add(role)
                await setup_session.flush()
                clerk = User(
                    id=user_id,
                    company_id=company_id,
                    email=f"clerk-{uuid.uuid4().hex[:6]}@atcraft.test",
                    full_name="Dispatch Clerk",
                    password_hash="x",
                    is_active=True,
                )
                clerk.roles.append(role)
                setup_session.add(clerk)
        finally:
            await eng.dispose()

    async def teardown() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, other_company_id, user_id
    asyncio.run(teardown())


class Ctx:
    def __init__(
        self,
        session: AsyncSession,
        company_id: uuid.UUID,
        other_company_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.session = session
        self.company_id = company_id
        self.tenant = TenantContext(company_id=company_id, user_id=user_id)
        self.other_tenant = TenantContext(company_id=other_company_id, user_id=uuid.uuid4())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[Ctx]:
    schema, company_id, other_company_id, user_id = _schema
    eng = _schema_engine(schema)
    try:
        async with eng.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield Ctx(session, company_id, other_company_id, user_id)
            await outer.rollback()
    finally:
        await eng.dispose()


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------

TODAY = date(2026, 9, 1)


async def seed_uom(ctx: Ctx) -> UnitOfMeasure:
    uom = UnitOfMeasure(
        company_id=ctx.company_id,
        code=f"NOS{uuid.uuid4().hex[:6].upper()}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
    )
    ctx.session.add(uom)
    await ctx.session.flush()
    return uom


async def seed_item(ctx: Ctx, uom: UnitOfMeasure) -> Item:
    item = Item(
        company_id=ctx.company_id,
        code=f"FG-{uuid.uuid4().hex[:8].upper()}",
        name="Mortise Lock",
        item_group=ItemGroup.FINISHED_GOOD,
        base_uom_id=uom.id,
        production_type=ProductionType.MAKE,
        gst_rate=Decimal("18"),
        sale_price=Decimal("100"),
        dealer_price=Decimal("90"),
        standard_cost=Decimal("60"),
        can_sell=True,
        can_manufacture=True,
    )
    ctx.session.add(item)
    await ctx.session.flush()
    return item


async def seed_warehouse(ctx: Ctx) -> Warehouse:
    warehouse = Warehouse(
        company_id=ctx.company_id,
        code=f"WH{uuid.uuid4().hex[:6].upper()}",
        name="Finished goods store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        state_code="27",
        is_active=True,
    )
    ctx.session.add(warehouse)
    await ctx.session.flush()
    return warehouse


async def seed_customer(ctx: Ctx) -> Customer:
    customer = Customer(
        company_id=ctx.company_id,
        code=f"CU{uuid.uuid4().hex[:8].upper()}",
        name="Hardware Distributors",
        customer_type=CustomerType.CUSTOMER,
        credit_days=30,
        is_active=True,
        is_blocked=False,
    )
    ctx.session.add(customer)
    await ctx.session.flush()
    return customer


class Bench:
    """One warehouse, one customer, one saleable item, one allocated order."""

    def __init__(
        self,
        *,
        uom: UnitOfMeasure,
        item: Item,
        warehouse: Warehouse,
        customer: Customer,
        order: SalesOrder,
        line: SalesOrderLine,
        reservation: StockReservation,
    ) -> None:
        self.uom = uom
        self.item = item
        self.warehouse = warehouse
        self.customer = customer
        self.order = order
        self.line = line
        self.reservation = reservation

    def payload(self, quantity: str, **overrides: object) -> DispatchCreate:
        body: dict[str, object] = {
            "dispatch_type": DispatchType.SALES,
            "warehouse_id": self.warehouse.id,
            "dispatch_date": TODAY,
            "expected_delivery_date": TODAY + timedelta(days=5),
            "customer_id": self.customer.id,
            "sales_order_id": self.order.id,
            "lines": [
                DispatchLineIn(
                    item_id=self.item.id,
                    quantity=Decimal(quantity),
                    sales_order_line_id=self.line.id,
                )
            ],
        }
        body.update(overrides)
        return DispatchCreate(**body)  # type: ignore[arg-type]


async def seed_bench(
    ctx: Ctx, *, ordered: str = "100", on_hand: str = "100", reserved: str | None = None
) -> Bench:
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom)
    warehouse = await seed_warehouse(ctx)
    customer = await seed_customer(ctx)

    async with transaction(ctx.session):
        await inv.post_movement(
            ctx.session,
            ctx.tenant,
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal(on_hand),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="dispatch fixture opening stock",
            unit_cost=Decimal("60"),
        )

    order = SalesOrder(
        company_id=ctx.company_id,
        code=f"SO-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.id,
        order_date=TODAY,
        required_date=TODAY + timedelta(days=7),
        warehouse_id=warehouse.id,
        status=SalesOrderStatus.ALLOCATED,
    )
    ctx.session.add(order)
    await ctx.session.flush()

    line = SalesOrderLine(
        company_id=ctx.company_id,
        sales_order_id=order.id,
        line_number=1,
        item_id=item.id,
        quantity=Decimal(ordered),
        uom_id=uom.id,
        unit_price=Decimal("100"),
        allocated_quantity=Decimal(reserved or ordered),
        status=SalesOrderLineStatus.ALLOCATED,
    )
    ctx.session.add(line)
    await ctx.session.flush()

    reservation = await allocation.reserve(
        ctx.session,
        ctx.tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal(reserved or ordered),
        demand_type=DemandType.SALES_ORDER,
        demand_id=order.id,
        demand_line_id=line.id,
        demand_code=order.code,
        reason="Sales order confirmation",
    )
    line.reservation_id = reservation.id
    await ctx.session.flush()

    return Bench(
        uom=uom,
        item=item,
        warehouse=warehouse,
        customer=customer,
        order=order,
        line=line,
        reservation=reservation,
    )


async def balance_of(ctx: Ctx, bench: Bench) -> InventoryBalance:
    stmt = select(InventoryBalance).where(
        InventoryBalance.company_id == ctx.company_id,
        InventoryBalance.item_id == bench.item.id,
        InventoryBalance.warehouse_id == bench.warehouse.id,
    )
    row = (await ctx.session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    return row


async def send(ctx: Ctx, bench: Bench, quantity: str):
    """Raise a consignment for ``quantity`` and send it."""
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload(quantity))
    await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Loaded")
    return await service.dispatch_consignment(
        ctx.session,
        ctx.tenant,
        detail.id,
        DispatchRequest(reason="Handed to the transporter", lr_number="LR-001"),
    )


# -----------------------------------------------------------------------------
# Dispatching consumes the reservation and reduces stock
# -----------------------------------------------------------------------------


async def test_dispatch_draws_down_the_reservation_and_the_shelf_together(ctx: Ctx) -> None:
    bench = await seed_bench(ctx, ordered="100", on_hand="100")

    before = await balance_of(ctx, bench)
    assert before.on_hand_quantity == Decimal("100.0000")
    assert before.reserved_quantity == Decimal("100.0000")
    assert before.available_quantity == Decimal("0.0000")

    result = await send(ctx, bench, "40")

    after = await balance_of(ctx, bench)
    # Both fall, and by the same amount. Either one moving without the other is
    # the defect this whole module exists to prevent.
    assert after.on_hand_quantity == Decimal("60.0000")
    assert after.reserved_quantity == Decimal("60.0000")
    assert after.available_quantity == Decimal("0.0000")

    assert result.consumed_reservation_ids == [bench.reservation.id]


async def test_gate_details_are_saved_with_dispatch_and_tracking_event(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    plan = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("40"))
    await service.mark_ready(ctx.session, ctx.tenant, plan.id, reason="Loaded")

    result = await service.dispatch_consignment(
        ctx.session,
        ctx.tenant,
        plan.id,
        DispatchRequest(
            reason="Vehicle cleared the gate",
            dispatch_date=TODAY + timedelta(days=1),
            expected_delivery_date=TODAY + timedelta(days=6),
            lr_number="LR-204",
            vehicle_number="MH12AB1234",
            tracking_id="TRACK-204",
            location="Pune plant gate",
        ),
    )

    assert result.dispatch.dispatch_date == TODAY + timedelta(days=1)
    assert result.dispatch.expected_delivery_date == TODAY + timedelta(days=6)
    assert result.dispatch.lr_number == "LR-204"
    assert result.dispatch.vehicle_number == "MH12AB1234"
    assert result.dispatch.tracking_id == "TRACK-204"
    assert result.dispatch.dispatched_at is not None
    event = (
        await ctx.session.execute(
            select(DispatchTrackingEvent).where(
                DispatchTrackingEvent.company_id == ctx.company_id,
                DispatchTrackingEvent.dispatch_id == plan.id,
                DispatchTrackingEvent.event_status == DispatchStatus.DISPATCHED,
            )
        )
    ).scalar_one()
    assert event.location == "Pune plant gate"


async def test_dispatch_writes_one_negative_ledger_row_pointing_at_the_consignment(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "40")

    stmt = select(InventoryTransaction).where(
        InventoryTransaction.company_id == ctx.company_id,
        InventoryTransaction.reference_type == service.MOVEMENT_REFERENCE_DISPATCH,
        InventoryTransaction.reference_id == result.dispatch.id,
    )
    rows = list((await ctx.session.execute(stmt)).scalars().all())

    assert len(rows) == 1
    movement = rows[0]
    assert movement.transaction_type == TransactionType.DISPATCH
    # Signed: DISPATCH always decreases on-hand (database.md section 4.2).
    assert movement.quantity == Decimal("-40.0000")
    assert movement.balance_after == Decimal("60.0000")
    assert movement.reference_code == result.dispatch.code
    assert movement.reason
    assert result.inventory_transaction_ids == [movement.id]


async def test_the_reservation_records_the_consumption_rather_than_a_release(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    await send(ctx, bench, "40")

    reservation = (
        await ctx.session.execute(
            select(StockReservation)
            .where(StockReservation.id == bench.reservation.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    # The goods shipped; they were not handed back to the pool. A RELEASED
    # reservation here would misreport where the stock went.
    assert reservation.status is ReservationStatus.PARTIALLY_CONSUMED
    assert reservation.consumed_quantity == Decimal("40.0000")
    assert reservation.released_quantity == Decimal("0.0000")
    assert reservation.remaining_quantity == Decimal("60.0000")


# -----------------------------------------------------------------------------
# Partial dispatch (master rules section 72)
# -----------------------------------------------------------------------------


async def test_a_partial_dispatch_leaves_the_order_partially_dispatched(ctx: Ctx) -> None:
    bench = await seed_bench(ctx, ordered="100")

    result = await send(ctx, bench, "40")

    assert result.sales_order_status == SalesOrderStatus.PARTIALLY_DISPATCHED
    assert result.open_sales_order_lines == 1

    line = (
        await ctx.session.execute(
            select(SalesOrderLine)
            .where(SalesOrderLine.id == bench.line.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert line.dispatched_quantity == Decimal("40.0000")
    assert line.status == SalesOrderLineStatus.PARTIALLY_DISPATCHED


async def test_the_closing_consignment_completes_the_order(ctx: Ctx) -> None:
    bench = await seed_bench(ctx, ordered="100")

    await send(ctx, bench, "40")
    result = await send(ctx, bench, "60")

    assert result.sales_order_status == SalesOrderStatus.DISPATCHED
    assert result.open_sales_order_lines == 0

    line = (
        await ctx.session.execute(
            select(SalesOrderLine)
            .where(SalesOrderLine.id == bench.line.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert line.dispatched_quantity == Decimal("100.0000")
    assert line.status == SalesOrderLineStatus.DISPATCHED

    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == Decimal("0.0000")
    assert balance.reserved_quantity == Decimal("0.0000")

    reservation = (
        await ctx.session.execute(
            select(StockReservation)
            .where(StockReservation.id == bench.reservation.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reservation.status is ReservationStatus.CONSUMED


async def test_an_order_shipped_complete_in_one_go_is_walked_through_both_statuses(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx, ordered="100")
    result = await send(ctx, bench, "100")

    assert result.sales_order_status == SalesOrderStatus.DISPATCHED

    # The sales lifecycle has no ALLOCATED -> DISPATCHED edge, so the audit
    # trail has to show both hops rather than a jump the machine cannot make.
    stmt = select(AuditLog.new_values).where(
        AuditLog.company_id == ctx.company_id,
        AuditLog.entity_type == service.AUDIT_SALES_ORDER,
        AuditLog.entity_id == bench.order.id,
    )
    statuses = [row.new_values["status"] for row in (await ctx.session.execute(stmt)).all()]
    assert statuses == [
        SalesOrderStatus.PARTIALLY_DISPATCHED.value,
        SalesOrderStatus.DISPATCHED.value,
    ]


async def test_a_second_plan_cannot_claim_what_an_open_consignment_already_carries(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx, ordered="100")

    # The first plan has not left, so the order line's dispatched quantity has not
    # moved - but 40 of the 100 are already spoken for. Counting open consignments
    # when a plan is saved is what stops 70 + 40 from both being accepted.
    first = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("40"))
    await service.mark_ready(ctx.session, ctx.tenant, first.id, reason="Loaded")

    with pytest.raises(ValidationError) as excinfo:
        await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("70"))
    assert excinfo.value.details["dispatchable"] == "60.0000"
    assert excinfo.value.details["on_open_dispatches"] == "40.0000"
    assert "Line 1" in excinfo.value.message

    # What is left is exactly plannable.
    await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("60"))
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == Decimal("100.0000")


async def test_a_plan_overtaken_before_it_leaves_is_refused_and_moves_nothing(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx, ordered="100")

    # Planned while the reservation covered it; the promise is then given back
    # from the allocation side. The re-check under the locks at departure is the
    # authority - the plan-time read is worthless by then.
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("70"))
    await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Loaded")
    await allocation.release(
        ctx.session, ctx.tenant, reservation_id=bench.reservation.id, reason="Promise withdrawn"
    )

    with pytest.raises(ValidationError, match="no live stock reservation") as excinfo:
        await service.dispatch_consignment(
            ctx.session, ctx.tenant, detail.id, DispatchRequest(reason="Leaving anyway")
        )
    assert excinfo.value.details["reserved"] == "0.0000"

    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == Decimal("100.0000")
    refused = (
        await ctx.session.execute(
            select(Dispatch)
            .where(Dispatch.id == detail.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refused.status == DispatchStatus.READY_FOR_DISPATCH


async def test_legacy_duplicate_demand_links_cannot_ship_or_change_fulfilment(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("40"))
    # Simulate a draft saved before the duplicate-link fix, without bypassing the
    # stock engine or mocking its posting behaviour.
    ctx.session.add(
        DispatchLine(
            company_id=ctx.company_id,
            dispatch_id=detail.id,
            line_number=2,
            item_id=bench.item.id,
            quantity=Decimal("40"),
            sales_order_line_id=bench.line.id,
            serial_ids=[],
            carton_ids=[],
        )
    )
    await ctx.session.flush()
    await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Loaded")
    with pytest.raises(BusinessRuleError, match="same sales-order line"):
        await service.dispatch_consignment(
            ctx.session, ctx.tenant, detail.id, DispatchRequest(reason="Send legacy draft")
        )
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == balance.reserved_quantity == Decimal("100")
    line = (
        await ctx.session.execute(
            select(SalesOrderLine)
            .where(SalesOrderLine.id == bench.line.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert line.dispatched_quantity == 0
    assert line.status == SalesOrderLineStatus.ALLOCATED
    movements = await ctx.session.scalar(
        select(func.count())
        .select_from(InventoryTransaction)
        .where(
            InventoryTransaction.reference_type == "dispatch",
            InventoryTransaction.reference_id == detail.id,
        )
    )
    assert movements == 0
    refused = await service.get_dispatch_detail(ctx.session, ctx.tenant, detail.id)
    assert refused.status == DispatchStatus.READY_FOR_DISPATCH


async def carton_sales_line(ctx: Ctx, bench: Bench) -> tuple[Item, SalesOrderLine]:
    """A second actual demand and reservation, required by order-backed dispatch."""
    item = await seed_item(ctx, bench.uom)
    await inv.post_movement(
        ctx.session,
        ctx.tenant,
        item_id=item.id,
        warehouse_id=bench.warehouse.id,
        quantity=Decimal("10"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code=None,
        reason="Mixed-carton second sales line stock",
        unit_cost=Decimal("10"),
    )
    line = SalesOrderLine(
        company_id=ctx.company_id,
        sales_order_id=bench.order.id,
        line_number=2,
        item_id=item.id,
        quantity=Decimal("5"),
        uom_id=bench.uom.id,
        unit_price=Decimal("100"),
        allocated_quantity=Decimal("5"),
        status=SalesOrderLineStatus.ALLOCATED,
    )
    ctx.session.add(line)
    await ctx.session.flush()
    reservation = await allocation.reserve(
        ctx.session,
        ctx.tenant,
        item_id=item.id,
        warehouse_id=bench.warehouse.id,
        quantity=Decimal("5"),
        demand_type=DemandType.SALES_ORDER,
        demand_id=bench.order.id,
        demand_line_id=line.id,
        demand_code=bench.order.code,
        reason="Mixed-carton second sales line allocation",
    )
    line.reservation_id = reservation.id
    await ctx.session.flush()
    return item, line


async def packed_carton(
    ctx: Ctx, bench: Bench, rows: list[tuple[uuid.UUID, Decimal, uuid.UUID | None]]
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seal real packed goods, including their packing-material stock movement."""
    box = await seed_item(ctx, bench.uom)
    box.item_group = ItemGroup.PACKING_MATERIAL
    await ctx.session.flush()
    await inv.post_movement(
        ctx.session,
        ctx.tenant,
        item_id=box.id,
        warehouse_id=bench.warehouse.id,
        quantity=Decimal("10"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reason="Boxes for dispatch carton regressions",
        reference_code=None,
        unit_cost=Decimal("2"),
    )
    order, _ = await packing.create_order(
        ctx.session,
        ctx.tenant,
        PackingOrderCreate(
            customer_id=bench.customer.id,
            warehouse_id=bench.warehouse.id,
            sales_order_id=bench.order.id,
            planned_date=TODAY,
            lines=[
                PackingOrderLineIn(
                    item_id=item_id,
                    quantity_to_pack=quantity,
                    sales_order_line_id=demand_id,
                    uom_id=bench.uom.id,
                )
                for item_id, quantity, demand_id in rows
            ],
        ),
    )
    order_id = order.id
    await packing.start_packing(ctx.session, ctx.tenant, order_id, reason="Pack shipment")
    carton = await packing.add_carton(
        ctx.session,
        ctx.tenant,
        order_id,
        CartonCreate(
            packing_material_item_id=box.id,
            contents=[
                CartonContentIn(line_number=number, quantity=quantity, net_weight_kg=Decimal("1"))
                for number, (_, quantity, _) in enumerate(rows, start=1)
            ],
        ),
    )
    carton_id = carton.id
    await packing.seal_carton(
        ctx.session, ctx.tenant, order_id, carton_id, SealCartonRequest(reason="Seal shipment")
    )
    await packing.complete_packing(
        ctx.session, ctx.tenant, order_id, PackingCompleteRequest(reason="Contents complete")
    )
    return order_id, carton_id


def packing_payload(bench: Bench, quantity: str) -> PackingOrderCreate:
    return PackingOrderCreate(
        customer_id=bench.customer.id,
        warehouse_id=bench.warehouse.id,
        sales_order_id=bench.order.id,
        planned_date=TODAY,
        lines=[
            PackingOrderLineIn(
                item_id=bench.item.id,
                quantity_to_pack=Decimal(quantity),
                sales_order_line_id=bench.line.id,
                uom_id=bench.uom.id,
            )
        ],
    )


async def test_open_packing_orders_cannot_overplan_a_sales_order_line(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "60"))

    with pytest.raises(BusinessRuleError, match="still available") as error:
        await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "50"))

    assert error.value.details["available_quantity"] == "40.0000"


async def test_replacing_packing_lines_excludes_the_order_being_amended(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    order, _ = await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "40"))
    await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "50"))

    with pytest.raises(BusinessRuleError, match="still available") as error:
        await packing.update_order(
            ctx.session,
            ctx.tenant,
            order.id,
            PackingOrderUpdate(
                lines=[
                    PackingOrderLineIn(
                        item_id=bench.item.id,
                        quantity_to_pack=Decimal("60"),
                        sales_order_line_id=bench.line.id,
                        uom_id=bench.uom.id,
                    )
                ]
            ),
        )

    assert error.value.details["available_quantity"] == "50.0000"


async def test_packing_availability_accounts_for_open_dispatch_plans(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("40"))

    with pytest.raises(BusinessRuleError, match="still available") as error:
        await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "61"))

    assert error.value.details["dispatch_planned_quantity"] == "40.0000"
    assert error.value.details["available_quantity"] == "60.0000"


async def test_packing_availability_accounts_for_dispatched_quantity(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    await send(ctx, bench, "40")

    with pytest.raises(BusinessRuleError, match="still available") as error:
        await packing.create_order(ctx.session, ctx.tenant, packing_payload(bench, "61"))

    assert error.value.details["dispatched_quantity"] == "40.0000"
    assert error.value.details["available_quantity"] == "60.0000"


async def test_mixed_item_carton_can_ship_on_distinct_content_rows(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    second, second_line = await carton_sales_line(ctx, bench)
    packing_id, carton_id = await packed_carton(
        ctx,
        bench,
        [(bench.item.id, Decimal("40"), bench.line.id), (second.id, Decimal("5"), second_line.id)],
    )
    detail = await service.create_dispatch(
        ctx.session,
        ctx.tenant,
        bench.payload(
            "40",
            packing_order_id=packing_id,
            lines=[
                DispatchLineIn(
                    item_id=bench.item.id,
                    quantity=Decimal("40"),
                    sales_order_line_id=bench.line.id,
                    carton_ids=[carton_id],
                ),
                DispatchLineIn(
                    item_id=second.id,
                    quantity=Decimal("5"),
                    sales_order_line_id=second_line.id,
                    carton_ids=[carton_id],
                ),
            ],
        ),
    )
    await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Mixed carton loaded")
    result = await service.dispatch_consignment(
        ctx.session, ctx.tenant, detail.id, DispatchRequest(reason="Ship entire mixed carton")
    )
    assert result.dispatch.status == DispatchStatus.DISPATCHED
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == balance.reserved_quantity == Decimal("60")
    assert await inv.get_available(ctx.session, ctx.tenant, second.id, bench.warehouse.id) == 5
    await ctx.session.refresh(bench.line)
    assert bench.line.dispatched_quantity == 40
    await ctx.session.refresh(second_line)
    assert second_line.dispatched_quantity == 5
    assert second_line.status == SalesOrderLineStatus.DISPATCHED
    packed = await ctx.session.get(PackingOrder, packing_id)
    assert packed is not None and packed.total_cartons == 1 and packed.dispatch_id == detail.id


@pytest.mark.parametrize("mismatch", ["quantity", "missing_content"])
async def test_incorrect_carton_plan_cannot_create_a_dispatch(ctx: Ctx, mismatch: str) -> None:
    bench = await seed_bench(ctx)
    rows = [(bench.item.id, Decimal("40"), bench.line.id)]
    if mismatch == "missing_content":
        other, other_line = await carton_sales_line(ctx, bench)
        rows.append((other.id, Decimal("5"), other_line.id))
    packing_id, carton_id = await packed_carton(ctx, bench, rows)
    quantity = Decimal("39") if mismatch == "quantity" else Decimal("40")
    with pytest.raises(BusinessRuleError, match=r"does not match|some contents are missing"):
        await service.create_dispatch(
            ctx.session,
            ctx.tenant,
            bench.payload(
                "40",
                packing_order_id=packing_id,
                lines=[
                    DispatchLineIn(
                        item_id=bench.item.id,
                        quantity=quantity,
                        sales_order_line_id=bench.line.id,
                        carton_ids=[carton_id],
                    )
                ],
            ),
        )
    assert (
        await ctx.session.scalar(
            select(func.count()).select_from(Dispatch).where(Dispatch.company_id == ctx.company_id)
        )
        == 0
    )
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == balance.reserved_quantity == Decimal("100")
    await ctx.session.refresh(bench.line)
    assert bench.line.dispatched_quantity == 0
    packed = await ctx.session.get(PackingOrder, packing_id)
    assert packed is not None and packed.dispatch_id is None


async def test_carton_mismatch_on_patch_keeps_original_lines(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    item_id, demand_id = bench.item.id, bench.line.id
    packing_id, carton_id = await packed_carton(ctx, bench, [(item_id, Decimal("40"), demand_id)])
    detail = await service.create_dispatch(
        ctx.session,
        ctx.tenant,
        bench.payload(
            "40",
            packing_order_id=packing_id,
            lines=[
                DispatchLineIn(
                    item_id=item_id,
                    quantity=Decimal("40"),
                    sales_order_line_id=demand_id,
                    carton_ids=[carton_id],
                )
            ],
        ),
    )
    dispatch_id = detail.id
    with pytest.raises(BusinessRuleError, match="quantity does not match"):
        await service.update_dispatch(
            ctx.session,
            ctx.tenant,
            dispatch_id,
            DispatchUpdate(
                lines=[
                    DispatchLineIn(
                        item_id=item_id,
                        quantity=Decimal("39"),
                        sales_order_line_id=demand_id,
                        carton_ids=[carton_id],
                    )
                ]
            ),
        )
    unchanged = await service.get_dispatch_detail(ctx.session, ctx.tenant, dispatch_id)
    assert len(unchanged.lines) == 1 and unchanged.lines[0].quantity == 40
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == balance.reserved_quantity == Decimal("100")


async def test_legacy_carton_mismatch_is_refused_before_stock_posting(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    item_id, demand_id = bench.item.id, bench.line.id
    packing_id, carton_id = await packed_carton(ctx, bench, [(item_id, Decimal("40"), demand_id)])
    detail = await service.create_dispatch(
        ctx.session,
        ctx.tenant,
        bench.payload(
            "40",
            packing_order_id=packing_id,
            lines=[
                DispatchLineIn(
                    item_id=item_id,
                    quantity=Decimal("40"),
                    sales_order_line_id=demand_id,
                    carton_ids=[carton_id],
                )
            ],
        ),
    )
    dispatch_id = detail.id
    line = (
        await ctx.session.execute(
            select(DispatchLine).where(DispatchLine.dispatch_id == dispatch_id)
        )
    ).scalar_one()
    line.quantity = Decimal("39")
    await ctx.session.flush()
    await service.mark_ready(ctx.session, ctx.tenant, dispatch_id, reason="Legacy shipment")
    with pytest.raises(BusinessRuleError, match="quantity does not match"):
        await service.dispatch_consignment(
            ctx.session,
            ctx.tenant,
            dispatch_id,
            DispatchRequest(reason="Must not ship an inconsistent carton"),
        )
    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == balance.reserved_quantity == Decimal("100")
    await ctx.session.refresh(bench.line)
    assert bench.line.dispatched_quantity == 0
    assert (
        await ctx.session.scalar(
            select(func.count())
            .select_from(InventoryTransaction)
            .where(
                InventoryTransaction.reference_type == "dispatch",
                InventoryTransaction.reference_id == dispatch_id,
            )
        )
        == 0
    )
    unchanged = await service.get_dispatch_detail(ctx.session, ctx.tenant, dispatch_id)
    assert unchanged.status == DispatchStatus.READY_FOR_DISPATCH


async def test_carton_content_lookup_is_scoped_to_tenant_and_packing_order(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    packing_id, carton_id = await packed_carton(
        ctx, bench, [(bench.item.id, Decimal("40"), bench.line.id)]
    )
    owned = CartonLookupRepository(ctx.session, ctx.tenant)
    assert len(await owned.contents_of(packing_id, [carton_id])) == 1
    assert await owned.contents_of(uuid.uuid4(), [carton_id]) == []
    foreign = CartonLookupRepository(ctx.session, ctx.other_tenant)
    assert await foreign.contents_of(packing_id, [carton_id]) == []
    assert await foreign.sealed_cartons_of(packing_id, [carton_id]) == set()


# -----------------------------------------------------------------------------
# The lifecycle
# -----------------------------------------------------------------------------


async def test_a_consignment_that_was_never_made_ready_cannot_be_sent(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("10"))

    with pytest.raises(InvalidStateTransitionError):
        await service.dispatch_consignment(
            ctx.session, ctx.tenant, detail.id, DispatchRequest(reason="Too early")
        )

    balance = await balance_of(ctx, bench)
    assert balance.on_hand_quantity == Decimal("100.0000")


async def test_a_sent_consignment_can_no_longer_be_edited(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    with pytest.raises(BusinessRuleError):
        await service.update_dispatch(
            ctx.session, ctx.tenant, result.dispatch.id, DispatchUpdate(courier_name="Other")
        )


async def test_a_sent_consignment_cannot_be_cancelled(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    with pytest.raises(InvalidStateTransitionError):
        await service.cancel_dispatch(
            ctx.session, ctx.tenant, result.dispatch.id, reason="Changed our mind"
        )


async def test_every_status_change_is_audited(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    stmt = select(AuditLog.new_values).where(
        AuditLog.company_id == ctx.company_id,
        AuditLog.entity_type == service.AUDIT_DISPATCH,
        AuditLog.entity_id == result.dispatch.id,
        AuditLog.new_values.isnot(None),
    )
    statuses = {
        row.new_values.get("status")
        for row in (await ctx.session.execute(stmt)).all()
        if row.new_values
    }
    assert DispatchStatus.READY_FOR_DISPATCH.value in statuses
    assert DispatchStatus.DISPATCHED.value in statuses


# -----------------------------------------------------------------------------
# Delivery
# -----------------------------------------------------------------------------


async def test_confirming_delivery_records_the_date_and_a_tracking_event(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    arrived = TODAY + timedelta(days=3)
    detail = await service.confirm_delivery(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        DeliveryConfirmation(
            actual_delivery_date=arrived, delivery_note="Signed by the store keeper"
        ),
    )

    assert detail.status is DispatchStatus.DELIVERED
    assert detail.actual_delivery_date == arrived
    assert detail.delivery_confirmed_by == ctx.tenant.user_id

    events, _ = await service.list_tracking_events(
        ctx.session, ctx.tenant, result.dispatch.id, params=CursorParams(limit=50)
    )
    assert next(event.event_status for event in events) is DispatchStatus.DELIVERED
    assert any(event.event_status is DispatchStatus.DISPATCHED for event in events)


async def test_a_late_arrival_must_be_explained(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")
    late = TODAY + timedelta(days=9)  # promised for day 5

    with pytest.raises(BusinessRuleError) as excinfo:
        await service.confirm_delivery(
            ctx.session,
            ctx.tenant,
            result.dispatch.id,
            DeliveryConfirmation(actual_delivery_date=late),
        )
    assert excinfo.value.details["delay_days"] == 4

    detail = await service.confirm_delivery(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        DeliveryConfirmation(actual_delivery_date=late, delay_reason="Vehicle broke down"),
    )
    assert detail.delay_reason == "Vehicle broke down"


async def test_an_arrival_before_the_lorry_left_is_refused(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    with pytest.raises(BusinessRuleError):
        await service.confirm_delivery(
            ctx.session,
            ctx.tenant,
            result.dispatch.id,
            DeliveryConfirmation(actual_delivery_date=TODAY - timedelta(days=1)),
        )


# -----------------------------------------------------------------------------
# Tracking events
# -----------------------------------------------------------------------------


async def test_a_tracking_event_moves_the_consignment_through_the_machine(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    event = await service.add_tracking_event(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        TrackingEventIn(event_status=DispatchStatus.IN_TRANSIT, location="Pune hub"),
    )
    assert event.event_status is DispatchStatus.IN_TRANSIT

    detail = await service.get_dispatch_detail(ctx.session, ctx.tenant, result.dispatch.id)
    assert detail.status is DispatchStatus.IN_TRANSIT


async def test_a_courier_cannot_walk_a_consignment_into_an_illegal_status(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    with pytest.raises(InvalidStateTransitionError):
        await service.add_tracking_event(
            ctx.session,
            ctx.tenant,
            result.dispatch.id,
            TrackingEventIn(event_status=DispatchStatus.GRN_COMPLETED),
        )


async def test_a_position_update_appends_history_without_moving_the_status(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")
    await service.add_tracking_event(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        TrackingEventIn(event_status=DispatchStatus.IN_TRANSIT, location="Pune hub"),
    )

    await service.add_tracking_event(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        TrackingEventIn(event_status=DispatchStatus.IN_TRANSIT, location="Nashik hub"),
    )

    detail = await service.get_dispatch_detail(ctx.session, ctx.tenant, result.dispatch.id)
    assert detail.status is DispatchStatus.IN_TRANSIT
    events, _ = await service.list_tracking_events(
        ctx.session, ctx.tenant, result.dispatch.id, params=CursorParams(limit=50)
    )
    assert [e.location for e in events if e.location][:2] == ["Nashik hub", "Pune hub"]


# -----------------------------------------------------------------------------
# The tracking board and the delay sweep
# -----------------------------------------------------------------------------


async def test_the_tracking_board_answers_every_column_from_one_row(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    rows, _ = await service.tracking_board(
        ctx.session,
        ctx.tenant,
        filters=DispatchFilters(),
        params=PageParams(page=1, limit=50),
    )
    row = next(r for r in rows if r.dispatch_id == result.dispatch.id)

    assert row.party_name == bench.customer.name
    assert row.party_type == "CUSTOMER"
    assert row.reference_code == bench.order.code
    assert row.status is DispatchStatus.DISPATCHED
    assert row.expected_delivery_date == TODAY + timedelta(days=5)
    assert row.qc_pending is False
    assert row.grn_pending is False


async def test_a_delivered_consignment_still_owes_a_goods_receipt(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")
    await service.confirm_delivery(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        DeliveryConfirmation(actual_delivery_date=TODAY + timedelta(days=2)),
    )

    rows, _ = await service.tracking_board(
        ctx.session,
        ctx.tenant,
        filters=DispatchFilters(),
        params=PageParams(page=1, limit=50),
    )
    row = next(r for r in rows if r.dispatch_id == result.dispatch.id)
    assert row.grn_pending is True
    assert row.qc_pending is False


async def test_the_delay_sweep_flags_overdue_consignments_and_alerts_their_owners(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")

    # Long past the promised date. ``as_of`` is passed rather than freezing the
    # clock so the assertion is about the predicate, not about today.
    overdue_on = TODAY + timedelta(days=12)
    scanned = await service.scan_delays(ctx.session, as_of=overdue_on)

    ours = next(item for item in scanned if item.company_id == ctx.company_id)
    assert ours.delayed_count >= 1
    assert ours.notified_count >= 1

    flagged = (
        await ctx.session.execute(
            select(Dispatch)
            .where(Dispatch.id == result.dispatch.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert flagged.delay_notified_at is not None
    # The status is untouched: being late is a derived fact, not a state.
    assert flagged.status == DispatchStatus.DISPATCHED

    alerts = (
        await ctx.session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.company_id == ctx.company_id,
                Notification.notification_type == NotificationType.DISPATCH_DELAYED,
            )
        )
    ).scalar_one()
    assert alerts >= 1


async def test_a_delivered_consignment_is_never_swept_up_as_late(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    result = await send(ctx, bench, "10")
    await service.confirm_delivery(
        ctx.session,
        ctx.tenant,
        result.dispatch.id,
        DeliveryConfirmation(
            actual_delivery_date=TODAY + timedelta(days=9), delay_reason="Vehicle broke down"
        ),
    )

    rows, _ = await service.list_delayed(
        ctx.session, ctx.tenant, params=PageParams(page=1, limit=50)
    )
    assert all(row.dispatch_id != result.dispatch.id for row in rows)


# -----------------------------------------------------------------------------
# Validation and isolation
# -----------------------------------------------------------------------------


async def test_a_line_cannot_reference_another_orders_line(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    other = await seed_bench(ctx)

    payload = bench.payload("10")
    payload.lines[0].sales_order_line_id = other.line.id

    with pytest.raises(ValidationError, match="different sales order"):
        await service.create_dispatch(ctx.session, ctx.tenant, payload)


async def test_a_consignment_against_an_order_must_name_the_order_line_of_every_line(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx)
    payload = bench.payload("10")
    payload.lines[0].sales_order_line_id = None

    # The defect this closes: a line with no order line skipped the reservation
    # and the order-line arithmetic, shipped free stock, and still walked the
    # order to PARTIALLY_DISPATCHED.
    with pytest.raises(ValidationError, match="Line 1") as excinfo:
        await service.create_dispatch(ctx.session, ctx.tenant, payload)
    assert excinfo.value.details["line"] == 1


async def test_a_consignment_against_an_order_leaves_from_the_orders_own_warehouse(
    ctx: Ctx,
) -> None:
    bench = await seed_bench(ctx)
    elsewhere = await seed_warehouse(ctx)

    # The reservation is held at the order's warehouse and is consumed there; a
    # document naming another store would record goods leaving where they never were.
    with pytest.raises(ValidationError, match="must leave from that warehouse"):
        await service.create_dispatch(
            ctx.session, ctx.tenant, bench.payload("10", warehouse_id=elsewhere.id)
        )


async def test_a_line_for_an_item_the_order_does_not_carry_is_refused(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    stranger = await seed_item(ctx, bench.uom)
    payload = bench.payload("10")
    payload.lines[0].item_id = stranger.id

    with pytest.raises(ValidationError, match="different item"):
        await service.create_dispatch(ctx.session, ctx.tenant, payload)


async def test_one_order_line_may_appear_on_a_consignment_only_once(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    payload = bench.payload("10")
    payload.lines.append(
        DispatchLineIn(
            item_id=bench.item.id, quantity=Decimal("5"), sales_order_line_id=bench.line.id
        )
    )

    with pytest.raises(ValidationError, match="two lines"):
        await service.create_dispatch(ctx.session, ctx.tenant, payload)


async def test_a_direct_sale_with_no_order_still_ships_from_free_stock(ctx: Ctx) -> None:
    bench = await seed_bench(ctx, ordered="40", on_hand="100")
    payload = bench.payload("25", sales_order_id=None)
    payload.lines[0].sales_order_line_id = None

    detail = await service.create_dispatch(ctx.session, ctx.tenant, payload)
    await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Loaded")
    result = await service.dispatch_consignment(
        ctx.session, ctx.tenant, detail.id, DispatchRequest(reason="Counter sale")
    )

    assert result.consumed_reservation_ids == []
    balance = await balance_of(ctx, bench)
    # 100 on hand, 40 promised to the order and untouched by the counter sale.
    assert balance.on_hand_quantity == Decimal("75.0000")
    assert balance.reserved_quantity == Decimal("40.0000")


async def test_a_consignment_with_no_lines_cannot_be_made_ready(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("10"))
    # Emptying the line set is only reachable internally; do it the way the
    # update path would if a caller had removed every line.
    await ctx.session.execute(
        text("DELETE FROM dispatch_lines WHERE dispatch_id = :id"), {"id": detail.id}
    )

    with pytest.raises(BusinessRuleError):
        await service.mark_ready(ctx.session, ctx.tenant, detail.id, reason="Nothing loaded")


async def test_another_tenant_cannot_see_or_touch_the_consignment(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    detail = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("10"))

    with pytest.raises(NotFoundError):
        await service.get_dispatch_detail(ctx.session, ctx.other_tenant, detail.id)

    rows, _ = await service.list_dispatches(
        ctx.session,
        ctx.other_tenant,
        filters=DispatchFilters(),
        params=PageParams(page=1, limit=50),
        sort=SortParams(),
    )
    assert all(row.id != detail.id for row in rows)


async def test_a_draft_consignment_can_be_deleted_and_a_prepared_one_cannot(ctx: Ctx) -> None:
    bench = await seed_bench(ctx)
    draft = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("10"))
    await service.delete_draft_dispatch(ctx.session, ctx.tenant, draft.id, reason="Raised in error")
    with pytest.raises(NotFoundError):
        await service.get_dispatch_detail(ctx.session, ctx.tenant, draft.id)

    prepared = await service.create_dispatch(ctx.session, ctx.tenant, bench.payload("10"))
    await service.mark_ready(ctx.session, ctx.tenant, prepared.id, reason="Loaded")
    with pytest.raises(BusinessRuleError):
        await service.delete_draft_dispatch(ctx.session, ctx.tenant, prepared.id, reason="Too late")
