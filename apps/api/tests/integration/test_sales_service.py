"""Service-level tests for sales orders against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible
without it: the generated ``available_quantity`` column that decides what may be
promised, the CHECK constraints that bound ``allocated_quantity`` by the ordered
quantity, the transaction-scoped advisory lock that serialises two
confirmations competing for the same item, the ``GROUP BY`` behind the demand
summary, and the audit row written inside the same transaction as the change it
describes.

Isolation follows the pattern in ``test_bom_service.py``: one throwaway schema
per module, built once, with each test's writes rolled back.

The allocation engine is reached through a recording wrapper
(:class:`FakeAllocation`) around the real ``AllocationService``. What these tests
are about is *this* module's rule - reserve the minimum of demand and
availability, report the rest - but since WP E1a the module also reads each
line's *live* reservation (a released or consumed one counts as none), so the
reservations have to be real rows: a double that only moved
``inventory_balances.reserved_quantity`` would leave every line reading as
unreserved. The wrapper records the calls the assertions below inspect.
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

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    ApprovalRequiredError,
    AuthorizationError,
    BusinessRuleError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.allocation.models import (
    DemandType,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import AddressType, Customer, CustomerAddress, CustomerType
from src.modules.inventory.models import InventoryBalance
from src.modules.organization.models import Company
from src.modules.products.models import (
    Item,
    ItemGroup,
    ItemPrice,
    PriceType,
    ProductionType,
    UnitOfMeasure,
    UomCategory,
)
from src.modules.sales import service
from src.modules.sales.models import (
    SalesOrder,
    SalesOrderLine,
    SalesOrderLineStatus,
    SalesOrderStatus,
)
from src.modules.sales.schemas import (
    DemandSummaryFilters,
    SalesOrderCreate,
    SalesOrderFilters,
    SalesOrderLineIn,
    SalesOrderUpdate,
)
from src.modules.users.models import User
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
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and two users, built once."""
    prepare_metadata()

    schema = f"it_sales_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    author_id = uuid.uuid4()
    approver_id = uuid.uuid4()

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
                for cid, code, state in (
                    (company_id, f"ATC{uuid.uuid4().hex[:6].upper()}", "27"),
                    (other_company_id, f"OTH{uuid.uuid4().hex[:6].upper()}", "27"),
                ):
                    setup_session.add(
                        Company(
                            id=cid,
                            code=code,
                            name="Atcraft Manufacturing",
                            country="India",
                            currency="INR",
                            # Maharashtra: the seller's state, and therefore the
                            # thing every GST assertion below turns on.
                            state_code=state,
                            is_active=True,
                        )
                    )
                for uid, handle in ((author_id, "author"), (approver_id, "approver")):
                    setup_session.add(
                        User(
                            id=uid,
                            company_id=company_id,
                            email=f"{handle}-{uuid.uuid4().hex[:6]}@atcraft.test",
                            full_name="Tester",
                            password_hash="x",
                            is_active=True,
                        )
                    )
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

    yield schema, company_id, other_company_id, author_id, approver_id
    asyncio.run(teardown())


# -----------------------------------------------------------------------------
# Allocation double
# -----------------------------------------------------------------------------


class FakeAllocation:
    """A minimal but honest stand-in for ``allocation.service``.

    It moves ``inventory_balances.reserved_quantity`` for real, so the generated
    ``available_quantity`` column falls as stock is promised - which is what
    makes "the second order sees what the first one took" a genuine assertion
    rather than a scripted one.
    """

    def __init__(self) -> None:
        self.held: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, Decimal]] = {}
        self.reserve_calls: list[dict[str, object]] = []
        self.release_calls: list[uuid.UUID] = []

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        quantity: Decimal,
        demand_id: uuid.UUID,
        demand_line_id: uuid.UUID,
        demand_code: str,
        priority: int,
        required_date: date,
        reason: str,
    ) -> service.ReservationRef:
        self.reserve_calls.append(
            {
                "item_id": item_id,
                "warehouse_id": warehouse_id,
                "quantity": Decimal(quantity),
                "demand_id": demand_id,
                "demand_line_id": demand_line_id,
                "demand_code": demand_code,
                "priority": priority,
                "required_date": required_date,
            }
        )
        reservation = await service.AllocationService().reserve(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            demand_id=demand_id,
            demand_line_id=demand_line_id,
            demand_code=demand_code,
            priority=priority,
            required_date=required_date,
            reason=reason,
        )
        reservation_id = reservation.id
        self.held[reservation_id] = (item_id, warehouse_id, Decimal(quantity))
        return service.ReservationRef(id=reservation_id, quantity=Decimal(quantity))

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> None:
        self.held.pop(reservation_id)
        self.release_calls.append(reservation_id)
        await service.AllocationService().release(
            session, tenant, reservation_id=reservation_id, reason=reason, quantity=quantity
        )

    @property
    def total_reserved(self) -> Decimal:
        return sum((qty for _, _, qty in self.held.values()), Decimal("0"))


# -----------------------------------------------------------------------------
# Context
# -----------------------------------------------------------------------------


def _principal(
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    permissions: frozenset[str] | None = None,
) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email=f"{user_id}@atcraft.test",
        full_name="Tester",
        is_superuser=False,
        permissions=permissions or frozenset(str(p) for p in Permission),
        role_codes=("SALES_EXECUTIVE",),
    )


class Ctx:
    def __init__(
        self,
        session: AsyncSession,
        company_id: uuid.UUID,
        other_company_id: uuid.UUID,
        author_id: uuid.UUID,
        approver_id: uuid.UUID,
    ) -> None:
        self.session = session
        self.tenant = TenantContext(company_id=company_id, user_id=author_id)
        self.approver_tenant = TenantContext(company_id=company_id, user_id=approver_id)
        self.other_tenant = TenantContext(company_id=other_company_id, user_id=uuid.uuid4())
        self.principal = _principal(company_id, author_id)
        self.approver = _principal(company_id, approver_id)
        self.allocation = FakeAllocation()


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[Ctx]:
    schema, company_id, other_company_id, author_id, approver_id = _schema
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
                yield Ctx(session, company_id, other_company_id, author_id, approver_id)
            await outer.rollback()
    finally:
        await eng.dispose()


# -----------------------------------------------------------------------------
# Seeding helpers
# -----------------------------------------------------------------------------


async def seed_uom(ctx: Ctx) -> UnitOfMeasure:
    uom = UnitOfMeasure(
        company_id=ctx.tenant.company_id,
        code=f"NOS{uuid.uuid4().hex[:6].upper()}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
    )
    ctx.session.add(uom)
    await ctx.session.flush()
    return uom


async def seed_item(
    ctx: Ctx,
    uom: UnitOfMeasure,
    *,
    sale_price: str = "100",
    standard_cost: str = "60",
    gst_rate: str = "18",
    can_sell: bool = True,
) -> Item:
    item = Item(
        company_id=ctx.tenant.company_id,
        code=f"FG-{uuid.uuid4().hex[:8].upper()}",
        name="Mortise Lock",
        item_group=ItemGroup.FINISHED_GOOD,
        base_uom_id=uom.id,
        production_type=ProductionType.MAKE,
        gst_rate=Decimal(gst_rate),
        sale_price=Decimal(sale_price),
        dealer_price=Decimal(sale_price),
        standard_cost=Decimal(standard_cost),
        can_sell=can_sell,
        can_manufacture=True,
    )
    ctx.session.add(item)
    await ctx.session.flush()
    return item


async def seed_warehouse(ctx: Ctx) -> Warehouse:
    warehouse = Warehouse(
        company_id=ctx.tenant.company_id,
        code=f"WH{uuid.uuid4().hex[:6].upper()}",
        name="Finished goods store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        state_code="27",
        is_active=True,
    )
    ctx.session.add(warehouse)
    await ctx.session.flush()
    return warehouse


async def seed_customer(
    ctx: Ctx,
    *,
    state_code: str = "27",
    credit_limit: str | None = None,
    price_list_type: PriceType = PriceType.SALE,
    is_blocked: bool = False,
) -> tuple[Customer, CustomerAddress]:
    customer = Customer(
        company_id=ctx.tenant.company_id,
        code=f"CU{uuid.uuid4().hex[:8].upper()}",
        name="Hardware Distributors",
        customer_type=CustomerType.CUSTOMER,
        price_list_type=price_list_type,
        credit_limit=None if credit_limit is None else Decimal(credit_limit),
        credit_days=30,
        is_active=True,
        is_blocked=is_blocked,
        block_reason="Overdue" if is_blocked else None,
        blocked_at=None,
    )
    if is_blocked:
        from src.core.clock import utcnow

        customer.blocked_at = utcnow()
    ctx.session.add(customer)
    await ctx.session.flush()

    address = CustomerAddress(
        company_id=ctx.tenant.company_id,
        customer_id=customer.id,
        address_type=AddressType.SHIPPING,
        address_line1="Plot 14, MIDC",
        city="Pune",
        state_code=state_code,
        country="India",
        is_default=True,
        is_active=True,
    )
    ctx.session.add(address)
    await ctx.session.flush()
    return customer, address


async def seed_stock(ctx: Ctx, item: Item, warehouse: Warehouse, on_hand: str) -> InventoryBalance:
    """Put physical stock on the shelf.

    Written straight to the balance row rather than through a ledger movement:
    what these tests exercise is how sales reads ``available_quantity``, and
    that column is generated by PostgreSQL from the columns set here, so the
    number under test is produced by the real mechanism either way.
    """
    balance = InventoryBalance(
        company_id=ctx.tenant.company_id,
        item_id=item.id,
        warehouse_id=warehouse.id,
        on_hand_quantity=Decimal(on_hand),
    )
    ctx.session.add(balance)
    await ctx.session.flush()
    await ctx.session.refresh(balance)
    return balance


async def available_of(ctx: Ctx, item: Item, warehouse: Warehouse) -> Decimal:
    stmt = select(func.coalesce(func.sum(InventoryBalance.available_quantity), 0)).where(
        InventoryBalance.company_id == ctx.tenant.company_id,
        InventoryBalance.item_id == item.id,
        InventoryBalance.warehouse_id == warehouse.id,
    )
    return Decimal(str((await ctx.session.execute(stmt)).scalar_one()))


def create_payload(
    customer: Customer,
    warehouse: Warehouse,
    lines: list[SalesOrderLineIn],
    **overrides: object,
) -> SalesOrderCreate:
    payload: dict[str, object] = {
        "customer_id": customer.id,
        "warehouse_id": warehouse.id,
        "required_date": today() + timedelta(days=14),
        "lines": lines,
    }
    payload.update(overrides)
    return SalesOrderCreate(**payload)  # type: ignore[arg-type]


def line(item: Item, quantity: str, number: int = 1, **overrides: object) -> SalesOrderLineIn:
    payload: dict[str, object] = {
        "line_number": number,
        "item_id": item.id,
        "quantity": Decimal(quantity),
    }
    payload.update(overrides)
    return SalesOrderLineIn(**payload)  # type: ignore[arg-type]


async def make_draft(
    ctx: Ctx, customer: Customer, warehouse: Warehouse, lines: list[SalesOrderLineIn], **kw: object
) -> SalesOrder:
    order, _ = await service.create_order(
        ctx.session, ctx.tenant, ctx.principal, create_payload(customer, warehouse, lines, **kw)
    )
    return order


# -----------------------------------------------------------------------------
# Creation, pricing and tax
# -----------------------------------------------------------------------------


async def test_create_prices_from_the_price_list_and_numbers_the_document(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom, sale_price="100")
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)

    ctx.session.add(
        ItemPrice(
            company_id=ctx.tenant.company_id,
            item_id=item.id,
            price_type=PriceType.SALE,
            price=Decimal("85"),
            min_quantity=Decimal("50"),
            valid_from=today() - timedelta(days=30),
            is_active=True,
        )
    )
    await ctx.session.flush()

    order, lines = await service.create_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        create_payload(customer, warehouse, [line(item, "60")]),
    )

    assert order.code.startswith("SO/")
    assert order.status == SalesOrderStatus.DRAFT
    # The 50-unit slab applies to an order for 60, not the item master price.
    assert lines[0].unit_price == Decimal("85.000000")
    assert lines[0].line_total == Decimal("6018.00")
    assert order.total_amount == Decimal("6018.00")


async def test_same_state_shipping_address_produces_cgst_and_sgst(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom, sale_price="100", gst_rate="18")
    warehouse = await seed_warehouse(ctx)
    # Company is registered in 27; shipping into 27 is an intra-state supply.
    customer, _ = await seed_customer(ctx, state_code="27")

    order, lines = await service.create_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        create_payload(customer, warehouse, [line(item, "10")]),
    )

    assert order.is_interstate is False
    assert order.place_of_supply_state_code == "27"
    assert lines[0].cgst_amount == lines[0].sgst_amount == Decimal("90.0000")
    assert lines[0].igst_amount == Decimal("0.0000")
    assert order.tax_amount == Decimal("180.0000")


async def test_other_state_shipping_address_produces_igst(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom, sale_price="100", gst_rate="18")
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx, state_code="29")

    order, lines = await service.create_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        create_payload(customer, warehouse, [line(item, "10")]),
    )

    assert order.is_interstate is True
    assert lines[0].igst_amount == Decimal("180.0000")
    assert lines[0].cgst_amount == lines[0].sgst_amount == Decimal("0.0000")


async def test_an_unsellable_item_is_refused(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom, can_sell=False)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)

    with pytest.raises(BusinessRuleError, match="sellable"):
        await make_draft(ctx, customer, warehouse, [line(item, "5")])


async def test_a_warehouse_from_another_tenant_is_not_found(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom)
    customer, _ = await seed_customer(ctx)

    foreign = Warehouse(
        company_id=ctx.other_tenant.company_id,
        code=f"WH{uuid.uuid4().hex[:6].upper()}",
        name="Someone else's store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    ctx.session.add(foreign)
    await ctx.session.flush()

    with pytest.raises(NotFoundError):
        await make_draft(ctx, customer, foreign, [line(item, "5")])


async def test_a_price_override_without_price_manage_is_refused(ctx: Ctx):
    uom = await seed_uom(ctx)
    item = await seed_item(ctx, uom)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)

    weak = _principal(
        ctx.tenant.company_id,
        ctx.tenant.user_id,
        permissions=frozenset({str(Permission.SALES_ORDER_CREATE)}),
    )

    with pytest.raises(PermissionDeniedError):
        await service.create_order(
            ctx.session,
            ctx.tenant,
            weak,
            create_payload(customer, warehouse, [line(item, "5", unit_price=Decimal("40"))]),
        )


# -----------------------------------------------------------------------------
# Confirmation with partial stock - the behaviour this module exists for
# -----------------------------------------------------------------------------


async def test_confirm_with_partial_stock_reserves_what_exists_and_reports_the_rest(ctx: Ctx):
    """Master rules section 72: a short line is a shortage, not a rejection."""
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)

    plentiful = await seed_item(ctx, uom, sale_price="100")
    scarce = await seed_item(ctx, uom, sale_price="200")
    absent = await seed_item(ctx, uom, sale_price="300")

    await seed_stock(ctx, plentiful, warehouse, "500")
    await seed_stock(ctx, scarce, warehouse, "30")
    # `absent` has no balance row at all: never received, not merely at zero.

    order = await make_draft(
        ctx,
        customer,
        warehouse,
        [line(plentiful, "100", 1), line(scarce, "80", 2), line(absent, "10", 3)],
    )

    result = await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Customer confirmed by email",
        allocation=ctx.allocation,
    )

    by_number = {row.line_number: row for row in result.allocation.lines}

    assert by_number[1].allocated_quantity == Decimal("100.0000")
    assert by_number[1].shortfall_quantity == Decimal("0.0000")
    assert by_number[1].status is SalesOrderLineStatus.ALLOCATED

    # Reserve what exists: 30 of the 80 asked for.
    assert by_number[2].allocated_quantity == Decimal("30.0000")
    assert by_number[2].shortfall_quantity == Decimal("50.0000")
    assert by_number[2].status is SalesOrderLineStatus.PARTIALLY_ALLOCATED

    # Nothing on the shelf: nothing reserved, and no empty reservation taken.
    assert by_number[3].allocated_quantity == Decimal("0.0000")
    assert by_number[3].shortfall_quantity == Decimal("10.0000")
    assert by_number[3].status is SalesOrderLineStatus.PENDING
    assert by_number[3].reservation_id is None

    assert result.allocation.fully_allocated is False
    assert result.allocation.total_shortfall_lines == 2

    # The order is CONFIRMED, not ALLOCATED: it is not fully covered.
    assert result.order.status is SalesOrderStatus.CONFIRMED

    # Exactly two reservations were taken, for exactly the quantities reported.
    assert len(ctx.allocation.reserve_calls) == 2
    assert {call["quantity"] for call in ctx.allocation.reserve_calls} == {
        Decimal("100.0000"),
        Decimal("30.0000"),
    }
    # And the stock really is promised away now.
    assert await available_of(ctx, scarce, warehouse) == Decimal("0.0000")
    assert await available_of(ctx, plentiful, warehouse) == Decimal("400.0000")


async def test_confirm_with_full_stock_moves_the_order_to_allocated(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "500")

    order = await make_draft(ctx, customer, warehouse, [line(item, "100")])
    result = await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Stock confirmed",
        allocation=ctx.allocation,
    )

    assert result.order.status is SalesOrderStatus.ALLOCATED
    assert result.allocation.fully_allocated is True


async def test_two_lines_for_the_same_item_share_one_pool(ctx: Ctx):
    """Both lines must not be promised the same units.

    The classic double-promise: 60 in stock, two lines of 40. The first takes
    40, the second may only have 20.
    """
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "60")

    order = await make_draft(ctx, customer, warehouse, [line(item, "40", 1), line(item, "40", 2)])
    result = await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    allocated = sum(row.allocated_quantity for row in result.allocation.lines)
    assert allocated == Decimal("60.0000")
    assert await available_of(ctx, item, warehouse) == Decimal("0.0000")


async def test_a_second_order_only_sees_what_the_first_left_behind(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    first = await make_draft(ctx, customer, warehouse, [line(item, "70")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        first.id,
        reason="First",
        allocation=ctx.allocation,
    )

    second = await make_draft(ctx, customer, warehouse, [line(item, "70")])
    result = await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        second.id,
        reason="Second",
        allocation=ctx.allocation,
    )

    assert result.allocation.lines[0].allocated_quantity == Decimal("30.0000")
    assert result.allocation.lines[0].shortfall_quantity == Decimal("40.0000")


async def test_confirming_writes_a_status_change_and_a_reservation_audit_row(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "10")

    order = await make_draft(ctx, customer, warehouse, [line(item, "40")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    stmt = select(AuditLog).where(
        AuditLog.company_id == ctx.tenant.company_id,
        AuditLog.entity_type == service.AUDIT_SALES_ORDER,
        AuditLog.entity_id == order.id,
    )
    entries = list((await ctx.session.execute(stmt)).scalars().all())
    actions = {entry.action for entry in entries}

    assert AuditAction.STATUS_CHANGE in actions
    assert AuditAction.RESERVATION_CHANGE in actions

    reservation_entry = next(
        entry for entry in entries if entry.action == AuditAction.RESERVATION_CHANGE
    )
    # The shortfall is on the audit trail, not only in the HTTP response.
    assert reservation_entry.new_values["shortfall_by_line"] == {"1": "30.0000"}


async def test_a_confirmed_order_cannot_be_confirmed_again(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "5")

    order = await make_draft(ctx, customer, warehouse, [line(item, "100")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    with pytest.raises(InvalidStateTransitionError):
        await service.confirm_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            reason="Again",
            allocation=ctx.allocation,
        )


# -----------------------------------------------------------------------------
# Credit
# -----------------------------------------------------------------------------


async def test_confirmation_is_refused_past_the_credit_limit(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    item = await seed_item(ctx, uom, sale_price="1000")
    await seed_stock(ctx, item, warehouse, "500")
    customer, _ = await seed_customer(ctx, credit_limit="1000")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])

    with pytest.raises(BusinessRuleError, match="credit limit"):
        await service.confirm_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            reason="Confirmed",
            allocation=ctx.allocation,
        )

    # Nothing was reserved: the whole confirmation rolled back together.
    assert ctx.allocation.reserve_calls == []
    assert await available_of(ctx, item, warehouse) == Decimal("500.0000")


async def test_a_draft_may_be_raised_for_a_customer_that_could_not_confirm(ctx: Ctx):
    """Credit is consumed at confirmation, not at quotation time."""
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    item = await seed_item(ctx, uom, sale_price="1000")
    customer, _ = await seed_customer(ctx, credit_limit="1")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    assert order.status == SalesOrderStatus.DRAFT


# -----------------------------------------------------------------------------
# Below-cost approval
# -----------------------------------------------------------------------------


async def test_a_below_cost_override_blocks_confirmation_until_approved(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, sale_price="100", standard_cost="60")
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10", unit_price=Decimal("55"))])
    assert order.requires_approval is True

    with pytest.raises(ApprovalRequiredError):
        await service.confirm_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            reason="Confirmed",
            allocation=ctx.allocation,
        )

    await service.approve_order(
        ctx.session, ctx.approver_tenant, ctx.approver, order.id, reason="Strategic account"
    )
    result = await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    assert result.order.status is SalesOrderStatus.ALLOCATED


async def test_the_author_cannot_approve_their_own_order(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, standard_cost="60")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10", unit_price=Decimal("55"))])

    with pytest.raises(AuthorizationError):
        await service.approve_order(
            ctx.session, ctx.tenant, ctx.principal, order.id, reason="Signing my own work"
        )


# -----------------------------------------------------------------------------
# Cancellation
# -----------------------------------------------------------------------------


async def test_cancelling_releases_every_reservation(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    first = await seed_item(ctx, uom)
    second = await seed_item(ctx, uom)
    await seed_stock(ctx, first, warehouse, "100")
    await seed_stock(ctx, second, warehouse, "100")

    order = await make_draft(
        ctx, customer, warehouse, [line(first, "40", 1), line(second, "25", 2)]
    )
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )
    assert await available_of(ctx, first, warehouse) == Decimal("60.0000")

    await service.cancel_order(
        ctx.session, ctx.tenant, order.id, reason="Customer withdrew", allocation=ctx.allocation
    )

    assert len(ctx.allocation.release_calls) == 2
    assert ctx.allocation.total_reserved == Decimal("0")
    # The stock is promisable again.
    assert await available_of(ctx, first, warehouse) == Decimal("100.0000")
    assert await available_of(ctx, second, warehouse) == Decimal("100.0000")

    lines = await service.get_order_detail(ctx.session, ctx.tenant, order.id)
    assert lines.status is SalesOrderStatus.CANCELLED
    assert lines.cancellation_reason == "Customer withdrew"
    assert all(row.allocated_quantity == Decimal("0.0000") for row in lines.lines)
    assert all(row.status is SalesOrderLineStatus.CANCELLED for row in lines.lines)


# -----------------------------------------------------------------------------
# Editing a confirmed order
# -----------------------------------------------------------------------------


async def test_raising_a_confirmed_quantity_takes_more_stock(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, sale_price="100")
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "40")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    _, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(lines=[line(item, "70")]),
        allocation=ctx.allocation,
    )

    assert lines[0].quantity == Decimal("70.0000")
    assert lines[0].allocated_quantity == Decimal("70.0000")
    assert await available_of(ctx, item, warehouse) == Decimal("30.0000")
    # The amounts follow the quantity at the agreed price.
    assert lines[0].line_total == Decimal("8260.00")


async def _confirmed_order_of_forty(ctx: Ctx, customer: Customer, warehouse: Warehouse, item: Item):
    """Forty units at 100 plus 18% GST: a 4,720.00 order, confirmed and reserved."""
    order = await make_draft(ctx, customer, warehouse, [line(item, "40")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )
    return order.id


async def _stored_total(ctx: Ctx, order_id: uuid.UUID) -> Decimal:
    return Decimal(
        (
            await ctx.session.execute(
                select(SalesOrder.total_amount).where(SalesOrder.id == order_id)
            )
        ).scalar_one()
    )


async def test_raising_a_confirmed_quantity_past_the_credit_limit_is_refused(ctx: Ctx):
    """The credit check ran at confirmation for the total at that moment. Growing
    the order afterwards is exposure it never saw, so it runs again - against
    the full prospective total - and a refusal leaves the reservation and the
    stored total exactly as they were."""
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    # 4,720.00 fits under the limit; 70 x 100 + 18% = 8,260.00 does not.
    customer, _ = await seed_customer(ctx, credit_limit="5000")
    item = await seed_item(ctx, uom, sale_price="100")
    await seed_stock(ctx, item, warehouse, "100")
    order_id = await _confirmed_order_of_forty(ctx, customer, warehouse, item)
    reserve_calls_before = len(ctx.allocation.reserve_calls)

    with pytest.raises(BusinessRuleError, match="credit limit"):
        await service.update_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order_id,
            SalesOrderUpdate(lines=[line(item, "70")]),
            allocation=ctx.allocation,
        )

    assert len(ctx.allocation.reserve_calls) == reserve_calls_before
    assert await _stored_total(ctx, order_id) == Decimal("4720.00")
    assert await available_of(ctx, item, warehouse) == Decimal("60.0000")


async def test_a_customer_put_on_hold_after_confirmation_cannot_grow_the_order(ctx: Ctx):
    """A hold placed after confirmation is caught by the same re-check: the
    growth is refused as a credit hold, and nothing about the order moves."""
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, sale_price="100")
    await seed_stock(ctx, item, warehouse, "100")
    order_id = await _confirmed_order_of_forty(ctx, customer, warehouse, item)
    reserve_calls_before = len(ctx.allocation.reserve_calls)

    customer.is_blocked = True
    customer.block_reason = "Overdue"
    customer.blocked_at = utcnow()
    await ctx.session.flush()

    with pytest.raises(BusinessRuleError, match="credit hold"):
        await service.update_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order_id,
            SalesOrderUpdate(lines=[line(item, "70")]),
            allocation=ctx.allocation,
        )

    assert len(ctx.allocation.reserve_calls) == reserve_calls_before
    assert await _stored_total(ctx, order_id) == Decimal("4720.00")
    assert await available_of(ctx, item, warehouse) == Decimal("60.0000")


async def test_lowering_a_confirmed_quantity_gives_stock_back(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "80")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    _, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(lines=[line(item, "20")]),
        allocation=ctx.allocation,
    )

    assert lines[0].allocated_quantity == Decimal("20.0000")
    assert await available_of(ctx, item, warehouse) == Decimal("80.0000")


async def test_raising_a_quantity_past_free_stock_leaves_a_shortfall(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "50")

    order = await make_draft(ctx, customer, warehouse, [line(item, "40")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    _, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(lines=[line(item, "90")]),
        allocation=ctx.allocation,
    )

    assert lines[0].quantity == Decimal("90.0000")
    assert lines[0].allocated_quantity == Decimal("50.0000")
    assert lines[0].status == SalesOrderLineStatus.PARTIALLY_ALLOCATED

    view = await service.allocation_status(ctx.session, ctx.tenant, order.id)
    assert view.lines[0].shortfall_quantity == Decimal("40.0000")


async def test_a_confirmed_order_refuses_a_new_line(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    other = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    with pytest.raises(BusinessRuleError, match="added or removed"):
        await service.update_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            SalesOrderUpdate(lines=[line(item, "10", 1), line(other, "5", 2)]),
            allocation=ctx.allocation,
        )


async def test_a_confirmed_order_refuses_a_warehouse_change(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    elsewhere = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    with pytest.raises(BusinessRuleError, match="cannot be changed"):
        await service.update_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            SalesOrderUpdate(warehouse_id=elsewhere.id),
            allocation=ctx.allocation,
        )


async def test_a_draft_is_repriced_when_its_lines_are_replaced(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, sale_price="100", gst_rate="18")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    assert order.total_amount == Decimal("1180.0000")

    updated, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(lines=[line(item, "20")]),
        allocation=ctx.allocation,
    )

    assert len(lines) == 1
    assert updated.total_amount == Decimal("2360.00")


# -----------------------------------------------------------------------------
# Deletion
# -----------------------------------------------------------------------------


async def test_a_confirmed_order_cannot_be_deleted(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    with pytest.raises(BusinessRuleError, match="cancelled rather than deleted"):
        await service.delete_draft_order(
            ctx.session, ctx.tenant, order.id, reason="Changed my mind"
        )


async def test_deleting_a_draft_takes_its_lines_with_it(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.delete_draft_order(ctx.session, ctx.tenant, order.id, reason="Duplicate entry")

    remaining = await ctx.session.execute(
        select(func.count())
        .select_from(SalesOrderLine)
        .where(SalesOrderLine.sales_order_id == order.id)
    )
    assert remaining.scalar_one() == 0


# -----------------------------------------------------------------------------
# Demand summary
# -----------------------------------------------------------------------------


async def test_demand_summary_consolidates_open_demand_per_item(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    other = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "30")

    first = await make_draft(ctx, customer, warehouse, [line(item, "50", 1), line(other, "5", 2)])
    second = await make_draft(ctx, customer, warehouse, [line(item, "20")])
    for order in (first, second):
        await service.confirm_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            reason="Confirmed",
            allocation=ctx.allocation,
        )

    rows, pagination = await service.demand_summary(
        ctx.session, ctx.tenant, DemandSummaryFilters(), PageParams(page=1, limit=50)
    )
    by_item = {row.item_id: row for row in rows}

    # Two orders, one consolidated row: this is what MRP nets against stock.
    assert by_item[item.id].open_quantity == Decimal("70.0000")
    assert by_item[item.id].allocated_quantity == Decimal("30.0000")
    assert by_item[item.id].shortfall_quantity == Decimal("40.0000")
    assert by_item[item.id].order_count == 2
    assert by_item[item.id].line_count == 2
    assert by_item[other.id].shortfall_quantity == Decimal("5.0000")
    assert pagination.total == 2


async def test_demand_summary_ignores_drafts_and_cancelled_orders(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    draft = await make_draft(ctx, customer, warehouse, [line(item, "15")])
    cancelled = await make_draft(ctx, customer, warehouse, [line(item, "25")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        cancelled.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )
    await service.cancel_order(
        ctx.session, ctx.tenant, cancelled.id, reason="Withdrawn", allocation=ctx.allocation
    )

    rows, _ = await service.demand_summary(
        ctx.session, ctx.tenant, DemandSummaryFilters(), PageParams(page=1, limit=50)
    )

    assert rows == []
    assert draft.status == SalesOrderStatus.DRAFT


async def test_demand_summary_shortfall_only_filter_hides_covered_items(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    covered = await seed_item(ctx, uom)
    short = await seed_item(ctx, uom)
    await seed_stock(ctx, covered, warehouse, "100")

    order = await make_draft(
        ctx, customer, warehouse, [line(covered, "10", 1), line(short, "10", 2)]
    )
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    rows, _ = await service.demand_summary(
        ctx.session,
        ctx.tenant,
        DemandSummaryFilters(shortfall_only=True),
        PageParams(page=1, limit=50),
    )

    assert [row.item_id for row in rows] == [short.id]


async def test_demand_summary_carries_urgency_and_the_earliest_date(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    soon = today() + timedelta(days=3)
    later = today() + timedelta(days=40)

    urgent = await make_draft(
        ctx,
        customer,
        warehouse,
        [line(item, "5")],
        required_date=soon,
        is_urgent=True,
        priority=2,
    )
    routine = await make_draft(
        ctx, customer, warehouse, [line(item, "5")], required_date=later, priority=7
    )
    for order in (urgent, routine):
        await service.confirm_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            reason="Confirmed",
            allocation=ctx.allocation,
        )

    rows, _ = await service.demand_summary(
        ctx.session, ctx.tenant, DemandSummaryFilters(), PageParams(page=1, limit=50)
    )

    assert rows[0].earliest_required_date == soon
    assert rows[0].highest_priority == 2
    assert rows[0].has_urgent_order is True


async def test_demand_summary_is_tenant_scoped(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    rows, _ = await service.demand_summary(
        ctx.session, ctx.other_tenant, DemandSummaryFilters(), PageParams(page=1, limit=50)
    )

    assert rows == []


# -----------------------------------------------------------------------------
# Reads and tenant isolation
# -----------------------------------------------------------------------------


async def test_allocation_status_reports_live_free_stock_beside_the_shortfall(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "30")

    order = await make_draft(ctx, customer, warehouse, [line(item, "100")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    view = await service.allocation_status(ctx.session, ctx.tenant, order.id)

    assert view.lines[0].allocated_quantity == Decimal("30.0000")
    assert view.lines[0].shortfall_quantity == Decimal("70.0000")
    assert view.lines[0].item_code == item.code
    # Everything on the shelf is now promised to this order.
    assert view.lines[0].available_quantity == Decimal("0.0000")
    assert view.fully_allocated is False


async def test_another_tenant_cannot_read_the_order(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])

    with pytest.raises(NotFoundError):
        await service.get_order_detail(ctx.session, ctx.other_tenant, order.id)


async def test_list_filters_narrow_to_open_orders(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    draft = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    confirmed = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    await service.confirm_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        confirmed.id,
        reason="Confirmed",
        allocation=ctx.allocation,
    )

    rows, _ = await service.list_orders(
        ctx.session,
        ctx.tenant,
        SalesOrderFilters(open_only=True),
        PageParams(page=1, limit=50),
        SortParams(),
    )

    codes = {row.code for row in rows}
    assert confirmed.code in codes
    assert draft.code not in codes


async def test_the_document_number_is_released_when_creation_rolls_back(ctx: Ctx):
    """The counter lives in the same transaction as the document (codes.py).

    A create that fails must not burn a number and leave a permanent gap in a
    GST-relevant series.
    """
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    good = await seed_item(ctx, uom)
    bad = await seed_item(ctx, uom, can_sell=False)

    first = await make_draft(ctx, customer, warehouse, [line(good, "1")])

    with pytest.raises(BusinessRuleError):
        await make_draft(ctx, customer, warehouse, [line(bad, "1")])

    second = await make_draft(ctx, customer, warehouse, [line(good, "1")])

    assert int(second.code.rsplit("/", 1)[1]) == int(first.code.rsplit("/", 1)[1]) + 1


async def test_optimistic_lock_column_moves_on_every_write(ctx: Ctx):
    """Several people edit a live order; a lost update must be a 409, not a
    silent overwrite."""
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)

    order = await make_draft(ctx, customer, warehouse, [line(item, "10")])
    first_version = order.version

    await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(notes="Ship in two tranches"),
        allocation=ctx.allocation,
    )

    async with transaction(ctx.session):
        refreshed = await ctx.session.get(SalesOrder, order.id)
    assert refreshed is not None
    assert refreshed.version > first_version


# -----------------------------------------------------------------------------
# End-to-end against the real allocation engine
# -----------------------------------------------------------------------------


async def test_confirm_through_the_real_allocation_service(ctx: Ctx):
    """The default port, with no double anywhere.

    Proves the two modules agree on the contract in database.md section 4.5:
    the reservation row lands with the right demand reference and quantity, and
    the balance the reservation engine locked is the balance sales read.
    """
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "30")

    order = await make_draft(ctx, customer, warehouse, [line(item, "100")])
    result = await service.confirm_order(
        ctx.session, ctx.tenant, ctx.principal, order.id, reason="Confirmed"
    )

    assert result.allocation.lines[0].allocated_quantity == Decimal("30.0000")
    assert result.allocation.lines[0].shortfall_quantity == Decimal("70.0000")

    reservation = (
        await ctx.session.execute(
            select(StockReservation).where(
                StockReservation.company_id == ctx.tenant.company_id,
                StockReservation.demand_id == order.id,
            )
        )
    ).scalar_one()

    assert reservation.demand_type is DemandType.SALES_ORDER
    assert reservation.reservation_type is ReservationType.HARD
    assert reservation.demand_code == order.code
    assert reservation.quantity == Decimal("30.0000")
    assert reservation.status is ReservationStatus.ACTIVE
    # The reservation engine moved the balance the sales module read.
    assert await available_of(ctx, item, warehouse) == Decimal("0.0000")


async def test_cancel_through_the_real_allocation_service_gives_the_stock_back(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "80")

    order = await make_draft(ctx, customer, warehouse, [line(item, "50")])
    await service.confirm_order(
        ctx.session, ctx.tenant, ctx.principal, order.id, reason="Confirmed"
    )
    assert await available_of(ctx, item, warehouse) == Decimal("30.0000")

    await service.cancel_order(
        ctx.session, ctx.tenant, order.id, reason="Customer withdrew the order"
    )

    reservation = (
        await ctx.session.execute(
            select(StockReservation).where(
                StockReservation.company_id == ctx.tenant.company_id,
                StockReservation.demand_id == order.id,
            )
        )
    ).scalar_one()

    assert reservation.status is ReservationStatus.RELEASED
    assert reservation.remaining_quantity == Decimal("0.0000")
    assert await available_of(ctx, item, warehouse) == Decimal("80.0000")


async def test_raising_a_quantity_through_the_real_engine_moves_the_reservation(ctx: Ctx):
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom)
    await seed_stock(ctx, item, warehouse, "100")

    order = await make_draft(ctx, customer, warehouse, [line(item, "40")])
    await service.confirm_order(
        ctx.session, ctx.tenant, ctx.principal, order.id, reason="Confirmed"
    )

    _, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order.id,
        SalesOrderUpdate(lines=[line(item, "90")]),
    )

    assert lines[0].allocated_quantity == Decimal("90.0000")
    assert await available_of(ctx, item, warehouse) == Decimal("10.0000")

    # The old reservation was released rather than left holding stock nobody
    # can see: exactly one reservation is still standing for this order.
    standing = (
        await ctx.session.execute(
            select(func.count())
            .select_from(StockReservation)
            .where(
                StockReservation.company_id == ctx.tenant.company_id,
                StockReservation.demand_id == order.id,
                StockReservation.status == ReservationStatus.ACTIVE,
            )
        )
    ).scalar_one()
    assert standing == 1


async def test_a_header_edit_does_not_demand_price_manage(ctx: Ctx):
    """A delivery-date change is not a repricing decision.

    The draft is still re-priced - the shipping address may have moved the tax
    treatment - but the prices it carries forward are the order's own, so
    somebody who may edit orders and not prices can still move the date.
    """
    uom = await seed_uom(ctx)
    warehouse = await seed_warehouse(ctx)
    customer, _ = await seed_customer(ctx)
    item = await seed_item(ctx, uom, sale_price="100", standard_cost="60")

    order = await make_draft(ctx, customer, warehouse, [line(item, "10", unit_price=Decimal("55"))])
    assert order.requires_approval is True

    no_pricing_rights = _principal(
        ctx.tenant.company_id,
        ctx.tenant.user_id,
        permissions=frozenset({str(Permission.SALES_ORDER_UPDATE)}),
    )
    updated, lines = await service.update_order(
        ctx.session,
        ctx.tenant,
        no_pricing_rights,
        order.id,
        SalesOrderUpdate(required_date=today() + timedelta(days=30)),
    )

    # The agreed price survived, and so did the reason it needs approval.
    assert lines[0].unit_price == Decimal("55.000000")
    assert updated.requires_approval is True
