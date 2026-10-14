"""Assembly completion against a real PostgreSQL: one QC verdict path, the board,
the kit, top-ups and the hand-over to packing (WP E4).

The regression this file exists for is the sentence in the work package: decide
on /quality -> the order completes -> stock moved exactly once. Before it, the
inspection's decision released the held output and left the order in
QC_PENDING for ever, and the order's own verdict endpoint would have released
the same stock a second time.

Isolation follows ``test_assembly_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext, system_tenant
from src.core.errors import (
    BusinessRuleError,
    InsufficientStockError,
    InvalidStateTransitionError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.allocation.models import StockReservation
from src.modules.assembly import service
from src.modules.assembly.models import AssemblyOrder, AssemblyOrderStatus, AssemblySourceType
from src.modules.assembly.schemas import (
    AssemblyBoardFilters,
    AssemblyOrderCreate,
    CompleteRequest,
    ComponentIssueIn,
    IssueComponentsRequest,
    QcResultRequest,
)
from src.modules.audit.models import AuditLog
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.inventory import service as inv
from src.modules.inventory.models import InventoryTransaction, StockState, TransactionType
from src.modules.packing import service as packing_service
from src.modules.packing.models import PackingOrderLine, PackingOrderStatus
from src.modules.packing.schemas import PackingFromAssemblyCreate
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.quality import owners as quality_owners
from src.modules.quality import service as quality_service
from src.modules.quality.models import QcInspection
from src.modules.quality.schemas import DecideRequest
from src.modules.sales.models import SalesOrder
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

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
    schema = f"it_asm_e4_{uuid.uuid4().hex[:10]}"
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
                await seed_support.seed_company(session, company_id=company_id, prefix="AE4")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email="bench@atcraft.test",
                    full_name="Bench Lead",
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
# Seeding - through the ORM (tests/seed_support), never raw INSERTs
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Kit:
    """A lock set and its components, stocked at one store."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        output_item_id: uuid.UUID,
        components: dict[uuid.UUID, Decimal],
        warehouse_id: uuid.UUID,
        output_warehouse_id: uuid.UUID,
    ) -> None:
        self.uom_id = uom_id
        self.output_item_id = output_item_id
        self.components = components
        self.warehouse_id = warehouse_id
        self.output_warehouse_id = output_warehouse_id


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
            reason="assembly completion fixture opening stock",
            unit_cost=Decimal("10"),
        )


async def make_kit(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    stock: dict[str, str] | None = None,
    separate_output_warehouse: bool = False,
) -> Kit:
    """A lock set of a handle (x1), keys (x2) and a screw kit (x4)."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    output_warehouse = (
        await seed_support.seed_warehouse(session, company_id=tenant.company_id, code=_code("FG"))
        if separate_output_warehouse
        else warehouse
    )
    output = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("LOCKSET"),
        base_uom_id=uom.id,
        item_group=ItemGroup.ASSEMBLY,
        standard_cost=Decimal("40"),
        production_type=ProductionType.MAKE,
        can_manufacture=True,
    )
    per_set = {"HANDLE": Decimal("1"), "KEYS": Decimal("2"), "SCREWKIT": Decimal("4")}
    stock = stock or {}
    components: dict[uuid.UUID, Decimal] = {}
    for name, quantity_per in per_set.items():
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code(name),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            standard_cost=Decimal("10"),
            can_be_assembly_component=True,
        )
        components[item.id] = quantity_per
        on_hand = stock.get(name, "1000")
        if Decimal(on_hand) > 0:
            await stock_in(session, tenant, item.id, warehouse.id, on_hand)

    bom = Bom(
        company_id=tenant.company_id,
        code=_code("BOM"),
        item_id=output.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.ASSEMBLY,
        is_default=True,
        output_quantity=Decimal("1"),
        base_uom_id=uom.id,
        effective_from=today(),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    session.add(bom)
    await session.flush()
    for number, (item_id, quantity_per) in enumerate(components.items(), start=1):
        session.add(
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=output.id,
                line_number=number,
                component_item_id=item_id,
                quantity_per=quantity_per,
                uom_id=uom.id,
            )
        )
    await session.flush()
    return Kit(
        uom_id=uom.id,
        output_item_id=output.id,
        components=components,
        warehouse_id=warehouse.id,
        output_warehouse_id=output_warehouse.id,
    )


async def raise_order(
    session: AsyncSession,
    tenant: TenantContext,
    kit: Kit,
    *,
    planned: str = "10",
    requires_qc: bool = False,
    **fields: object,
) -> AssemblyOrder:
    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=kit.output_item_id,
            planned_quantity=Decimal(planned),
            warehouse_id=kit.warehouse_id,
            output_warehouse_id=kit.output_warehouse_id,
            requires_qc=requires_qc,
            **fields,  # type: ignore[arg-type]
        ),
    )
    return order


async def to_in_progress(session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID) -> None:
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")
    await service.issue_components(
        session, tenant, order_id, IssueComponentsRequest(reason="Kitted")
    )
    await service.start_assembly(session, tenant, order_id, reason="Shift started")


async def held_for_qc(
    session: AsyncSession, tenant: TenantContext, kit: Kit, *, quantity: str = "10"
) -> tuple[uuid.UUID, uuid.UUID]:
    """An order completed onto QC hold. Returns (order id, inspection id)."""
    order = await raise_order(session, tenant, kit, planned=quantity, requires_qc=True)
    order_id = order.id
    await to_in_progress(session, tenant, order_id)
    result = await service.complete_assembly(
        session,
        tenant,
        order_id,
        CompleteRequest(assembled_quantity=Decimal(quantity), reason="Needs a key test"),
    )
    assert result.order.status is AssemblyOrderStatus.QC_PENDING
    assert result.order.qc_inspection_id is not None
    return order_id, result.order.qc_inspection_id


async def reload_order(session: AsyncSession, order_id: uuid.UUID) -> AssemblyOrder:
    order = (
        await session.execute(
            select(AssemblyOrder)
            .where(AssemblyOrder.id == order_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return order


async def released_from_hold(session: AsyncSession, item_id: uuid.UUID) -> Decimal:
    """Everything the ledger has ever moved *out* of QC hold for one item."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(func.abs(InventoryTransaction.quantity)), 0)).where(
                InventoryTransaction.item_id == item_id,
                InventoryTransaction.source_state == StockState.QC_HOLD,
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def position(session: AsyncSession, tenant: TenantContext, item_id, warehouse_id):
    row = await inv.get_balance(session, tenant, item_id, warehouse_id)
    assert row is not None
    await session.refresh(row)
    return row


# -----------------------------------------------------------------------------
# One QC verdict path
# -----------------------------------------------------------------------------


async def test_deciding_the_inspection_completes_the_order_and_moves_stock_exactly_once(
    ctx,
) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, inspection_id = await held_for_qc(session, tenant, kit)

    await quality_service.decide_inspection(
        session,
        tenant,
        inspection_id,
        DecideRequest(
            accepted_quantity=Decimal("8"),
            rejected_quantity=Decimal("2"),
            reason="Key test on every set",
            rejection_reason="Two sets bind on the second key",
        ),
    )

    order = await reload_order(session, order_id)
    assert AssemblyOrderStatus(order.status) is AssemblyOrderStatus.COMPLETED
    assert Decimal(order.assembled_quantity) == Decimal("8.0000")
    assert Decimal(order.rejected_quantity) == Decimal("2.0000")
    assert order.qc_held_at is None
    assert order.qc_inspection_id == inspection_id

    row = await position(session, tenant, kit.output_item_id, kit.output_warehouse_id)
    assert Decimal(row.qc_hold_quantity) == 0
    assert Decimal(row.damaged_quantity) == Decimal("2.0000")
    assert Decimal(row.available_quantity) == Decimal("8.0000")
    # Exactly once: ten units were held and ten left the hold, all of them on
    # the inspection's own ledger rows.
    assert await released_from_hold(session, kit.output_item_id) == Decimal("10.0000")

    # The completed order is now offered to packing.
    rows = await service.ready_for_packing(session, tenant, warehouse_id=kit.output_warehouse_id)
    assert [row.assembly_order_id for row in rows] == [order_id]

    # And the order's own verdict endpoint cannot release the hold again.
    with pytest.raises(InvalidStateTransitionError):
        await service.record_qc_result(
            session,
            tenant,
            order_id,
            QcResultRequest(
                accepted_quantity=Decimal("8"),
                rejected_quantity=Decimal("2"),
                reason="Recorded a second time",
            ),
        )
    assert await released_from_hold(session, kit.output_item_id) == Decimal("10.0000")


async def test_the_owner_side_verdict_points_at_an_open_inspection(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, _ = await held_for_qc(session, tenant, kit)

    with pytest.raises(BusinessRuleError, match="held for inspection") as raised:
        await service.record_qc_result(
            session,
            tenant,
            order_id,
            QcResultRequest(accepted_quantity=Decimal("10"), reason="Looks fine"),
        )
    assert raised.value.details["qc_inspection_code"].startswith("QC/")

    order = await reload_order(session, order_id)
    assert AssemblyOrderStatus(order.status) is AssemblyOrderStatus.QC_PENDING
    assert await released_from_hold(session, kit.output_item_id) == 0


async def test_a_rework_decision_keeps_the_order_waiting_until_it_is_re_decided(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, inspection_id = await held_for_qc(session, tenant, kit)

    await quality_service.decide_inspection(
        session,
        tenant,
        inspection_id,
        DecideRequest(rework_quantity=Decimal("10"), reason="Re-lubricate every cylinder"),
    )
    order = await reload_order(session, order_id)
    assert AssemblyOrderStatus(order.status) is AssemblyOrderStatus.QC_PENDING

    await quality_service.decide_inspection(
        session,
        tenant,
        inspection_id,
        DecideRequest(accepted_quantity=Decimal("10"), reason="Rework done, all pass"),
    )
    order = await reload_order(session, order_id)
    assert AssemblyOrderStatus(order.status) is AssemblyOrderStatus.COMPLETED
    assert Decimal(order.assembled_quantity) == Decimal("10.0000")
    assert await released_from_hold(session, kit.output_item_id) == Decimal("10.0000")


async def strand(session, tenant, inspection_id, monkeypatch, **quantities: str) -> None:
    """Decide an inspection the way a release before the callback did: with no
    owner told. The resolver is forced to nothing for the decision only."""
    with monkeypatch.context() as patched:
        patched.setattr(quality_owners, "resolve_owner", lambda _reference_type: None)
        await quality_service.decide_inspection(
            session,
            tenant,
            inspection_id,
            DecideRequest(
                reason="Decided before the owner callback existed",
                rejection_reason="Out of tolerance" if quantities.get("rejected") else None,
                accepted_quantity=Decimal(quantities.get("accepted", "0")),
                rejected_quantity=Decimal(quantities.get("rejected", "0")),
            ),
        )


async def test_a_stranded_order_completes_from_the_inspection_only_with_its_own_figures(
    ctx, monkeypatch
) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, inspection_id = await held_for_qc(session, tenant, kit)
    await strand(session, tenant, inspection_id, monkeypatch, accepted="9", rejected="1")
    assert AssemblyOrderStatus((await reload_order(session, order_id)).status) is (
        AssemblyOrderStatus.QC_PENDING
    )

    with pytest.raises(BusinessRuleError, match="already decided"):
        await service.record_qc_result(
            session,
            tenant,
            order_id,
            QcResultRequest(accepted_quantity=Decimal("10"), reason="All good"),
        )

    completed = await service.record_qc_result(
        session,
        tenant,
        order_id,
        QcResultRequest(
            accepted_quantity=Decimal("9"), rejected_quantity=Decimal("1"), reason="As inspected"
        ),
    )
    assert AssemblyOrderStatus(completed.status) is AssemblyOrderStatus.COMPLETED
    assert Decimal(completed.rejected_quantity) == Decimal("1.0000")
    # The repair moved nothing: the inspection had already released all ten.
    assert await released_from_hold(session, kit.output_item_id) == Decimal("10.0000")


async def test_the_reconciliation_sweep_repairs_a_stranded_order_as_the_system(
    ctx, monkeypatch
) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, inspection_id = await held_for_qc(session, tenant, kit)
    await strand(session, tenant, inspection_id, monkeypatch, accepted="10")

    result = await quality_owners.reconcile_owners(session, system_tenant(tenant.company_id))

    assert result.owners_repaired >= 1
    assert result.failed == 0
    order = await reload_order(session, order_id)
    assert AssemblyOrderStatus(order.status) is AssemblyOrderStatus.COMPLETED
    assert order.updated_by_id is None

    # Audited as the system, never as the nil placeholder (which no user row has).
    audit = (
        (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == service.AUDIT_ASSEMBLY_ORDER,
                    AuditLog.entity_id == order_id,
                    AuditLog.actor_label == "System",
                )
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert audit
    assert all(row.created_by_id is None for row in audit)

    again = await quality_owners.reconcile_owners(session, system_tenant(tenant.company_id))
    assert again.owners_repaired == 0
    assert await released_from_hold(session, kit.output_item_id) == Decimal("10.0000")


# -----------------------------------------------------------------------------
# Release: the whole kit's gap, and the reservation's priority and date
# -----------------------------------------------------------------------------


async def test_a_short_release_names_every_short_component_and_reserves_nothing(ctx) -> None:
    session, tenant = ctx
    # 10 sets need 10 handles, 20 keys and 40 screw kits.
    kit = await make_kit(session, tenant, stock={"KEYS": "5", "SCREWKIT": "0"})
    order = await raise_order(session, tenant, kit, planned="10")
    order_id = order.id
    await service.approve_order(session, tenant, order_id, reason="Scheduled")

    with pytest.raises(InsufficientStockError) as raised:
        await service.release_order(session, tenant, order_id, reason="To the bench")

    shortages = raised.value.details["shortages"]
    assert [row["short"] for row in shortages] == ["15.0000", "40.0000"]
    reserved = (
        await session.execute(
            select(func.count())
            .select_from(StockReservation)
            .where(StockReservation.demand_id == order_id)
        )
    ).scalar_one()
    assert reserved == 0


async def test_availability_reports_the_kit_gap_before_release_and_none_after(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant, stock={"KEYS": "5"})
    order = await raise_order(session, tenant, kit, planned="10")
    order_id = order.id

    before = {
        row.line_number: row
        for row in await service.component_availability(session, tenant, order_id)
    }
    assert before[2].shortage_quantity == Decimal("15.0000")
    assert before[1].shortage_quantity == 0
    assert not any(row.is_reserved for row in before.values())

    keys_id = before[2].item_id
    await stock_in(session, tenant, keys_id, kit.warehouse_id, "15")
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")

    after = await service.component_availability(session, tenant, order_id)
    assert all(row.is_reserved for row in after)
    assert all(row.shortage_quantity == 0 for row in after)


async def test_release_carries_the_priority_and_planned_start_onto_reservations(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    start = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)
    order = await raise_order(
        session,
        tenant,
        kit,
        planned="5",
        priority=7,
        planned_start=start,
        planned_end=start + timedelta(hours=8),
    )
    order_id = order.id
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")

    reservations = (
        (
            await session.execute(
                select(StockReservation).where(StockReservation.demand_id == order_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(reservations) == 3
    assert {row.priority for row in reservations} == {7}
    assert {row.required_date for row in reservations} == {start.date()}


# -----------------------------------------------------------------------------
# Issue top-ups
# -----------------------------------------------------------------------------


async def test_a_short_issue_is_topped_up_on_the_bench_without_moving_the_status(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order = await raise_order(session, tenant, kit, planned="10")
    order_id = order.id
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")
    await service.issue_components(
        session,
        tenant,
        order_id,
        IssueComponentsRequest(
            reason="Screw kits short until noon",
            lines=[ComponentIssueIn(line_number=3, issued_quantity=Decimal("20"))],
        ),
    )
    await service.start_assembly(session, tenant, order_id, reason="Started on what we have")

    topped, components = await service.issue_components(
        session, tenant, order_id, IssueComponentsRequest(reason="Screw kits arrived")
    )

    assert AssemblyOrderStatus(topped.status) is AssemblyOrderStatus.IN_PROGRESS
    assert {row.line_number: Decimal(row.issued_quantity) for row in components} == {
        1: Decimal("10.0000"),
        2: Decimal("20.0000"),
        3: Decimal("40.0000"),
    }

    with pytest.raises(BusinessRuleError, match="nothing to top up"):
        await service.issue_components(
            session, tenant, order_id, IssueComponentsRequest(reason="Again")
        )
    with pytest.raises(BusinessRuleError, match="cannot lower"):
        await service.issue_components(
            session,
            tenant,
            order_id,
            IssueComponentsRequest(
                reason="Take some back",
                lines=[ComponentIssueIn(line_number=1, issued_quantity=Decimal("5"))],
            ),
        )


# -----------------------------------------------------------------------------
# The board
# -----------------------------------------------------------------------------


async def test_the_board_groups_open_orders_by_line_and_flags_the_late_ones(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    now = utcnow()
    line = _code("LINE")
    late = await raise_order(
        session,
        tenant,
        kit,
        planned="2",
        assembly_line=line,
        planned_start=now - timedelta(days=3),
        planned_end=now - timedelta(days=2),
    )
    on_time = await raise_order(
        session,
        tenant,
        kit,
        planned="3",
        assembly_line=line,
        planned_start=now + timedelta(days=1),
        planned_end=now + timedelta(days=2),
    )
    draft = await raise_order(session, tenant, kit, planned="1", assembly_line=line)
    for order_id in (late.id, on_time.id):
        await service.approve_order(session, tenant, order_id, reason="Scheduled")

    lines, _ = await service.board_lines(
        session, tenant, warehouse_id=kit.warehouse_id, params=PageParams(page=1, limit=50)
    )
    load = next(row for row in lines if row.assembly_line == line)
    # The draft owes nothing yet, so it is not on the board.
    assert load.open_orders == 2
    assert load.not_started_orders == 2
    assert load.late_orders == 1
    assert load.planned_quantity == Decimal("5.0000")

    rows, _ = await service.board_orders(
        session,
        tenant,
        AssemblyBoardFilters(assembly_line=line),
        PageParams(page=1, limit=50),
    )
    by_id = {row.id: row for row in rows}
    assert set(by_id) == {late.id, on_time.id}
    assert draft.id not in by_id
    assert by_id[late.id].is_late and by_id[late.id].days_late >= 1
    assert by_id[late.id].start_overdue
    assert not by_id[on_time.id].is_late and not by_id[on_time.id].start_overdue
    # Schedule order within the line: the earlier planned start first.
    assert [row.id for row in rows] == [late.id, on_time.id]


# -----------------------------------------------------------------------------
# Hand-over to packing
# -----------------------------------------------------------------------------


async def completed_order(
    session: AsyncSession, tenant: TenantContext, kit: Kit, *, quantity: str = "6", **fields
) -> uuid.UUID:
    order = await raise_order(session, tenant, kit, planned=quantity, **fields)
    order_id = order.id
    await to_in_progress(session, tenant, order_id)
    await service.complete_assembly(
        session,
        tenant,
        order_id,
        CompleteRequest(assembled_quantity=Decimal(quantity), reason="Bench finished"),
    )
    return order_id


async def test_a_packing_order_raised_from_an_assembly_claims_it_once(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant, separate_output_warehouse=True)
    order_id = await completed_order(session, tenant, kit)
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)

    packing_order, lines = await packing_service.create_order_from_assembly(
        session,
        tenant,
        PackingFromAssemblyCreate(assembly_order_id=order_id, customer_id=customer.id),
    )

    assert PackingOrderStatus(packing_order.status) is PackingOrderStatus.DRAFT
    assert packing_order.warehouse_id == kit.output_warehouse_id
    assert packing_order.customer_id == customer.id
    assert [(row.item_id, Decimal(row.quantity_to_pack), row.uom_id) for row in lines] == [
        (kit.output_item_id, Decimal("6.0000"), kit.uom_id)
    ]
    assembly = await reload_order(session, order_id)
    assert assembly.packing_order_id == packing_order.id
    assert assembly.packed_at is not None
    # It has left the hand-over feed.
    assert (
        await service.ready_for_packing(session, tenant, warehouse_id=kit.output_warehouse_id) == []
    )

    create_audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == packing_service.AUDIT_PACKING_ORDER,
                AuditLog.entity_id == packing_order.id,
            )
        )
    ).scalar_one()
    assert create_audit.context is not None
    assert create_audit.context["assembly_order_id"] == str(order_id)

    with pytest.raises(BusinessRuleError, match="already been taken"):
        await packing_service.create_order_from_assembly(
            session,
            tenant,
            PackingFromAssemblyCreate(assembly_order_id=order_id, customer_id=customer.id),
        )
    count = (
        await session.execute(
            select(func.count())
            .select_from(PackingOrderLine)
            .where(PackingOrderLine.item_id == kit.output_item_id)
        )
    ).scalar_one()
    assert count == 1


async def test_an_assembly_for_a_sales_order_is_packed_for_that_customer(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant, separate_output_warehouse=True)
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    sales_order = SalesOrder(
        company_id=tenant.company_id,
        code=_code("SO"),
        customer_id=customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=7),
        warehouse_id=kit.output_warehouse_id,
    )
    session.add(sales_order)
    await session.flush()
    order_id = await completed_order(
        session,
        tenant,
        kit,
        quantity="4",
        source_type=AssemblySourceType.SALES_ORDER,
        source_id=sales_order.id,
    )

    packing_order, lines = await packing_service.create_order_from_assembly(
        session,
        tenant,
        PackingFromAssemblyCreate(assembly_order_id=order_id, quantity=Decimal("3")),
    )

    assert packing_order.customer_id == customer.id
    assert packing_order.sales_order_id == sales_order.id
    assert Decimal(lines[0].quantity_to_pack) == Decimal("3.0000")


async def test_packing_an_assembly_with_no_sales_order_needs_a_customer(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id = await completed_order(session, tenant, kit)
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    customer_id = customer.id

    with pytest.raises(ValidationError, match="name the customer"):
        await packing_service.create_order_from_assembly(
            session, tenant, PackingFromAssemblyCreate(assembly_order_id=order_id)
        )
    with pytest.raises(BusinessRuleError, match="cannot take"):
        await packing_service.create_order_from_assembly(
            session,
            tenant,
            PackingFromAssemblyCreate(
                assembly_order_id=order_id, customer_id=customer_id, quantity=Decimal("7")
            ),
        )
    assembly = await reload_order(session, order_id)
    assert assembly.packing_order_id is None


async def test_an_order_still_on_qc_hold_cannot_be_packed(ctx) -> None:
    session, tenant = ctx
    kit = await make_kit(session, tenant)
    order_id, _ = await held_for_qc(session, tenant, kit, quantity="2")
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)

    with pytest.raises(BusinessRuleError, match="only a completed assembly"):
        await packing_service.create_order_from_assembly(
            session,
            tenant,
            PackingFromAssemblyCreate(assembly_order_id=order_id, customer_id=customer.id),
        )
    inspections = (
        await session.execute(select(func.count()).select_from(QcInspection))
    ).scalar_one()
    assert inspections >= 1
