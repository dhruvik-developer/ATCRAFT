"""FIFO valuation end to end: layers, transfers, and the invariant behind both.

What is under test here is not arithmetic - ``tests/unit/test_inventory_valuation.py``
covers that without a database - but the things only a real PostgreSQL can show:

* the invariant the whole method rests on, that a position's open cost layers
  add up to its ``on_hand_quantity`` and their value to its ``stock_value``;
* that moving stock between two warehouses carries its layers with it, so the
  company's total stock value is the same on both sides of the move;
* that a document-based transfer, whose two legs are separate transactions days
  apart, does the same through the consumption rows its dispatch wrote;
* that the finance valuation report reads the FIFO number and not a leftover
  average;
* that a position whose layers do not cover its stock is repaired rather than
  either refused or silently under-valued, in **both** directions;
* that a document states the cost the ledger will charge it, and cannot walk an
  approval threshold past a figure the engine does not use.

The schema pattern is the module-scoped one from ``test_products_service.py``:
one private schema built once, one rolled-back session per test.
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

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ErrorCode
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.finance.schemas import ValuationFilters
from src.modules.finance.service import inventory_valuation
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    LAYER_ORIGIN_RECONCILIATION,
    AdjustmentReason,
    AdjustmentStatus,
    CostLayerPool,
    InventoryBalance,
    InventoryCostLayer,
    InventoryTransaction,
    PhysicalCountStatus,
    TransactionType,
)
from src.modules.inventory.repository import CostLayerRepository
from src.modules.inventory.schemas import (
    PhysicalCountCreate,
    PhysicalCountEntry,
    PhysicalCountEntryRequest,
    StockAdjustmentCreate,
    StockAdjustmentLineCreate,
    StockBatchCreate,
    StockTransferCreate,
    StockTransferLineCreate,
)
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
    schema = f"it_inv_fifo_{uuid.uuid4().hex[:10]}"
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
                await seed_support.seed_company(session, company_id=company_id, prefix="FIFO")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"fifo-{uuid.uuid4().hex[:8]}@atcraft.test",
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


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def _second_principal(session: AsyncSession, tenant: TenantContext) -> TenantContext:
    """A different user in the same tenant, for segregation of duties.

    An adjustment may not be approved by the person who raised it, so a test
    that has to get one approved needs two principals rather than one.
    """
    other = await seed_support.seed_user(
        session,
        company_id=tenant.company_id,
        email=f"approver-{uuid.uuid4().hex[:8]}@atcraft.test",
    )
    return TenantContext(company_id=tenant.company_id, user_id=other.id)


async def _item(
    session: AsyncSession, tenant: TenantContext, *, standard_cost: str = "0"
) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("RM"),
        gst_rate=Decimal("18"),
        standard_cost=Decimal(standard_cost),
    )
    return item.id


async def _warehouse(session: AsyncSession, tenant: TenantContext) -> uuid.UUID:
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    return warehouse.id


async def _receive(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    unit_cost: str,
) -> None:
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            reference_type="goods_receipt",
            reference_id=None,
            reference_code=_code("GRN"),
            reason="fifo fixture receipt",
            unit_cost=Decimal(unit_cost),
        )


async def _assert_layers_reconcile(
    session: AsyncSession, tenant: TenantContext, balance: InventoryBalance
) -> None:
    """The invariant the whole method rests on, checked against the database.

    Quantity first, then value. A position whose layers do not add up to its
    on-hand is one where the next issue is costed out of thin air, and a
    position whose layers do not add up to its ``stock_value`` is one where the
    balance sheet and the cost of goods issued disagree. Only the on-hand
    queue is asserted here: stock moved into scrap opens layers in the
    position's SCRAP queue (``CostLayerPool.SCRAP``), which stands against
    ``scrap_quantity`` and carries no book value.
    """
    quantity, value = (
        await session.execute(
            select(
                func.coalesce(func.sum(InventoryCostLayer.remaining_quantity), 0),
                func.coalesce(
                    func.sum(InventoryCostLayer.remaining_quantity * InventoryCostLayer.unit_cost),
                    0,
                ),
            ).where(
                InventoryCostLayer.balance_id == balance.id,
                InventoryCostLayer.pool == CostLayerPool.ON_HAND,
            )
        )
    ).one()
    assert Decimal(quantity) == Decimal(balance.on_hand_quantity), (
        f"layers hold {quantity} but the position holds {balance.on_hand_quantity}"
    )
    assert Decimal(value).quantize(Decimal("0.0001")) == Decimal(balance.stock_value), (
        f"layers are worth {value} but the position claims {balance.stock_value}"
    )


class TestLayerInvariant:
    async def test_receipts_and_issues_leave_the_layers_reconciled(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, warehouse_id, "100", "10")
        await _receive(session, tenant, item_id, warehouse_id, "50", "20")
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("120"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/INV",
                reason="draw",
            )
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        await _assert_layers_reconcile(session, tenant, balance)
        # 100 @ 10 gone, 20 @ 20 gone; 30 @ 20 left.
        assert balance.stock_value == Decimal("600.0000")

    async def test_a_qc_hold_moves_no_value(self, ctx) -> None:
        """Holding stock changes which pool it is in, not what it cost."""
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "10")

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("40"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
            )
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.qc_hold_quantity == Decimal("40.0000")
        assert balance.stock_value == Decimal("1000.0000")
        await _assert_layers_reconcile(session, tenant, balance)

    async def test_scrap_consumes_layers_at_what_the_scrapped_stock_cost(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "10", "5")
        await _receive(session, tenant, item_id, warehouse_id, "10", "50")

        async with transaction(session):
            scrapped = await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.SCRAP,
                reason="damaged in handling",
            )
        assert scrapped.unit_cost == Decimal("5.000000"), "scrap was not costed FIFO"
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("500.0000")
        await _assert_layers_reconcile(session, tenant, balance)
        scrap_covered = (
            await session.execute(
                select(func.coalesce(func.sum(InventoryCostLayer.remaining_quantity), 0)).where(
                    InventoryCostLayer.balance_id == balance.id,
                    InventoryCostLayer.pool == CostLayerPool.SCRAP,
                )
            )
        ).scalar_one()
        assert Decimal(scrap_covered) == Decimal(balance.scrap_quantity) == Decimal("10.0000")


class TestLayerGrain:
    """A layer belongs to one position and may never be spent by a neighbour.

    The grain is ``(company, item, warehouse, location, batch)`` - the same
    tuple ``inventory_balances`` is unique on - and a layer states it twice: as
    ``balance_id``, which is the grain by construction, and as the four columns,
    copied from the same locked balance row so a report can group without a
    join. Both are asserted, because a mismatch between them would be invisible
    until someone reported on the wrong one.
    """

    async def test_two_batches_of_one_item_do_not_share_layers(self, ctx) -> None:
        session, tenant = ctx
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            gst_rate=Decimal("18"),
            standard_cost=Decimal("0"),
            is_batch_tracked=True,
        )
        warehouse_id = await _warehouse(session, tenant)
        traceability = inv.TraceabilityService(session, tenant)
        cheap = await traceability.create_batch(
            StockBatchCreate(item_id=item.id, batch_number=_code("LOT"))
        )
        dear = await traceability.create_batch(
            StockBatchCreate(item_id=item.id, batch_number=_code("LOT"))
        )

        for batch, unit_cost in ((cheap, "5"), (dear, "500")):
            async with transaction(session):
                await inv.post_movement(
                    session,
                    tenant,
                    item_id=item.id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("10"),
                    transaction_type=TransactionType.PURCHASE_RECEIPT,
                    reference_type="goods_receipt",
                    reference_id=None,
                    reference_code=_code("GRN"),
                    reason="batched receipt",
                    unit_cost=Decimal(unit_cost),
                    batch_id=batch.id,
                )

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item.id,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/BATCH",
                reason="draw the dear lot",
                batch_id=dear.id,
            )
        # The cheap lot arrived first. If the grain were item+warehouse the
        # issue would have been costed at 5 and the dear lot's layer emptied.
        assert issue.unit_cost == Decimal("500.000000")

        cheap_balance = await inv.get_balance(
            session, tenant, item.id, warehouse_id, batch_id=cheap.id
        )
        dear_balance = await inv.get_balance(
            session, tenant, item.id, warehouse_id, batch_id=dear.id
        )
        assert cheap_balance is not None
        assert dear_balance is not None
        assert cheap_balance.stock_value == Decimal("50.0000")
        assert dear_balance.stock_value == Decimal("0.0000")
        await _assert_layers_reconcile(session, tenant, cheap_balance)
        await _assert_layers_reconcile(session, tenant, dear_balance)

    async def test_every_layer_carries_the_grain_of_its_own_balance(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "10", "3")

        mismatched = (
            await session.execute(
                text(
                    "SELECT l.id FROM inventory_cost_layers l "
                    "JOIN inventory_balances b ON b.id = l.balance_id "
                    "WHERE l.company_id IS DISTINCT FROM b.company_id "
                    "   OR l.item_id IS DISTINCT FROM b.item_id "
                    "   OR l.warehouse_id IS DISTINCT FROM b.warehouse_id "
                    "   OR l.location_id IS DISTINCT FROM b.location_id "
                    "   OR l.batch_id IS DISTINCT FROM b.batch_id"
                )
            )
        ).all()
        assert not mismatched, f"layers disagree with their own position: {mismatched}"


class TestTransfersCarryTheirCost:
    async def test_an_immediate_transfer_preserves_total_value(self, ctx) -> None:
        """Moving a box between two of the company's own warehouses is value-neutral.

        The draw spans two layers at different prices, which is the case a
        single blended cost cannot carry: 30 units costing 20*100 + 10*120 =
        3 200 have no exact price per unit at six decimal places, so re-costing
        the arrival at 106.666667 would land 3 200.00001 at the destination and
        the company would be worth a fraction more for having moved a box.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        source_id = await _warehouse(session, tenant)
        destination_id = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, source_id, "20", "100")
        await _receive(session, tenant, item_id, source_id, "50", "120")
        before = Decimal("20") * 100 + Decimal("50") * 120

        async with transaction(session):
            out_leg, in_leg = await inv.transfer_stock(
                session,
                tenant,
                item_id=item_id,
                from_warehouse_id=source_id,
                to_warehouse_id=destination_id,
                quantity=Decimal("30"),
                reference_code="ST/IMMEDIATE",
                reason="rebalance",
            )
        assert out_leg.total_cost == Decimal("3200.0000")
        assert in_leg.total_cost == Decimal("3200.0000")

        source = await inv.get_balance(session, tenant, item_id, source_id)
        destination = await inv.get_balance(session, tenant, item_id, destination_id)
        assert source is not None
        assert destination is not None
        assert source.stock_value + destination.stock_value == before, (
            "moving stock between warehouses changed what the company owns"
        )
        await _assert_layers_reconcile(session, tenant, source)
        await _assert_layers_reconcile(session, tenant, destination)

        # And the destination holds the *source's* layers, not one blended one.
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(destination.id)
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in layers] == [
            (Decimal("20.0000"), Decimal("100.000000")),
            (Decimal("10.0000"), Decimal("120.000000")),
        ]

    async def test_the_destination_consumes_the_carried_layers_in_order(self, ctx) -> None:
        """The point of carrying layers rather than a blend: the next issue.

        With one blended layer at 106.666667 the destination's next issue of 20
        would cost 2 133.33; with the source's real layers it costs 2 000,
        because those 20 units are the ones that were bought at 100.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        source_id = await _warehouse(session, tenant)
        destination_id = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, source_id, "20", "100")
        await _receive(session, tenant, item_id, source_id, "50", "120")
        async with transaction(session):
            await inv.transfer_stock(
                session,
                tenant,
                item_id=item_id,
                from_warehouse_id=source_id,
                to_warehouse_id=destination_id,
                quantity=Decimal("30"),
                reference_code="ST/ORDER",
                reason="rebalance",
            )

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=destination_id,
                quantity=Decimal("20"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/DEST",
                reason="draw at destination",
            )
        assert issue.total_cost == Decimal("2000.0000")
        assert issue.unit_cost == Decimal("100.000000")

    async def test_a_transfer_document_carries_its_cost_across_two_transactions(self, ctx) -> None:
        """Dispatch and receive are days apart, so the breakdown is read back.

        The consumption rows the outbound leg wrote are the record of what left,
        and ``stock_transfer_lines.dispatch_transaction_id`` is how the arrival
        finds them. A blended ``unit_cost`` on the line would be an average of a
        two-layer draw and would change the company's stock value.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        source_id = await _warehouse(session, tenant)
        destination_id = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, source_id, "20", "100")
        await _receive(session, tenant, item_id, source_id, "50", "120")
        before = Decimal("20") * 100 + Decimal("50") * 120

        service = inv.StockTransferService(session, tenant)
        created = await service.create(
            StockTransferCreate(
                from_warehouse_id=source_id,
                to_warehouse_id=destination_id,
                reason="works order relocation",
                lines=[StockTransferLineCreate(item_id=item_id, quantity=Decimal("30"))],
            )
        )
        await service.approve(created.id)
        await service.dispatch(created.id)

        source = await inv.get_balance(session, tenant, item_id, source_id)
        destination = await inv.get_balance(session, tenant, item_id, destination_id)
        assert source is not None
        assert destination is not None
        # In transit: the source has given the value up and the destination has
        # not booked it yet. That is the same as it was under weighted average.
        assert source.stock_value == Decimal("4800.0000")
        assert destination.in_transit_quantity == Decimal("30.0000")

        await service.receive(created.id)

        source = await inv.get_balance(session, tenant, item_id, source_id)
        destination = await inv.get_balance(session, tenant, item_id, destination_id)
        assert source is not None
        assert destination is not None
        assert source.stock_value + destination.stock_value == before
        await _assert_layers_reconcile(session, tenant, destination)
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(destination.id)
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in layers] == [
            (Decimal("20.0000"), Decimal("100.000000")),
            (Decimal("10.0000"), Decimal("120.000000")),
        ]


class TestFinanceReadsTheFifoNumber:
    async def test_the_valuation_report_reports_fifo_not_an_average(self, ctx) -> None:
        """The report is what the balance sheet is read off, so it has to move too.

        Buy 100 @ 10 then 100 @ 20, issue 100. FIFO leaves 100 units worth
        2 000; a weighted average would have left them worth 1 500. The
        difference is the whole point of the change, and it has to reach the
        finance module rather than stopping at the inventory tables.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, warehouse_id, "100", "10")
        await _receive(session, tenant, item_id, warehouse_id, "100", "20")
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("100"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/FIN",
                reason="draw",
            )

        rows, summary, _ = await inventory_valuation(
            session,
            tenant,
            filters=ValuationFilters(item_id=item_id),
            params=PageParams(page=1, page_size=50),
        )
        assert len(rows) == 1
        weighted_average_answer = Decimal("1500.00")
        assert rows[0].value == Decimal("2000.00")
        assert rows[0].value != weighted_average_answer
        assert rows[0].average_cost == Decimal("20.000000")
        assert summary.total_value == Decimal("2000.00")
        # The cross-check recomputed from the position's own per-unit cost.
        assert rows[0].recorded_value == Decimal("2000.00")


class TestARepairedPosition:
    async def test_stock_with_no_layers_is_given_one_at_its_recorded_cost(self, ctx) -> None:
        """The deploy-window case: on-hand moved while the layers were not there.

        Simulated by deleting the layers of a position that has stock, which is
        the state a balance row written by the previous deployment - or missed
        by the migration's backfill - would be in. The next posting must not
        refuse it (the stock is physically present) and must not value it at
        nothing (that would under-value the balance sheet until the position
        empties). It opens one layer for the gap at the position's own recorded
        ``average_cost``, which invents nothing: the balance already claimed
        exactly that.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "7")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        await session.execute(
            text("DELETE FROM inventory_cost_layers WHERE balance_id = :balance"),
            {"balance": balance.id},
        )

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("30"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/REPAIR",
                reason="draw against unlayered stock",
            )
        assert issue.unit_cost == Decimal("7.000000")
        assert issue.total_cost == Decimal("210.0000")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("490.0000")
        await _assert_layers_reconcile(session, tenant, balance)

        repaired = (
            (
                await session.execute(
                    select(InventoryCostLayer).where(
                        InventoryCostLayer.balance_id == balance.id,
                        InventoryCostLayer.origin == LAYER_ORIGIN_RECONCILIATION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(repaired) == 1
        assert repaired[0].original_quantity == Decimal("100.0000")
        assert repaired[0].source_transaction_id is None, (
            "a repair is not a movement and must not claim one"
        )

        audited = (
            (await session.execute(select(AuditLog).where(AuditLog.entity_id == balance.id)))
            .scalars()
            .all()
        )
        assert audited, "a change to a position's valuation basis must be audited"

    async def test_a_repair_layer_is_consumed_before_the_stock_that_came_after_it(
        self, ctx
    ) -> None:
        """Untracked stock arrived before anything the system can account for.

        The repair layer's timestamp is set a microsecond before the oldest
        layer at the position for exactly this reason: the FIFO key breaks a tie
        on the primary key, and a repair layer's UUIDv7 id is the newest in the
        system, so an equal timestamp would sort it last.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "50", "6")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # Leave the layers in place but understate them, so the repair covers
        # only part of the position and has to queue against a real layer.
        await session.execute(
            text(
                "UPDATE inventory_cost_layers SET remaining_quantity = 20, "
                "original_quantity = 20 WHERE balance_id = :balance"
            ),
            {"balance": balance.id},
        )

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/REPAIR2",
                reason="draw",
            )
        # Both layers happen to cost 6 here, so the ordering is asserted on
        # which layer was drawn down rather than on the price.
        assert issue.total_cost == Decimal("60.0000")
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(balance.id)
        assert layers[0].origin == LAYER_ORIGIN_RECONCILIATION
        assert layers[0].remaining_quantity == Decimal("20.0000"), (
            "the repair layer should have been consumed first"
        )
        assert layers[1].remaining_quantity == Decimal("20.0000")


class TestADocumentIsCostedTheWayItPosts:
    """The reproduction that let a 30 000 write-off through a 25 000 gate.

    Position: 100 received at 500, then 900 at 10. On hand 1 000,
    ``stock_value`` 59 000, ``average_cost`` 59.

    A write-off of 60 units is worth 3 540 at the average and 30 000 against the
    layers it actually consumes, because FIFO takes the oldest - the hundred
    that cost 500 each. While the document priced itself from ``average_cost``
    it showed 3 540, decided it was below the 25 000 threshold, and took the
    DRAFT -> POSTED edge with no approver at all, while the engine charged the
    ledger 30 000. The gate was measured with one ruler and the write-off booked
    with another.

    What is asserted here is the rule, not the arithmetic: the number a document
    shows, the number its approval threshold is measured against and the number
    the ledger posts are one number.
    """

    async def _dear_then_cheap(self, session, tenant) -> tuple[uuid.UUID, uuid.UUID]:
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "500")
        await _receive(session, tenant, item_id, warehouse_id, "900", "10")
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1000.0000")
        assert balance.stock_value == Decimal("59000.0000")
        assert balance.average_cost == Decimal("59.000000")
        return item_id, warehouse_id

    async def test_a_write_off_over_the_threshold_cannot_post_unapproved(self, ctx) -> None:
        session, tenant = ctx
        item_id, warehouse_id = await self._dear_then_cheap(session, tenant)

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.DAMAGE,
                reason="water damage in the old bay",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-60"))],
            )
        )
        # 60 units off the layers they will really consume: 60 @ 500.
        assert adjustment.total_value == Decimal("30000.0000"), (
            "the document is still priced from the average, not from the layers"
        )
        assert adjustment.total_value > inv.ADJUSTMENT_APPROVAL_VALUE_THRESHOLD
        assert adjustment.requires_approval is True

        adjustment_id = adjustment.id
        with pytest.raises(BusinessRuleError) as excinfo:
            await service.post(adjustment_id)
        assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED

        # And nothing moved.
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1000.0000")
        assert balance.stock_value == Decimal("59000.0000")
        still_draft = await service.get_adjustment(adjustment_id)
        assert still_draft.status is AdjustmentStatus.DRAFT

    async def test_the_posted_document_states_what_the_ledger_charged(self, ctx) -> None:
        session, tenant = ctx
        item_id, warehouse_id = await self._dear_then_cheap(session, tenant)
        approver = await _second_principal(session, tenant)

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.DAMAGE,
                reason="water damage in the old bay",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-60"))],
            )
        )
        adjustment_id = adjustment.id
        await service.submit(adjustment_id)
        await inv.StockAdjustmentService(session, approver).approve(adjustment_id, "seen")
        posted = await service.post(adjustment_id)

        assert posted.status is AdjustmentStatus.POSTED
        assert posted.total_value == Decimal("30000.0000")
        assert posted.lines[0].unit_cost == Decimal("500.000000")
        assert posted.lines[0].total_cost == Decimal("30000.0000")

        movement = (
            (
                await session.execute(
                    select(InventoryTransaction).where(
                        InventoryTransaction.reference_id == adjustment_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert Decimal(movement.total_cost) == posted.total_value, (
            "the document and the ledger row disagree about what the write-off cost"
        )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("29000.0000")
        await _assert_layers_reconcile(session, tenant, balance)

    async def test_a_draft_that_grew_past_the_threshold_is_refused_at_posting(self, ctx) -> None:
        """The preview is not a lock, so the post-time re-check is the real gate.

        The document is drafted while the position is cheap, so it prices itself
        below the threshold and is legitimately postable. Before it posts, the
        cheap layer is issued away and a dear one arrives - so the write-off the
        engine will actually charge is over the threshold. Trusting the draft
        would put the whole hole back.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "10")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.DAMAGE,
                reason="dropped",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-100"))],
            )
        )
        adjustment_id = adjustment.id
        assert adjustment.total_value == Decimal("1000.0000")
        assert adjustment.requires_approval is False

        # The cheap stock leaves and dear stock replaces it.
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("100"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/DRIFT",
                reason="consumed while the draft sat",
            )
        await _receive(session, tenant, item_id, warehouse_id, "100", "400")

        with pytest.raises(BusinessRuleError) as excinfo:
            await service.post(adjustment_id)
        assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("100.0000"), (
            "a refused posting must leave the stock alone"
        )

        # And the refusal is actionable: submitting re-prices the document, so
        # it can be sent for the approval it turned out to need.
        resubmitted = await service.submit(adjustment_id)
        assert resubmitted.total_value == Decimal("40000.0000")
        assert resubmitted.requires_approval is True

    async def test_an_approved_document_posts_whatever_it_turns_out_to_cost(self, ctx) -> None:
        """The other half of the rule, and the reason the re-check is one-sided.

        A person looked at this document and signed it. If stock moves after
        that, refusing to post would leave an approved document permanently
        unpostable through no fault of anyone who touched it. It posts, and the
        value it actually booked is recorded on it.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        approver = await _second_principal(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "300")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.THEFT,
                reason="missing after audit",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-100"))],
            )
        )
        adjustment_id = adjustment.id
        assert adjustment.total_value == Decimal("30000.0000")
        await service.submit(adjustment_id)
        await inv.StockAdjustmentService(session, approver).approve(adjustment_id, "seen")

        # Dearer stock arrives and the cheap layer is drawn down, so the
        # write-off now costs more than what was approved.
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("50"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/AFTER",
                reason="issued after approval",
            )
        await _receive(session, tenant, item_id, warehouse_id, "50", "900")

        posted = await service.post(adjustment_id)
        assert posted.status is AdjustmentStatus.POSTED
        # 50 @ 300 + 50 @ 900.
        assert posted.total_value == Decimal("60000.0000")


class TestACountReportsWhatItPosted:
    """The same break on the count sheet, and the same rule.

    The sheet stamps each line with the position's cost basis when it is cut.
    Multiplying a shortage by that figure reports the blended price of stock
    that is on the shelf, not the price of the units that are missing - which
    under FIFO are the oldest and dearest. On 100 @ 500 then 900 @ 10, a sheet
    60 short reported 3 540 of shrinkage and the ledger booked 30 000 of it.
    """

    async def _counted_sheet(self, session, tenant, counted: str):
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "500")
        await _receive(session, tenant, item_id, warehouse_id, "900", "10")

        service = inv.PhysicalCountService(session, tenant)
        count = await service.create(
            PhysicalCountCreate(warehouse_id=warehouse_id, reason="quarterly stock take")
        )
        await service.start(count.id)
        lines, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        await service.enter_counts(
            count.id,
            PhysicalCountEntryRequest(
                entries=[PhysicalCountEntry(line_id=lines[0].id, counted_quantity=Decimal(counted))]
            ),
        )
        return service, count, item_id, warehouse_id

    async def test_the_reported_variance_is_the_variance_the_ledger_booked(self, ctx) -> None:
        session, tenant = ctx
        service, count, item_id, warehouse_id = await self._counted_sheet(session, tenant, "940")

        entered, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        # Even before confirming, the sheet says what the shortage will cost.
        assert entered[0].variance_quantity == Decimal("-60.0000")
        assert entered[0].variance_value == Decimal("-30000.0000")

        confirmed = await service.confirm(count.id)
        assert confirmed.status is PhysicalCountStatus.CONFIRMED
        assert confirmed.variance_line_count == 1
        assert confirmed.variance_value == Decimal("-30000.0000")

        posted, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        assert posted[0].variance_value == Decimal("-30000.0000")

        movement = (
            (
                await session.execute(
                    select(InventoryTransaction).where(
                        InventoryTransaction.reference_id == count.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert -Decimal(movement.total_cost) == confirmed.variance_value, (
            "the count sheet and the ledger disagree about the shrinkage"
        )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("940.0000")
        assert balance.stock_value == Decimal("29000.0000")
        await _assert_layers_reconcile(session, tenant, balance)

    async def test_a_count_that_finds_stock_books_it_at_the_sheets_cost(self, ctx) -> None:
        """The other direction: found stock is an arrival and is re-booked.

        It is priced at the position's cost basis, which is what the engine does
        with an arrival that names a cost - so here, and only here, the sheet's
        ``unit_cost`` really is the price. The value reported is still read back
        off the posting rather than recomputed beside it.
        """
        session, tenant = ctx
        service, count, item_id, warehouse_id = await self._counted_sheet(session, tenant, "1010")

        confirmed = await service.confirm(count.id)
        assert confirmed.variance_value == Decimal("590.0000")  # 10 @ 59

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1010.0000")
        assert balance.stock_value == Decimal("59590.0000")
        await _assert_layers_reconcile(session, tenant, balance)


class TestLayersOverThePosition:
    """Reconciliation in the direction the previous round left open.

    The deploy-window case run backwards: the layers were built, then a movement
    posted by the old deployment took stock out without touching them. The
    layers now account for stock the position does not hold, and because the
    engine recomputes ``stock_value`` from the layers, the very next ordinary
    posting *raised* the position's value back to what the layers claimed.
    """

    async def test_layers_claiming_more_than_the_position_holds_are_trimmed(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "600", "150")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("90000.0000")

        # The previous deployment issues 100 without touching a layer.
        await session.execute(
            text(
                "UPDATE inventory_balances SET on_hand_quantity = 500, "
                "stock_value = 75000, average_cost = 150 WHERE id = :balance"
            ),
            {"balance": balance.id},
        )
        session.expire_all()

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/EXCESS",
                reason="one unit, after the rollout window",
            )
        assert issue.total_cost == Decimal("150.0000")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("499.0000")
        assert balance.stock_value == Decimal("74850.0000"), (
            "one unit issued re-inflated the position to what the layers claimed"
        )
        await _assert_layers_reconcile(session, tenant, balance)

    async def test_the_trim_is_audited_against_the_balance(self, ctx) -> None:
        """Visible, in the durable place. A WARNING in a log is not the record."""
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "600", "150")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        balance_id = balance.id
        await session.execute(
            text(
                "UPDATE inventory_balances SET on_hand_quantity = 500, "
                "stock_value = 75000 WHERE id = :balance"
            ),
            {"balance": balance_id},
        )
        session.expire_all()

        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/EXCESS2",
                reason="one unit",
            )

        audited = (
            (await session.execute(select(AuditLog).where(AuditLog.entity_id == balance_id)))
            .scalars()
            .all()
        )
        trims = [
            row
            for row in audited
            if (row.context or {}).get("direction") == inv.LAYER_RECONCILIATION_EXCESS
        ]
        assert len(trims) == 1, "trimming a position's layers must leave a trace"
        assert Decimal(str(trims[0].context["quantity"])) == Decimal("100.0000")
        assert Decimal(str(trims[0].context["value_change"])) == Decimal("-15000.0000")

    async def test_the_excess_is_taken_from_the_oldest_layers(self, ctx) -> None:
        """A replay of the movement the layers missed, in the order it took.

        The missed movement took stock out, and FIFO says it took the oldest.
        Trimming the newest instead would leave the position holding the wrong
        prices, and the next issue would be costed from them.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "10", "5")
        await _receive(session, tenant, item_id, warehouse_id, "10", "50")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # The old deployment issues the whole cheap lot, layers untouched.
        await session.execute(
            text(
                "UPDATE inventory_balances SET on_hand_quantity = 10, "
                "stock_value = 500, average_cost = 50 WHERE id = :balance"
            ),
            {"balance": balance.id},
        )
        session.expire_all()

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/OLDEST",
                reason="draw",
            )
        # The cheap layer is gone, so this unit costs 50, not 5.
        assert issue.unit_cost == Decimal("50.000000")
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(balance.id)
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in layers] == [
            (Decimal("9.0000"), Decimal("50.000000"))
        ]


class TestStockValuePrecision:
    """``stock_value`` is rounded once, at the scale its column stores.

    ``Money`` is ``NUMERIC(18, 4)``. It used to be quantized with ``to_money``,
    which rounds to two places - the scale a *document total* is presented at -
    so two of the four places the column was holding were thrown away, once per
    position. Splitting one item's stock across two warehouses then changed what
    the company was worth.
    """

    async def test_the_position_keeps_the_four_places_its_column_holds(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        # 3 @ 1.111111 = 3.333333, which is 3.3333 at four places and 3.33 at two.
        await _receive(session, tenant, item_id, warehouse_id, "3", "1.111111")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("3.3333")
        await _assert_layers_reconcile(session, tenant, balance)

    async def test_splitting_a_position_across_warehouses_keeps_the_total(self, ctx) -> None:
        """Two warehouses, one item, the same stock. Same company value.

        Not exact - four places cannot hold the product of a four-place quantity
        and a six-place price, so a fraction survives - but the residual is a
        rounding at the column's own scale rather than a rounding two places
        coarser, and the test states the size it may be.
        """
        session, tenant = ctx
        item_id = await _item(session, tenant)
        one = await _warehouse(session, tenant)
        two = await _warehouse(session, tenant)

        await _receive(session, tenant, item_id, one, "7", "3.333333")
        await _receive(session, tenant, item_id, two, "7", "3.333333")

        first = await inv.get_balance(session, tenant, item_id, one)
        second = await inv.get_balance(session, tenant, item_id, two)
        assert first is not None
        assert second is not None
        exact = Decimal("14") * Decimal("3.333333")
        split = Decimal(first.stock_value) + Decimal(second.stock_value)
        assert abs(split - exact) <= Decimal("0.0001"), (
            f"splitting the position moved {split - exact} of company value"
        )


class TestTheValuationReportReconcilesIndependently:
    """The cross-check has to reach the layers, or it checks nothing.

    ``recorded_value`` used to be ``sum(on_hand x average_cost)``, and
    ``average_cost`` is ``stock_value / on_hand`` written by the same statement -
    so the report was comparing ``sum(stock_value)`` with itself. It could not
    detect the one thing it exists to detect.
    """

    async def test_a_balance_that_drifted_from_its_layers_shows_a_difference(self, ctx) -> None:
        session, tenant = ctx
        item_id = await _item(session, tenant)
        warehouse_id = await _warehouse(session, tenant)
        await _receive(session, tenant, item_id, warehouse_id, "100", "10")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # Something that is not the engine writes the balance. Under the old
        # cross-check ``average_cost`` moved with it and the report reconciled.
        await session.execute(
            text(
                "UPDATE inventory_balances SET stock_value = 4000, average_cost = 40 "
                "WHERE id = :balance"
            ),
            {"balance": balance.id},
        )

        rows, _, _ = await inventory_valuation(
            session,
            tenant,
            filters=ValuationFilters(item_id=item_id),
            params=PageParams(page=1, page_size=50),
        )
        assert len(rows) == 1
        assert rows[0].value == Decimal("4000.00")
        assert rows[0].recorded_value == Decimal("1000.00"), (
            "the reconciliation is still derived from the balance it is checking"
        )
