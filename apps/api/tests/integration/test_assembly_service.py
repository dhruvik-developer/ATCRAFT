"""Service-level tests for assembly orders against a real PostgreSQL.

What is under test is the sentence master rules section 19 opens with: a Lock Set
consumes a Handle, a Cylinder, a Lock body, a set of Keys and a Screw kit, and
the system consumes the components and creates finished assembly stock. Every
assertion here is about the balances and the ledger afterwards.

These need PostgreSQL because none of it is expressible without it: the generated
``available_quantity`` column, the check constraints that make an over-consuming
completion impossible, ``SELECT ... FOR UPDATE`` on the header and the balances,
the ``uuid[]`` traceability array with its cardinality constraint, and the audit
rows written inside the same transaction as the movements they describe.

Isolation follows ``test_allocation_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    InsufficientStockError,
    InvalidStateTransitionError,
)
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.allocation import service as allocation
from src.modules.allocation.models import DemandType, ReservationStatus, StockReservation
from src.modules.allocation.schemas import ReallocationRequestCreate
from src.modules.assembly import service
from src.modules.assembly.models import AssemblyOrder, AssemblyOrderComponent, AssemblyOrderStatus
from src.modules.assembly.schemas import (
    AssemblyOrderCreate,
    AssemblyOrderUpdate,
    CompleteRequest,
    ComponentConsumptionIn,
    ComponentIssueIn,
    IssueComponentsRequest,
    QcResultRequest,
)
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    InventoryTransaction,
    SerialStatus,
    StockSerial,
    StockState,
    TransactionType,
)
from src.modules.inventory.schemas import StockBatchCreate
from src.modules.inventory.service import TraceabilityService
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.quality import service as quality_service
from src.modules.quality.schemas import DecideRequest
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """Private schema with one company and one operator."""
    schema = f"it_assembly_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                await seed_support.seed_company(session, company_id=company_id, prefix="ASM")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email="operator@atcraft.test",
                    full_name="Operator",
                )
        finally:
            await engine.dispose()

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

    yield schema, company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext]]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, user_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield session, tenant
            await outer.rollback()
    finally:
        await engine.dispose()


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# The seeds below return ids because that is all an assembly needs; the rows
# themselves are built by the owning module's model, so the Python-side defaults
# on the item master and the warehouse apply.


async def seed_uom(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    return (await seed_support.seed_uom(session, company_id=company_id)).id


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    uom_id: uuid.UUID,
    *,
    code: str,
    serial_tracked: bool = False,
    batch_tracked: bool = False,
    assembly_component: bool = True,
) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=code,
        base_uom_id=uom_id,
        item_group=ItemGroup.COMPONENT,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("10"),
        production_type=ProductionType.MAKE,
        is_batch_tracked=batch_tracked,
        is_serial_tracked=serial_tracked,
        can_be_assembly_component=assembly_component,
        can_manufacture=True,
    )
    return item.id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=_code("WH"))
    return warehouse.id


async def seed_assembly_bom(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    parent_item_id: uuid.UUID,
    uom_id: uuid.UUID,
    components: dict[uuid.UUID, Decimal],
    output_quantity: Decimal = Decimal("1"),
    bom_type: BomType = BomType.ASSEMBLY,
    scrap: Decimal = Decimal("0"),
) -> Bom:
    bom = Bom(
        company_id=tenant.company_id,
        code=_code("BOM"),
        item_id=parent_item_id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=bom_type,
        is_default=True,
        output_quantity=output_quantity,
        base_uom_id=uom_id,
        effective_from=today(),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    session.add(bom)
    await session.flush()
    for number, (component_item_id, quantity_per) in enumerate(components.items(), start=1):
        session.add(
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=parent_item_id,
                line_number=number,
                component_item_id=component_item_id,
                quantity_per=quantity_per,
                uom_id=uom_id,
                scrap_percentage=scrap,
            )
        )
    await session.flush()
    return bom


async def stock_in(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
) -> None:
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="assembly fixture opening stock",
            unit_cost=Decimal("10"),
        )


async def available(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Decimal:
    return await inv.get_available(session, tenant, item_id, warehouse_id)


class LockSet:
    """The worked example: a Lock Set and its five component parts, all in stock."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        output_item_id: uuid.UUID,
        components: dict[uuid.UUID, Decimal],
        warehouse_id: uuid.UUID,
        output_warehouse_id: uuid.UUID,
        bom: Bom,
    ) -> None:
        self.uom_id = uom_id
        self.output_item_id = output_item_id
        self.components = components
        self.warehouse_id = warehouse_id
        self.output_warehouse_id = output_warehouse_id
        self.bom = bom


async def lock_set_setup(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    stock_per_component: str = "1000",
    scrap: Decimal = Decimal("0"),
    separate_output_warehouse: bool = False,
) -> LockSet:
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    output_warehouse_id = (
        await seed_warehouse(session, tenant.company_id)
        if separate_output_warehouse
        else warehouse_id
    )

    output_item_id = await seed_item(
        session, tenant.company_id, uom_id, code=_code("LOCKSET"), assembly_component=False
    )
    quantities = {
        "HANDLE": Decimal("1"),
        "CYLINDER": Decimal("1"),
        "LOCKBODY": Decimal("1"),
        "KEYS": Decimal("2"),
        "SCREWKIT": Decimal("4"),
    }
    components: dict[uuid.UUID, Decimal] = {}
    for name, quantity_per in quantities.items():
        item_id = await seed_item(session, tenant.company_id, uom_id, code=_code(name))
        components[item_id] = quantity_per
        await stock_in(session, tenant, item_id, warehouse_id, stock_per_component)

    bom = await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=output_item_id,
        uom_id=uom_id,
        components=components,
        scrap=scrap,
    )
    return LockSet(
        uom_id=uom_id,
        output_item_id=output_item_id,
        components=components,
        warehouse_id=warehouse_id,
        output_warehouse_id=output_warehouse_id,
        bom=bom,
    )


async def raise_order(
    session: AsyncSession,
    tenant: TenantContext,
    setup: LockSet,
    *,
    planned: str = "100",
    requires_qc: bool = False,
) -> AssemblyOrder:
    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=setup.output_item_id,
            planned_quantity=Decimal(planned),
            warehouse_id=setup.warehouse_id,
            output_warehouse_id=setup.output_warehouse_id,
            requires_qc=requires_qc,
        ),
    )
    return order


async def take_to_in_progress(
    session: AsyncSession, tenant: TenantContext, order: AssemblyOrder
) -> AssemblyOrder:
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")
    await service.issue_components(
        session, tenant, order.id, IssueComponentsRequest(reason="Kitted")
    )
    return await service.start_assembly(session, tenant, order.id, reason="Shift started")


# -----------------------------------------------------------------------------
# Creation explodes the structure
# -----------------------------------------------------------------------------


async def test_creating_an_order_explodes_the_assembly_bom_onto_it(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)

    order, components = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=setup.output_item_id,
            planned_quantity=Decimal("100"),
            warehouse_id=setup.warehouse_id,
        ),
    )

    assert order.status == AssemblyOrderStatus.DRAFT
    assert order.code.startswith("AO/")
    # The output warehouse defaults to the component source when not named.
    assert order.output_warehouse_id == setup.warehouse_id
    assert len(components) == 5
    by_item = {row.item_id: Decimal(row.required_quantity) for row in components}
    assert by_item == {
        item_id: quantity_per * 100 for item_id, quantity_per in setup.components.items()
    }


async def test_scrap_on_a_bom_line_inflates_the_kitting_requirement(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, scrap=Decimal("5"))

    _, components = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=setup.output_item_id,
            planned_quantity=Decimal("100"),
            warehouse_id=setup.warehouse_id,
        ),
    )

    screw_kit = next(
        row
        for row in components
        if setup.components[row.item_id] == Decimal("4")  # four screws per set
    )
    assert Decimal(screw_kit.required_quantity) == Decimal("420.0000")


async def test_a_phantom_bom_cannot_be_used_for_an_assembly_order(ctx) -> None:
    """A phantom is never built, so no bench can assemble from one."""
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    parent = await seed_item(session, tenant.company_id, uom_id, code=_code("PART"))
    child = await seed_item(session, tenant.company_id, uom_id, code=_code("CHILD"))
    await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=parent,
        uom_id=uom_id,
        components={child: Decimal("1")},
        bom_type=BomType.PHANTOM,
    )

    with pytest.raises(BusinessRuleError, match="ASSEMBLY or PRODUCTION"):
        await service.create_order(
            session,
            tenant,
            AssemblyOrderCreate(
                item_id=parent, planned_quantity=Decimal("1"), warehouse_id=warehouse_id
            ),
        )


async def test_a_production_bom_can_be_assembled_on_the_bench(ctx) -> None:
    """A lock set filed as a production structure is still bench work
    (docs/assumptions.md §E4-4)."""
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    parent = await seed_item(session, tenant.company_id, uom_id, code=_code("PART"))
    child = await seed_item(session, tenant.company_id, uom_id, code=_code("CHILD"))
    bom = await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=parent,
        uom_id=uom_id,
        components={child: Decimal("2")},
        bom_type=BomType.PRODUCTION,
    )

    order, components = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=parent, planned_quantity=Decimal("3"), warehouse_id=warehouse_id
        ),
    )

    assert order.bom_id == bom.id
    assert [Decimal(row.required_quantity) for row in components] == [Decimal("6.0000")]


async def test_changing_the_planned_quantity_re_explodes_the_component_set(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="100")

    _, components = await service.update_order(
        session, tenant, order.id, AssemblyOrderUpdate(planned_quantity=Decimal("50"))
    )

    by_item = {row.item_id: Decimal(row.required_quantity) for row in components}
    assert by_item == {
        item_id: quantity_per * 50 for item_id, quantity_per in setup.components.items()
    }


# -----------------------------------------------------------------------------
# Release reserves, cancel gives back
# -----------------------------------------------------------------------------


async def test_releasing_reserves_every_component_without_moving_stock(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    await service.approve_order(session, tenant, order.id, reason="Scheduled")

    handle_id = next(iter(setup.components))
    on_hand_before = await available(session, tenant, handle_id, setup.warehouse_id)

    _, components = await service.release_order(session, tenant, order.id, reason="To the line")

    assert all(row.reservation_id is not None for row in components)
    # A reservation promises stock; it does not move it. Available drops by the
    # reserved amount while on-hand is untouched.
    balance = await inv.get_balance(session, tenant, handle_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    assert Decimal(balance.on_hand_quantity) == Decimal("1000.0000")
    assert Decimal(balance.reserved_quantity) == Decimal("100.0000")
    assert Decimal(balance.available_quantity) == on_hand_before - Decimal("100")

    reservations = (
        (
            await session.execute(
                select(StockReservation).where(
                    StockReservation.demand_type == DemandType.ASSEMBLY_ORDER,
                    StockReservation.demand_id == order.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(reservations) == 5


async def test_a_component_short_of_stock_refuses_the_whole_release(ctx) -> None:
    session, tenant = ctx
    # Two keys per set and only 50 in stock: 100 sets need 200.
    setup = await lock_set_setup(session, tenant, stock_per_component="50")
    order = await raise_order(session, tenant, setup, planned="100")
    await service.approve_order(session, tenant, order.id, reason="Scheduled")

    with pytest.raises(InsufficientStockError):
        await service.release_order(session, tenant, order.id, reason="To the line")


async def test_cancelling_a_released_order_gives_every_reservation_back(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")

    cancelled = await service.cancel_order(
        session, tenant, order.id, reason="Customer pulled the order"
    )

    assert cancelled.status == AssemblyOrderStatus.CANCELLED
    handle_id = next(iter(setup.components))
    balance = await inv.get_balance(session, tenant, handle_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    # A cancelled order holding reservations would make stock invisible to every
    # other document while belonging to nobody.
    assert Decimal(balance.reserved_quantity) == Decimal("0.0000")

    components = await service.list_components(session, tenant, order.id)
    assert all(row.reservation_id is None for row in components)


async def test_an_order_whose_components_are_issued_can_no_longer_be_cancelled(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")
    await service.issue_components(
        session, tenant, order.id, IssueComponentsRequest(reason="Kitted")
    )

    with pytest.raises(InvalidStateTransitionError):
        await service.cancel_order(session, tenant, order.id, reason="Too late")


async def test_issuing_records_the_handover_without_touching_the_ledger(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")

    ledger_before = (
        await session.execute(
            select(func.count())
            .select_from(InventoryTransaction)
            .where(InventoryTransaction.company_id == tenant.company_id)
        )
    ).scalar_one()

    _, components = await service.issue_components(
        session, tenant, order.id, IssueComponentsRequest(reason="Kitted")
    )

    ledger_after = (
        await session.execute(
            select(func.count())
            .select_from(InventoryTransaction)
            .where(InventoryTransaction.company_id == tenant.company_id)
        )
    ).scalar_one()

    assert ledger_after == ledger_before
    assert all(Decimal(row.issued_quantity) == Decimal(row.required_quantity) for row in components)


async def test_issuing_more_than_was_reserved_is_refused(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")

    with pytest.raises(BusinessRuleError, match="at most"):
        await service.issue_components(
            session,
            tenant,
            order.id,
            IssueComponentsRequest(
                reason="Over-kitted",
                lines=[ComponentIssueIn(line_number=1, issued_quantity=Decimal("500"))],
            ),
        )


# -----------------------------------------------------------------------------
# Completion: the operation the module exists for
# -----------------------------------------------------------------------------


async def test_completion_consumes_the_components_and_creates_the_assembly(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, separate_output_warehouse=True)
    order = await raise_order(session, tenant, setup, planned="100")
    await take_to_in_progress(session, tenant, order)

    result = await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("100"), reason="Night shift"),
    )

    assert result.order.status == AssemblyOrderStatus.COMPLETED
    assert result.output_quantity == Decimal("100.0000")
    assert result.output_held_for_qc is False
    assert result.consume_movement_count == 5

    # Every component is down by exactly its requirement, and nothing is left
    # reserved: the reservation became a consumption, not an extra hold.
    for item_id, quantity_per in setup.components.items():
        balance = await inv.get_balance(session, tenant, item_id, setup.warehouse_id)
        assert balance is not None
        await session.refresh(balance)
        expected = Decimal("1000") - quantity_per * 100
        assert Decimal(balance.on_hand_quantity) == expected, item_id
        assert Decimal(balance.reserved_quantity) == Decimal("0.0000")
        assert Decimal(balance.available_quantity) == expected

    # And the finished assembly exists in the output warehouse.
    output = await inv.get_balance(session, tenant, setup.output_item_id, setup.output_warehouse_id)
    assert output is not None
    await session.refresh(output)
    assert Decimal(output.on_hand_quantity) == Decimal("100.0000")
    assert Decimal(output.available_quantity) == Decimal("100.0000")


async def test_completion_writes_one_consume_row_per_component_and_one_receipt(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)

    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Day shift"),
    )

    rows = (
        (
            await session.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.company_id == tenant.company_id,
                    InventoryTransaction.reference_id == order.id,
                )
            )
        )
        .scalars()
        .all()
    )

    consumes = [r for r in rows if r.transaction_type == TransactionType.ASSEMBLY_CONSUME]
    receipts = [r for r in rows if r.transaction_type == TransactionType.ASSEMBLY_RECEIPT]
    assert len(consumes) == 5
    assert len(receipts) == 1
    # Signs are a property of the movement type, never of the caller's arithmetic.
    assert all(Decimal(r.quantity) < 0 for r in consumes)
    assert Decimal(receipts[0].quantity) == Decimal("10.0000")
    assert all(r.reference_code == order.code for r in rows)


async def test_a_partial_run_consumes_proportionally_and_leaves_the_rest_available(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="100")
    await take_to_in_progress(session, tenant, order)

    # 60 good, 10 rejected: the rejected sets ate their components too.
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(
            assembled_quantity=Decimal("60"),
            rejected_quantity=Decimal("10"),
            reason="Short run",
        ),
    )

    handle_id = next(item_id for item_id, per in setup.components.items() if per == Decimal("1"))
    balance = await inv.get_balance(session, tenant, handle_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    assert Decimal(balance.on_hand_quantity) == Decimal("930.0000")
    # The unused 30 handles' reservation was released, not left dangling.
    assert Decimal(balance.reserved_quantity) == Decimal("0.0000")

    # Only the good output became stock.
    output = await inv.get_available(session, tenant, setup.output_item_id, setup.warehouse_id)
    assert output == Decimal("60.0000")


async def test_reporting_more_output_than_was_planned_is_refused(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)

    with pytest.raises(BusinessRuleError, match="planned"):
        await service.complete_assembly(
            session,
            tenant,
            order.id,
            CompleteRequest(assembled_quantity=Decimal("11"), reason="Over-run"),
        )


async def test_consuming_more_than_was_issued_is_refused_and_posts_nothing(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="100")
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")
    # Line 1 is short-kitted: 40 handles issued against a requirement of 100.
    await service.issue_components(
        session,
        tenant,
        order.id,
        IssueComponentsRequest(
            reason="Partial kit",
            lines=[ComponentIssueIn(line_number=1, issued_quantity=Decimal("40"))],
        ),
    )
    await service.start_assembly(session, tenant, order.id, reason="Started")
    order_id = order.id

    with pytest.raises(BusinessRuleError, match="issued"):
        await service.complete_assembly(
            session,
            tenant,
            order_id,
            CompleteRequest(assembled_quantity=Decimal("100"), reason="Full run"),
        )

    # Nothing was posted: the whole completion is one transaction. Capture the
    # id first: a savepoint rollback expires the ORM instance, and reading
    # `order.id` here would need IO from a non-awaiting expression.
    posted = (
        await session.execute(
            select(func.count())
            .select_from(InventoryTransaction)
            .where(InventoryTransaction.reference_id == order_id)
        )
    ).scalar_one()
    assert posted == 0


async def test_completion_writes_its_audit_trail_in_the_same_transaction(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)

    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Audited run"),
    )

    entries = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.company_id == tenant.company_id,
                    AuditLog.entity_type == service.AUDIT_ASSEMBLY_ORDER,
                    AuditLog.entity_id == order.id,
                )
            )
        )
        .scalars()
        .all()
    )

    actions = {entry.action for entry in entries}
    assert AuditAction.INVENTORY_MOVEMENT in actions
    assert AuditAction.STATUS_CHANGE in actions
    movement_entry = next(
        entry for entry in entries if entry.action == AuditAction.INVENTORY_MOVEMENT
    )
    assert movement_entry.context["movement_count"] == 6
    assert movement_entry.context["reservations_released"] == 5


# -----------------------------------------------------------------------------
# Serial traceability
# -----------------------------------------------------------------------------


async def seed_serials(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    count: int,
) -> list[uuid.UUID]:
    serials = [
        StockSerial(
            company_id=tenant.company_id,
            item_id=item_id,
            serial_number=_code("SN"),
            warehouse_id=warehouse_id,
            status=SerialStatus.IN_STOCK,
            unit_cost=Decimal("10"),
        )
        for _ in range(count)
    ]
    session.add_all(serials)
    await session.flush()
    return [serial.id for serial in serials]


async def test_a_serialised_component_stays_traceable_to_the_units_consumed(ctx) -> None:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    lock_set = await seed_item(
        session, tenant.company_id, uom_id, code=_code("LOCKSET"), assembly_component=False
    )
    cylinder = await seed_item(
        session, tenant.company_id, uom_id, code=_code("CYLINDER"), serial_tracked=True
    )
    serials = await seed_serials(session, tenant, cylinder, warehouse_id, 3)
    # Serialised stock is posted one unit at a time, one serial per movement.
    for serial_id in serials:
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=cylinder,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="serialised opening stock",
                serial_id=serial_id,
                unit_cost=Decimal("10"),
            )
    await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=lock_set,
        uom_id=uom_id,
        components={cylinder: Decimal("1")},
    )

    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=lock_set, planned_quantity=Decimal("3"), warehouse_id=warehouse_id
        ),
    )
    await take_to_in_progress(session, tenant, order)

    result = await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(
            assembled_quantity=Decimal("3"),
            reason="Traced run",
            components=[ComponentConsumptionIn(line_number=1, serial_ids=serials)],
        ),
    )

    # One movement per serial: a serial identifies exactly one physical unit.
    assert result.consume_movement_count == 3
    stored = (
        await session.execute(
            select(AssemblyOrderComponent.consumed_serial_ids).where(
                AssemblyOrderComponent.assembly_order_id == order.id
            )
        )
    ).scalar_one()
    assert list(stored) == serials

    posted_serials = (
        (
            await session.execute(
                select(InventoryTransaction.serial_id).where(
                    InventoryTransaction.reference_id == order.id,
                    InventoryTransaction.transaction_type == TransactionType.ASSEMBLY_CONSUME,
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(str(s) for s in posted_serials) == sorted(str(s) for s in serials)


async def test_a_serial_another_assembly_already_ate_cannot_be_consumed_twice(ctx) -> None:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    lock_set = await seed_item(
        session, tenant.company_id, uom_id, code=_code("LOCKSET"), assembly_component=False
    )
    cylinder = await seed_item(
        session, tenant.company_id, uom_id, code=_code("CYLINDER"), serial_tracked=True
    )
    serials = await seed_serials(session, tenant, cylinder, warehouse_id, 2)
    for serial_id in serials:
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=cylinder,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="serialised opening stock",
                serial_id=serial_id,
                unit_cost=Decimal("10"),
            )
    await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=lock_set,
        uom_id=uom_id,
        components={cylinder: Decimal("1")},
    )

    async def build_one(serial_id: uuid.UUID) -> None:
        order, _ = await service.create_order(
            session,
            tenant,
            AssemblyOrderCreate(
                item_id=lock_set, planned_quantity=Decimal("1"), warehouse_id=warehouse_id
            ),
        )
        await take_to_in_progress(session, tenant, order)
        await service.complete_assembly(
            session,
            tenant,
            order.id,
            CompleteRequest(
                assembled_quantity=Decimal("1"),
                reason="Traced run",
                components=[ComponentConsumptionIn(line_number=1, serial_ids=[serial_id])],
            ),
        )

    await build_one(serials[0])

    with pytest.raises(BusinessRuleError, match="already consumed"):
        await build_one(serials[0])


# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------


async def test_an_order_requiring_qc_receives_its_output_onto_hold(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10", requires_qc=True)
    await take_to_in_progress(session, tenant, order)

    result = await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Needs inspection"),
    )

    assert result.order.status == AssemblyOrderStatus.QC_PENDING
    assert result.output_held_for_qc is True

    balance = await inv.get_balance(session, tenant, setup.output_item_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    # The stock exists and is countable, but nothing can be promised from it.
    assert Decimal(balance.on_hand_quantity) == Decimal("10.0000")
    assert Decimal(balance.qc_hold_quantity) == Decimal("10.0000")
    assert Decimal(balance.available_quantity) == Decimal("0.0000")

    # With no quality module deployed the requirement is still recorded, which
    # is what makes the un-serviced hold findable.
    reloaded = await session.get(AssemblyOrder, order.id)
    assert reloaded is not None
    assert reloaded.qc_held_at is not None


async def test_an_inspection_verdict_releases_the_good_and_damages_the_rest(
    ctx, monkeypatch
) -> None:
    """The owner-side verdict, for a hold no inspection covers.

    Quality's absence is *simulated* by forcing the port to resolve to nothing
    (CLAUDE.md): with an inspection raised, this path defers to it instead.
    """
    session, tenant = ctx
    monkeypatch.setattr(service, "resolve_quality_port", lambda: None)
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10", requires_qc=True)
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Needs inspection"),
    )

    decided = await service.record_qc_result(
        session,
        tenant,
        order.id,
        QcResultRequest(
            accepted_quantity=Decimal("8"),
            rejected_quantity=Decimal("2"),
            reason="Two failed the key test",
        ),
    )

    assert decided.status == AssemblyOrderStatus.COMPLETED
    assert Decimal(decided.assembled_quantity) == Decimal("8.0000")
    assert Decimal(decided.rejected_quantity) == Decimal("2.0000")

    balance = await inv.get_balance(session, tenant, setup.output_item_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    assert Decimal(balance.qc_hold_quantity) == Decimal("0.0000")
    assert Decimal(balance.damaged_quantity) == Decimal("2.0000")
    assert Decimal(balance.available_quantity) == Decimal("8.0000")


async def test_a_verdict_that_does_not_account_for_the_whole_hold_is_refused(
    ctx, monkeypatch
) -> None:
    session, tenant = ctx
    monkeypatch.setattr(service, "resolve_quality_port", lambda: None)
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10", requires_qc=True)
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Needs inspection"),
    )

    with pytest.raises(BusinessRuleError, match="holding"):
        await service.record_qc_result(
            session,
            tenant,
            order.id,
            QcResultRequest(accepted_quantity=Decimal("6"), reason="Only looked at six"),
        )


# -----------------------------------------------------------------------------
# Packing handover
# -----------------------------------------------------------------------------


async def test_a_completed_assembly_with_free_output_is_offered_to_packing(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, separate_output_warehouse=True)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Ready to pack"),
    )

    rows = await service.ready_for_packing(session, tenant, warehouse_id=setup.output_warehouse_id)

    assert [row.assembly_order_id for row in rows] == [order.id]
    assert rows[0].available_quantity == Decimal("10.0000")


async def test_output_still_held_for_inspection_is_not_offered_to_packing(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, separate_output_warehouse=True)
    order = await raise_order(session, tenant, setup, planned="10", requires_qc=True)
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Needs inspection"),
    )

    rows = await service.ready_for_packing(session, tenant, warehouse_id=setup.output_warehouse_id)
    assert rows == []


async def test_an_assembly_a_packing_order_has_claimed_stops_being_offered(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, separate_output_warehouse=True)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Ready to pack"),
    )
    packing_order_id = uuid.uuid4()

    await service.mark_packed(
        session, tenant, order.id, packing_order_id=packing_order_id, reason="Packed"
    )

    rows = await service.ready_for_packing(session, tenant, warehouse_id=setup.output_warehouse_id)
    assert rows == []

    # A different packing order may not steal an assembly already claimed.
    with pytest.raises(BusinessRuleError, match="another packing order"):
        await service.mark_packed(
            session, tenant, order.id, packing_order_id=uuid.uuid4(), reason="Second claim"
        )


async def test_the_packing_feed_is_paginated_and_scoped_to_the_tenant(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant, separate_output_warehouse=True)
    for _ in range(3):
        order = await raise_order(session, tenant, setup, planned="5")
        await take_to_in_progress(session, tenant, order)
        await service.complete_assembly(
            session,
            tenant,
            order.id,
            CompleteRequest(assembled_quantity=Decimal("5"), reason="Ready to pack"),
        )

    rows, pagination = await service.list_ready_for_packing(
        session,
        tenant,
        warehouse_id=setup.output_warehouse_id,
        params=PageParams(page=1, limit=2),
    )
    assert len(rows) == 2
    assert pagination.has_next is True
    assert pagination.total == 3

    stranger = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    assert await service.ready_for_packing(session, stranger) == []


# -----------------------------------------------------------------------------
# Reservation bookkeeping
# -----------------------------------------------------------------------------


async def test_completion_leaves_no_reservation_behind(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="10")
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("10"), reason="Clean finish"),
    )

    live = (
        await session.execute(
            select(func.count())
            .select_from(StockReservation)
            .where(
                StockReservation.demand_id == order.id,
                StockReservation.status.in_(
                    [ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CONSUMED]
                ),
            )
        )
    ).scalar_one()
    assert live == 0

    components = await service.list_components(session, tenant, order.id)
    assert all(row.reservation_id is None for row in components)
    assert all(
        Decimal(row.consumed_quantity) == Decimal(row.required_quantity) for row in components
    )


# A component reservation is a promise allocation may close on its own -
# released from the Reservations screen, expired, or taken by an approved
# reallocation. Completion and cancellation must still work afterwards; they
# used to try to release the closed promise a second time and refuse.


async def _held_component(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """The first component line's item and the reservation it holds, as plain ids."""
    first = (await service.list_components(session, tenant, order_id))[0]
    assert first.reservation_id is not None
    return first.item_id, first.reservation_id


async def _reservation(session: AsyncSession, reservation_id: uuid.UUID) -> StockReservation:
    stmt = (
        select(StockReservation)
        .where(StockReservation.id == reservation_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def test_completion_after_a_reservation_was_released_elsewhere_consumes_free_stock(
    ctx,
) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="100")
    order_id = order.id
    await take_to_in_progress(session, tenant, order)
    item_id, reservation_id = await _held_component(session, tenant, order_id)
    # The Reservations screen's Release action, exactly as the route calls it.
    await allocation.AllocationService(session, tenant).release_reservation(
        reservation_id, reason="Released from the reservations screen", quantity=None
    )

    result = await service.complete_assembly(
        session,
        tenant,
        order_id,
        CompleteRequest(assembled_quantity=Decimal("100"), reason="Night shift"),
    )

    assert result.order.status == AssemblyOrderStatus.COMPLETED
    balance = await inv.get_balance(session, tenant, item_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    expected = Decimal("1000") - setup.components[item_id] * 100
    assert Decimal(balance.on_hand_quantity) == expected
    assert Decimal(balance.reserved_quantity) == Decimal("0.0000")
    # Released once, by the screen - never a second time by the completion.
    reservation = await _reservation(session, reservation_id)
    assert reservation.status is ReservationStatus.RELEASED
    assert reservation.release_reason == "Released from the reservations screen"
    components = await service.list_components(session, tenant, order_id)
    assert all(row.reservation_id is None for row in components)


async def test_completion_cannot_consume_a_component_promised_elsewhere_since(ctx) -> None:
    """The order lost its promise; it did not gain a right to anyone else's."""
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="100")
    order_id = order.id
    await take_to_in_progress(session, tenant, order)
    item_id, reservation_id = await _held_component(session, tenant, order_id)
    await allocation.release(
        session,
        tenant,
        reservation_id=reservation_id,
        reason="Released from the reservations screen",
    )
    await allocation.reserve(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=setup.warehouse_id,
        quantity=Decimal("950"),
        demand_type=DemandType.PRODUCTION_ORDER,
        demand_id=uuid.uuid4(),
        reason="Promised to a production order meanwhile",
    )

    with pytest.raises(InsufficientStockError):
        await service.complete_assembly(
            session,
            tenant,
            order_id,
            CompleteRequest(assembled_quantity=Decimal("100"), reason="Night shift"),
        )

    still = (
        await session.execute(
            select(AssemblyOrder)
            .where(AssemblyOrder.id == order_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert still.status == AssemblyOrderStatus.IN_PROGRESS
    balance = await inv.get_balance(session, tenant, item_id, setup.warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    assert Decimal(balance.on_hand_quantity) == Decimal("1000.0000")
    assert Decimal(balance.reserved_quantity) == Decimal("950.0000")


async def test_a_released_order_whose_reservation_was_released_elsewhere_can_be_cancelled(
    ctx,
) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup)
    order_id = order.id
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the line")
    _, reservation_id = await _held_component(session, tenant, order_id)
    await allocation.release(
        session,
        tenant,
        reservation_id=reservation_id,
        reason="Released from the reservations screen",
    )

    cancelled = await service.cancel_order(session, tenant, order_id, reason="Customer pulled it")

    assert cancelled.status == AssemblyOrderStatus.CANCELLED
    for item_id in setup.components:
        balance = await inv.get_balance(session, tenant, item_id, setup.warehouse_id)
        assert balance is not None
        await session.refresh(balance)
        assert Decimal(balance.reserved_quantity) == Decimal("0.0000")
    components = await service.list_components(session, tenant, order_id)
    assert all(row.reservation_id is None for row in components)
    # Four were still open and given back; the fifth was closed already.
    audit = (
        await session.execute(
            select(AuditLog.new_values).where(
                AuditLog.entity_id == order_id,
                AuditLog.action == AuditAction.RESERVATION_CHANGE,
            )
        )
    ).scalar_one()
    assert audit == {"released_reservations": 4}


async def test_a_component_taken_by_a_reallocation_cancels_and_completes_from_new_stock(
    ctx,
) -> None:
    """An approved reallocation empties the donor; the order follows it forward.

    Exactly the kit is in stock, so the reallocation has no free stock to use
    and takes the orders' reservations. Cancelling then succeeds; completing is
    refused by the stock engine - the recipient holds the units - and succeeds
    from replacement stock, never from the units the reallocation gave away.
    """
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    parent = await seed_item(
        session, tenant.company_id, uom_id, code=_code("ASM"), assembly_component=False
    )
    part = await seed_item(session, tenant.company_id, uom_id, code=_code("HANDLE"))
    await seed_assembly_bom(
        session, tenant, parent_item_id=parent, uom_id=uom_id, components={part: Decimal("1")}
    )
    await stock_in(session, tenant, part, warehouse_id, "200")

    async def raised() -> uuid.UUID:
        order, _ = await service.create_order(
            session,
            tenant,
            AssemblyOrderCreate(
                item_id=parent, planned_quantity=Decimal("100"), warehouse_id=warehouse_id
            ),
        )
        return order.id

    completing_id = await raised()
    completing = (
        await session.execute(select(AssemblyOrder).where(AssemblyOrder.id == completing_id))
    ).scalar_one()
    await take_to_in_progress(session, tenant, completing)
    cancelling_id = await raised()
    await service.approve_order(session, tenant, cancelling_id, reason="Scheduled")
    await service.release_order(session, tenant, cancelling_id, reason="To the line")
    approver = await seed_support.seed_user(
        session, company_id=tenant.company_id, email=f"approver-{uuid.uuid4().hex[:6]}@atcraft.test"
    )
    approving = TenantContext(company_id=tenant.company_id, user_id=approver.id)
    approver_id = approver.id

    for order_id in (completing_id, cancelling_id):
        _, reservation_id = await _held_component(session, tenant, order_id)
        request = await allocation.AllocationService(session, tenant).create_reallocation_request(
            ReallocationRequestCreate(
                item_id=part,
                warehouse_id=warehouse_id,
                quantity=Decimal("100"),
                to_demand_type=DemandType.PRODUCTION_ORDER,
                to_demand_id=uuid.uuid4(),
                priority=1,
                reason="Line-down production order",
                from_reservation_id=reservation_id,
            )
        )
        await allocation.reallocate(
            session, approving, request_id=request.id, approved_by=approver_id
        )
        assert (await _reservation(session, reservation_id)).status is ReservationStatus.RELEASED

    cancelled = await service.cancel_order(session, tenant, cancelling_id, reason="Plan changed")
    assert cancelled.status == AssemblyOrderStatus.CANCELLED

    completion = CompleteRequest(assembled_quantity=Decimal("100"), reason="Night shift")
    with pytest.raises(InsufficientStockError):
        await service.complete_assembly(session, tenant, completing_id, completion)
    still = (
        await session.execute(
            select(AssemblyOrder)
            .where(AssemblyOrder.id == completing_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert still.status == AssemblyOrderStatus.IN_PROGRESS

    await stock_in(session, tenant, part, warehouse_id, "100")
    result = await service.complete_assembly(session, tenant, completing_id, completion)

    assert result.order.status == AssemblyOrderStatus.COMPLETED
    balance = await inv.get_balance(session, tenant, part, warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    assert Decimal(balance.on_hand_quantity) == Decimal("200.0000")
    # Both reallocations' recipients still hold the 100 each they were given.
    assert Decimal(balance.reserved_quantity) == Decimal("200.0000")


async def test_a_batch_tracked_component_is_accepted_and_requires_a_lot(ctx) -> None:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    parent = await seed_item(
        session, tenant.company_id, uom_id, code=_code("ASM"), assembly_component=False
    )
    child = await seed_item(
        session, tenant.company_id, uom_id, code=_code("GLUE"), batch_tracked=True
    )
    await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=parent,
        uom_id=uom_id,
        components={child: Decimal("1")},
    )

    # A batch-tracked component is supported now. Creation records that fact on
    # the line; completion is where the operator must name the lot consumed.
    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=parent, planned_quantity=Decimal("1"), warehouse_id=warehouse_id
        ),
    )
    components = await service.list_components(session, tenant, order.id)
    assert [line.is_batch_tracked_component for line in components] == [True]


@pytest.mark.parametrize("requires_qc", [False, True])
async def test_a_tracked_component_and_tracked_output_are_recorded_end_to_end(
    ctx, requires_qc
) -> None:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    output_warehouse_id = await seed_warehouse(session, tenant.company_id)
    parent = await seed_item(
        session,
        tenant.company_id,
        uom_id,
        code=_code("TRACKED-ASM"),
        assembly_component=False,
        batch_tracked=True,
        serial_tracked=True,
    )
    child = await seed_item(
        session,
        tenant.company_id,
        uom_id,
        code=_code("TRACKED-GLUE"),
        batch_tracked=True,
    )
    await seed_assembly_bom(
        session,
        tenant,
        parent_item_id=parent,
        uom_id=uom_id,
        components={child: Decimal("1")},
    )
    trace = TraceabilityService(session, tenant)
    component_batch = await trace.create_batch(
        StockBatchCreate(
            item_id=child,
            batch_number=_code("COMP-LOT"),
            received_on=today(),
        )
    )
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=child,
            warehouse_id=warehouse_id,
            quantity=Decimal("3"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="assembly tracked fixture",
            unit_cost=Decimal("10"),
            batch_id=component_batch.id,
        )
    balance = await inv.get_balance(
        session, tenant, child, warehouse_id, batch_id=component_batch.id
    )
    assert balance is not None
    assert balance.available_quantity == Decimal("3")

    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=parent,
            planned_quantity=Decimal("2"),
            warehouse_id=warehouse_id,
            output_warehouse_id=output_warehouse_id,
            requires_qc=requires_qc,
        ),
    )
    await service.approve_order(session, tenant, order.id, reason="Scheduled")
    await service.release_order(session, tenant, order.id, reason="To the line")
    await service.issue_components(
        session,
        tenant,
        order.id,
        IssueComponentsRequest(
            reason="Kitted",
            lines=[
                ComponentIssueIn(
                    line_number=1,
                    issued_quantity=Decimal("2"),
                    batch_id=component_batch.id,
                )
            ],
        ),
    )
    await service.start_assembly(session, tenant, order.id, reason="Shift started")

    output_batch = await trace.create_batch(
        StockBatchCreate(
            item_id=parent,
            batch_number=_code("OUT-LOT"),
            received_on=today(),
        )
    )
    completion = await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(
            assembled_quantity=Decimal("2"),
            reason="Built and checked",
            output_batch_id=output_batch.id,
            output_serial_numbers=["ASM-SN-001", "ASM-SN-002"],
            components=[ComponentConsumptionIn(line_number=1, batch_id=component_batch.id)],
        ),
    )

    assert completion.order.output_batch_id == output_batch.id
    assert completion.order.components[0].consumed_batch_id == component_batch.id
    assert len(completion.order.output_serial_ids or []) == 2
    serials = (
        (
            await session.execute(
                select(StockSerial).where(
                    StockSerial.company_id == tenant.company_id,
                    StockSerial.item_id == parent,
                    StockSerial.batch_id == output_batch.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {serial.serial_number for serial in serials} == {"ASM-SN-001", "ASM-SN-002"}
    assert all(serial.status is SerialStatus.IN_STOCK for serial in serials)
    assert all(serial.warehouse_id == output_warehouse_id for serial in serials)
    if requires_qc:
        assert completion.order.qc_inspection_id is not None
        decided = await quality_service.decide_inspection(
            session,
            tenant,
            completion.order.qc_inspection_id,
            DecideRequest(
                accepted_quantity=Decimal("2"),
                reason="Assembled units accepted",
                serial_dispositions=dict.fromkeys(completion.order.output_serial_ids, "ACCEPTED"),
            ),
        )
        assert decided.released_quantity == 2
        assert decided.inspection.batch_id == output_batch.id


async def test_the_output_stock_state_is_recorded_on_the_receipt_row(ctx) -> None:
    session, tenant = ctx
    setup = await lock_set_setup(session, tenant)
    order = await raise_order(session, tenant, setup, planned="4", requires_qc=True)
    await take_to_in_progress(session, tenant, order)
    await service.complete_assembly(
        session,
        tenant,
        order.id,
        CompleteRequest(assembled_quantity=Decimal("4"), reason="Held"),
    )

    receipt = (
        await session.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.reference_id == order.id,
                InventoryTransaction.transaction_type == TransactionType.ASSEMBLY_RECEIPT,
            )
        )
    ).scalar_one()
    assert receipt.stock_state == StockState.QC_HOLD
    assert receipt.warehouse_id == setup.output_warehouse_id
