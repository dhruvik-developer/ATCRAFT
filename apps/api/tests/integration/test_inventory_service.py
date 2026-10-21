"""Service-level tests for the inventory engine against a real PostgreSQL.

Everything here needs a real database, because what is under test is exactly
what SQLite cannot express: the generated ``available_quantity`` column, the
COALESCE-based unique grain, ``SELECT ... FOR UPDATE``, the check constraints
that make negative stock impossible, and the audit row written inside the same
transaction as the movement.

Isolation follows the pattern in ``test_products_service.py``: one private
schema per module, created once and dropped afterwards, with every test's writes
rolled back at the end.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
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
    ErrorCode,
    InsufficientStockError,
    InvalidStateTransitionError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    AdjustmentReason,
    AdjustmentStatus,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
    InventoryTransaction,
    PhysicalCountStatus,
    StockBatch,
    StockState,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.repository import (
    CostLayerRepository,
    InventoryRepository,
    LedgerRepository,
    StockBatchRepository,
)
from src.modules.inventory.schemas import (
    AdjustmentFilters,
    BalanceFilters,
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


async def test_batch_picker_search_pages_and_selected_lookup_keep_item_and_tenant_scope(
    ctx,
) -> None:
    session, tenant, _ = ctx
    item_id = await seed_item(session, tenant.company_id, code=_code("LOTS"))
    other_item_id = await seed_item(session, tenant.company_id, code=_code("OTHER"))
    repo = StockBatchRepository(session, tenant)
    records = [
        repo.add(
            StockBatch(
                item_id=item_id,
                batch_number=f"OLDER-{number:03}",
                received_on=date(2026, 1, 1) + timedelta(days=number),
            )
        )
        for number in range(55)
    ]
    await session.flush()
    oldest = records[0].id
    newest = records[-1].id
    service = inv.TraceabilityService(session, tenant)
    first, first_page = await service.list_batches(
        item_id, None, PageParams(limit=20, with_total=False)
    )
    third, third_page = await service.list_batches(
        item_id, None, PageParams(page=3, limit=20, with_total=False)
    )
    assert len(first) == 20 and first_page.has_next
    # No lot has an expiry date, so the pick order is receipt date: the oldest
    # is page one and the newest is page three. This is FEFO's fallback, not
    # insertion order - it changed deliberately with the tracker.
    assert oldest in {batch.id for batch in first}
    assert newest in {batch.id for batch in third} and not third_page.has_next

    searched, _ = await service.list_batches(item_id, "OLDER-000", PageParams(limit=20))
    assert [batch.id for batch in searched] == [oldest]
    selected, _ = await service.list_batches(item_id, None, PageParams(limit=1), batch_id=oldest)
    assert [batch.id for batch in selected] == [oldest]
    wrong_item, _ = await service.list_batches(
        other_item_id, None, PageParams(limit=1), batch_id=oldest
    )
    assert wrong_item == []
    foreign = inv.TraceabilityService(
        session, TenantContext(company_id=uuid.uuid4(), user_id=tenant.user_id)
    )
    wrong_tenant, _ = await foreign.list_batches(
        item_id, None, PageParams(limit=1), batch_id=oldest
    )
    assert wrong_tenant == []


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
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with a company, two users and a second tenant."""
    schema = f"it_inventory_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    approver_id = uuid.uuid4()

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
                # checkfirst=False: the shared conftest already built these
                # tables in `public`, which stays on the search path for the
                # pg_trgm operator classes.
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                await _insert_company(session, company_id, "ATC")
                await _insert_user(session, user_id, company_id, "storekeeper@atcraft.test")
                await _insert_user(session, approver_id, company_id, "manager@atcraft.test")
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

    yield schema, company_id, user_id, approver_id
    asyncio.run(teardown())


async def _insert_company(session: AsyncSession, company_id: uuid.UUID, prefix: str) -> None:
    await seed_support.seed_company(session, company_id=company_id, prefix=prefix)


async def _insert_user(
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID, email: str
) -> None:
    await seed_support.seed_user(session, company_id=company_id, user_id=user_id, email=email)


# The seeds below return ids because that is all a movement needs; the rows
# themselves are built by the owning module's model, so the Python-side defaults
# on the item master and the warehouse apply.


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    batch_tracked: bool = False,
    serial_tracked: bool = False,
    standard_cost: str = "0",
    reorder_level: str = "0",
    is_active: bool = True,
) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=code,
        gst_rate=Decimal("18"),
        standard_cost=Decimal(standard_cost),
        reorder_level=Decimal(reorder_level),
        is_batch_tracked=batch_tracked,
        is_serial_tracked=serial_tracked,
        is_active=is_active,
    )
    return item.id


async def seed_warehouse(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    allow_negative: bool = False,
) -> uuid.UUID:
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=company_id,
        code=code,
        allow_negative_stock=allow_negative,
    )
    return warehouse.id


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, TenantContext]]:
    """Session rolled back after each test, plus the raiser's and approver's scopes."""
    schema, company_id, user_id, approver_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    approver = TenantContext(company_id=company_id, user_id=approver_id)
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
                yield session, tenant, approver
            await outer.rollback()
    finally:
        await engine.dispose()


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def receive(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    unit_cost: str | None = None,
    **kwargs: object,
) -> InventoryTransaction:
    async with transaction(session):
        return await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            reference_type="goods_receipt",
            reference_id=None,
            reference_code="GRN/TEST",
            reason="test receipt",
            unit_cost=Decimal(unit_cost) if unit_cost is not None else None,
            **kwargs,  # type: ignore[arg-type]
        )


# -----------------------------------------------------------------------------
# The engine
# -----------------------------------------------------------------------------


class TestMovementPosting:
    async def test_a_receipt_writes_ledger_balance_and_audit_together(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        txn = await receive(session, tenant, item_id, warehouse_id, "100", "50")

        assert txn.quantity == Decimal("100.0000")
        assert txn.balance_after == Decimal("100.0000")
        assert txn.total_cost == Decimal("5000.00")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("100.0000")
        assert balance.available_quantity == Decimal("100.0000")
        assert balance.average_cost == Decimal("50.000000")
        assert balance.stock_value == Decimal("5000.0000")

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "inventory_transaction",
                    AuditLog.entity_id == txn.id,
                )
            )
        ).scalar_one()
        assert audit.action == AuditAction.INVENTORY_MOVEMENT
        assert audit.context["item_code"] is not None

    async def test_the_generated_available_column_tracks_its_inputs(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "10")

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("30"),
                from_state=StockState.AVAILABLE,
                to_state=StockState.QC_HOLD,
                reason="incoming inspection",
            )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # Holding stock does not move it: it is still physically here.
        assert balance.on_hand_quantity == Decimal("100.0000")
        assert balance.qc_hold_quantity == Decimal("30.0000")
        assert balance.available_quantity == Decimal("70.0000")

    async def test_the_generated_column_is_readable_straight_after_a_write(self, ctx) -> None:
        """No re-query, and no MissingGreenlet from an expired attribute.

        ``available_quantity`` is generated by PostgreSQL, so SQLAlchemy marks it
        as server-updated and would expire it after every UPDATE. Reading it
        again - which is exactly what serialising a response does - would then
        become an async lazy load and raise. ``eager_defaults`` on the mapper is
        what makes the value come back with the UPDATE itself.
        """
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        engine = inv.MovementEngine(session, tenant)
        async with transaction(session):
            _, balances = await engine.post_with_balances(
                [
                    inv.MovementRequest(
                        item_id=item_id,
                        warehouse_id=warehouse_id,
                        quantity=Decimal("12"),
                        transaction_type=TransactionType.PURCHASE_RECEIPT,
                        reference_type="goods_receipt",
                        reference_id=None,
                        reference_code="GRN/EAGER",
                        reason="eager default probe",
                        unit_cost=Decimal("2"),
                    )
                ]
            )
        balance = next(iter(balances.values()))
        assert balance.available_quantity == Decimal("12.0000")

    async def test_an_issue_is_costed_from_the_oldest_layer_not_an_average(self, ctx) -> None:
        """The case that separates FIFO from the weighted average it replaced.

        This test used to assert the opposite - the issue costed at 60, the
        average unmoved - and that behaviour is deliberately gone. Buy 100 @ 50
        and then 100 @ 70 and the position holds two layers, not 200 units at
        60. Issuing 50 empties half of the *first* layer, so it costs 50 a unit
        and 2 500 in total; a weighted average would have charged 60 and 3 000.
        What is left is the dearer stock - 50 @ 50 plus 100 @ 70 = 9 500 - where
        a weighted average would have said 9 000.

        ``average_cost`` still reads 60 before the issue, because with 100 units
        at each price the two methods agree at that point. It is a *derived*
        figure now: after the issue it is 9 500 / 150 = 63.333333, which is the
        blend of what is actually on the shelf, not a number the engine carried
        forward.
        """
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        await receive(session, tenant, item_id, warehouse_id, "100", "50")
        await receive(session, tenant, item_id, warehouse_id, "100", "70")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.average_cost == Decimal("60.000000")
        assert balance.stock_value == Decimal("12000.0000")

        async with transaction(session):
            issue = await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("50"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/1",
                reason="production draw",
            )
        assert issue.unit_cost == Decimal("50.000000"), "the issue was not costed FIFO"
        assert issue.total_cost == Decimal("2500.0000")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.stock_value == Decimal("9500.0000")
        assert balance.average_cost == Decimal("63.333333")

    async def test_the_layers_left_behind_are_the_dear_ones(self, ctx) -> None:
        """Partial consumption, exhaustion, and what the layer table looks like after."""
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        await receive(session, tenant, item_id, warehouse_id, "20", "100")
        await receive(session, tenant, item_id, warehouse_id, "50", "120")

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
                reference_code="MI/PARTIAL",
                reason="draw across two layers",
            )
        # 20 * 100 + 10 * 120 = 3 200, blended to 106.666667 on the ledger row.
        assert issue.total_cost == Decimal("3200.0000")
        assert issue.unit_cost == Decimal("106.666667")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(balance.id)
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in layers] == [
            (Decimal("40.0000"), Decimal("120.000000"))
        ], "the exhausted layer should be closed and the dear one left open"

        exhausted = (
            (
                await session.execute(
                    select(InventoryCostLayer).where(
                        InventoryCostLayer.balance_id == balance.id,
                        InventoryCostLayer.remaining_quantity == 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(exhausted) == 1, "an emptied layer is exhausted, not deleted"
        assert exhausted[0].original_quantity == Decimal("20.0000")

        drawn = (
            (
                await session.execute(
                    select(InventoryCostLayerConsumption)
                    .where(InventoryCostLayerConsumption.transaction_id == issue.id)
                    .order_by(
                        InventoryCostLayerConsumption.created_at,
                        InventoryCostLayerConsumption.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(row.quantity, row.unit_cost) for row in drawn] == [
            (Decimal("20.0000"), Decimal("100.000000")),
            (Decimal("10.0000"), Decimal("120.000000")),
        ]
        assert sum(row.total_cost for row in drawn) == Decimal("3200.0000")

    async def test_a_receipt_keyed_late_is_consumed_in_arrival_order(self, ctx) -> None:
        """FIFO follows business time, not the order the documents were keyed.

        A receipt entered on Friday for goods that arrived on Monday sits *in
        front* of the ones already booked. Ordering by the system clock - or by
        ``created_at``, which is transaction start time and identical for every
        row of one document - would consume the wrong end of the queue.
        """
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        recent = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
        backdated = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        async with transaction(session):
            await inv.post_movements(
                session,
                tenant,
                [
                    inv.MovementRequest(
                        item_id=item_id,
                        warehouse_id=warehouse_id,
                        quantity=Decimal("10"),
                        transaction_type=TransactionType.PURCHASE_RECEIPT,
                        reference_type="goods_receipt",
                        reference_id=None,
                        reference_code="GRN/LATE-KEYED",
                        reason="keyed first, arrived second",
                        unit_cost=Decimal("90"),
                        occurred_at=recent,
                    ),
                    inv.MovementRequest(
                        item_id=item_id,
                        warehouse_id=warehouse_id,
                        quantity=Decimal("10"),
                        transaction_type=TransactionType.PURCHASE_RECEIPT,
                        reference_type="goods_receipt",
                        reference_id=None,
                        reference_code="GRN/BACKDATED",
                        reason="keyed second, arrived first",
                        unit_cost=Decimal("40"),
                        occurred_at=backdated,
                    ),
                ],
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
                reference_code="MI/BACKDATE",
                reason="draw",
            )
        assert issue.unit_cost == Decimal("40.000000"), (
            "the layer that arrived first should have been consumed first"
        )

    async def test_the_ledger_reconciles_to_the_balance(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        await receive(session, tenant, item_id, warehouse_id, "100", "10")
        await receive(session, tenant, item_id, warehouse_id, "25", "10")
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("40"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/2",
                reason="draw",
            )
            # A hold and a release change availability but not on-hand, so the
            # reconciliation identity has to exclude them.
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                from_state=StockState.AVAILABLE,
                to_state=StockState.QC_HOLD,
                reason="sample check",
            )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        posted = await LedgerRepository(session, tenant).posted_quantity(
            item_id=item_id, warehouse_id=warehouse_id
        )
        assert balance is not None
        assert posted == balance.on_hand_quantity == Decimal("85.0000")
        assert balance.available_quantity == Decimal("75.0000")

    async def test_an_over_issue_is_refused_and_writes_nothing(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "10", "5")

        with pytest.raises(InsufficientStockError) as excinfo:
            async with transaction(session):
                await inv.post_movement(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("11"),
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    reference_type="material_issue",
                    reference_id=None,
                    reference_code="MI/3",
                    reason="too much",
                )
        assert excinfo.value.code is ErrorCode.INSUFFICIENT_AVAILABLE_STOCK
        assert excinfo.value.details["requested"] == "11.0000"
        assert excinfo.value.details["available"] == "10.0000"

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("10.0000")
        ledger_rows = (
            await session.execute(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(InventoryTransaction.item_id == item_id)
            )
        ).scalar_one()
        assert ledger_rows == 1

    async def test_a_multi_line_posting_is_all_or_nothing(self, ctx) -> None:
        session, tenant, _ = ctx
        good_item = await seed_item(session, tenant.company_id, code=_code("RM"))
        short_item = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, good_item, warehouse_id, "50", "5")
        await receive(session, tenant, short_item, warehouse_id, "1", "5")

        movements = [
            inv.MovementRequest(
                item_id=good_item,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/BATCH",
                reason="kit issue",
            ),
            inv.MovementRequest(
                item_id=short_item,
                warehouse_id=warehouse_id,
                quantity=Decimal("10"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/BATCH",
                reason="kit issue",
            ),
        ]
        with pytest.raises(InsufficientStockError):
            async with transaction(session):
                await inv.post_movements(session, tenant, movements)

        # The first line must not have survived the second line's failure.
        good_balance = await inv.get_balance(session, tenant, good_item, warehouse_id)
        assert good_balance is not None
        assert good_balance.on_hand_quantity == Decimal("50.0000")

    async def test_negative_stock_is_refused_even_where_the_warehouse_allows_it(self, ctx) -> None:
        """The flag relaxes the availability pre-flight, never the database floor."""
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(
            session, tenant.company_id, code=_code("WIP"), allow_negative=True
        )
        await receive(session, tenant, item_id, warehouse_id, "5", "1")

        with pytest.raises(InsufficientStockError):
            async with transaction(session):
                await inv.post_movement(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("6"),
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    reference_type="material_issue",
                    reference_id=None,
                    reference_code="MI/4",
                    reason="line-side draw",
                )

    async def test_allow_negative_stock_permits_issuing_reserved_free_stock_early(
        self, ctx
    ) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(
            session, tenant.company_id, code=_code("WIP"), allow_negative=True
        )
        await receive(session, tenant, item_id, warehouse_id, "10", "1")
        # Put half of it on QC hold, so only 5 is nominally available.
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("5"),
                from_state=StockState.AVAILABLE,
                to_state=StockState.QC_HOLD,
                reason="hold",
            )
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("8"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/5",
                reason="line-side draw ahead of the release",
            )
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("2.0000")

    async def test_a_batch_tracked_item_cannot_move_without_its_batch(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("BT"), batch_tracked=True)
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        with pytest.raises(BusinessRuleError) as excinfo:
            await receive(session, tenant, item_id, warehouse_id, "10", "1")
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED

        batch = await inv.TraceabilityService(session, tenant).create_batch(
            StockBatchCreate(item_id=item_id, batch_number="L-2026-07", unit_cost=Decimal("1"))
        )
        await receive(session, tenant, item_id, warehouse_id, "10", "1", batch_id=batch.id)

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id, batch_id=batch.id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("10.0000")
        # Batch-less position for the same item/warehouse stays absent: the two
        # are different grains, not the same row.
        assert await inv.get_balance(session, tenant, item_id, warehouse_id) is None

    async def test_two_movements_at_the_same_null_grain_share_one_row(self, ctx) -> None:
        """The COALESCE unique index is what stops a split stock position."""
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "10", "1")
        await receive(session, tenant, item_id, warehouse_id, "15", "1")

        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM inventory_balances "
                    "WHERE item_id = :item AND warehouse_id = :wh"
                ),
                {"item": item_id, "wh": warehouse_id},
            )
        ).scalar_one()
        assert rows == 1

    async def test_availability_sums_across_warehouses(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        first = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        second = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, first, "40", "1")
        await receive(session, tenant, item_id, second, "60", "1")

        assert await inv.get_available(session, tenant, item_id) == Decimal("100.0000")
        assert await inv.get_available(session, tenant, item_id, first) == Decimal("40.0000")

    async def test_another_tenants_stock_is_invisible(self, ctx) -> None:
        session, tenant, _ = ctx
        other_company = uuid.uuid4()
        await _insert_company(session, other_company, "OTH")
        other_user = uuid.uuid4()
        await _insert_user(
            session, other_user, other_company, f"other-{uuid.uuid4().hex[:6]}@atcraft.test"
        )
        other_tenant = TenantContext(company_id=other_company, user_id=other_user)

        item_id = await seed_item(session, other_company, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, other_company, code=_code("WH"))
        await receive(session, other_tenant, item_id, warehouse_id, "99", "1")

        assert await inv.get_balance(session, tenant, item_id, warehouse_id) is None
        assert await inv.get_available(session, tenant, item_id) == Decimal("0")


class TestBalanceQueries:
    async def test_below_reorder_only_matches_items_with_a_level_set(self, ctx) -> None:
        session, tenant, _ = ctx
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        short = await seed_item(session, tenant.company_id, code=_code("SHORT"), reorder_level="50")
        plenty = await seed_item(
            session, tenant.company_id, code=_code("PLENTY"), reorder_level="5"
        )
        unmanaged = await seed_item(session, tenant.company_id, code=_code("NOLVL"))
        await receive(session, tenant, short, warehouse_id, "10", "1")
        await receive(session, tenant, plenty, warehouse_id, "500", "1")
        await receive(session, tenant, unmanaged, warehouse_id, "1", "1")

        rows, _ = await inv.InventoryQueryService(session, tenant).list_balances(
            BalanceFilters(warehouse_id=warehouse_id, below_reorder=True),
            PageParams(page=1, limit=50),
            SortParams(),
        )
        item_ids = {row.item_id for row in rows}
        assert short in item_ids
        assert plenty not in item_ids
        assert unmanaged not in item_ids

    async def test_the_item_summary_totals_cover_every_warehouse(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        first = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        second = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, first, "40", "10")
        await receive(session, tenant, item_id, second, "60", "20")

        summary, pagination = await inv.InventoryQueryService(session, tenant).item_summary(
            item_id, PageParams(page=1, limit=50)
        )
        assert summary.total_on_hand == Decimal("100.0000")
        assert summary.total_available == Decimal("100.0000")
        assert summary.total_value == Decimal("1600.0000")
        assert summary.total_owned_quantity == Decimal("100.0000")
        assert pagination.total == 2
        assert len(summary.warehouses) == 2


# -----------------------------------------------------------------------------
# Stock transfer document
# -----------------------------------------------------------------------------


class TestStockTransfer:
    async def test_the_full_lifecycle_moves_stock_through_in_transit(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        source = await seed_warehouse(session, tenant.company_id, code=_code("SRC"))
        destination = await seed_warehouse(session, tenant.company_id, code=_code("DST"))
        await receive(session, tenant, item_id, source, "100", "40")

        service = inv.StockTransferService(session, tenant)
        transfer = await service.create(
            StockTransferCreate(
                from_warehouse_id=source,
                to_warehouse_id=destination,
                reason="rebalance the stores",
                lines=[StockTransferLineCreate(item_id=item_id, quantity=Decimal("30"))],
            )
        )
        assert transfer.status is TransferStatus.DRAFT
        assert transfer.code.startswith("ST/")

        await service.approve(transfer.id)
        dispatched = await service.dispatch(transfer.id)
        assert dispatched.status is TransferStatus.IN_TRANSIT

        source_balance = await inv.get_balance(session, tenant, item_id, source)
        destination_balance = await inv.get_balance(session, tenant, item_id, destination)
        assert source_balance is not None
        assert source_balance.on_hand_quantity == Decimal("70.0000")
        # In transit is company stock: absent from both on-hand figures, but
        # visible at the destination so nobody re-orders it.
        assert destination_balance is not None
        assert destination_balance.on_hand_quantity == Decimal("0.0000")
        assert destination_balance.in_transit_quantity == Decimal("30.0000")

        received = await service.receive(transfer.id)
        assert received.status is TransferStatus.RECEIVED

        destination_balance = await inv.get_balance(session, tenant, item_id, destination)
        assert destination_balance is not None
        assert destination_balance.on_hand_quantity == Decimal("30.0000")
        assert destination_balance.in_transit_quantity == Decimal("0.0000")
        # The stock carried its value across: total company value is unchanged.
        assert destination_balance.average_cost == Decimal("40.000000")

    async def test_a_transfer_cannot_be_dispatched_without_approval(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        source = await seed_warehouse(session, tenant.company_id, code=_code("SRC"))
        destination = await seed_warehouse(session, tenant.company_id, code=_code("DST"))
        await receive(session, tenant, item_id, source, "10", "1")

        service = inv.StockTransferService(session, tenant)
        transfer = await service.create(
            StockTransferCreate(
                from_warehouse_id=source,
                to_warehouse_id=destination,
                reason="premature dispatch",
                lines=[StockTransferLineCreate(item_id=item_id, quantity=Decimal("5"))],
            )
        )
        with pytest.raises(InvalidStateTransitionError):
            await service.dispatch(transfer.id)

    async def test_dispatching_more_than_is_on_hand_is_refused(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        source = await seed_warehouse(session, tenant.company_id, code=_code("SRC"))
        destination = await seed_warehouse(session, tenant.company_id, code=_code("DST"))
        await receive(session, tenant, item_id, source, "5", "1")

        service = inv.StockTransferService(session, tenant)
        transfer = await service.create(
            StockTransferCreate(
                from_warehouse_id=source,
                to_warehouse_id=destination,
                reason="over-transfer",
                lines=[StockTransferLineCreate(item_id=item_id, quantity=Decimal("50"))],
            )
        )
        await service.approve(transfer.id)
        transfer_id = transfer.id
        with pytest.raises(InsufficientStockError):
            await service.dispatch(transfer_id)

        still_draftish = await service.get_transfer(transfer_id)
        assert still_draftish.status is TransferStatus.APPROVED
        source_balance = await inv.get_balance(session, tenant, item_id, source)
        assert source_balance is not None
        assert source_balance.on_hand_quantity == Decimal("5.0000")

    async def test_the_direct_two_legged_transfer_carries_its_cost(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        source = await seed_warehouse(session, tenant.company_id, code=_code("SRC"))
        destination = await seed_warehouse(session, tenant.company_id, code=_code("DST"))
        await receive(session, tenant, item_id, source, "100", "12.5")

        async with transaction(session):
            out_leg, in_leg = await inv.transfer_stock(
                session,
                tenant,
                item_id=item_id,
                from_warehouse_id=source,
                to_warehouse_id=destination,
                quantity=Decimal("20"),
                reference_code="ST/DIRECT",
                reason="bay move",
            )
        assert out_leg.quantity == Decimal("-20.0000")
        assert in_leg.quantity == Decimal("20.0000")
        assert in_leg.unit_cost == Decimal("12.500000")

        destination_balance = await inv.get_balance(session, tenant, item_id, destination)
        assert destination_balance is not None
        assert destination_balance.stock_value == Decimal("250.0000")


# -----------------------------------------------------------------------------
# Stock adjustment document
# -----------------------------------------------------------------------------


class TestStockAdjustment:
    async def test_a_small_adjustment_posts_directly(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "10")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.DAMAGE,
                reason="crushed in handling",
                lines=[
                    StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-5")),
                ],
            )
        )
        assert adjustment.requires_approval is False
        assert adjustment.total_value == Decimal("50.0000")

        posted = await service.post(adjustment.id)
        assert posted.status is AdjustmentStatus.POSTED

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("95.0000")

        audit = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "stock_adjustment",
                        AuditLog.entity_id == adjustment.id,
                        AuditLog.action == AuditAction.STOCK_ADJUSTMENT,
                        AuditLog.new_values.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        # Both the draft and the posting are audited as STOCK_ADJUSTMENT.
        assert len(audit) >= 1

    async def test_the_list_serialises_a_row_whose_lines_are_not_loaded(self, ctx) -> None:
        """The list endpoint answered 500 for every row it found.

        `StockAdjustmentRead` declares a `lines` field, so
        `model_validate(orm_row)` read the relationship - which the model
        declares `lazy="raise"` - and the assignment that cleared it afterwards
        came too late. The endpoint was clean only while the table was empty,
        which is every automated run: the demo data raises no adjustments. It
        broke the moment a person raised one.
        """
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "10")

        service = inv.StockAdjustmentService(session, tenant)
        await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.CORRECTION,
                reason="found on the shelf after the cycle count",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("5"))],
            )
        )
        # Expire everything, so the rows the list loads are fresh instances with
        # the relationship genuinely unloaded - as they are in a real request.
        session.expunge_all()

        rows, pagination = await service.list_adjustments(
            AdjustmentFilters(), PageParams(page=1, limit=50), SortParams()
        )

        assert pagination.total is not None and pagination.total >= 1
        found = [row for row in rows if row.warehouse_id == warehouse_id]
        assert len(found) == 1
        # A list row carries no lines; the detail endpoint is what fills them.
        assert found[0].lines == []
        assert found[0].reason == "found on the shelf after the cycle count"

    async def test_a_large_adjustment_cannot_post_without_approval(self, ctx) -> None:
        session, tenant, approver = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "1000", "100")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.THEFT,
                reason="stock missing after audit",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-500"))],
            )
        )
        assert adjustment.total_value == Decimal("50000.0000")
        assert adjustment.total_value > inv.ADJUSTMENT_APPROVAL_VALUE_THRESHOLD
        assert adjustment.requires_approval is True

        adjustment_id = adjustment.id
        with pytest.raises(BusinessRuleError) as excinfo:
            await service.post(adjustment_id)
        assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED

        await service.submit(adjustment_id)
        # Segregation of duties: the raiser may not sign off their own document.
        with pytest.raises(BusinessRuleError, match="someone other than"):
            await service.approve(adjustment_id, "self approval")

        approver_service = inv.StockAdjustmentService(session, approver)
        approved = await approver_service.approve(adjustment_id, "verified against CCTV")
        assert approved.status is AdjustmentStatus.APPROVED
        assert approved.approved_by_id == approver.user_id

        posted = await service.post(adjustment_id)
        assert posted.status is AdjustmentStatus.POSTED
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("500.0000")

    async def test_a_write_down_is_valued_at_the_positions_average_cost(self, ctx) -> None:
        """A caller-quoted price must not be able to change the write-off value."""
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"), standard_cost="1")
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "37.5")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.EXPIRY,
                reason="past shelf life",
                lines=[
                    StockAdjustmentLineCreate(
                        item_id=item_id, quantity=Decimal("-10"), unit_cost=Decimal("0.01")
                    )
                ],
            )
        )
        assert adjustment.total_value == Decimal("375.0000")

    async def test_an_over_write_down_is_refused(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "5", "1")

        service = inv.StockAdjustmentService(session, tenant)
        adjustment = await service.create(
            StockAdjustmentCreate(
                warehouse_id=warehouse_id,
                reason_code=AdjustmentReason.CORRECTION,
                reason="keying correction",
                lines=[StockAdjustmentLineCreate(item_id=item_id, quantity=Decimal("-50"))],
            )
        )
        adjustment_id = adjustment.id
        with pytest.raises(InsufficientStockError):
            await service.post(adjustment_id)

        unchanged = await service.get_adjustment(adjustment_id)
        assert unchanged.status is AdjustmentStatus.DRAFT


# -----------------------------------------------------------------------------
# Physical count
# -----------------------------------------------------------------------------


class TestPhysicalCount:
    async def test_a_count_posts_the_variance_and_lands_on_the_counted_figure(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "10")

        service = inv.PhysicalCountService(session, tenant)
        count = await service.create(
            PhysicalCountCreate(warehouse_id=warehouse_id, reason="quarterly stock take")
        )
        assert count.status is PhysicalCountStatus.DRAFT
        assert count.line_count == 1

        await service.start(count.id)
        lines, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        assert lines[0].system_quantity == Decimal("100.0000")

        counted = await service.enter_counts(
            count.id,
            PhysicalCountEntryRequest(
                entries=[PhysicalCountEntry(line_id=lines[0].id, counted_quantity=Decimal("94"))]
            ),
        )
        assert counted.status is PhysicalCountStatus.COUNTED

        confirmed = await service.confirm(count.id)
        assert confirmed.status is PhysicalCountStatus.CONFIRMED
        assert confirmed.variance_line_count == 1
        assert confirmed.variance_value == Decimal("-60.0000")

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("94.0000")

    async def test_the_variance_is_posted_from_current_stock_not_the_snapshot(self, ctx) -> None:
        """A movement made while the count was open must not be reversed by it.

        The sheet snapshots 100. A receipt of 10 is booked before the count is
        confirmed. Posting ``counted - snapshot`` would post no variance at all
        and leave 110 on hand; posting ``counted - current`` lands on the 100
        that was actually counted.
        """
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "100", "10")

        service = inv.PhysicalCountService(session, tenant)
        count = await service.create(
            PhysicalCountCreate(warehouse_id=warehouse_id, reason="cycle count")
        )
        await service.start(count.id)
        lines, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        await service.enter_counts(
            count.id,
            PhysicalCountEntryRequest(
                entries=[PhysicalCountEntry(line_id=lines[0].id, counted_quantity=Decimal("100"))]
            ),
        )

        # Somebody books a receipt while the sheet is open.
        await receive(session, tenant, item_id, warehouse_id, "10", "10")

        confirmed = await service.confirm(count.id)
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("100.0000")
        assert confirmed.variance_line_count == 1

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "physical_count",
                    AuditLog.entity_id == count.id,
                    AuditLog.action == AuditAction.STOCK_ADJUSTMENT,
                )
            )
        ).scalar_one()
        drifted = audit.context["drifted_positions"]
        assert drifted is not None
        assert drifted[0]["snapshot_quantity"] == "100.0000"
        assert drifted[0]["current_quantity"] == "110.0000"

    async def test_a_matching_count_posts_no_movement(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "42", "1")

        service = inv.PhysicalCountService(session, tenant)
        count = await service.create(
            PhysicalCountCreate(warehouse_id=warehouse_id, reason="spot check")
        )
        await service.start(count.id)
        lines, _ = await service.list_lines(count.id, PageParams(page=1, limit=50))
        await service.enter_counts(
            count.id,
            PhysicalCountEntryRequest(
                entries=[PhysicalCountEntry(line_id=lines[0].id, counted_quantity=Decimal("42"))]
            ),
        )
        confirmed = await service.confirm(count.id)
        assert confirmed.variance_line_count == 0

        movements = (
            await session.execute(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(InventoryTransaction.item_id == item_id)
            )
        ).scalar_one()
        assert movements == 1

    async def test_two_open_counts_for_one_warehouse_are_refused(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "10", "1")

        service = inv.PhysicalCountService(session, tenant)
        await service.create(PhysicalCountCreate(warehouse_id=warehouse_id, reason="first sheet"))
        with pytest.raises(BusinessRuleError, match="already open"):
            await service.create(
                PhysicalCountCreate(warehouse_id=warehouse_id, reason="second sheet")
            )

    async def test_a_sheet_cannot_be_confirmed_before_it_is_counted(self, ctx) -> None:
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        await receive(session, tenant, item_id, warehouse_id, "10", "1")

        service = inv.PhysicalCountService(session, tenant)
        count = await service.create(
            PhysicalCountCreate(warehouse_id=warehouse_id, reason="unfinished")
        )
        with pytest.raises(InvalidStateTransitionError):
            await service.confirm(count.id)


class TestLockingPrimitive:
    async def test_locking_creates_the_positions_it_is_asked_for(self, ctx) -> None:
        """The create-if-missing upsert is what stops two first movements racing."""
        session, tenant, _ = ctx
        item_id = await seed_item(session, tenant.company_id, code=_code("RM"))
        warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))

        repo = InventoryRepository(session, tenant)
        grain = inv.MovementRequest(
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal("1"),
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            reference_type="test",
            reason="lock probe",
        ).grain

        async with transaction(session):
            first = await repo.lock_balances([grain])
            assert grain in first
            assert first[grain].on_hand_quantity == Decimal("0.0000")
            # Idempotent: locking again returns the same row, not a second one.
            second = await repo.lock_balances([grain, grain])
            assert second[grain].id == first[grain].id
