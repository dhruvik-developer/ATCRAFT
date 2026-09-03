"""Every position a document writes has to be a real position of this tenant.

The movement engine has always checked the bin a *movement* names. It did not
check the bins a document writes **without** moving anything through them - the
``extra_grains`` a stock transfer hands to ``post_with_balances`` for its
destination, whose ``in_transit_quantity`` it raises while the goods travel.
Those grains were locked, created and written without a single check, and
``StockTransferService.create`` did not look at ``to_location_id`` either,
although it arrives as raw request-body input.

The result, reproduced against PostgreSQL before this module existed: a transfer
W1 -> W2 whose line named a bin belonging to **another tenant** was approved and
dispatched without complaint. Four units left W1 and an ``inventory_balances``
row appeared at ``(company=A, warehouse=W2, location=<tenant B's bin>)`` holding
them in transit - a position no warehouse view will ever show.

And then the document could never be finished. ``receive()`` posts the inbound
leg as a real movement, so it *is* validated, and it refused the very bin the
outbound leg had accepted::

    BusinessRuleError: Storage location BIN-... is not in warehouse WB-...

``TRANSFER_FLOW`` has no edge out of ``IN_TRANSIT`` except ``RECEIVED`` - the
cancel edges are from DRAFT and APPROVED only, because stock on a vehicle cannot
be un-dispatched - so nothing could ever book those four units in or back. They
were stranded for good.

Both halves are pinned here: the bin is refused at ``create``, where it is
keyed, and refused again at ``dispatch`` for a line that got in before the check
existed. And the whole batch is checked in one query, so a two-hundred-line
transfer costs what a one-line transfer costs.

Isolation follows ``test_inventory_serials.py``: one private schema per module
with two tenants, created once and dropped afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.models import Base
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    StockTransfer,
    StockTransferLine,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.repository import BalanceGrain
from src.modules.inventory.schemas import StockTransferCreate, StockTransferLineCreate
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests import seed_support
from tests.schema_support import prepare_metadata

pytestmark = pytest.mark.integration

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
prepare_metadata()


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with two tenants, so a foreign bin is a real foreign bin."""
    schema = f"it_inv_xfer_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

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
                await seed_support.seed_company(session, company_id=company_id, prefix="XFR")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"xfer-{uuid.uuid4().hex[:8]}@atcraft.test",
                )
                await seed_support.seed_company(
                    session, company_id=other_company_id, prefix="OTH"
                )
                await seed_support.seed_user(
                    session,
                    company_id=other_company_id,
                    user_id=other_user_id,
                    email=f"other-{uuid.uuid4().hex[:8]}@atcraft.test",
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
        pytest.skip(f"PostgreSQL is not available for integration tests: {exc}")

    yield schema, company_id, user_id, other_company_id, other_user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, TenantContext, Any]]:
    """Session rolled back after each test, plus both tenants' scopes."""
    schema, company_id, user_id, other_company_id, other_user_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    other = TenantContext(company_id=other_company_id, user_id=other_user_id)
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
                yield session, tenant, other, engine
            await outer.rollback()
    finally:
        await engine.dispose()


def seeded(session: AsyncSession):
    """Write fixture rows inside their own committed business transaction.

    ``transaction(...)`` rolls the session back when the block it wraps raises,
    and with ``join_transaction_mode="create_savepoint"`` that reaches every row
    the test has flushed so far - fixtures included. Most cases here provoke a
    refusal and then keep asserting against the item and warehouses they seeded,
    so the seeds have to be released into the connection's transaction first.
    The outer rollback in ``ctx`` still removes them at the end of the test.
    """
    return transaction(session)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def seed_bin(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    is_active: bool = True,
    deleted: bool = False,
) -> StorageLocation:
    """A storage location, built through the warehouses module's own model.

    Its NOT NULL columns default in Python, not in the database, so a
    hand-written INSERT here would go stale the day that module adds one.
    """
    location = StorageLocation(
        company_id=company_id,
        warehouse_id=warehouse_id,
        code=_code("BIN"),
        name="Test bin",
        location_type=StorageLocationType.BIN,
        is_active=is_active,
    )
    session.add(location)
    await session.flush()
    if deleted:
        location.deleted_at = utcnow()
        await session.flush()
    return location


class Bench:
    """One item and two warehouses of the tenant under test, holding stock."""

    def __init__(self, item_id: uuid.UUID, source: uuid.UUID, destination: uuid.UUID) -> None:
        self.item_id = item_id
        self.source = source
        self.destination = destination


async def seed_bench(
    session: AsyncSession, tenant: TenantContext, *, on_hand: str = "10"
) -> Bench:
    async with seeded(session):
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
        )
        source = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WA")
        )
        destination = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WB")
        )
    bench = Bench(item.id, source.id, destination.id)
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=bench.item_id,
            warehouse_id=bench.source,
            quantity=Decimal(on_hand),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="transfer bench",
            unit_cost=Decimal("40"),
        )
    return bench


def transfer_payload(
    bench: Bench,
    *,
    to_location_id: uuid.UUID | None = None,
    from_location_id: uuid.UUID | None = None,
    quantity: str = "4",
) -> StockTransferCreate:
    return StockTransferCreate(
        from_warehouse_id=bench.source,
        to_warehouse_id=bench.destination,
        reason="move stock between the stores",
        lines=[
            StockTransferLineCreate(
                item_id=bench.item_id,
                quantity=Decimal(quantity),
                from_location_id=from_location_id,
                to_location_id=to_location_id,
            )
        ],
    )


async def force_line_destination(
    session: AsyncSession, transfer_id: uuid.UUID, location_id: uuid.UUID
) -> None:
    """Point a line at a bin ``create`` would now refuse.

    Stands in for a document raised before that check existed: the only way to
    reach ``dispatch`` with a bad destination now, and the reason the dispatch
    path has to check for itself rather than trusting the document.
    """
    async with seeded(session):
        line = (
            await session.execute(
                select(StockTransferLine).where(StockTransferLine.transfer_id == transfer_id)
            )
        ).scalar_one()
        line.to_location_id = location_id


# -----------------------------------------------------------------------------
# The bin is refused where it is keyed
# -----------------------------------------------------------------------------


class TestADestinationBinIsCheckedAtCreate:
    async def test_another_tenants_bin_is_refused(self, ctx) -> None:
        """Not "forbidden" - not found. Another tenant's bins do not exist as far
        as this company is concerned, and saying otherwise confirms an id."""
        session, tenant, other, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WX")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=foreign_bin.id)
            )

    async def test_a_bin_of_a_third_warehouse_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WC")
            )
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=elsewhere.id
            )

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=wrong_bin.id)
            )

    async def test_a_soft_deleted_bin_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=bench.destination,
                deleted=True,
            )

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=retired.id)
            )

    async def test_a_bin_that_never_existed_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=uuid.uuid4())
            )

    async def test_an_inactive_destination_bin_cannot_receive(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=bench.destination,
                is_active=False,
            )

        with pytest.raises(BusinessRuleError, match="inactive"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=retired.id)
            )

    async def test_an_inactive_source_bin_may_still_give_stock_up(self, ctx) -> None:
        """The asymmetry every other bin rule follows: refusing outbound
        movements would strand whatever is still in the bin behind the flag that
        retired it."""
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=bench.source,
                is_active=False,
            )
            retired_id = retired.id

        transfer = await inv.StockTransferService(session, tenant).create(
            transfer_payload(bench, from_location_id=retired_id)
        )
        assert transfer.lines[0].from_location_id == retired_id

    async def test_a_source_bin_from_the_wrong_warehouse_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=bench.destination
            )

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, from_location_id=wrong_bin.id)
            )

    async def test_the_refused_document_leaves_no_transfer_behind(self, ctx) -> None:
        """The header and its lines are written before the bins are checked in
        the same transaction, so the refusal has to take them with it."""
        session, tenant, other, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WX")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )

        with pytest.raises(NotFoundError):
            await inv.StockTransferService(session, tenant).create(
                transfer_payload(bench, to_location_id=foreign_bin.id)
            )

        transfers = (
            await session.execute(
                select(StockTransfer).where(
                    StockTransfer.company_id == tenant.company_id,
                    StockTransfer.from_warehouse_id == bench.source,
                )
            )
        ).scalars().all()
        assert transfers == []


# -----------------------------------------------------------------------------
# ...and again at the moment it would be written
# -----------------------------------------------------------------------------


class TestADestinationBinIsCheckedAtDispatch:
    """The check at ``create`` is not the last line of defence, and must not be.

    A document raised before that check existed still has to be refused, and it
    has to be refused *before* the outbound leg posts - a dispatch that goes
    through leaves the stock nowhere, because the inbound leg validates the same
    bin and there is no edge out of IN_TRANSIT except RECEIVED.
    """

    @pytest.fixture
    def service(self, ctx):
        session, tenant, _, _ = ctx
        return inv.StockTransferService(session, tenant)

    async def _approved_with_bad_destination(
        self, session: AsyncSession, tenant: TenantContext, bench: Bench, bad_bin: uuid.UUID
    ) -> uuid.UUID:
        service = inv.StockTransferService(session, tenant)
        transfer = await service.create(transfer_payload(bench))
        transfer_id = transfer.id
        await force_line_destination(session, transfer_id, bad_bin)
        await service.approve(transfer_id)
        return transfer_id

    async def _assert_nothing_moved(
        self, session: AsyncSession, tenant: TenantContext, bench: Bench, transfer_id: uuid.UUID
    ) -> None:
        """The document is still approvable-and-dispatchable, and the stock is
        exactly where it was. Nothing is stranded."""
        transfer = await inv.StockTransferService(session, tenant).get_transfer(transfer_id)
        assert transfer.status is TransferStatus.APPROVED

        source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
        assert source is not None
        assert source.on_hand_quantity == Decimal("10.0000")
        assert (
            await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        ) is None
        posted = (
            await session.execute(
                text(
                    "SELECT count(*) FROM inventory_transactions "
                    "WHERE reference_id = :ref"
                ),
                {"ref": transfer_id},
            )
        ).scalar_one()
        assert posted == 0

    async def test_another_tenants_bin_is_refused(self, ctx) -> None:
        session, tenant, other, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WX")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )
            foreign_bin_id = foreign_bin.id
        transfer_id = await self._approved_with_bad_destination(
            session, tenant, bench, foreign_bin_id
        )

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        await self._assert_nothing_moved(session, tenant, bench, transfer_id)

    async def test_a_bin_of_a_third_warehouse_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WC")
            )
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=elsewhere.id
            )
            wrong_bin_id = wrong_bin.id
        transfer_id = await self._approved_with_bad_destination(
            session, tenant, bench, wrong_bin_id
        )

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        await self._assert_nothing_moved(session, tenant, bench, transfer_id)

    async def test_a_soft_deleted_bin_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=bench.destination,
                deleted=True,
            )
            retired_id = retired.id
        transfer_id = await self._approved_with_bad_destination(
            session, tenant, bench, retired_id
        )

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        await self._assert_nothing_moved(session, tenant, bench, transfer_id)

    async def test_no_phantom_balance_row_is_created(self, ctx) -> None:
        """The refusal has to come before the lock, because the lock *creates*
        the row it locks - that is how the stock got lost in the first place."""
        session, tenant, other, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WX")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )
            foreign_bin_id = foreign_bin.id
        transfer_id = await self._approved_with_bad_destination(
            session, tenant, bench, foreign_bin_id
        )

        with pytest.raises(NotFoundError):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM inventory_balances WHERE location_id = :bin"
                ),
                {"bin": foreign_bin_id},
            )
        ).scalar_one()
        assert rows == 0

    async def test_a_good_bin_still_dispatches_and_receives(self, ctx) -> None:
        """The whole journey, so the guard is a guard and not a wall."""
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            inbound = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=bench.destination
            )
            inbound_id = inbound.id

        service = inv.StockTransferService(session, tenant)
        transfer = await service.create(transfer_payload(bench, to_location_id=inbound_id))
        transfer_id = transfer.id
        await service.approve(transfer_id)
        await service.dispatch(transfer_id)

        in_transit = await inv.get_balance(
            session, tenant, bench.item_id, bench.destination, location_id=inbound_id
        )
        assert in_transit is not None
        assert in_transit.in_transit_quantity == Decimal("4.0000")
        assert in_transit.on_hand_quantity == Decimal("0.0000")

        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

        landed = await inv.get_balance(
            session, tenant, bench.item_id, bench.destination, location_id=inbound_id
        )
        assert landed is not None
        assert landed.on_hand_quantity == Decimal("4.0000")
        assert landed.in_transit_quantity == Decimal("0.0000")
        # The stock carried its value across two of the company's own stores.
        assert landed.average_cost == Decimal("40.000000")


# -----------------------------------------------------------------------------
# The immediate two-legged helper takes its locks before it posts, too
# -----------------------------------------------------------------------------


class TestTransferStockChecksItsDestination:
    """``transfer_stock`` locks both positions *before* ``post_movements`` runs.

    ``lock_balances`` creates the row it locks, so the destination grain was
    written one statement before the movement validation that would have refused
    it - the same hole as the transfer document's, reached through the public
    service API in database.md 4.5.
    """

    async def test_another_tenants_bin_is_refused(self, ctx) -> None:
        session, tenant, other, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WX")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )
            foreign_bin_id = foreign_bin.id

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.transfer_stock(
                session,
                tenant,
                item_id=bench.item_id,
                from_warehouse_id=bench.source,
                to_warehouse_id=bench.destination,
                quantity=Decimal("2"),
                reference_code="ST/DIRECT",
                reason="straight across",
                to_location_id=foreign_bin_id,
            )

        rows = (
            await session.execute(
                text("SELECT count(*) FROM inventory_balances WHERE location_id = :bin"),
                {"bin": foreign_bin_id},
            )
        ).scalar_one()
        assert rows == 0
        source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
        assert source is not None
        assert source.on_hand_quantity == Decimal("10.0000")

    async def test_a_bin_of_a_third_warehouse_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WC")
            )
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=elsewhere.id
            )
            wrong_bin_id = wrong_bin.id

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await inv.transfer_stock(
                session,
                tenant,
                item_id=bench.item_id,
                from_warehouse_id=bench.source,
                to_warehouse_id=bench.destination,
                quantity=Decimal("2"),
                reference_code="ST/DIRECT",
                reason="straight across",
                to_location_id=wrong_bin_id,
            )

    async def test_a_good_bin_still_transfers(self, ctx) -> None:
        session, tenant, _, _ = ctx
        bench = await seed_bench(session, tenant)
        async with seeded(session):
            inbound = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=bench.destination
            )
            inbound_id = inbound.id

        out_leg, in_leg = await inv.transfer_stock(
            session,
            tenant,
            item_id=bench.item_id,
            from_warehouse_id=bench.source,
            to_warehouse_id=bench.destination,
            quantity=Decimal("2"),
            reference_code="ST/DIRECT",
            reason="straight across",
            to_location_id=inbound_id,
        )
        assert out_leg.transaction_type is TransactionType.TRANSFER_OUT
        assert in_leg.transaction_type is TransactionType.TRANSFER_IN

        landed = await inv.get_balance(
            session, tenant, bench.item_id, bench.destination, location_id=inbound_id
        )
        assert landed is not None
        assert landed.on_hand_quantity == Decimal("2.0000")


# -----------------------------------------------------------------------------
# What the check costs
# -----------------------------------------------------------------------------


@contextmanager
def counted(engine: Any) -> Iterator[list[str]]:
    """Record every statement the engine executes while the block runs."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


class TestTheCheckIsBatched:
    async def test_thirty_destination_bins_read_no_more_than_one(self, ctx) -> None:
        """One query for the whole set, not one per grain.

        A per-grain lookup is invisible on the two-line transfer a developer
        writes and is a two-hundred-round-trip dispatch in production (section
        32). Inserts and updates are not counted - a ledger row and an audit row
        per line is the work itself, not a lookup.
        """
        session, tenant, _, engine = ctx
        async with seeded(session):
            source = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WA")
            )
            destination = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WB")
            )
            items = [
                (
                    await seed_support.seed_item(
                        session,
                        company_id=tenant.company_id,
                        code=_code("RM"),
                        gst_rate=Decimal("18"),
                        standard_cost=Decimal("40"),
                    )
                ).id
                for _ in range(30)
            ]
            bins = [
                (
                    await seed_bin(
                        session,
                        company_id=tenant.company_id,
                        warehouse_id=destination.id,
                    )
                ).id
                for _ in range(30)
            ]
        async with transaction(session):
            await inv.post_movements(
                session,
                tenant,
                [
                    inv.MovementRequest(
                        item_id=item_id,
                        warehouse_id=source.id,
                        quantity=Decimal("10"),
                        transaction_type=TransactionType.OPENING_BALANCE,
                        reference_type="opening",
                        reference_id=None,
                        reference_code="OPENING",
                        reason="cost probe",
                        unit_cost=Decimal("40"),
                    )
                    for item_id in items
                ],
            )

        service = inv.StockTransferService(session, tenant)

        def payload(count: int, offset: int) -> StockTransferCreate:
            return StockTransferCreate(
                from_warehouse_id=source.id,
                to_warehouse_id=destination.id,
                reason="cost probe",
                lines=[
                    StockTransferLineCreate(
                        item_id=items[index],
                        quantity=Decimal("1"),
                        to_location_id=bins[index],
                    )
                    for index in range(offset, offset + count)
                ],
            )

        small = await service.create(payload(1, 0))
        large = await service.create(payload(29, 1))
        await service.approve(small.id)
        await service.approve(large.id)

        with counted(engine) as one_line:
            await service.dispatch(small.id)
        with counted(engine) as many_lines:
            await service.dispatch(large.id)

        one = [s for s in one_line if s.lstrip().upper().startswith("SELECT")]
        many = [s for s in many_lines if s.lstrip().upper().startswith("SELECT")]
        assert len(many) == len(one), (
            f"a 29-line dispatch read {len(many)} times where a one-line dispatch read "
            f"{len(one)}:\n" + "\n".join(sorted(set(many) - set(one)))
        )

    async def test_validating_a_position_set_is_three_queries(self, ctx) -> None:
        """The seam ``transfer_stock`` uses, measured on its own.

        Item, warehouse and bin, one ``IN`` query each, however many positions
        are handed in.
        """
        session, tenant, _, engine = ctx
        async with seeded(session):
            warehouse = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WA")
            )
            grains = []
            for _ in range(25):
                item = await seed_support.seed_item(
                    session,
                    company_id=tenant.company_id,
                    code=_code("RM"),
                    gst_rate=Decimal("18"),
                    standard_cost=Decimal("40"),
                )
                location = await seed_bin(
                    session, company_id=tenant.company_id, warehouse_id=warehouse.id
                )
                grains.append(
                    BalanceGrain(
                        item_id=item.id,
                        warehouse_id=warehouse.id,
                        location_id=location.id,
                    )
                )

        engine_under_test = inv.MovementEngine(session, tenant)
        with counted(engine) as statements:
            await engine_under_test.validate_positions(grains, receiving=grains)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 3, selects
