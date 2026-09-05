"""The two netting hooks MRP resolves by name, and the planner that uses them.

``src.modules.mrp.repository._optional_supply_hook`` looks up
``procurement.service.incoming_quantities`` and
``production.service.in_progress_output`` at call time. While neither existed,
the hook returned ``({}, False)``, every run carried a "did NOT net ... treat as
an upper bound" warning, and the plan over-ordered by the whole of the supply
already on order and already on the floor.

Four things are proved here, in this order:

1. **Each hook answers correctly.** Included and excluded document statuses,
   partial-receipt and partial-production arithmetic, the per-line/per-order
   flooring at zero, the date horizon boundary, warehouse scope, tenant
   isolation and the empty-input case. The status tests spell their expected
   membership out in full rather than deriving it from the constant under test:
   a test that reads ``NETTABLE_PO_STATUSES`` to decide what should be netted
   passes whatever that set happens to say.
2. **Each hook answers with dates.** Both return ``{item: [(due, quantity)]}``
   tranches rather than one summed figure per item, because a figure with no
   date cannot answer the only question the planner has - does this arrive
   before the demand needs it?
3. **The planner nets, and only against supply that arrives in time.** One
   end-to-end run over real stock, real demand, a confirmed incoming purchase
   order and a live production order, asserting the suggested quantity falls by
   exactly the netted supply - then the time-phasing section, which proves
   supply landing after the demand date does *not* erase the shortage, that the
   boundary is the required date itself, and that a parent covered by an
   incoming order does not explode its structure anyway.
4. **The defect is dead.** The same scenario with the seam forced back to its
   old state - the hook attribute removed from the service module, so
   ``getattr`` returns ``None`` exactly as it did before the hooks landed - and
   the plan over-orders again and both warnings come back.

Isolation follows ``test_mrp_service.py``: one throwaway schema per module,
built once, with each test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.customers.models import Customer, CustomerType
from src.modules.inventory.models import InventoryBalance
from src.modules.mrp import service as mrp_service
from src.modules.mrp.models import MrpRequirement, MrpRunStatus, MrpSuggestion
from src.modules.mrp.schemas import MrpRunRequest, ShortageReportFilters
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
)
from src.modules.procurement.repository import (
    NETTABLE_PO_LINE_STATUSES,
    NETTABLE_PO_STATUSES,
)
from src.modules.production import service as production_service
from src.modules.production.models import (
    ProductionOrder,
    ProductionOrderStatus,
    ProductionSourceType,
)
from src.modules.production.repository import NETTABLE_PRODUCTION_STATUSES
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration

D = Decimal

# Foreign keys reach into modules built alongside these two; ``prepare_metadata``
# imports every module that exists and stands in for whatever targets are still
# missing, naming none of them.
prepare_metadata()


# -----------------------------------------------------------------------------
# The membership these tests hold the constants to
# -----------------------------------------------------------------------------
#
# Written out rather than imported so that a change to the constant is a test
# failure and not a silently re-baselined expectation. Each entry is here for
# the reason the constant's own docstring gives.

EXPECTED_NETTABLE_PO_STATUSES = {
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
}

EXPECTED_NETTABLE_PO_LINE_STATUSES = {
    PurchaseOrderLineStatus.PENDING,
    PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
}

EXPECTED_NETTABLE_PRODUCTION_STATUSES = {
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
}


class _Unset:
    """Distinguishes "not supplied" from an explicit ``None`` in a fixture."""


UNSET = _Unset()


#: Statuses that need a reason column filled in before the row will insert.
_PO_REASONS = {
    PurchaseOrderStatus.REJECTED: "rejection_reason",
    PurchaseOrderStatus.CANCELLED: "cancellation_reason",
}


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and a user each, built once."""
    schema = f"it_netting_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    planner_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

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
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )

            factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
            async with factory() as setup_session, setup_session.begin():
                for cid, prefix in ((company_id, "ATC"), (other_company_id, "OTH")):
                    await seed_company(setup_session, company_id=cid, prefix=prefix)
                await seed_user(
                    setup_session,
                    company_id=company_id,
                    user_id=planner_id,
                    email="planner@netting.test",
                    full_name="Planner",
                )
                await seed_user(
                    setup_session,
                    company_id=other_company_id,
                    user_id=other_user_id,
                    email="intruder@netting.test",
                    full_name="Other Planner",
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
        # Everything is caught, and the shared reporter decides what it means: a
        # server that is not listening skips (fails under CI), while a model that
        # will not create or a constraint the fixtures violate re-raises. Catching
        # ``OSError`` here instead used to miss the common case, because SQLAlchemy
        # wraps asyncpg's refusal in a ``DBAPIError``.
        report_database_setup_failure(exc)

    yield schema, company_id, other_company_id, planner_id, other_user_id
    asyncio.run(teardown())


def _schema_engine(schema: str) -> Any:
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


class Ctx:
    """A session, both tenants, and the master data every test here needs."""

    def __init__(self, session: AsyncSession, tenant: TenantContext, other: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.other = other
        self.uom: UnitOfMeasure
        self.main: Warehouse
        self.spare: Warehouse
        self.other_warehouse: Warehouse
        self.vendor: Vendor
        self.other_vendor: Vendor
        self.customer: Customer
        self.bolt: Item
        self.frame: Item
        self.screw: Item
        #: A panel that is *sold on its own and built into a cabinet* - the only
        #: shape in which an item's needed-by date can still be moving while a
        #: supply decision is being taken against it. Purchasable as well as
        #: made, so a purchase order against it is a legitimate document.
        self.panel: Item
        self.cabinet: Item
        self.other_item: Item
        self._po_sequence = 0
        self._pro_sequence = 0

    def next_po_code(self) -> str:
        self._po_sequence += 1
        return f"PO/{self._po_sequence:05d}"

    def next_production_code(self) -> str:
        self._pro_sequence += 1
        return f"MO/{self._pro_sequence:05d}"


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[Ctx]:
    schema, company_id, other_company_id, planner_id, other_user_id = _schema
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
                context = Ctx(
                    session=session,
                    tenant=TenantContext(company_id=company_id, user_id=planner_id),
                    other=TenantContext(company_id=other_company_id, user_id=other_user_id),
                )
                await _seed_master_data(context)
                yield context
            await outer.rollback()
    finally:
        await eng.dispose()


@pytest.fixture(autouse=True)
def _in_memory_planning_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Redis planning lock, as ``test_mrp_service`` does.

    The lock is not what is under test here, and depending on a live Redis to
    exercise netting would make an unrelated service a prerequisite.
    """
    held: dict[str, str] = {}

    async def fake_acquire(
        name: str, ttl_seconds: int = 300, token: str | None = None
    ) -> str | None:
        if name in held:
            return None
        held[name] = token or uuid.uuid4().hex
        return held[name]

    async def fake_release(name: str, token: str) -> bool:
        if held.get(name) == token:
            del held[name]
            return True
        return False

    monkeypatch.setattr(mrp_service, "acquire_lock", fake_acquire)
    monkeypatch.setattr(mrp_service, "release_lock", fake_release)


# -----------------------------------------------------------------------------
# Seeding - every row through the ORM, so Python-side defaults apply
# -----------------------------------------------------------------------------


async def _seed_master_data(ctx: Ctx) -> None:
    session = ctx.session
    company_id = ctx.tenant.company_id
    other_id = ctx.other.company_id
    suffix = uuid.uuid4().hex[:6].upper()

    ctx.uom = UnitOfMeasure(
        company_id=company_id,
        code=f"NOS{suffix}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=True,
        decimal_places=0,
        is_active=True,
    )
    ctx.main = Warehouse(
        company_id=company_id,
        code=f"WH-M{suffix}",
        name="Main store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        is_active=True,
    )
    ctx.spare = Warehouse(
        company_id=company_id,
        code=f"WH-S{suffix}",
        name="Spares store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        is_active=True,
    )
    ctx.other_warehouse = Warehouse(
        company_id=other_id,
        code=f"WH-O{suffix}",
        name="Other company store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        is_active=True,
    )
    ctx.vendor = Vendor(
        company_id=company_id,
        code=f"VEN{suffix}",
        name="Fastener Supplies",
        vendor_types=[str(VendorType.COMPONENT_SUPPLIER)],
        is_active=True,
    )
    ctx.other_vendor = Vendor(
        company_id=other_id,
        code=f"VEO{suffix}",
        name="Other company's vendor",
        vendor_types=[str(VendorType.COMPONENT_SUPPLIER)],
        is_active=True,
    )
    ctx.customer = Customer(
        company_id=company_id,
        code=f"CUS{suffix}",
        name="Hardware Distributors",
        customer_type=CustomerType.DEALER,
        is_active=True,
    )
    session.add_all(
        [
            ctx.uom,
            ctx.main,
            ctx.spare,
            ctx.other_warehouse,
            ctx.vendor,
            ctx.other_vendor,
            ctx.customer,
        ]
    )
    await session.flush()

    ctx.bolt = _item(ctx, f"BOLT-{suffix}", can_sell=True, lead_time_days=7)
    ctx.frame = _item(
        ctx,
        f"FRAME-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
        can_sell=True,
        lead_time_days=10,
    )
    ctx.screw = _item(ctx, f"SCREW-{suffix}", lead_time_days=21)
    # PANEL is made here *and* buyable, so a purchase order against it is a real
    # document rather than a fixture that could not exist. It has no lead time
    # of its own: the only offset in the panel scenarios is the cabinet's, which
    # keeps the arithmetic in those tests down to one subtraction.
    ctx.panel = _item(
        ctx,
        f"PANEL-{suffix}",
        item_group=ItemGroup.SEMI_FINISHED,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=True,
        can_sell=True,
        lead_time_days=0,
    )
    ctx.cabinet = _item(
        ctx,
        f"CABINET-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
        can_sell=True,
        lead_time_days=30,
    )
    ctx.other_item = _item(ctx, f"OTHER-{suffix}", company_id=other_id)
    session.add_all([ctx.bolt, ctx.frame, ctx.screw, ctx.panel, ctx.cabinet, ctx.other_item])
    await session.flush()

    async def _structure(parent: Item, code: str, lines: list[tuple[Item, str]]) -> None:
        bom = Bom(
            company_id=company_id,
            code=code,
            item_id=parent.id,
            version=1,
            status=BomStatus.APPROVED,
            bom_type=BomType.PRODUCTION,
            is_default=True,
            output_quantity=D("1"),
            base_uom_id=ctx.uom.id,
            effective_from=today() - timedelta(days=30),
            approved_by_id=ctx.tenant.user_id,
            approved_at=utcnow(),
        )
        session.add(bom)
        await session.flush()
        session.add_all(
            [
                BomLine(
                    company_id=company_id,
                    bom_id=bom.id,
                    parent_item_id=parent.id,
                    line_number=number,
                    component_item_id=component.id,
                    quantity_per=D(quantity_per),
                    uom_id=ctx.uom.id,
                )
                for number, (component, quantity_per) in enumerate(lines, start=1)
            ]
        )
        await session.flush()

    await _structure(ctx.frame, f"BOM-{suffix}", [(ctx.screw, "2")])
    await _structure(ctx.panel, f"BOM-P{suffix}", [(ctx.screw, "2")])
    # One panel and two screws a cabinet, so a cabinet consolidates to four
    # screws once the panel's own two are exploded through it.
    await _structure(ctx.cabinet, f"BOM-C{suffix}", [(ctx.panel, "1"), (ctx.screw, "2")])


def _item(ctx: Ctx, code: str, *, company_id: uuid.UUID | None = None, **kwargs: Any) -> Item:
    defaults: dict[str, Any] = {
        "company_id": company_id or ctx.tenant.company_id,
        "code": code,
        "name": f"{code} description",
        "item_group": ItemGroup.COMPONENT,
        "base_uom_id": ctx.uom.id,
        "production_type": ProductionType.BUY,
        "can_purchase": True,
        "can_be_bom_component": True,
        "min_order_quantity": D("0"),
        "safety_stock": D("0"),
        "is_active": True,
    }
    defaults.update(kwargs)
    return Item(**defaults)


async def add_purchase_order(
    ctx: Ctx,
    *,
    lines: list[dict[str, Any]],
    status: PurchaseOrderStatus = PurchaseOrderStatus.APPROVED,
    expected_delivery_date: date | _Unset | None = UNSET,
    warehouse: Warehouse | None = None,
    tenant: TenantContext | None = None,
    vendor: Vendor | None = None,
) -> PurchaseOrder:
    """One order with its lines, built through the ORM.

    ``expected_delivery_date`` defaults to a week out; ``po_date`` sits well
    behind it because ``expected_delivery_not_before_po_date`` refuses the
    reverse.
    """
    scope = tenant or ctx.tenant
    order = PurchaseOrder(
        company_id=scope.company_id,
        code=ctx.next_po_code(),
        vendor_id=(vendor or ctx.vendor).id,
        po_date=today() - timedelta(days=60),
        # ``UNSET`` is a distinct sentinel from ``None``: an explicit ``None``
        # is a test asking for an order with no promised date at all.
        expected_delivery_date=(
            today() + timedelta(days=7)
            if isinstance(expected_delivery_date, _Unset)
            else expected_delivery_date
        ),
        warehouse_id=(warehouse or ctx.main).id,
        status=status,
        priority=5,
        currency="INR",
    )
    reason_column = _PO_REASONS.get(status)
    if reason_column is not None:
        setattr(order, reason_column, "Test fixture reason")
    if status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL):
        order.approved_by_id = scope.user_id
        order.approved_at = utcnow()
    ctx.session.add(order)
    await ctx.session.flush()

    for number, line in enumerate(lines, start=1):
        received = D(line.get("received", "0"))
        ctx.session.add(
            PurchaseOrderLine(
                company_id=scope.company_id,
                purchase_order_id=order.id,
                line_number=number,
                item_id=line["item_id"],
                description=line.get("description", "Fixture line"),
                quantity=D(line["quantity"]),
                uom_id=ctx.uom.id,
                unit_price=D("10"),
                received_quantity=received,
                accepted_quantity=received,
                rejected_quantity=D("0"),
                required_date=line.get("required_date"),
                status=line.get("status", PurchaseOrderLineStatus.PENDING),
            )
        )
    await ctx.session.flush()
    return order


async def add_production_order(
    ctx: Ctx,
    *,
    item_id: uuid.UUID,
    planned: str,
    produced: str = "0",
    rejected: str = "0",
    scrap: str = "0",
    status: ProductionOrderStatus = ProductionOrderStatus.RELEASED,
    planned_end_date: date | None = None,
    warehouse: Warehouse | None = None,
    tenant: TenantContext | None = None,
) -> ProductionOrder:
    scope = tenant or ctx.tenant
    end = planned_end_date if planned_end_date is not None else today() + timedelta(days=7)
    order = ProductionOrder(
        company_id=scope.company_id,
        code=ctx.next_production_code(),
        item_id=item_id,
        planned_quantity=D(planned),
        produced_quantity=D(produced),
        rejected_quantity=D(rejected),
        scrap_quantity=D(scrap),
        warehouse_id=(warehouse or ctx.main).id,
        planned_start_date=end - timedelta(days=5),
        planned_end_date=end,
        priority=5,
        status=status,
        source_type=ProductionSourceType.MANUAL,
        # ``approved_orders_carry_approval_evidence`` refuses any post-approval
        # status without both columns, and they are paired.
        approved_by_id=scope.user_id,
        approved_at=utcnow(),
    )
    ctx.session.add(order)
    await ctx.session.flush()
    return order


async def add_stock(ctx: Ctx, item: Item, warehouse: Warehouse, *, on_hand: str) -> None:
    ctx.session.add(
        InventoryBalance(
            company_id=ctx.tenant.company_id,
            item_id=item.id,
            warehouse_id=warehouse.id,
            on_hand_quantity=D(on_hand),
            reserved_quantity=D("0"),
        )
    )
    await ctx.session.flush()


async def add_sales_order(
    ctx: Ctx, *, code: str, lines: list[tuple[Item, str]], required_in_days: int = 30
) -> SalesOrder:
    order = SalesOrder(
        company_id=ctx.tenant.company_id,
        code=code,
        customer_id=ctx.customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=required_in_days),
        warehouse_id=ctx.main.id,
        status=SalesOrderStatus.CONFIRMED,
        priority=5,
        currency="INR",
    )
    ctx.session.add(order)
    await ctx.session.flush()
    ctx.session.add_all(
        [
            SalesOrderLine(
                company_id=ctx.tenant.company_id,
                sales_order_id=order.id,
                line_number=index,
                item_id=item.id,
                quantity=D(quantity),
                uom_id=ctx.uom.id,
                unit_price=D("100"),
                allocated_quantity=D("0"),
            )
            for index, (item, quantity) in enumerate(lines, start=1)
        ]
    )
    await ctx.session.flush()
    return order


HORIZON = 30

#: What :func:`add_purchase_order` and :func:`add_production_order` promise when
#: a test does not care about the date. Named so the schedule assertions below
#: read as "the tranche lands on the day the fixture said", not as a magic 7.
DEFAULT_DUE = 7


def horizon_end() -> date:
    return today() + timedelta(days=HORIZON)


def due(days: int = DEFAULT_DUE) -> date:
    """The date a fixture document is promised for."""
    return today() + timedelta(days=days)


def totals(schedule: dict[uuid.UUID, list[tuple[date, Decimal]]]) -> dict[uuid.UUID, Decimal]:
    """Collapse a dated schedule back to one figure per item.

    For the handful of assertions that are genuinely about the arithmetic rather
    than the phasing. Everything else asserts the tranches *with* their dates -
    a total is exactly the shape that hid the time-blind netting defect.
    """
    return {item: sum(q for _, q in arrivals) for item, arrivals in schedule.items()}


async def incoming(
    ctx: Ctx, *items: Item, **kwargs: Any
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    return await procurement_service.incoming_quantities(
        ctx.session,
        kwargs.pop("tenant", ctx.tenant),
        item_ids=[item.id for item in items],
        upto=kwargs.pop("upto", horizon_end()),
        **kwargs,
    )


async def in_progress(
    ctx: Ctx, *items: Item, **kwargs: Any
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    return await production_service.in_progress_output(
        ctx.session,
        kwargs.pop("tenant", ctx.tenant),
        item_ids=[item.id for item in items],
        upto=kwargs.pop("upto", horizon_end()),
        **kwargs,
    )


# =============================================================================
# The constants themselves
# =============================================================================


def test_the_nettable_status_sets_are_what_planning_expects() -> None:
    """Membership is asserted literally; the constant does not get to define it.

    A wrong constant is exactly where this defect would hide again: adding
    ``DRAFT`` here would net a plan against an order nobody has approved, and
    adding ``RECEIVED`` would count stock that is already in
    ``inventory_balances`` twice.
    """
    assert set(NETTABLE_PO_STATUSES) == EXPECTED_NETTABLE_PO_STATUSES
    assert set(NETTABLE_PO_LINE_STATUSES) == EXPECTED_NETTABLE_PO_LINE_STATUSES
    assert set(NETTABLE_PRODUCTION_STATUSES) == EXPECTED_NETTABLE_PRODUCTION_STATUSES


# =============================================================================
# procurement.service.incoming_quantities
# =============================================================================


@pytest.mark.parametrize("status", list(PurchaseOrderStatus))
async def test_only_committed_undelivered_orders_are_netted(
    ctx: Ctx, status: PurchaseOrderStatus
) -> None:
    await add_purchase_order(
        ctx,
        status=status,
        lines=[{"item_id": ctx.bolt.id, "quantity": "40"}],
    )

    result = await incoming(ctx, ctx.bolt)

    if status in EXPECTED_NETTABLE_PO_STATUSES:
        assert result == {ctx.bolt.id: [(due(), D("40.0000"))]}, f"{status} should be netted"
    else:
        assert result == {}, f"{status} must not be netted"


@pytest.mark.parametrize("line_status", list(PurchaseOrderLineStatus))
async def test_only_lines_that_still_expect_a_delivery_are_netted(
    ctx: Ctx, line_status: PurchaseOrderLineStatus
) -> None:
    """A short-closed or cancelled line is not supply, even on a live order."""
    await add_purchase_order(
        ctx,
        lines=[{"item_id": ctx.bolt.id, "quantity": "40", "status": line_status}],
    )

    result = await incoming(ctx, ctx.bolt)

    if line_status in EXPECTED_NETTABLE_PO_LINE_STATUSES:
        assert result == {ctx.bolt.id: [(due(), D("40.0000"))]}
    else:
        assert result == {}, f"line status {line_status} must not be netted"


async def test_a_partly_received_line_contributes_only_its_balance(ctx: Ctx) -> None:
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
        lines=[
            {
                "item_id": ctx.bolt.id,
                "quantity": "100",
                "received": "30",
                "status": PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
            }
        ],
    )

    assert await incoming(ctx, ctx.bolt) == {ctx.bolt.id: [(due(), D("70.0000"))]}


async def test_an_over_received_line_cannot_eat_another_lines_shortfall(ctx: Ctx) -> None:
    """The flooring has to be inside the SUM, per line.

    Both lines are for the same item on the same order. The first has more
    received than ordered - within the 5% tolerance, and a row that can be
    reached by an amendment or a data fix without its status catching up. Summed
    raw it contributes -5 and quietly cancels part of the second line's genuine
    40, netting the plan against supply that does not exist.
    """
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
        lines=[
            {
                "item_id": ctx.bolt.id,
                "quantity": "100",
                "received": "105",
                "status": PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
            },
            {"item_id": ctx.bolt.id, "quantity": "40"},
        ],
    )

    # Both lines share the header's promised date, so they consolidate into one
    # tranche - of the second line's genuine 40 and nothing else.
    assert await incoming(ctx, ctx.bolt) == {ctx.bolt.id: [(due(), D("40.0000"))]}


async def test_the_horizon_boundary_is_inclusive_of_the_due_date(ctx: Ctx) -> None:
    upto = today() + timedelta(days=10)
    await add_purchase_order(
        ctx,
        expected_delivery_date=upto,
        lines=[{"item_id": ctx.bolt.id, "quantity": "40"}],
    )
    await add_purchase_order(
        ctx,
        expected_delivery_date=upto + timedelta(days=1),
        lines=[{"item_id": ctx.screw.id, "quantity": "40"}],
    )

    result = await incoming(ctx, ctx.bolt, ctx.screw, upto=upto)

    assert result == {ctx.bolt.id: [(upto, D("40.0000"))]}, (
        "a day past the horizon is not supply for it"
    )


async def test_the_line_date_wins_over_the_header_date(ctx: Ctx) -> None:
    upto = today() + timedelta(days=10)
    await add_purchase_order(
        ctx,
        expected_delivery_date=today() + timedelta(days=90),
        lines=[
            # Called off in tranches: the first lands inside the horizon even
            # though the order as a whole does not.
            {"item_id": ctx.bolt.id, "quantity": "40", "required_date": upto},
            {"item_id": ctx.bolt.id, "quantity": "60"},
        ],
    )

    assert await incoming(ctx, ctx.bolt, upto=upto) == {ctx.bolt.id: [(upto, D("40.0000"))]}


async def test_a_line_with_no_date_anywhere_is_not_counted_as_supply(ctx: Ctx) -> None:
    """Deliberate, and the conservative direction.

    Nobody has committed to when an undated balance arrives, so it is left out:
    over-stating the shortage costs a duplicate order a buyer can cancel, while
    under-stating it costs a stockout on the floor.
    """
    await add_purchase_order(
        ctx,
        expected_delivery_date=None,
        lines=[{"item_id": ctx.bolt.id, "quantity": "40"}],
    )

    assert await incoming(ctx, ctx.bolt) == {}
    # ... and far-future horizons do not rescue it either: it is unknown, not late.
    assert await incoming(ctx, ctx.bolt, upto=today() + timedelta(days=3650)) == {}


async def test_incoming_is_scoped_to_the_runs_warehouses(ctx: Ctx) -> None:
    await add_purchase_order(
        ctx, warehouse=ctx.main, lines=[{"item_id": ctx.bolt.id, "quantity": "40"}]
    )
    await add_purchase_order(
        ctx, warehouse=ctx.spare, lines=[{"item_id": ctx.bolt.id, "quantity": "25"}]
    )

    scoped = await incoming(ctx, ctx.bolt, warehouse_ids=[ctx.main.id])
    assert scoped == {ctx.bolt.id: [(due(), D("40.0000"))]}

    # None means the whole company - what a run that named no warehouses plans
    # against. Both orders are promised for the same day, so they consolidate.
    assert await incoming(ctx, ctx.bolt, warehouse_ids=None) == {
        ctx.bolt.id: [(due(), D("65.0000"))]
    }


async def test_another_companys_order_is_never_netted(ctx: Ctx) -> None:
    """A data-leak test, not a formality.

    The other company's order names *this* company's item id - nothing in the
    schema stops it, and it is what a missing company predicate would surface.
    """
    await add_purchase_order(
        ctx,
        tenant=ctx.other,
        vendor=ctx.other_vendor,
        warehouse=ctx.other_warehouse,
        lines=[
            {"item_id": ctx.bolt.id, "quantity": "500"},
            {"item_id": ctx.other_item.id, "quantity": "700"},
        ],
    )
    await add_purchase_order(ctx, lines=[{"item_id": ctx.bolt.id, "quantity": "40"}])

    result = await procurement_service.incoming_quantities(
        ctx.session,
        ctx.tenant,
        item_ids=[ctx.bolt.id, ctx.other_item.id],
        upto=horizon_end(),
    )

    assert result == {ctx.bolt.id: [(due(), D("40.0000"))]}

    # And the isolation holds in both directions.
    other_side = await procurement_service.incoming_quantities(
        ctx.session,
        ctx.other,
        item_ids=[ctx.bolt.id, ctx.other_item.id],
        upto=horizon_end(),
    )
    assert other_side == {
        ctx.bolt.id: [(due(), D("500.0000"))],
        ctx.other_item.id: [(due(), D("700.0000"))],
    }


async def test_incoming_with_no_items_asks_nothing(ctx: Ctx) -> None:
    await add_purchase_order(ctx, lines=[{"item_id": ctx.bolt.id, "quantity": "40"}])

    assert (
        await procurement_service.incoming_quantities(
            ctx.session, ctx.tenant, item_ids=[], upto=horizon_end()
        )
        == {}
    )


async def test_incoming_quantities_are_decimals(ctx: Ctx) -> None:
    await add_purchase_order(
        ctx,
        lines=[
            {"item_id": ctx.bolt.id, "quantity": "10.5", "received": "0.25"},
            {"item_id": ctx.bolt.id, "quantity": "0.75"},
        ],
    )

    result = await incoming(ctx, ctx.bolt)

    [(arrives, value)] = result[ctx.bolt.id]
    assert arrives == due()
    assert isinstance(value, Decimal) and not isinstance(value, float)
    assert value == D("11.0000")
    assert value.as_tuple().exponent == -4, "quantities are NUMERIC(18,4)"


async def test_orders_landing_on_different_days_stay_separate_tranches(ctx: Ctx) -> None:
    """The dates are the contract, and one summed figure cannot carry them.

    Deliveries of the same bolt on two different days come back as two dated
    tranches in date order - not as 90 with no way to tell which half is here
    next week. Two orders promised for the *same* day consolidate, because a
    tranche is keyed on the date, not on the document.
    """
    for days, quantity in ((3, "10"), (20, "50"), (3, "30")):
        await add_purchase_order(
            ctx,
            expected_delivery_date=due(days),
            lines=[{"item_id": ctx.bolt.id, "quantity": quantity}],
        )

    result = await incoming(ctx, ctx.bolt)

    assert result == {ctx.bolt.id: [(due(3), D("40.0000")), (due(20), D("50.0000"))]}
    assert totals(result) == {ctx.bolt.id: D("90.0000")}


# =============================================================================
# production.service.in_progress_output
# =============================================================================


@pytest.mark.parametrize("status", list(ProductionOrderStatus))
async def test_only_live_jobs_are_netted(ctx: Ctx, status: ProductionOrderStatus) -> None:
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40", status=status)

    result = await in_progress(ctx, ctx.frame)

    if status in EXPECTED_NETTABLE_PRODUCTION_STATUSES:
        assert result == {ctx.frame.id: [(due(), D("40.0000"))]}, f"{status} should be netted"
    else:
        assert result == {}, f"{status} must not be netted"


async def test_a_part_made_job_owes_only_its_balance(ctx: Ctx) -> None:
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="100",
        produced="30",
        status=ProductionOrderStatus.IN_PROCESS,
    )

    assert await in_progress(ctx, ctx.frame) == {ctx.frame.id: [(due(), D("70.0000"))]}


async def test_rejects_and_scrap_do_not_reduce_what_a_job_still_owes(ctx: Ctx) -> None:
    """The claim the formula rests on, asserted rather than assumed.

    ``assert_within_over_production_tolerance`` bounds
    ``already_produced + incoming`` against ``planned * 1.10``: neither
    ``rejected_quantity`` nor ``scrap_quantity`` appears in it, so refusing a
    piece does not consume any of the plan and the job is still expected to make
    the whole planned quantity of *good* output. The WIP board states the same
    rule as ``balance_quantity = max(0, planned - produced)``.
    """
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="100",
        produced="30",
        rejected="20",
        scrap="10",
        status=ProductionOrderStatus.IN_PROCESS,
    )

    assert await in_progress(ctx, ctx.frame) == {ctx.frame.id: [(due(), D("70.0000"))]}

    # The rule the arithmetic is derived from, exercised directly: a booking that
    # takes good output to 100 is allowed on this job even after 20 rejects.
    production_service.assert_within_over_production_tolerance(
        order_code="MO/00001",
        planned_quantity=D("100"),
        already_produced=D("30"),
        incoming=D("70"),
    )


async def test_an_over_produced_job_cannot_eat_another_jobs_balance(ctx: Ctx) -> None:
    """Flooring per order, inside the SUM.

    105 of a planned 100 is legal - the over-production tolerance is 10%. Summed
    raw, that job contributes -5 and cancels part of the second job's genuine 40.
    """
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="100",
        produced="105",
        status=ProductionOrderStatus.IN_PROCESS,
    )
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40")

    # Both jobs are due the same day, so they consolidate into one tranche - of
    # the second job's genuine 40 and nothing else.
    assert await in_progress(ctx, ctx.frame) == {ctx.frame.id: [(due(), D("40.0000"))]}


async def test_the_production_horizon_boundary_is_inclusive(ctx: Ctx) -> None:
    upto = today() + timedelta(days=10)
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40", planned_end_date=upto)
    await add_production_order(
        ctx, item_id=ctx.screw.id, planned="40", planned_end_date=upto + timedelta(days=1)
    )

    result = await in_progress(ctx, ctx.frame, ctx.screw, upto=upto)

    assert result == {ctx.frame.id: [(upto, D("40.0000"))]}


async def test_a_late_job_is_still_supply(ctx: Ctx) -> None:
    """``planned_end_date`` in the past is late, not gone."""
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="40",
        planned_end_date=today() - timedelta(days=5),
    )

    assert await in_progress(ctx, ctx.frame) == {
        ctx.frame.id: [(today() - timedelta(days=5), D("40.0000"))]
    }


async def test_in_progress_output_is_scoped_to_the_runs_warehouses(ctx: Ctx) -> None:
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40", warehouse=ctx.main)
    await add_production_order(ctx, item_id=ctx.frame.id, planned="25", warehouse=ctx.spare)

    assert await in_progress(ctx, ctx.frame, warehouse_ids=[ctx.main.id]) == {
        ctx.frame.id: [(due(), D("40.0000"))]
    }
    assert await in_progress(ctx, ctx.frame, warehouse_ids=None) == {
        ctx.frame.id: [(due(), D("65.0000"))]
    }


async def test_another_companys_job_is_never_netted(ctx: Ctx) -> None:
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="500",
        tenant=ctx.other,
        warehouse=ctx.other_warehouse,
    )
    await add_production_order(
        ctx,
        item_id=ctx.other_item.id,
        planned="700",
        tenant=ctx.other,
        warehouse=ctx.other_warehouse,
    )
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40")

    result = await production_service.in_progress_output(
        ctx.session,
        ctx.tenant,
        item_ids=[ctx.frame.id, ctx.other_item.id],
        upto=horizon_end(),
    )
    assert result == {ctx.frame.id: [(due(), D("40.0000"))]}

    other_side = await production_service.in_progress_output(
        ctx.session,
        ctx.other,
        item_ids=[ctx.frame.id, ctx.other_item.id],
        upto=horizon_end(),
    )
    assert other_side == {
        ctx.frame.id: [(due(), D("500.0000"))],
        ctx.other_item.id: [(due(), D("700.0000"))],
    }


async def test_in_progress_output_with_no_items_asks_nothing(ctx: Ctx) -> None:
    await add_production_order(ctx, item_id=ctx.frame.id, planned="40")

    assert (
        await production_service.in_progress_output(
            ctx.session, ctx.tenant, item_ids=[], upto=horizon_end()
        )
        == {}
    )


async def test_in_progress_quantities_are_decimals(ctx: Ctx) -> None:
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="10.5",
        produced="0.25",
        status=ProductionOrderStatus.IN_PROCESS,
    )

    [(arrives, value)] = (await in_progress(ctx, ctx.frame))[ctx.frame.id]

    assert arrives == due()
    assert isinstance(value, Decimal) and not isinstance(value, float)
    assert value == D("10.2500")
    assert value.as_tuple().exponent == -4


async def test_jobs_finishing_on_different_days_stay_separate_tranches(ctx: Ctx) -> None:
    """Same contract on the production side: a tranche per completion date."""
    for days, planned in ((3, "10"), (20, "50"), (3, "30")):
        await add_production_order(
            ctx, item_id=ctx.frame.id, planned=planned, planned_end_date=due(days)
        )

    result = await in_progress(ctx, ctx.frame)

    assert result == {ctx.frame.id: [(due(3), D("40.0000")), (due(20), D("50.0000"))]}
    assert totals(result) == {ctx.frame.id: D("90.0000")}


# =============================================================================
# The planner, end to end - the test that matters
# =============================================================================


async def _seed_netting_scenario(ctx: Ctx) -> None:
    """Demand, stock, one confirmed incoming order and one live job.

    BOLT  demand 100, stock 20  -> 80 net, 30 on order      -> shortage 50
    FRAME demand  60, stock 10  -> 50 net, 25 on the floor  -> shortage 25
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100"), (ctx.frame, "60")])
    await add_stock(ctx, ctx.bolt, ctx.main, on_hand="20")
    await add_stock(ctx, ctx.frame, ctx.main, on_hand="10")
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=today() + timedelta(days=20),
        lines=[
            {
                "item_id": ctx.bolt.id,
                "quantity": "50",
                "received": "20",
                "status": PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
            }
        ],
    )
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="40",
        produced="15",
        status=ProductionOrderStatus.IN_PROCESS,
        planned_end_date=today() + timedelta(days=25),
    )


async def _plan(ctx: Ctx) -> Any:
    run, dispatched = await mrp_service.start_run(
        ctx.session, ctx.tenant, MrpRunRequest(horizon_days=45)
    )
    assert dispatched is False
    assert run.status is MrpRunStatus.COMPLETED
    return run


async def _suggestion(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpSuggestion:
    row = (
        await ctx.session.execute(
            select(MrpSuggestion).where(
                MrpSuggestion.run_id == run_id, MrpSuggestion.item_id == item.id
            )
        )
    ).scalar_one()
    return row


async def _requirement(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpRequirement:
    return (
        await ctx.session.execute(
            select(MrpRequirement).where(
                MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.id
            )
        )
    ).scalar_one()


async def _maybe_requirement(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpRequirement | None:
    """The requirement row, or ``None`` when the run raised none for the item."""
    return (
        await ctx.session.execute(
            select(MrpRequirement).where(
                MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.id
            )
        )
    ).scalar_one_or_none()


async def _maybe_suggestion(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpSuggestion | None:
    """The suggestion row, or ``None`` when the item was covered."""
    return (
        await ctx.session.execute(
            select(MrpSuggestion).where(
                MrpSuggestion.run_id == run_id, MrpSuggestion.item_id == item.id
            )
        )
    ).scalar_one_or_none()


NOT_NETTED_INCOMING = "Confirmed incoming purchase orders could not be read"
NOT_NETTED_PRODUCTION = "In-progress production output could not be read"


async def test_the_planner_nets_against_incoming_orders_and_live_jobs(ctx: Ctx) -> None:
    """The whole point: both stages ran, and the plan is smaller because of it."""
    await _seed_netting_scenario(ctx)

    run = await _plan(ctx)

    assert run.result_summary is not None
    assert run.result_summary["netted_against_incoming"] is True
    assert run.result_summary["netted_against_production"] is True
    assert not [w for w in run.warnings if "did NOT net" in w], run.warnings

    bolt_requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert bolt_requirement.net_requirement == D("80.0000")
    assert bolt_requirement.incoming_quantity == D("30.0000")
    assert bolt_requirement.shortage_quantity == D("50.0000")

    frame_requirement = await _requirement(ctx, run.id, ctx.frame)
    assert frame_requirement.net_requirement == D("50.0000")
    assert frame_requirement.in_production_quantity == D("25.0000")
    assert frame_requirement.shortage_quantity == D("25.0000")

    assert (await _suggestion(ctx, run.id, ctx.bolt)).suggested_quantity == D("50.0000")
    assert (await _suggestion(ctx, run.id, ctx.frame)).suggested_quantity == D("25.0000")


async def test_without_the_hooks_the_same_plan_over_orders_and_says_so(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, reproduced - and the proof these tests would have failed.

    ``mrp.repository._optional_supply_hook`` resolves each hook with
    ``getattr(module, name, None)``. Removing the attribute puts the seam back
    exactly where it was before the hooks landed: ``getattr`` returns ``None``,
    the hook reports ``({}, False)``, and the run says so. Nothing asserts that
    the hook is *unresolvable* - only that a seam forced to resolve to nothing
    behaves the way the planner claims it does.
    """
    monkeypatch.delattr(procurement_service, "incoming_quantities")
    monkeypatch.delattr(production_service, "in_progress_output")
    await _seed_netting_scenario(ctx)

    run = await _plan(ctx)

    assert run.result_summary is not None
    assert run.result_summary["netted_against_incoming"] is False
    assert run.result_summary["netted_against_production"] is False
    assert any(NOT_NETTED_INCOMING in w for w in run.warnings), run.warnings
    assert any(NOT_NETTED_PRODUCTION in w for w in run.warnings), run.warnings

    # 30 more bolts and 25 more frames than the company needs - the exact supply
    # the netted run subtracted.
    assert (await _requirement(ctx, run.id, ctx.bolt)).incoming_quantity == D("0.0000")
    assert (await _suggestion(ctx, run.id, ctx.bolt)).suggested_quantity == D("80.0000")
    assert (await _requirement(ctx, run.id, ctx.frame)).in_production_quantity == D("0.0000")
    assert (await _suggestion(ctx, run.id, ctx.frame)).suggested_quantity == D("50.0000")


# =============================================================================
# Time phasing - supply that arrives after the demand is not supply for it
# =============================================================================
#
# The defect these tests exist for, in the reviewer's own words: a CONFIRMED
# order for 100 bolts needed in two days, zero stock, and one SENT purchase
# order for 100 bolts arriving in forty-four. The run reported
# ``shortage 0, suggestions 0, warnings []`` and ordered nothing. Netting was
# performed once for the whole horizon, so supply landing six weeks after the
# demand silently erased it - and unlike the over-ordering the netting hooks
# replaced, which a buyer sees and cancels, this failure is invisible until the
# line cannot be built.

LATE_SUPPLY_WARNING = "arrives AFTER it is needed"


async def test_supply_that_lands_after_the_demand_does_not_erase_the_shortage(
    ctx: Ctx,
) -> None:
    """The reproduction, exactly: demand on day 2, supply on day 44, horizon 45."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.bolt.id, "quantity": "100"}],
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert requirement.net_requirement == D("100.0000")
    assert requirement.incoming_quantity == D("0.0000"), (
        "an order landing six weeks late is not supply for a line shipping in two days"
    )
    assert requirement.shortage_quantity == D("100.0000")

    suggestion = await _maybe_suggestion(ctx, run.id, ctx.bolt)
    assert suggestion is not None, "the line needs 100 bolts in two days; order them"
    assert suggestion.suggested_quantity == D("100.0000")

    # The netting stage still ran - this is not the "could not read" caveat.
    assert run.result_summary is not None
    assert run.result_summary["netted_against_incoming"] is True
    assert not [w for w in run.warnings if "did NOT net" in w], run.warnings


async def test_the_excluded_supply_is_reported_rather_than_quietly_dropped(
    ctx: Ctx,
) -> None:
    """A planner has to be able to see that the order exists and is simply late.

    Dropping it in silence is its own trap: the shortage and the open purchase
    order both sit in the system with nothing connecting them, and the next
    person to look "fixes" the plan by widening the netting again.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.bolt.id, "quantity": "100"}],
    )

    run = await _plan(ctx)

    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == 1
    late = [w for w in run.warnings if LATE_SUPPLY_WARNING in w]
    assert late, run.warnings
    assert ctx.bolt.code in late[0]
    assert due(44).isoformat() in late[0], "the warning names when it does arrive"
    assert due(2).isoformat() in late[0], "...and when it was needed"


@pytest.mark.parametrize(
    ("arrives_in_days", "expected_incoming", "expected_shortage"),
    [
        (1, "100.0000", "0.0000"),
        (2, "100.0000", "0.0000"),
        (3, "0.0000", "100.0000"),
    ],
)
async def test_the_boundary_is_the_required_date_itself(
    ctx: Ctx, arrives_in_days: int, expected_incoming: str, expected_shortage: str
) -> None:
    """On the day counts; the day after does not.

    A delivery promised for the day the line ships is supply for it - excluding
    it would raise a duplicate order for stock already arriving. One day later
    is not, however close: the plan cannot ship what has not landed.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(arrives_in_days),
        lines=[{"item_id": ctx.bolt.id, "quantity": "100"}],
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert requirement.incoming_quantity == D(expected_incoming)
    assert requirement.shortage_quantity == D(expected_shortage)

    suggestion = await _maybe_suggestion(ctx, run.id, ctx.bolt)
    if D(expected_shortage) > 0:
        assert suggestion is not None
        assert suggestion.suggested_quantity == D(expected_shortage)
    else:
        assert suggestion is None, "covered by a delivery arriving on the day it is needed"


async def test_only_the_tranches_that_arrive_in_time_are_netted(ctx: Ctx) -> None:
    """Part of the supply is in time and part is not - the split is per tranche."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=10)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(5),
        lines=[{"item_id": ctx.bolt.id, "quantity": "40"}],
    )
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(25),
        lines=[{"item_id": ctx.bolt.id, "quantity": "60"}],
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert requirement.incoming_quantity == D("40.0000")
    assert requirement.shortage_quantity == D("60.0000")
    suggestion = await _maybe_suggestion(ctx, run.id, ctx.bolt)
    assert suggestion is not None
    assert suggestion.suggested_quantity == D("60.0000")

    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == 1


async def test_a_job_finishing_late_does_not_cover_the_parent_that_needs_it(
    ctx: Ctx,
) -> None:
    """The production side of the same rule, and its consequence downstream.

    The job finishes after the frames are needed, so it is not supply for them:
    the parent is short, and its structure is exploded for the whole shortfall.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.frame, "60")], required_in_days=5)
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="60",
        status=ProductionOrderStatus.IN_PROCESS,
        planned_end_date=due(40),
    )

    run = await _plan(ctx)

    frame = await _requirement(ctx, run.id, ctx.frame)
    assert frame.in_production_quantity == D("0.0000")
    assert frame.shortage_quantity == D("60.0000")

    screw = await _requirement(ctx, run.id, ctx.screw)
    assert screw.dependent_demand_quantity == D("120.0000"), "two screws a frame, 60 frames"


# =============================================================================
# The explosion and the parent's own suggestion have to agree
# =============================================================================


async def test_a_parent_covered_by_a_purchase_order_does_not_explode_its_structure(
    ctx: Ctx,
) -> None:
    """Dependent demand must not be exploded from the pre-netting shortfall.

    60 panels are demanded and a confirmed purchase order for 60 lands before
    they are needed, so the plan raises no suggestion for the panel. Its screws
    must not be demanded either: a bought panel arrives as a finished panel and
    consumes nothing, so raising component demand behind it orders screws for a
    build the plan had already decided not to raise.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "60")], required_in_days=25)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(10),
        lines=[{"item_id": ctx.panel.id, "quantity": "60"}],
    )

    run = await _plan(ctx)

    panel = await _requirement(ctx, run.id, ctx.panel)
    assert panel.incoming_quantity == D("60.0000")
    assert panel.shortage_quantity == D("0.0000")
    assert await _maybe_suggestion(ctx, run.id, ctx.panel) is None

    assert await _maybe_requirement(ctx, run.id, ctx.screw) is None, (
        "screws were demanded for a build the plan had already decided not to raise"
    )
    assert run.result_summary is not None
    assert run.result_summary["explosion_count"] == 0


async def test_a_parent_covered_only_in_part_explodes_only_the_uncovered_half(
    ctx: Ctx,
) -> None:
    """The same rule, mid-range: the explosion follows the netted shortfall."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "60")], required_in_days=25)
    await add_stock(ctx, ctx.panel, ctx.main, on_hand="10")
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(10),
        lines=[{"item_id": ctx.panel.id, "quantity": "20"}],
    )

    run = await _plan(ctx)

    panel = await _requirement(ctx, run.id, ctx.panel)
    assert panel.available_quantity == D("10.0000")
    assert panel.incoming_quantity == D("20.0000")
    assert panel.shortage_quantity == D("30.0000")

    screw = await _requirement(ctx, run.id, ctx.screw)
    assert screw.dependent_demand_quantity == D("60.0000"), "two screws for each of 30 panels"


async def test_a_live_job_covers_the_parent_but_its_components_are_still_planned(
    ctx: Ctx,
) -> None:
    """A released job is a build that still has to be given its parts.

    This is the half of the old rule that was wrong. Netting the *exploded*
    quantity against in-progress production assumed a live job had already had
    its components planned and issued against it. It has not, necessarily:
    ``production.service.release_order`` reserves material only when
    ``ReleaseRequest.reserve_material`` asks it to - false is a supported
    release, for material already staged at the line - and ``_reserve_components``
    otherwise holds only what happened to be free and reports the remainder as a
    shortage rather than refusing the release. Issue is a separate document
    later still. So a RELEASED job can be holding nothing at all, and deleting
    its component demand is a stockout.

    The frame's own requirement still nets against the job, because those frames
    are real and ordering them twice would be a duplicate. Only the explosion is
    left alone.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.frame, "60")], required_in_days=25)
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="60",
        status=ProductionOrderStatus.RELEASED,
        planned_end_date=due(10),
    )

    run = await _plan(ctx)

    frame = await _requirement(ctx, run.id, ctx.frame)
    assert frame.in_production_quantity == D("60.0000")
    assert frame.shortage_quantity == D("0.0000")
    assert await _maybe_suggestion(ctx, run.id, ctx.frame) is None

    screw = await _requirement(ctx, run.id, ctx.screw)
    assert screw.dependent_demand_quantity == D("120.0000"), (
        "the live job's components were deleted; the job may hold none of them"
    )
    assert (await _suggestion(ctx, run.id, ctx.screw)).suggested_quantity == D("120.0000")
    assert run.result_summary is not None
    assert run.result_summary["explosion_count"] == 1


# =============================================================================
# The two stages have to agree on the DATE, not only on the quantity
# =============================================================================


async def test_an_item_that_is_sold_and_used_is_netted_at_one_date_not_two(
    ctx: Ctx,
) -> None:
    """The reproduction, against PostgreSQL.

    PANEL is sold on its own for day 25 and is also a component of CABINET,
    which is sold for day 40 and takes thirty days to build - so the cabinet
    needs its panels on day 10. ``MrpDemandRepository.all_for_run`` orders
    ``required_date ASC, id ASC``, so PANEL enters the buckets first and is
    visited *before* the cabinet's explosion pulls its date back from day 25 to
    day 10.

    A purchase order for 60 panels lands on day 20 - in time for day 25, far too
    late for day 10. Reading the date while the merge was still moving it, the
    explosion saw that order as in time and raised no screws, while the
    requirement saw it as late and reported the full shortage. The plan said
    "build 70 panels" and ordered screws for none of them.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "60")], required_in_days=25)
    await add_sales_order(ctx, code="SO/0002", lines=[(ctx.cabinet, "10")], required_in_days=40)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(20),
        lines=[{"item_id": ctx.panel.id, "quantity": "60"}],
    )

    run = await _plan(ctx)

    panel = await _requirement(ctx, run.id, ctx.panel)
    # The cabinet's thirty-day lead time governs the whole row.
    assert panel.earliest_required_date == due(10)
    assert panel.direct_demand_quantity == D("60.0000")
    assert panel.dependent_demand_quantity == D("10.0000")
    assert panel.incoming_quantity == D("0.0000"), "the order lands on day 20"
    assert panel.shortage_quantity == D("70.0000")
    # The excluded order is not dropped - the run reports it (asserted in full
    # by the test below).
    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == 1

    # ...and the screws for those 70 panels are actually in the plan: 120 for
    # the 60 the panel's own explosion raises, plus 40 through the cabinet (two
    # on the cabinet itself and the panel's two, per cabinet).
    screw = await _requirement(ctx, run.id, ctx.screw)
    assert screw.dependent_demand_quantity == D("160.0000")
    assert (await _suggestion(ctx, run.id, ctx.screw)).suggested_quantity == D("160.0000")

    assert run.result_summary is not None
    assert run.result_summary["demand_dates_settled"] is True
    # The panel is re-decided once, at the date that settled. The cabinet is
    # decided once. Nothing else moved, so nothing else was re-exploded.
    assert run.result_summary["explosion_count"] == 2


async def test_the_order_the_settled_date_made_late_is_still_reported(
    ctx: Ctx,
) -> None:
    """Sixty panels are genuinely on order, and the plan has to say so.

    Settling the date earlier is what turned that order late. Dropping it in
    silence would leave a buyer holding a purchase order for 60 and a plan
    demanding 70 with nothing connecting the two.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "60")], required_in_days=25)
    await add_sales_order(ctx, code="SO/0002", lines=[(ctx.cabinet, "10")], required_in_days=40)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(20),
        lines=[{"item_id": ctx.panel.id, "quantity": "60"}],
    )

    run = await _plan(ctx)

    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == 1
    late = [w for w in run.warnings if "arrives AFTER it is needed" in w]
    assert late, run.warnings
    assert ctx.panel.code in late[0]
    assert due(20).isoformat() in late[0], "when it arrives"
    assert due(10).isoformat() in late[0], "when it is needed"


# =============================================================================
# The excluded supply reaches the API as figures, not as English
# =============================================================================
#
# Everything above proves the engine time-phases and that the run *says* so. It
# said so in two places only: an integer in ``result_summary`` and a sentence in
# ``warnings``. A requirements table rendering "100 arriving, six weeks late"
# per row had to parse that sentence, which is fragile and untranslatable, and
# it hid the very information the phasing work exists to surface.
#
# ``mrp_requirements`` now persists the two quantities and the date the earliest
# excluded tranche lands, and ``MrpRequirementRead`` carries all three. These
# tests assert the columns and the read model, not the prose.


async def test_the_requirement_row_carries_the_late_quantity_and_its_date(
    ctx: Ctx,
) -> None:
    """The reproduction again, read off the row a table would render.

    Demand on day 2, one SENT purchase order for the whole quantity on day 44.
    Before this the row said "short 100" and nothing else; the order behind it
    existed only in a warning string.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.bolt.id, "quantity": "100"}],
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert requirement.shortage_quantity == D("100.0000")
    assert requirement.incoming_quantity == D("0.0000")
    assert requirement.late_incoming_quantity == D("100.0000")
    assert requirement.late_in_production_quantity == D("0.0000")
    assert requirement.first_late_supply_date == due(44)
    # BOTH ends of the interval have to survive the write. The quantity is a SUM
    # over the feed's late tranches, so persisting only the opening date puts
    # the original defect back on the row: 100 attributed to whichever tranche
    # happened to be first. One tranche here, so the interval is a point - and
    # the engine computing a closing date the service forgets to save is exactly
    # the gap this assertion exists to catch.
    assert requirement.first_late_incoming_date == due(44)
    assert requirement.last_late_incoming_date == due(44), (
        "the closing date was computed and dropped on the way to the database"
    )
    assert requirement.last_late_in_production_date is None
    # The column type, not just the value: a float here would be the defect the
    # whole module is written against.
    assert isinstance(requirement.late_incoming_quantity, Decimal)
    assert requirement.late_incoming_quantity.as_tuple().exponent == -4


async def test_a_late_job_is_dated_on_the_row_the_same_way(ctx: Ctx) -> None:
    """The production feed, not only the purchase feed."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.frame, "100")], required_in_days=2)
    await add_production_order(
        ctx,
        item_id=ctx.frame.id,
        planned="40",
        status=ProductionOrderStatus.IN_PROCESS,
        planned_end_date=due(20),
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.frame)
    assert requirement.in_production_quantity == D("0.0000")
    assert requirement.late_in_production_quantity == D("40.0000")
    assert requirement.late_incoming_quantity == D("0.0000")
    assert requirement.first_late_supply_date == due(20)


async def test_supply_that_arrives_in_time_leaves_the_row_with_no_late_figures(
    ctx: Ctx,
) -> None:
    """Zero and NULL, not a sentinel date standing in for "none".

    The same fixture as the netting test above: everything lands inside the
    demand date, so nothing was excluded and the row says nothing was.
    """
    await _seed_netting_scenario(ctx)

    run = await _plan(ctx)

    for item in (ctx.bolt, ctx.frame):
        requirement = await _requirement(ctx, run.id, item)
        assert requirement.late_incoming_quantity == D("0.0000")
        assert requirement.late_in_production_quantity == D("0.0000")
        assert requirement.first_late_supply_date is None
    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == 0


async def test_the_row_figures_and_the_run_count_are_the_same_statement(
    ctx: Ctx,
) -> None:
    """``late_supply_item_count`` counts the short rows carrying late supply.

    Two items with a late purchase order behind them; stock covers one of them
    outright. The stat counts the one whose shortage stands, and a client
    deriving that number from the rows has to land on the same answer - two
    numbers for one fact that disagree are worse than one number.
    """
    await add_sales_order(
        ctx, code="SO/0001", lines=[(ctx.bolt, "100"), (ctx.frame, "60")], required_in_days=2
    )
    await add_stock(ctx, ctx.frame, ctx.main, on_hand="500")
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[
            {"item_id": ctx.bolt.id, "quantity": "100"},
            {"item_id": ctx.frame.id, "quantity": "100"},
        ],
    )

    run = await _plan(ctx)

    rows = (
        (await ctx.session.execute(select(MrpRequirement).where(MrpRequirement.run_id == run.id)))
        .scalars()
        .all()
    )
    short_with_late_supply = [
        row
        for row in rows
        if row.shortage_quantity > 0
        and row.late_incoming_quantity + row.late_in_production_quantity > 0
    ]
    assert run.result_summary is not None
    assert run.result_summary["late_supply_item_count"] == len(short_with_late_supply) == 1
    assert short_with_late_supply[0].item_id == ctx.bolt.id

    # The covered item keeps its own dated figure even though the run raised no
    # caveat for it - the plan is right, and the figure is still a fact.
    covered = await _requirement(ctx, run.id, ctx.frame)
    assert covered.shortage_quantity == D("0.0000")
    assert covered.late_incoming_quantity == D("100.0000")
    assert covered.first_late_supply_date == due(44)


async def test_the_shortage_report_exposes_the_late_figures(ctx: Ctx) -> None:
    """The read model a requirements table actually consumes.

    ``shortage_report`` is what the screen calls. Asserting the persisted row
    alone would leave the schema free to drop the columns on the way out.
    """
    # The panel is the one seeded item that is both bought and made, so one row
    # can carry a late tranche from each feed.
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(20),
        lines=[{"item_id": ctx.panel.id, "quantity": "100"}],
    )
    await add_production_order(
        ctx,
        item_id=ctx.panel.id,
        planned="30",
        status=ProductionOrderStatus.IN_PROCESS,
        planned_end_date=due(40),
    )

    run = await _plan(ctx)

    rows, _ = await mrp_service.shortage_report(
        ctx.session,
        ctx.tenant,
        ShortageReportFilters(run_id=run.id),
        PageParams(page=1, limit=50),
        SortParams(),
    )

    row = next(r for r in rows if r.item_id == ctx.panel.id)
    assert row.late_incoming_quantity == D("100.0000")
    assert row.late_in_production_quantity == D("30.0000")
    # The convenience total, summed server side so no client adds two decimal
    # strings in JavaScript and gets a float back.
    assert row.late_supply_quantity == D("130.0000")
    assert isinstance(row.late_supply_quantity, Decimal)
    # Each quantity with the date of the feed it came from: the 100 on order
    # land on day 20, the 30 being made on day 40.
    assert row.first_late_incoming_date == due(20)
    assert row.first_late_in_production_date == due(40)
    # The purchase order lands twenty days before the job, so it is the soonest
    # relief and the one the row summarises with.
    assert row.first_late_supply_date == due(20)
    assert row.first_late_supply_date > row.earliest_required_date

    payload = row.model_dump(mode="json")
    assert payload["late_incoming_quantity"] == "100.0000"
    assert payload["late_in_production_quantity"] == "30.0000"
    assert payload["late_supply_quantity"] == "130.0000"
    assert payload["first_late_incoming_date"] == due(20).isoformat()
    assert payload["first_late_in_production_date"] == due(40).isoformat()
    assert payload["first_late_supply_date"] == due(20).isoformat()


async def test_the_shortage_report_reports_no_late_supply_as_null(ctx: Ctx) -> None:
    """A short item with nothing on order is the ordinary row, and reads as one."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)

    run = await _plan(ctx)

    rows, _ = await mrp_service.shortage_report(
        ctx.session,
        ctx.tenant,
        ShortageReportFilters(run_id=run.id),
        PageParams(page=1, limit=50),
        SortParams(),
    )

    row = next(r for r in rows if r.item_id == ctx.bolt.id)
    assert row.shortage_quantity == D("100.0000")
    assert row.late_supply_quantity == D("0.0000")
    assert row.first_late_supply_date is None
    assert row.first_late_incoming_date is None
    assert row.first_late_in_production_date is None
    payload = row.model_dump(mode="json")
    assert payload["first_late_supply_date"] is None
    assert payload["first_late_incoming_date"] is None
    assert payload["first_late_in_production_date"] is None


async def test_the_row_and_the_warning_name_the_same_date(ctx: Ctx) -> None:
    """One fact, two renderings. They are built from one ``SupplySplit``.

    This is the assertion that would fail if anyone reintroduced a second,
    independently computed date for the warning text.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.bolt.id, "quantity": "100"}],
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.bolt)
    assert requirement.first_late_supply_date is not None
    late = [w for w in run.warnings if LATE_SUPPLY_WARNING in w]
    assert late, run.warnings
    assert requirement.first_late_supply_date.isoformat() in late[0]


# -----------------------------------------------------------------------------
# ...and the two statements the columns make are enforced, not merely intended
# -----------------------------------------------------------------------------
#
# Both are written against a row a real run produced, with UPDATE rather than
# INSERT: a hand-built INSERT would have to name every NOT NULL column on the
# table and would go stale the moment one is added. The id is held in a plain
# variable before the failing statement, because a rolled-back savepoint expires
# the identity map and reading an attribute back off the ORM object afterwards
# would be a lazy load, not a value.


async def test_the_database_refuses_a_late_quantity_with_no_date(ctx: Ctx) -> None:
    """A quantity with no date is the half-fact the date column exists to end."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    requirement_id = (await _requirement(ctx, (await _plan(ctx)).id, ctx.bolt)).id

    with pytest.raises(IntegrityError):
        async with ctx.session.begin_nested():
            await ctx.session.execute(
                update(MrpRequirement)
                .where(MrpRequirement.id == requirement_id)
                .values(late_incoming_quantity=D("50"), first_late_supply_date=None)
            )


async def test_the_database_refuses_late_supply_that_is_not_actually_late(
    ctx: Ctx,
) -> None:
    """Late means exactly one thing: it lands after the date this row needs it.

    A date on the required date itself is in-time supply by the engine's own
    boundary rule, so a row claiming it as late is a phasing bug.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    requirement = await _requirement(ctx, (await _plan(ctx)).id, ctx.bolt)
    requirement_id = requirement.id
    needed_by = requirement.earliest_required_date

    with pytest.raises(IntegrityError):
        async with ctx.session.begin_nested():
            await ctx.session.execute(
                update(MrpRequirement)
                .where(MrpRequirement.id == requirement_id)
                .values(
                    late_incoming_quantity=D("50"),
                    first_late_supply_date=needed_by,
                )
            )


# =============================================================================
# ...and each late quantity is dated by ITS OWN feed
# =============================================================================
#
# The reproduction, in the reviewer's figures: one item, two independent
# schedules, one date. A purchase order for 5 000 arriving four months late and
# a released job for 0.0001 arriving the next day produced
#
#     late_incoming_quantity      = 5000.0000
#     late_in_production_quantity =    0.0001
#     first_late_supply_date      = <the job's date>
#
# so a table rendering the first figure beside the only date on the row said
# "5000 arriving, one day late". The columns exist precisely so a client does
# not have to parse the warning prose, which means each of them has to be true
# on its own. Two more columns fix it: one date per feed, beside the quantity it
# describes. ``first_late_supply_date`` stays, unchanged in meaning - the
# earliest of the two, the soonest anything at all lands - and is now derived
# from them rather than computed independently.
#
# The horizon here is 45 days (``_plan``), so "four months" is expressed as the
# far end of it: the shape is what matters - a large tranche far out and a tiny
# one just over the line.


async def test_each_late_quantity_on_the_row_carries_its_own_date(ctx: Ctx) -> None:
    """The reproduction against PostgreSQL, read off the persisted row."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.panel.id, "quantity": "5000"}],
    )
    await add_production_order(
        ctx,
        item_id=ctx.panel.id,
        planned="0.0001",
        status=ProductionOrderStatus.RELEASED,
        planned_end_date=due(3),
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.panel)
    assert requirement.earliest_required_date == due(2)
    assert requirement.shortage_quantity == D("100.0000"), "neither tranche is in time"

    assert requirement.late_incoming_quantity == D("5000.0000")
    assert requirement.first_late_incoming_date == due(44), (
        "the 5 000 land at the end of the horizon, whatever the job beside them does"
    )
    assert requirement.late_in_production_quantity == D("0.0001")
    assert requirement.first_late_in_production_date == due(3)
    # The summary is the earlier of the two - the soonest anything lands - and
    # is not the date of either quantity in particular.
    assert requirement.first_late_supply_date == due(3)


async def test_a_feed_with_no_late_supply_leaves_its_own_date_null(ctx: Ctx) -> None:
    """Null means "this feed excluded nothing", per feed rather than per row.

    The job lands in time and is netted; the purchase order does not. The
    production date has to be NULL even though the row as a whole has late
    supply, or "late" spreads to a feed that is on schedule.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "100")], required_in_days=10)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(30),
        lines=[{"item_id": ctx.panel.id, "quantity": "60"}],
    )
    await add_production_order(
        ctx,
        item_id=ctx.panel.id,
        planned="25",
        status=ProductionOrderStatus.RELEASED,
        planned_end_date=due(8),
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.panel)
    assert requirement.in_production_quantity == D("25.0000"), "the job is in time"
    assert requirement.late_in_production_quantity == D("0.0000")
    assert requirement.first_late_in_production_date is None
    assert requirement.late_incoming_quantity == D("60.0000")
    assert requirement.first_late_incoming_date == due(30)
    assert requirement.first_late_supply_date == due(30)


async def test_the_shortage_report_pairs_each_quantity_with_its_own_date(
    ctx: Ctx,
) -> None:
    """The read model a requirements table consumes, on the reproduction.

    Asserting the persisted row alone would leave the schema free to drop the
    per-feed dates on the way out and hand a client back the single date that
    caused this.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.panel.id, "quantity": "5000"}],
    )
    await add_production_order(
        ctx,
        item_id=ctx.panel.id,
        planned="0.0001",
        status=ProductionOrderStatus.RELEASED,
        planned_end_date=due(3),
    )

    run = await _plan(ctx)

    rows, _ = await mrp_service.shortage_report(
        ctx.session,
        ctx.tenant,
        ShortageReportFilters(run_id=run.id),
        PageParams(page=1, limit=50),
        SortParams(),
    )

    row = next(r for r in rows if r.item_id == ctx.panel.id)
    payload = row.model_dump(mode="json")
    # The two facts a planner acts on, each renderable without reference to the
    # other: expedite the 5 000, and do not wait for the 0.0001.
    assert (payload["late_incoming_quantity"], payload["first_late_incoming_date"]) == (
        "5000.0000",
        due(44).isoformat(),
    )
    assert (
        payload["late_in_production_quantity"],
        payload["first_late_in_production_date"],
    ) == ("0.0001", due(3).isoformat())
    assert payload["first_late_supply_date"] == due(3).isoformat()


async def test_the_row_and_the_warning_pair_the_same_quantity_with_the_same_date(
    ctx: Ctx,
) -> None:
    """One fact, two renderings, and neither may pair a feed with the other's date.

    The warning used to print the *sum* of both feeds beside the *earliest* date
    of either - the same false pairing the single column had, one level up. Both
    are now written from one ``SupplySplit``, per feed.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.panel, "100")], required_in_days=2)
    await add_purchase_order(
        ctx,
        status=PurchaseOrderStatus.SENT,
        expected_delivery_date=due(44),
        lines=[{"item_id": ctx.panel.id, "quantity": "5000"}],
    )
    await add_production_order(
        ctx,
        item_id=ctx.panel.id,
        planned="0.0001",
        status=ProductionOrderStatus.RELEASED,
        planned_end_date=due(3),
    )

    run = await _plan(ctx)

    requirement = await _requirement(ctx, run.id, ctx.panel)
    late = [w for w in run.warnings if LATE_SUPPLY_WARNING in w]
    assert late, run.warnings
    text = late[0]

    assert ctx.panel.code in text
    assert (
        f"{requirement.late_incoming_quantity} on order arriving "
        f"{requirement.first_late_incoming_date.isoformat()}" in text
    )
    assert (
        f"{requirement.late_in_production_quantity} in production arriving "
        f"{requirement.first_late_in_production_date.isoformat()}" in text
    )
    assert requirement.earliest_required_date.isoformat() in text
    # ...and the pairing that was wrong is not in the sentence either.
    assert f"5000.0000 on order arriving {due(3).isoformat()}" not in text
    assert "5000.0001" not in text, "the two feeds are not summed into one tranche"


# -----------------------------------------------------------------------------
# ...and the per-feed statements are enforced, not merely intended
# -----------------------------------------------------------------------------
#
# UPDATE against a row a real run produced, for the reason the block above
# gives: a hand-built INSERT would have to name every NOT NULL column on the
# table and would go stale the moment one is added. The id is held in a plain
# variable before the failing statement, because a rolled-back savepoint expires
# the identity map.


async def test_the_database_refuses_a_feed_date_that_is_not_actually_late(
    ctx: Ctx,
) -> None:
    """Each feed's date carries the same guarantee the summary always did.

    The summary is the *earlier* of the two, so it passing this check does not
    prove the later one would - which is exactly why the pair is checked too.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    requirement = await _requirement(ctx, (await _plan(ctx)).id, ctx.bolt)
    requirement_id = requirement.id
    needed_by = requirement.earliest_required_date

    with pytest.raises(IntegrityError):
        async with ctx.session.begin_nested():
            await ctx.session.execute(
                update(MrpRequirement)
                .where(MrpRequirement.id == requirement_id)
                .values(
                    late_in_production_quantity=D("5"),
                    first_late_in_production_date=needed_by,
                    first_late_supply_date=needed_by + timedelta(days=1),
                )
            )


async def test_the_database_refuses_a_summary_date_the_feeds_do_not_support(
    ctx: Ctx,
) -> None:
    """Two descriptions of one fact that can disagree is the defect itself.

    The summary is the earlier of the two feed dates by definition. A row
    claiming a date neither feed lands on is an engine bug, and the database is
    the only place that statement cannot be quietly broken later.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.bolt, "100")], required_in_days=2)
    requirement = await _requirement(ctx, (await _plan(ctx)).id, ctx.bolt)
    requirement_id = requirement.id

    with pytest.raises(IntegrityError):
        async with ctx.session.begin_nested():
            await ctx.session.execute(
                update(MrpRequirement)
                .where(MrpRequirement.id == requirement_id)
                .values(
                    late_incoming_quantity=D("5000"),
                    first_late_incoming_date=due(44),
                    # The job's date, on a row whose only late feed is the
                    # purchase order: the reproduction, written straight into
                    # the column.
                    first_late_supply_date=due(3),
                )
            )

    # ...and the honest version of the same row is accepted.
    async with ctx.session.begin_nested():
        await ctx.session.execute(
            update(MrpRequirement)
            .where(MrpRequirement.id == requirement_id)
            .values(
                late_incoming_quantity=D("5000"),
                first_late_incoming_date=due(44),
                first_late_supply_date=due(44),
            )
        )
