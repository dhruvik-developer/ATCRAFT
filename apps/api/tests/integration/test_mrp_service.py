"""Service-level planning tests against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible
without it: the ``UNIQUE (run_id, item_id)`` index that makes the common-part
merge structural rather than aspirational, the generated ``available_quantity``
column the netting reads, the ``DISTINCT ON`` BOM effectivity the explosion
resolves through, the advisory lock that serialises two simultaneous starts, and
the audit rows written inside the same transaction as the change they describe.

The scenario is the master rules' own: a lock kit built from a rose handle and a
screw, where the handle is *also* sold as a spare - the case that catches an
engine netting one stock pool against two demands.

Isolation follows the pattern in ``test_bom_service.py``: one throwaway schema
per module, built once, with each test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.customers.models import Customer, CustomerType
from src.modules.inventory.models import InventoryBalance
from src.modules.mrp import service
from src.modules.mrp.models import (
    MrpRequirement,
    MrpRun,
    MrpRunMode,
    MrpRunStatus,
    MrpSuggestion,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.mrp.schemas import (
    BulkIgnoreRequest,
    ConversionTarget,
    ManualDemandIn,
    MrpRunFilters,
    MrpRunRequest,
    ShortageReportFilters,
    SuggestionConvertRequest,
    SuggestionFilters,
)
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import PurchaseRequisition

# Production is deployed, so the MANUFACTURE half of the conversion seam is live:
# these tests read the order it actually created rather than trusting the hook.
from src.modules.production import service as production_service
from src.modules.production.models import ProductionOrder, ProductionSourceType
from src.modules.products.models import (
    Item,
    ItemGroup,
    ItemVendorLink,
    ProductionType,
    UnitOfMeasure,
    UomCategory,
)
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration

D = Decimal

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and a planner, built once."""
    schema = f"it_mrp_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    planner_id = uuid.uuid4()

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
                    email="planner@atcraft.test",
                    full_name="Planner",
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

    yield schema, company_id, other_company_id, planner_id
    asyncio.run(teardown())


def _schema_engine(schema: str) -> Any:
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(autouse=True)
def _in_memory_planning_lock(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Replace the Redis planning lock with an in-process one.

    The lock's *contract* is what these tests care about - "a second run is
    refused" - and asserting it against a real Redis would make the suite depend
    on a service that is not what is under test. The degradation path when Redis
    is unreachable is exercised separately, by the service's own except branch.
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

    monkeypatch.setattr(service, "acquire_lock", fake_acquire)
    monkeypatch.setattr(service, "release_lock", fake_release)
    yield held


class Ctx:
    """Everything a test needs: a session, the tenant, and the seeded master data."""

    def __init__(self, session: AsyncSession, tenant: TenantContext, other: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.other = other
        self.uom: UnitOfMeasure
        self.main_warehouse: Warehouse
        self.spare_warehouse: Warehouse
        self.customer: Customer
        self.kit: Item
        self.handle: Item
        self.screw: Item


@pytest_asyncio.fixture
async def ctx(_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]) -> AsyncIterator[Ctx]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, other_company_id, planner_id = _schema
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
                    other=TenantContext(company_id=other_company_id, user_id=uuid.uuid4()),
                )
                await seed_master_data(context)
                yield context
            await outer.rollback()
    finally:
        await eng.dispose()


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------


def _item(ctx: Ctx, code: str, **kwargs: Any) -> Item:
    defaults: dict[str, Any] = {
        "company_id": ctx.tenant.company_id,
        "code": code,
        "name": f"{code} description",
        "item_group": ItemGroup.COMPONENT,
        "base_uom_id": ctx.uom.id,
        "production_type": ProductionType.BUY,
        "can_purchase": True,
        "can_be_bom_component": True,
        # The item master's default store (database.md section 3). Converting a
        # suggestion has to name a warehouse for the document to receive into,
        # and a company-wide run names none, so an item without a default store
        # is incomplete master data rather than a plannable input.
        "default_warehouse_id": ctx.main_warehouse.id,
        "is_active": True,
    }
    defaults.update(kwargs)
    return Item(**defaults)


async def seed_master_data(ctx: Ctx) -> None:
    """Units, warehouses, a customer, three items and the kit's structure."""
    session = ctx.session
    company_id = ctx.tenant.company_id
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
    ctx.main_warehouse = Warehouse(
        company_id=company_id,
        code=f"WH-M{suffix}",
        name="Main store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        country="India",
        is_active=True,
    )
    ctx.spare_warehouse = Warehouse(
        company_id=company_id,
        code=f"WH-S{suffix}",
        name="Spares store",
        warehouse_type=WarehouseType.FINISHED_GOODS,
        country="India",
        is_active=True,
    )
    ctx.customer = Customer(
        company_id=company_id,
        code=f"CUST{suffix}",
        name="Hardware Distributors",
        customer_type=CustomerType.DEALER,
        is_active=True,
    )
    session.add_all([ctx.uom, ctx.main_warehouse, ctx.spare_warehouse, ctx.customer])
    await session.flush()

    ctx.kit = _item(
        ctx,
        f"LOCK-KIT-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
        can_sell=True,
        lead_time_days=10,
    )
    ctx.handle = _item(
        ctx,
        f"ROSE-HANDLE-{suffix}",
        can_sell=True,
        lead_time_days=7,
        min_order_quantity=D("0"),
    )
    ctx.screw = _item(ctx, f"SCREW-M4-{suffix}", lead_time_days=21)
    session.add_all([ctx.kit, ctx.handle, ctx.screw])
    await session.flush()

    bom = Bom(
        company_id=company_id,
        code=f"BOM-{suffix}",
        item_id=ctx.kit.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.PRODUCTION,
        is_default=True,
        output_quantity=D("1"),
        base_uom_id=ctx.uom.id,
        effective_from=today() - timedelta(days=30),
        # An APPROVED structure has to carry its approval evidence; the check
        # constraint on `boms` refuses one that does not.
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
                parent_item_id=ctx.kit.id,
                line_number=1,
                component_item_id=ctx.handle.id,
                quantity_per=D("2"),
                uom_id=ctx.uom.id,
            ),
            BomLine(
                company_id=company_id,
                bom_id=bom.id,
                parent_item_id=ctx.kit.id,
                line_number=2,
                component_item_id=ctx.screw.id,
                quantity_per=D("6"),
                uom_id=ctx.uom.id,
            ),
        ]
    )
    await session.flush()


async def add_stock(
    ctx: Ctx,
    item: Item,
    warehouse: Warehouse,
    *,
    on_hand: str,
    reserved: str = "0",
) -> InventoryBalance:
    balance = InventoryBalance(
        company_id=ctx.tenant.company_id,
        item_id=item.id,
        warehouse_id=warehouse.id,
        on_hand_quantity=D(on_hand),
        reserved_quantity=D(reserved),
    )
    ctx.session.add(balance)
    await ctx.session.flush()
    return balance


async def add_sales_order(
    ctx: Ctx,
    *,
    code: str,
    lines: list[tuple[Item, str]],
    required_in_days: int = 45,
    warehouse: Warehouse | None = None,
    status: SalesOrderStatus = SalesOrderStatus.CONFIRMED,
    allocated: str = "0",
) -> SalesOrder:
    order = SalesOrder(
        company_id=ctx.tenant.company_id,
        code=code,
        customer_id=ctx.customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=required_in_days),
        warehouse_id=(warehouse or ctx.main_warehouse).id,
        status=status,
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
                allocated_quantity=D(allocated),
            )
            for index, (item, quantity) in enumerate(lines, start=1)
        ]
    )
    await ctx.session.flush()
    return order


async def run_plan(ctx: Ctx, **kwargs: Any) -> Any:
    payload = MrpRunRequest(**kwargs)
    run, dispatched = await service.start_run(ctx.session, ctx.tenant, payload)
    assert dispatched is False, "this scenario is below the async threshold"
    return run


async def requirement_for(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpRequirement | None:
    stmt = select(MrpRequirement).where(
        MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.id
    )
    return (await ctx.session.execute(stmt)).scalar_one_or_none()


async def suggestion_for(ctx: Ctx, run_id: uuid.UUID, item: Item) -> MrpSuggestion | None:
    stmt = select(MrpSuggestion).where(
        MrpSuggestion.run_id == run_id, MrpSuggestion.item_id == item.id
    )
    return (await ctx.session.execute(stmt)).scalar_one_or_none()


# -----------------------------------------------------------------------------
# The plan itself
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_small_run_completes_inline_and_persists_its_plan(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "100")])

    run = await run_plan(ctx)

    assert run.status is MrpRunStatus.COMPLETED
    assert run.mode is MrpRunMode.SYNC
    assert run.demand_line_count == 1
    assert run.celery_task_id is None
    assert run.started_at is not None and run.completed_at is not None

    kit_requirement = await requirement_for(ctx, run.id, ctx.kit)
    assert kit_requirement is not None
    assert kit_requirement.gross_requirement == D("100.0000")
    assert kit_requirement.shortage_quantity == D("100.0000")

    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.gross_requirement == D("600.0000")
    assert screw.level == 1


@pytest.mark.asyncio
async def test_two_orders_for_one_product_merge_into_one_screw_requirement(ctx: Ctx) -> None:
    """The database enforces it: UNIQUE (run_id, item_id) on mrp_requirements.

    An engine that emitted two rows would fail on the index rather than reach a
    buyer, which is exactly why the constraint is there.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "60")])
    await add_sales_order(ctx, code="SO/0002", lines=[(ctx.kit, "40")])

    run = await run_plan(ctx)

    count = (
        await ctx.session.execute(
            select(func.count(MrpRequirement.id)).where(
                MrpRequirement.run_id == run.id, MrpRequirement.item_id == ctx.screw.id
            )
        )
    ).scalar_one()
    assert count == 1

    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.gross_requirement == D("600.0000")

    # Provenance is pegged level by level, which is what keeps a merged number
    # traceable: the screw names the branch it arrived through, and that branch
    # names both orders.
    assert screw.source_demand_count == 1
    assert screw.source_demands[0]["via_item_code"] == ctx.kit.code
    kit = await requirement_for(ctx, run.id, ctx.kit)
    assert kit is not None
    assert kit.source_demand_count == 2
    assert sorted(entry["code"] for entry in kit.source_demands) == ["SO/0001", "SO/0002"]


@pytest.mark.asyncio
async def test_an_item_sold_and_used_in_a_bom_nets_its_stock_once(ctx: Ctx) -> None:
    """100 handles sold as spares, 100 more consumed by 50 kits, 60 in stock.

    The requirement is 200 and the shortage is 140. Netting the 60 against both
    demands separately would report 80 and under-order by 60.
    """
    await add_stock(ctx, ctx.handle, ctx.main_warehouse, on_hand="60")
    await add_sales_order(ctx, code="SO/SPARE", lines=[(ctx.handle, "100")])
    await add_sales_order(ctx, code="SO/KIT", lines=[(ctx.kit, "50")])

    run = await run_plan(ctx)

    handle = await requirement_for(ctx, run.id, ctx.handle)
    assert handle is not None
    assert handle.direct_demand_quantity == D("100.0000")
    assert handle.dependent_demand_quantity == D("100.0000")
    assert handle.gross_requirement == D("200.0000")
    assert handle.available_quantity == D("60.0000")
    assert handle.shortage_quantity == D("140.0000")
    assert handle.level == 0


@pytest.mark.asyncio
async def test_a_stocked_sub_assembly_reduces_its_component_demand(ctx: Ctx) -> None:
    """The proposal's worked example, through a real database.

    100 kits need 200 handles; 50 handles are in stock, so only 150 are built
    and only those raise handle bodies. The old engine exploded the whole order
    because the handle was also directly demanded elsewhere in the plan's
    horizon; level ordering makes the sub-assembly's shortage - not its gross
    requirement - the quantity exploded.
    """
    body = _item(ctx, f"HANDLE-BODY-{uuid.uuid4().hex[:6].upper()}", lead_time_days=5)
    ctx.session.add(body)
    await ctx.session.flush()

    # The handle becomes a manufactured sub-assembly with a structure of its
    # own. Everything else about the master data is unchanged.
    ctx.handle.production_type = ProductionType.MAKE
    ctx.handle.can_manufacture = True
    ctx.handle.can_purchase = False
    handle_bom = Bom(
        company_id=ctx.tenant.company_id,
        code=f"BOM-HANDLE-{uuid.uuid4().hex[:6].upper()}",
        item_id=ctx.handle.id,
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
    ctx.session.add(handle_bom)
    await ctx.session.flush()
    ctx.session.add(
        BomLine(
            company_id=ctx.tenant.company_id,
            bom_id=handle_bom.id,
            parent_item_id=ctx.handle.id,
            line_number=1,
            component_item_id=body.id,
            quantity_per=D("4"),
            uom_id=ctx.uom.id,
        )
    )
    await ctx.session.flush()

    await add_stock(ctx, ctx.handle, ctx.main_warehouse, on_hand="50")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "100")])

    run = await run_plan(ctx)

    handle = await requirement_for(ctx, run.id, ctx.handle)
    assert handle is not None
    assert handle.gross_requirement == D("200.0000")
    assert handle.shortage_quantity == D("150.0000")
    assert handle.low_level_code == 1

    body_requirement = await requirement_for(ctx, run.id, body)
    assert body_requirement is not None
    assert body_requirement.gross_requirement == D("600.0000"), (
        "four bodies for each of the 150 handles actually built - not 800"
    )
    assert body_requirement.low_level_code == 2

    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.gross_requirement == D("600.0000"), "six screws per kit, unmoved"


@pytest.mark.asyncio
async def test_requirement_rows_carry_an_expected_availability_date(ctx: Ctx) -> None:
    """Every persisted row has a date, and the run summary agrees with them.

    A covered requirement is available from the run date; a short one waits for
    the order the suggestion raises. Neither is nullable on a completed run.
    """
    await add_stock(ctx, ctx.handle, ctx.main_warehouse, on_hand="500")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])

    run = await run_plan(ctx)

    kit = await requirement_for(ctx, run.id, ctx.kit)
    handle = await requirement_for(ctx, run.id, ctx.handle)
    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert kit is not None and handle is not None and screw is not None
    for requirement in (kit, handle, screw):
        assert requirement.expected_available_date is not None
        assert requirement.low_level_code is not None

    as_of = today()
    # The handle is fully covered by stock, so it is available now. The kit is
    # needed in 45 days; its screw is due 10 days earlier, still inside the
    # screw's 21-day lead, so nothing slips and both rows carry the order date.
    assert handle.expected_available_date == as_of
    assert await suggestion_for(ctx, run.id, ctx.handle) is None, "stock covers it"
    kit_suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    screw_suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert kit_suggestion is not None and screw_suggestion is not None
    assert kit.expected_available_date == as_of + timedelta(days=45)
    assert kit.expected_available_date == kit_suggestion.expected_available_date
    assert screw.expected_available_date == as_of + timedelta(days=35)
    assert screw.expected_available_date == screw_suggestion.expected_available_date
    assert run.result_summary is not None
    assert run.result_summary["low_level_code_max"] == 1


@pytest.mark.asyncio
async def test_the_sales_order_availability_comes_from_the_latest_completed_plan(
    ctx: Ctx,
) -> None:
    """The sales screen reads the plan's stamped dates, not a fresh calculation.

    The date has to be defensible against the plan it was taken from: the same
    number the shortage report shows, under the run that produced it.
    """
    order = await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    line = (
        await ctx.session.execute(
            select(SalesOrderLine).where(SalesOrderLine.sales_order_id == order.id)
        )
    ).scalar_one()

    run = await run_plan(ctx)
    availability = await service.sales_order_availability(ctx.session, ctx.tenant, order.id)

    assert availability.run_id == run.id
    assert availability.run_code == run.code
    assert availability.completed_at == run.completed_at
    assert len(availability.lines) == 1
    assert availability.lines[0].sales_order_line_id == line.id
    assert availability.lines[0].item_id == ctx.kit.id
    assert availability.lines[0].expected_available_date == today() + timedelta(days=45)


@pytest.mark.asyncio
async def test_an_order_no_completed_plan_has_seen_has_no_availability(ctx: Ctx) -> None:
    """No plan is an empty report, not an error and not a fabricated today."""
    order = await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])

    availability = await service.sales_order_availability(ctx.session, ctx.tenant, order.id)

    assert availability.run_id is None
    assert availability.run_code is None
    assert availability.completed_at is None
    assert availability.lines == []


@pytest.mark.asyncio
async def test_reserved_stock_is_not_planned_against(ctx: Ctx) -> None:
    """Section 14: reserved stock is not available.

    100 on hand with 90 promised elsewhere leaves 10 the plan may use. The
    reservation is also excluded from the demand side - the order line's
    allocated quantity - so it is neither counted as supply nor demanded twice.
    """
    await add_stock(ctx, ctx.screw, ctx.main_warehouse, on_hand="100", reserved="90")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "500")])

    run = await run_plan(ctx)

    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.on_hand_quantity == D("100.0000")
    assert screw.reserved_quantity == D("90.0000")
    assert screw.available_quantity == D("10.0000")
    assert screw.shortage_quantity == D("490.0000")


@pytest.mark.asyncio
async def test_an_already_allocated_order_line_is_not_planned_for_again(ctx: Ctx) -> None:
    """The other half of the same rule, read from the demand side."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "500")], allocated="200")

    run = await run_plan(ctx)

    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.gross_requirement == D("300.0000")


@pytest.mark.asyncio
async def test_a_run_nets_only_against_the_warehouses_it_was_scoped_to(ctx: Ctx) -> None:
    """Multi-warehouse: stock in a store the run was told to ignore is not supply."""
    await add_stock(ctx, ctx.screw, ctx.spare_warehouse, on_hand="1000")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "500")])

    scoped = await run_plan(ctx, warehouse_ids=[ctx.main_warehouse.id])
    scoped_screw = await requirement_for(ctx, scoped.id, ctx.screw)
    assert scoped_screw is not None
    assert scoped_screw.available_quantity == D("0.0000")
    assert scoped_screw.shortage_quantity == D("500.0000")

    company_wide = await run_plan(ctx)
    wide_screw = await requirement_for(ctx, company_wide.id, ctx.screw)
    assert wide_screw is not None
    assert wide_screw.available_quantity == D("1000.0000")
    assert wide_screw.shortage_quantity == D("0.0000")


@pytest.mark.asyncio
async def test_demand_is_scoped_to_the_requested_warehouses(ctx: Ctx) -> None:
    """An order shipping from another store is not this run's demand."""
    await add_sales_order(
        ctx, code="SO/SPARES", lines=[(ctx.screw, "500")], warehouse=ctx.spare_warehouse
    )

    run = await run_plan(ctx, warehouse_ids=[ctx.main_warehouse.id])

    assert run.demand_line_count == 0
    assert await requirement_for(ctx, run.id, ctx.screw) is None


@pytest.mark.asyncio
async def test_demand_beyond_the_horizon_is_not_planned(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/FAR", lines=[(ctx.kit, "100")], required_in_days=200)

    run = await run_plan(ctx, horizon_days=30)

    assert run.demand_line_count == 0


@pytest.mark.asyncio
async def test_draft_orders_are_not_demand(ctx: Ctx) -> None:
    """An unconfirmed order is a quotation, not a commitment to plan against."""
    await add_sales_order(
        ctx, code="SO/DRAFT", lines=[(ctx.kit, "100")], status=SalesOrderStatus.DRAFT
    )

    run = await run_plan(ctx)

    assert run.demand_line_count == 0


@pytest.mark.asyncio
async def test_manual_demand_is_planned_beside_the_order_book(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])

    run = await run_plan(
        ctx,
        manual_demand=[
            ManualDemandIn(
                item_id=ctx.screw.id,
                quantity=D("400"),
                required_date=today() + timedelta(days=20),
                notes="Spares buffer",
            )
        ],
    )

    assert run.demand_line_count == 2
    screw = await requirement_for(ctx, run.id, ctx.screw)
    assert screw is not None
    assert screw.gross_requirement == D("500.0000")
    # The manual line is the earlier of the two, so it sets the requirement date.
    assert screw.earliest_required_date == today() + timedelta(days=20)


@pytest.mark.asyncio
async def test_safety_stock_is_protected_by_default_and_releasable_on_request(ctx: Ctx) -> None:
    ctx.screw.safety_stock = D("200")
    await ctx.session.flush()
    await add_stock(ctx, ctx.screw, ctx.main_warehouse, on_hand="500")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "400")])

    protected = await run_plan(ctx)
    protected_screw = await requirement_for(ctx, protected.id, ctx.screw)
    assert protected_screw is not None
    assert protected_screw.available_quantity == D("300.0000")
    assert protected_screw.shortage_quantity == D("100.0000")

    released = await run_plan(ctx, respect_safety_stock=False)
    released_screw = await requirement_for(ctx, released.id, ctx.screw)
    assert released_screw is not None
    assert released_screw.available_quantity == D("500.0000")
    assert released_screw.shortage_quantity == D("0.0000")


# -----------------------------------------------------------------------------
# Suggestions
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggestion_types_follow_the_item_master(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])

    run = await run_plan(ctx)

    kit_suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    screw_suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert kit_suggestion is not None and screw_suggestion is not None
    assert kit_suggestion.suggestion_type is MrpSuggestionType.MANUFACTURE
    assert screw_suggestion.suggestion_type is MrpSuggestionType.PURCHASE


@pytest.mark.asyncio
async def test_the_preferred_vendors_terms_shape_the_suggested_quantity(ctx: Ctx) -> None:
    """A 500-piece minimum in cartons of 250: a shortage of 60 becomes 500."""
    vendor = Vendor(
        company_id=ctx.tenant.company_id,
        code=f"V{uuid.uuid4().hex[:8].upper()}",
        name="Fastener Supplies",
        vendor_types=[VendorType.COMPONENT_SUPPLIER.value],
    )
    ctx.session.add(vendor)
    await ctx.session.flush()
    ctx.session.add(
        ItemVendorLink(
            company_id=ctx.tenant.company_id,
            item_id=ctx.screw.id,
            vendor_id=vendor.id,
            is_preferred=True,
            is_active=True,
            lead_time_days=30,
            min_order_quantity=D("500"),
            order_multiple=D("250"),
        )
    )
    await ctx.session.flush()
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "60")])

    run = await run_plan(ctx)

    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    assert suggestion.required_quantity == D("60.0000")
    assert suggestion.suggested_quantity == D("500.0000")
    # The vendor's lead time, not the item's 21 days, paces a purchase.
    assert suggestion.lead_time_days == 30
    assert suggestion.suggested_order_date == suggestion.required_date - timedelta(days=30)


@pytest.mark.asyncio
async def test_a_suggestion_whose_order_date_has_passed_is_flagged_late(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/RUSH", lines=[(ctx.screw, "100")], required_in_days=5)

    run = await run_plan(ctx)

    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    assert suggestion.suggested_order_date < today()
    assert suggestion.is_late is True


@pytest.mark.asyncio
async def test_a_covered_item_gets_no_suggestion(ctx: Ctx) -> None:
    await add_stock(ctx, ctx.screw, ctx.main_warehouse, on_hand="5000")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])

    run = await run_plan(ctx)

    assert await suggestion_for(ctx, run.id, ctx.screw) is None
    requirement = await requirement_for(ctx, run.id, ctx.screw)
    assert requirement is not None
    assert requirement.shortage_quantity == D("0.0000")


# -----------------------------------------------------------------------------
# Concurrency, audit and isolation
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_run_is_refused_while_one_is_in_progress(ctx: Ctx) -> None:
    """The planning lock, from the caller's point of view."""
    ctx.session.add(
        MrpRun(
            company_id=ctx.tenant.company_id,
            code=f"MRP/{uuid.uuid4().hex[:8]}",
            status=MrpRunStatus.RUNNING,
            mode=MrpRunMode.ASYNC,
            horizon_days=90,
            started_at=service.utcnow(),
        )
    )
    await ctx.session.flush()

    with pytest.raises(service.MrpRunInProgressError):
        await service.start_run(ctx.session, ctx.tenant, MrpRunRequest())


@pytest.mark.asyncio
async def test_a_run_abandoned_by_a_dead_worker_does_not_block_planning(ctx: Ctx) -> None:
    """A stale RUNNING row is closed off as FAILED rather than blocking forever."""
    abandoned = MrpRun(
        company_id=ctx.tenant.company_id,
        code=f"MRP/{uuid.uuid4().hex[:8]}",
        status=MrpRunStatus.RUNNING,
        mode=MrpRunMode.ASYNC,
        horizon_days=90,
        started_at=service.utcnow() - timedelta(seconds=service.RUN_STALE_AFTER_SECONDS + 60),
    )
    ctx.session.add(abandoned)
    await ctx.session.flush()
    abandoned_id = abandoned.id

    run = await run_plan(ctx)
    assert run.status is MrpRunStatus.COMPLETED

    closed = (
        await ctx.session.execute(select(MrpRun).where(MrpRun.id == abandoned_id))
    ).scalar_one()
    assert closed.status is MrpRunStatus.FAILED
    assert closed.error_message is not None


@pytest.mark.asyncio
async def test_the_lock_is_released_so_a_second_run_can_follow(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "10")])

    first = await run_plan(ctx)
    second = await run_plan(ctx)

    assert first.id != second.id
    assert second.status is MrpRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_large_run_is_queued_rather_than_held_open(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 14: the server decides, and a big plan never blocks a request."""
    monkeypatch.setattr(settings, "mrp_sync_threshold_demand_lines", 1)
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10"), (ctx.screw, "10")])

    dispatched: dict[str, Any] = {}

    class _Task:
        id = "task-under-test"

    from src.workers import celery_app as celery_module

    def fake_send_task(name: str, **kwargs: Any) -> _Task:
        dispatched["name"] = name
        dispatched["kwargs"] = kwargs["kwargs"]
        return _Task()

    monkeypatch.setattr(celery_module.celery_app, "send_task", fake_send_task)

    run, was_dispatched = await service.start_run(ctx.session, ctx.tenant, MrpRunRequest())

    assert was_dispatched is True
    assert run.status is MrpRunStatus.QUEUED
    assert run.mode is MrpRunMode.ASYNC
    assert run.celery_task_id == "task-under-test"
    assert run.status_url.endswith(str(run.id))
    assert dispatched["name"] == service.MRP_TASK_NAME
    assert dispatched["kwargs"]["run_id"] == str(run.id)

    # Nothing was calculated: the demand snapshot exists, the plan does not.
    requirements = (
        await ctx.session.execute(
            select(func.count(MrpRequirement.id)).where(MrpRequirement.run_id == run.id)
        )
    ).scalar_one()
    assert requirements == 0


@pytest.mark.asyncio
async def test_a_queued_run_is_executed_by_the_worker_entry_point(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``execute_run`` is what the Celery task calls; drive it directly."""
    monkeypatch.setattr(settings, "mrp_sync_threshold_demand_lines", 1)
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10"), (ctx.screw, "10")])

    from src.workers import celery_app as celery_module

    class _Task:
        id = "task-under-test"

    monkeypatch.setattr(celery_module.celery_app, "send_task", lambda *a, **k: _Task())
    run, _ = await service.start_run(ctx.session, ctx.tenant, MrpRunRequest())

    completed = await service.execute_run(ctx.session, ctx.tenant, run_id=run.id)

    assert completed.status is MrpRunStatus.COMPLETED
    assert completed.suggestion_count > 0

    # A re-delivered task must not write a second plan for the same run.
    with pytest.raises(InvalidStateTransitionError):
        await service.execute_run(ctx.session, ctx.tenant, run_id=run.id)


@pytest.mark.asyncio
async def test_the_run_writes_its_audit_trail_in_the_same_transaction(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])

    run = await run_plan(ctx)

    stmt = select(AuditLog.action).where(
        AuditLog.company_id == ctx.tenant.company_id,
        AuditLog.entity_type == "mrp_run",
        AuditLog.entity_id == run.id,
    )
    actions = set((await ctx.session.execute(stmt)).scalars().all())
    assert AuditAction.CREATE in actions
    assert AuditAction.STATUS_CHANGE in actions
    assert AuditAction.MRP_RUN in actions


@pytest.mark.asyncio
async def test_another_tenants_runs_are_invisible(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    await run_plan(ctx)

    rows, _ = await service.list_runs(
        ctx.session,
        ctx.other,
        MrpRunFilters(),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert rows == []


# -----------------------------------------------------------------------------
# Acting on the output
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_undeployed_module_closes_the_suggestion_and_returns_the_request(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the target module absent, the seam hands the instruction back.

    ``handled`` is false and ``converted_to_id`` is NULL: the gap is visible in
    the payload rather than implied by a status that claims a document exists.

    The absence is *forced*, by pointing the hook at a module that cannot be
    imported. This test used to assert that procurement really was undeployed,
    which stopped being true the day it shipped its hook - CLAUDE.md records
    that rule: prove a seam's behaviour by resolving it to nothing, never by
    asserting that a real module is missing.
    """
    monkeypatch.setitem(
        service.CONVERSION_HOOKS,
        ConversionTarget.PURCHASE_REQUISITION,
        ("src.modules.not_deployed", "create_requisition_from_mrp"),
    )

    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    assert result.handled is False
    assert result.created_document_id is None
    assert result.request.target is ConversionTarget.PURCHASE_REQUISITION
    assert result.request.quantity == suggestion.suggested_quantity
    assert result.suggestion.status is MrpSuggestionStatus.CONVERTED
    assert result.suggestion.converted_to_type == "purchase_requisition"
    assert result.suggestion.converted_to_id is None


@pytest.mark.asyncio
async def test_a_buy_suggestion_becomes_a_draft_requisition(ctx: Ctx) -> None:
    """The other half: procurement IS deployed, so the document is real.

    Written against the module's own hook rather than a fake, because the pair
    of tests is what pins the seam: one proves the fallback when nothing is
    there, this one proves the wiring when something is.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    assert result.handled is True
    assert result.created_document_type == "purchase_requisition"
    assert result.created_document_id is not None
    assert result.suggestion.status is MrpSuggestionStatus.CONVERTED
    assert result.suggestion.converted_to_id == result.created_document_id

    # The requisition exists in this transaction - the hook runs inside it, so
    # the suggestion and the document it became commit together or not at all.
    requisition = (
        await ctx.session.execute(
            select(PurchaseRequisition).where(PurchaseRequisition.id == result.created_document_id)
        )
    ).scalar_one()
    assert requisition.code == result.created_document_code
    assert requisition.source_id == suggestion.id


@pytest.mark.asyncio
async def test_a_deployed_module_is_wired_through_the_same_seam(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the hook exists, the document is created inside this transaction."""
    document_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    async def fake_hook(session: AsyncSession, tenant: TenantContext, request: Any) -> Any:
        captured["item_code"] = request.item_code
        captured["quantity"] = request.quantity
        return document_id, "PR/2026-27/00001"

    monkeypatch.setitem(
        service.CONVERSION_HOOKS,
        ConversionTarget.PURCHASE_REQUISITION,
        ("src.modules.mrp.service", "_fake_conversion_hook"),
    )
    monkeypatch.setattr(service, "_fake_conversion_hook", fake_hook, raising=False)

    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    assert result.handled is True
    assert result.created_document_id == document_id
    assert result.created_document_code == "PR/2026-27/00001"
    assert result.suggestion.converted_to_id == document_id
    assert captured["item_code"] == ctx.screw.code


@pytest.mark.asyncio
async def test_a_suggestion_cannot_be_converted_twice(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None

    await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )
    with pytest.raises(InvalidStateTransitionError):
        await service.convert_suggestion(
            ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
        )


@pytest.mark.asyncio
async def test_a_manufacture_suggestion_is_received_into_the_run_s_warehouse(ctx: Ctx) -> None:
    """The run's warehouse scope survives the conversion boundary.

    A run is executed *for* a set of warehouses (database.md section 6), so the
    order it produces is received where the plan was made - the spares store here,
    not the item master's default store. A planner who scoped the run never has to
    restate the scope on every suggestion they convert.
    """
    await add_sales_order(
        ctx, code="SO/0001", lines=[(ctx.kit, "10")], warehouse=ctx.spare_warehouse
    )
    run = await run_plan(ctx, warehouse_ids=[ctx.spare_warehouse.id])
    suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    assert suggestion is not None
    assert suggestion.suggestion_type is MrpSuggestionType.MANUFACTURE

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    assert result.handled is True
    assert result.created_document_type == "production_order"
    assert result.request.warehouse_ids == [ctx.spare_warehouse.id]

    order = (
        await ctx.session.execute(
            select(ProductionOrder).where(ProductionOrder.id == result.created_document_id)
        )
    ).scalar_one()
    assert order.code == result.created_document_code
    # The item's default store is the main one, so a warehouse that arrived from
    # the run cannot be mistaken for the fallback.
    assert ctx.kit.default_warehouse_id == ctx.main_warehouse.id
    assert order.warehouse_id == ctx.spare_warehouse.id
    assert order.planned_quantity == suggestion.suggested_quantity
    assert order.source_type == ProductionSourceType.MRP_SUGGESTION
    assert order.source_id == suggestion.id


@pytest.mark.asyncio
async def test_a_company_wide_run_receives_into_the_item_s_default_warehouse(ctx: Ctx) -> None:
    """A run scoped to nothing has no warehouse to pass on, so the item master decides."""
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    assert suggestion is not None

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    assert result.request.warehouse_ids == []
    order = (
        await ctx.session.execute(
            select(ProductionOrder).where(ProductionOrder.id == result.created_document_id)
        )
    ).scalar_one()
    assert order.warehouse_id == ctx.main_warehouse.id


@pytest.mark.asyncio
async def test_a_multi_warehouse_run_receives_into_a_warehouse_inside_its_scope(ctx: Ctx) -> None:
    """docs/assumptions.md section 31: the item default wins when it lies inside
    the run's scope, even when listed second; otherwise the first warehouse the
    run named."""
    await add_sales_order(
        ctx, code="SO/0002", lines=[(ctx.kit, "10")], warehouse=ctx.spare_warehouse
    )
    run = await run_plan(ctx, warehouse_ids=[ctx.spare_warehouse.id, ctx.main_warehouse.id])
    suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    assert suggestion is not None

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )

    order = (
        await ctx.session.execute(
            select(ProductionOrder).where(ProductionOrder.id == result.created_document_id)
        )
    ).scalar_one()
    assert ctx.kit.default_warehouse_id == ctx.main_warehouse.id
    assert order.warehouse_id == ctx.main_warehouse.id


@pytest.mark.asyncio
async def test_conversion_is_refused_when_nothing_names_a_receiving_warehouse(ctx: Ctx) -> None:
    """The guard, and what it protects.

    Neither the run nor the item master says where the output goes. Picking a
    warehouse anyway would post a production receipt into a store nobody chose,
    so the conversion is refused whole: the suggestion is still OPEN and there is
    no half-created order for a planner to find.
    """
    ctx.kit.default_warehouse_id = None
    await ctx.session.flush()

    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    assert suggestion is not None

    with pytest.raises(BusinessRuleError):
        await service.convert_suggestion(
            ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
        )

    # Re-read: the rolled-back savepoint expired everything loaded before it.
    refreshed = await suggestion_for(ctx, run.id, ctx.kit)
    assert refreshed is not None
    assert refreshed.status is MrpSuggestionStatus.OPEN
    assert refreshed.converted_to_id is None
    orders = (
        await ctx.session.execute(
            select(func.count(ProductionOrder.id)).where(
                ProductionOrder.company_id == ctx.tenant.company_id
            )
        )
    ).scalar_one()
    assert orders == 0


@pytest.mark.asyncio
async def test_bulk_ignore_dismisses_open_rows_and_reports_the_rest(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    run = await run_plan(ctx)

    kit_suggestion = await suggestion_for(ctx, run.id, ctx.kit)
    screw_suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert kit_suggestion is not None and screw_suggestion is not None

    await service.convert_suggestion(
        ctx.session, ctx.tenant, kit_suggestion.id, SuggestionConvertRequest()
    )
    unknown = uuid.uuid4()

    result = await service.bulk_ignore_suggestions(
        ctx.session,
        ctx.tenant,
        BulkIgnoreRequest(
            suggestion_ids=[screw_suggestion.id, kit_suggestion.id, unknown],
            reason="Covered by an existing blanket order",
        ),
    )

    assert result.ignored_count == 1
    assert result.ignored_ids == [screw_suggestion.id]
    assert set(result.skipped_ids) == {kit_suggestion.id, unknown}

    refreshed = await suggestion_for(ctx, run.id, ctx.screw)
    assert refreshed is not None
    assert refreshed.status is MrpSuggestionStatus.IGNORED
    assert refreshed.ignored_reason == "Covered by an existing blanket order"


@pytest.mark.asyncio
async def test_every_dismissal_is_audited(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None

    await service.bulk_ignore_suggestions(
        ctx.session,
        ctx.tenant,
        BulkIgnoreRequest(suggestion_ids=[suggestion.id], reason="Obsolete design"),
    )

    stmt = select(func.count(AuditLog.id)).where(
        AuditLog.company_id == ctx.tenant.company_id,
        AuditLog.entity_type == "mrp_suggestion",
        AuditLog.entity_id == suggestion.id,
    )
    assert (await ctx.session.execute(stmt)).scalar_one() == 1


@pytest.mark.asyncio
async def test_the_run_detail_reports_its_suggestion_counts_and_caveats(ctx: Ctx) -> None:
    """What the polling screen shows: progress, counts, and which stages ran.

    Both netting hooks are deployed now - ``procurement.incoming_quantities``
    and ``production.in_progress_output`` - so a run that finds nothing on order
    reports that it *netted and found nothing*, which is a different statement
    from "this plan never looked". This test used to assert the second one, from
    the days before the hooks existed; the caveat path it was really about is
    now forced explicitly, by taking the hook away.
    """
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    run = await run_plan(ctx)

    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    await service.bulk_ignore_suggestions(
        ctx.session,
        ctx.tenant,
        BulkIgnoreRequest(suggestion_ids=[suggestion.id], reason="Held in a blanket order"),
    )

    detail = await service.get_run(ctx.session, ctx.tenant, run.id)

    assert detail.suggestion_counts_by_status[str(MrpSuggestionStatus.IGNORED)] == 1
    assert detail.suggestion_counts_by_status[str(MrpSuggestionStatus.OPEN)] == 2
    assert detail.result_summary is not None
    assert detail.result_summary["shortage_item_count"] == 3
    assert detail.result_summary["netted_against_incoming"] is True
    assert detail.result_summary["netted_against_production"] is True
    # Nothing is on order and no job is live, so nothing was netted off - and
    # nothing arrived too late to count either.
    assert detail.result_summary["late_supply_item_count"] == 0
    assert not [w for w in detail.warnings if "did NOT net" in w], detail.warnings
    assert detail.status_url.endswith(str(run.id))


@pytest.mark.asyncio
async def test_a_netting_stage_that_could_not_run_is_still_reported_as_a_caveat(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caveat the test above used to get for free, forced deliberately.

    ``mrp.repository._optional_supply_hook`` resolves each hook with
    ``getattr(module, name, None)``. Removing the attribute puts the seam back
    where it was before either hook existed: the stage cannot run, the plan is
    an upper bound, and it has to say so rather than implying nothing is on
    order. Asserted by forcing the seam to resolve to nothing - never by
    claiming the module itself is absent.
    """
    monkeypatch.delattr(procurement_service, "incoming_quantities")
    monkeypatch.delattr(production_service, "in_progress_output")
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])

    run = await run_plan(ctx)
    detail = await service.get_run(ctx.session, ctx.tenant, run.id)

    assert detail.result_summary is not None
    assert detail.result_summary["netted_against_incoming"] is False
    assert detail.result_summary["netted_against_production"] is False
    assert any("incoming purchase orders" in w for w in detail.warnings)
    assert any("In-progress production output" in w for w in detail.warnings)


@pytest.mark.asyncio
async def test_the_shortage_report_defaults_to_the_latest_completed_run(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    await run_plan(ctx)
    latest = await run_plan(ctx)

    rows, pagination = await service.shortage_report(
        ctx.session,
        ctx.tenant,
        ShortageReportFilters(),
        PageParams(page=1, limit=50),
        SortParams(),
    )

    assert rows
    assert {row.run_id for row in rows} == {latest.id}
    assert all(row.shortage_quantity > 0 for row in rows)
    assert pagination.total == len(rows)


@pytest.mark.asyncio
async def test_the_shortage_report_is_empty_before_any_run(ctx: Ctx) -> None:
    rows, _ = await service.shortage_report(
        ctx.session,
        ctx.tenant,
        ShortageReportFilters(),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert rows == []


@pytest.mark.asyncio
async def test_suggestions_can_be_filtered_by_type_and_status(ctx: Ctx) -> None:
    await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "10")])
    run = await run_plan(ctx)

    manufacture, _ = await service.list_suggestions(
        ctx.session,
        ctx.tenant,
        SuggestionFilters(run_id=run.id, suggestion_type=MrpSuggestionType.MANUFACTURE),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert [row.item_code for row in manufacture] == [ctx.kit.code]

    open_purchases, _ = await service.list_suggestions(
        ctx.session,
        ctx.tenant,
        SuggestionFilters(
            run_id=run.id,
            suggestion_type=MrpSuggestionType.PURCHASE,
            status=MrpSuggestionStatus.OPEN,
        ),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert sorted(row.item_code for row in open_purchases) == sorted(
        [ctx.handle.code, ctx.screw.code]
    )


@pytest.mark.asyncio
async def test_the_demand_snapshot_survives_the_order_it_came_from(ctx: Ctx) -> None:
    """A plan must keep saying what it was based on after the orders move on."""
    order = await add_sales_order(ctx, code="SO/0001", lines=[(ctx.kit, "100")])
    run = await run_plan(ctx)

    order.status = SalesOrderStatus.CANCELLED
    order.cancellation_reason = "Customer withdrew"
    await ctx.session.flush()

    rows, _ = await service.list_run_demand(
        ctx.session, ctx.tenant, run.id, PageParams(page=1, limit=50)
    )
    assert len(rows) == 1
    assert rows[0].source_code == "SO/0001"
    assert rows[0].quantity == D("100.0000")


# -----------------------------------------------------------------------------
# Cost of a plan
# -----------------------------------------------------------------------------


@contextmanager
def _captured_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Every ORM statement the session executes inside the block.

    ``do_orm_execute`` sees the repository's explicit ``session.execute`` calls -
    the planning feeds and the set-based stamps - but not the flush's inserts,
    which is the split this test is about: reads and bulk writes must be batched
    by id set, while ordinary inserts are the work itself.
    """
    statements: list[str] = []

    def record(state: Any) -> None:
        statements.append(str(state.statement))

    event.listen(session.sync_session, "do_orm_execute", record)
    try:
        yield statements
    finally:
        event.remove(session.sync_session, "do_orm_execute", record)


async def add_bom(
    ctx: Ctx, *, code: str, parent: Item, components: list[tuple[Item, Decimal]]
) -> None:
    """One approved single-level structure under ``parent``."""
    session = ctx.session
    bom = Bom(
        company_id=ctx.tenant.company_id,
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
                company_id=ctx.tenant.company_id,
                bom_id=bom.id,
                parent_item_id=parent.id,
                line_number=line_number,
                component_item_id=component.id,
                quantity_per=quantity_per,
                uom_id=ctx.uom.id,
            )
            for line_number, (component, quantity_per) in enumerate(components, start=1)
        ]
    )
    await session.flush()


async def add_structure(ctx: Ctx, *, prefix: str, breadth: int) -> Item:
    """A three-level MAKE structure, ``breadth`` sub-assemblies wide.

    Width-parameterisable so the query-count test can hold the depth and the
    shape constant and move only the number of nodes.
    """
    session = ctx.session
    kit = _item(
        ctx,
        f"{prefix}-KIT",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
        can_sell=True,
        lead_time_days=10,
    )
    subs = [
        _item(
            ctx,
            f"{prefix}-SUB-{index:02d}",
            production_type=ProductionType.MAKE,
            can_manufacture=True,
            can_purchase=False,
            lead_time_days=5,
        )
        for index in range(breadth)
    ]
    raws = [_item(ctx, f"{prefix}-RAW-{index:02d}", lead_time_days=3) for index in range(breadth)]
    session.add_all([kit, *subs, *raws])
    await session.flush()
    await add_bom(
        ctx,
        code=f"BOM-{prefix}-KIT",
        parent=kit,
        components=[(sub, D("1")) for sub in subs],
    )
    for index, (sub, raw) in enumerate(zip(subs, raws, strict=True)):
        await add_bom(
            ctx,
            code=f"BOM-{prefix}-SUB-{index:02d}",
            parent=sub,
            components=[(raw, D("2"))],
        )
    return kit


@pytest.mark.asyncio
async def test_a_fifty_part_plan_costs_no_more_queries_than_a_three_part_one(ctx: Ctx) -> None:
    """Reads batched by id set per level, writes one statement per table (section 32).

    Two runs over the *same* three-level shape, one three sub-assemblies wide and
    one fifty: every difference between them must be in rows, never in round
    trips. A per-node query anywhere in discovery, netting or the suggestion
    writes would make the wide plan cost roughly fifteen times as much, which is
    the failure this pins. The small order is cancelled first so the second run
    plans one structure, exactly as the first did.
    """
    small_kit = await add_structure(ctx, prefix="SMALL", breadth=3)
    small_order = await add_sales_order(ctx, code="SO/0001", lines=[(small_kit, "1")])

    with _captured_sql(ctx.session) as small_statements:
        await run_plan(ctx)

    small_order.status = SalesOrderStatus.CANCELLED
    small_order.cancellation_reason = "Replaced by the wide scenario"
    await ctx.session.flush()

    big_kit = await add_structure(ctx, prefix="BIG", breadth=50)
    await add_sales_order(ctx, code="SO/0002", lines=[(big_kit, "1")])

    with _captured_sql(ctx.session) as big_statements:
        await run_plan(ctx)

    # Guard against a vacuous pass: the recorder must have seen the planning
    # feeds at all. A run reads demand, item facts, stock, supply, BOMs and
    # vendor terms at the very least, so a handful of statements is the floor,
    # not a ceiling.
    assert len(small_statements) >= 8, small_statements
    assert len(big_statements) == len(small_statements), (
        f"the wide plan executed {len(big_statements)} statements against "
        f"{len(small_statements)} for the narrow one - a query is scaling with "
        f"the number of nodes"
    )


@pytest.mark.asyncio
async def test_bom_roots_survive_purchase_conversion_and_shared_fulfilment(ctx: Ctx) -> None:
    from src.modules.procurement.models import PurchaseRequisitionLine
    from src.modules.sales.fulfilment import _mrp_documents

    first = await add_sales_order(ctx, code="SO/ROOT-A", lines=[(ctx.kit, "10")])
    second = await add_sales_order(ctx, code="SO/ROOT-B", lines=[(ctx.kit, "5")])
    expected = {
        (str(line.sales_order_id), str(line.id))
        for line in (
            await ctx.session.scalars(
                select(SalesOrderLine).where(
                    SalesOrderLine.sales_order_id.in_([first.id, second.id])
                )
            )
        ).all()
    }
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    assert suggestion.source_demands[0]["source"] == "BOM"
    assert suggestion.source_demands[0]["root_demand_refs_complete"] is True

    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )
    line = (
        await ctx.session.scalars(
            select(PurchaseRequisitionLine).where(
                PurchaseRequisitionLine.requisition_id == result.created_document_id
            )
        )
    ).one()
    assert {
        (ref["demand_id"], ref["demand_line_id"]) for ref in line.source_demand_refs
    } == expected
    assert all(ref["demand_type"] == "SALES_ORDER" for ref in line.source_demand_refs)
    assert line.quantity == suggestion.suggested_quantity

    trail = await _mrp_documents(ctx.session, ctx.tenant, first.id)
    component = next(document for document in trail if document.id == suggestion.id)
    assert component.line_id is None, "Shared component supply is not a per-sales-line allocation."
    assert "aggregate" in component.detail
    assert await _mrp_documents(ctx.session, ctx.other, first.id) == []


@pytest.mark.asyncio
async def test_manual_snapshot_root_is_retained_on_the_purchase_requisition(ctx: Ctx) -> None:
    from src.modules.mrp.models import MrpDemand, MrpDemandSource
    from src.modules.procurement.models import PurchaseRequisitionLine

    run = await run_plan(
        ctx,
        manual_demand=[
            ManualDemandIn(
                item_id=ctx.screw.id, quantity=D("25"), required_date=today() + timedelta(days=7)
            )
        ],
    )
    demand = (
        await ctx.session.scalars(
            select(MrpDemand).where(
                MrpDemand.run_id == run.id, MrpDemand.demand_source == MrpDemandSource.MANUAL
            )
        )
    ).one()
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    result = await service.convert_suggestion(
        ctx.session, ctx.tenant, suggestion.id, SuggestionConvertRequest()
    )
    line = (
        await ctx.session.scalars(
            select(PurchaseRequisitionLine).where(
                PurchaseRequisitionLine.requisition_id == result.created_document_id
            )
        )
    ).one()
    assert line.source_demand_refs == [
        {"demand_type": "MRP_DEMAND", "demand_id": str(demand.id), "demand_line_id": None}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["legacy-bom", "incomplete", "empty-roots"])
async def test_unprovable_mrp_conversion_leaves_no_requisition_or_audit(
    ctx: Ctx, state: str
) -> None:
    await add_sales_order(ctx, code="SO/ROOT-GUARD", lines=[(ctx.screw, "100")])
    run = await run_plan(ctx)
    suggestion = await suggestion_for(ctx, run.id, ctx.screw)
    assert suggestion is not None
    suggestion_id = suggestion.id
    if state == "legacy-bom":
        suggestion.source_demands = [
            {"source": "BOM", "via_item_id": str(ctx.kit.id), "quantity": "100"}
        ]
    elif state == "incomplete":
        sources = [dict(fact) for fact in suggestion.source_demands]
        sources[0]["root_demand_refs_complete"] = False
        suggestion.source_demands = sources
    else:
        suggestion.source_demands = [
            {"source": "BOM", "root_demand_refs": [], "root_demand_refs_complete": True}
        ]
    await ctx.session.flush()
    before_pr = await ctx.session.scalar(select(func.count()).select_from(PurchaseRequisition))
    before_audit = await ctx.session.scalar(select(func.count()).select_from(AuditLog))

    with pytest.raises(BusinessRuleError, match=r"(?i)(demand|provenance)"):
        await service.convert_suggestion(
            ctx.session, ctx.tenant, suggestion_id, SuggestionConvertRequest()
        )

    current = (
        await ctx.session.scalars(
            select(MrpSuggestion)
            .where(MrpSuggestion.id == suggestion_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    assert current.status == MrpSuggestionStatus.OPEN
    assert current.converted_to_id is None
    assert (
        await ctx.session.scalar(select(func.count()).select_from(PurchaseRequisition)) == before_pr
    )
    assert await ctx.session.scalar(select(func.count()).select_from(AuditLog)) == before_audit
