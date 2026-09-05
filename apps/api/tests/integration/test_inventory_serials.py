"""Serial-number lifecycle and storage-location validation, against real PostgreSQL.

Two defects are pinned down here, and neither can be shown without a database.

**A serialised unit must not leave twice.** The status column on
``stock_serials`` exists so a traceability report knows where one physical
object is. Until the posting path assigned it, every unit stayed ``IN_STOCK``
forever: it could be issued, dispatched and consumed repeatedly, and the report
still showed it on the shelf after it had shipped. The pure half of the rule -
which movement is legal against which state - is covered in
``tests/unit/test_inventory_serial_lifecycle.py``. What is covered here is the
half that needs PostgreSQL: the row lock. Two genuinely concurrent issues of one
unit, on two connections, exactly one of which may win.

**A movement must not book stock into a bin that is not there.** ``location_id``
is a foreign key to ``storage_locations`` and nothing more - the constraint is
company-blind and says nothing about which warehouse the bin belongs to. A
movement naming another tenant's bin, or a bin in a different warehouse, keyed
an ``inventory_balances`` row at a grain no warehouse view will ever show, and
the stock in it was simply gone.

Isolation follows ``test_inventory_service.py``: one private schema per module,
created once and dropped afterwards. Seeds are written inside their own
``transaction(...)`` block on purpose - see :func:`seeded`.
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

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    AppError,
    BusinessRuleError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import SerialStatus, StockSerial, TransactionType
from src.modules.inventory.schemas import StockSerialCreate
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
prepare_metadata()

#: Error codes that mean "this posting correctly did not get the unit".
REFUSAL_CODES = {
    ErrorCode.INVALID_STATE_TRANSITION,
    ErrorCode.BUSINESS_RULE_VIOLATION,
    ErrorCode.INSUFFICIENT_AVAILABLE_STOCK,
    ErrorCode.INSUFFICIENT_STOCK,
    ErrorCode.STOCK_LOCK_TIMEOUT,
}


def _schema_engine(schema: str):
    """Engine pinned to the test schema, one connection per session (NullPool).

    NullPool matters for the concurrency case: the point is that two sessions
    hold two *different* PostgreSQL backends, so one can genuinely block on the
    other's row lock.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with two tenants, so cross-tenant leakage can be asserted."""
    schema = f"it_inv_serial_{uuid.uuid4().hex[:10]}"
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
                await seed_support.seed_company(session, company_id=company_id, prefix="SER")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"serials-{uuid.uuid4().hex[:8]}@atcraft.test",
                )
                await seed_support.seed_company(session, company_id=other_company_id, prefix="OTH")
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
        report_database_setup_failure(exc)

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

    Not decoration. ``transaction(...)`` rolls the *session* back when the block
    it wraps raises, and with ``join_transaction_mode="create_savepoint"`` that
    rollback reaches every row the test has flushed so far - including its
    fixtures. Half of the cases below deliberately provoke a refusal and then
    keep using the item and the warehouse they seeded, so the seeds have to be
    released into the connection's transaction first. The outer rollback in
    ``ctx`` still removes them at the end of the test.
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
        from src.core.clock import utcnow

        location.deleted_at = utcnow()
        await session.flush()
    return location


async def _seed_item_and_warehouse(
    session: AsyncSession, tenant: TenantContext, *, serial_tracked: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("SER" if serial_tracked else "RM"),
        gst_rate=Decimal("18"),
        standard_cost=Decimal("500"),
        is_serial_tracked=serial_tracked,
    )
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    return item.id, warehouse.id


async def seed_serialised_item(
    session: AsyncSession, tenant: TenantContext
) -> tuple[uuid.UUID, uuid.UUID]:
    return await _seed_item_and_warehouse(session, tenant, serial_tracked=True)


async def seed_plain_item(
    session: AsyncSession, tenant: TenantContext
) -> tuple[uuid.UUID, uuid.UUID]:
    """A fungible item. The bin rules are about the position, not about the unit,
    so they are exercised without a serial in the way."""
    return await _seed_item_and_warehouse(session, tenant, serial_tracked=False)


async def register_serial(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID
) -> uuid.UUID:
    """A serial row, created the way the goods-in bench creates it: before the
    stock arrives, from the packing list, with no position yet."""
    read = await inv.TraceabilityService(session, tenant).create_serial(
        StockSerialCreate(item_id=item_id, serial_number=_code("SN"), unit_cost=Decimal("500"))
    )
    return read.id


async def post(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    transaction_type: TransactionType,
    serial_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    quantity: str = "1",
    **kwargs: Any,
) -> Any:
    async with transaction(session):
        return await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=transaction_type,
            reference_type="test_document",
            reference_id=None,
            reference_code=_code("DOC"),
            reason="serial lifecycle test",
            serial_id=serial_id,
            location_id=location_id,
            **kwargs,
        )


async def read_serial(session: AsyncSession, serial_id: uuid.UUID) -> StockSerial:
    """Re-read a serial from the database.

    Always re-read after an expected failure: a rolled-back savepoint expires
    the identity map, so an instance loaded earlier is not safe to inspect.
    """
    return (
        await session.execute(
            select(StockSerial)
            .where(StockSerial.id == serial_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# -----------------------------------------------------------------------------
# The lifecycle, end to end
# -----------------------------------------------------------------------------


class TestSerialLifecycle:
    async def test_a_receipt_records_the_unit_where_it_landed(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            location_id=bin_id,
            unit_cost=Decimal("500"),
        )

        serial = await read_serial(session, serial_id)
        assert serial.status is SerialStatus.IN_STOCK
        assert serial.warehouse_id == warehouse_id
        assert serial.location_id == bin_id

    async def test_an_issue_moves_the_unit_out_of_stock(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=serial_id,
        )

        serial = await read_serial(session, serial_id)
        # The status is what says the unit has left. The position is where it
        # left *from*, and it is kept: clearing it threw away the answer to
        # "which warehouse did this unit go out of?", and it broke the three
        # modules - assembly, packing, dispatch - that read the column to place
        # a unit, which then reported a consumed unit as "not held at this
        # warehouse" instead of "already consumed by AO/...".
        assert serial.status is SerialStatus.ISSUED
        assert serial.warehouse_id == warehouse_id

    async def test_the_whole_journey_of_one_unit(self, ctx) -> None:
        """Receive, issue, return, dispatch, customer return, scrap.

        Every leg is a real posting, so the balance arithmetic and the unit's
        state have to agree at each step: the position is back to zero at the
        end and the unit is written off.
        """
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        legs = [
            (TransactionType.PURCHASE_RECEIPT, SerialStatus.IN_STOCK),
            (TransactionType.MATERIAL_ISSUE, SerialStatus.ISSUED),
            (TransactionType.MATERIAL_RETURN, SerialStatus.IN_STOCK),
            (TransactionType.DISPATCH, SerialStatus.DISPATCHED),
            (TransactionType.CUSTOMER_RETURN, SerialStatus.RETURNED),
            (TransactionType.SCRAP, SerialStatus.SCRAPPED),
        ]
        for transaction_type, expected in legs:
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=transaction_type,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
            serial = await read_serial(session, serial_id)
            assert serial.status is expected, f"{transaction_type} left it {serial.status}"

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("0.0000")
        assert balance.scrap_quantity == Decimal("1.0000")

    async def test_a_qc_hold_leaves_the_unit_alone(self, ctx) -> None:
        """A hold moves a quantity between pools. Nobody's hands change."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
                serial_id=serial_id,
            )

        serial = await read_serial(session, serial_id)
        assert serial.status is SerialStatus.IN_STOCK
        assert serial.warehouse_id == warehouse_id

    async def test_the_status_change_is_audited_in_the_same_transaction(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=serial_id,
        )

        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "stock_serial",
                        AuditLog.entity_id == serial_id,
                        AuditLog.action == AuditAction.STATUS_CHANGE,
                    )
                    # created_at ties inside one transaction, so the UUIDv7 id is
                    # the tiebreaker that recovers true insertion order.
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )

        assert [row.new_values["status"] for row in rows] == ["ISSUED"]
        assert rows[0].old_values["status"] == "IN_STOCK"
        assert rows[0].created_by_id == tenant.user_id


class TestWhereTheUnitIsRecorded:
    """Only a movement that *puts the unit somewhere* rewrites its position.

    ``warehouse_id`` and ``location_id`` are the last place the unit was put;
    ``status`` says whether it is still there. Assigning the movement's own
    warehouse and bin on every posting made a movement that moves nothing - a QC
    hold, a QC release, the outbound leg of a transfer - rewrite the unit's
    position anyway, which is how a hold keyed at warehouse level emptied the
    bin a unit was sitting in.
    """

    async def test_a_hold_keyed_without_a_bin_does_not_empty_the_bin(self, ctx) -> None:
        """The mis-key that costs a bin: the hold is raised on the warehouse
        position rather than on the bin the unit is in. Nothing moves - a hold
        shifts a quantity between two pools of one position - and the unit used
        to come out of it with its bin erased.
        """
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        binned = await register_serial(session, tenant, item_id)
        loose = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=binned,
            location_id=bin_id,
            unit_cost=Decimal("500"),
        )
        # A second unit sitting loose at the warehouse, so the hold below has a
        # quantity to work on at the position it names.
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=loose,
            unit_cost=Decimal("500"),
        )

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
                serial_id=binned,
            )

        serial = await read_serial(session, binned)
        assert serial.status is SerialStatus.IN_STOCK
        assert serial.warehouse_id == warehouse_id
        assert serial.location_id == bin_id, "the hold moved nothing but emptied the bin"

    async def test_a_hold_raised_at_the_wrong_warehouse_is_refused(self, ctx) -> None:
        """A QC hold does not move stock between warehouses, so it may neither
        move the unit's record nor be raised anywhere but where the unit is.

        Leaving the position alone fixed half of this: the mis-key no longer
        teleported the unit. The posting was still accepted, though, so the
        quantity moved at a store the unit is not in and the unit's own status
        was then decided by a movement that happened somewhere else - which is
        how a QC release raised against the wrong store put a returned unit
        "back into stock" at a store it had never been in. A movement that does
        not relocate a unit now has to name the position it is already at.
        """
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WH")
            )
            elsewhere_id = elsewhere.id
        here = await register_serial(session, tenant, item_id)
        over_there = await register_serial(session, tenant, item_id)

        for serial_id, at in ((here, warehouse_id), (over_there, elsewhere_id)):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=at,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )

        with pytest.raises(BusinessRuleError, match="held at another warehouse"):
            async with transaction(session):
                await inv.change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=elsewhere_id,
                    quantity=Decimal("1"),
                    from_state=inv.StockState.AVAILABLE,
                    to_state=inv.StockState.QC_HOLD,
                    reason="held against the wrong store",
                    serial_id=here,
                )

        # Nothing at all happened: not the unit's position, and not the other
        # store's quantity - the unit that really is over there is untouched.
        assert (await read_serial(session, here)).warehouse_id == warehouse_id
        elsewhere_balance = await inv.get_balance(session, tenant, item_id, elsewhere_id)
        assert elsewhere_balance is not None
        assert elsewhere_balance.qc_hold_quantity == Decimal("0.0000")

    async def test_the_same_hold_at_the_unit_s_own_warehouse_is_accepted(self, ctx) -> None:
        """The rule is about agreement, not about refusing QC decisions."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
                serial_id=serial_id,
            )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.qc_hold_quantity == Decimal("1.0000")
        assert (await read_serial(session, serial_id)).warehouse_id == warehouse_id

    async def test_a_unit_that_was_never_positioned_may_still_be_handled(self, ctx) -> None:
        """NULL is not a warehouse to disagree with. A unit registered from a
        packing list has never been put anywhere, so the rule has nothing to
        compare and the posting stands or falls on its own merits."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        stock = await register_serial(session, tenant, item_id)
        unpositioned = await register_serial(session, tenant, item_id)

        # A unit really on the shelf, so the issue below has a quantity to take.
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=stock,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=unpositioned,
        )
        issued = await read_serial(session, unpositioned)
        assert issued.status is SerialStatus.ISSUED
        assert issued.warehouse_id is None

    async def test_a_receipt_into_another_bin_does_move_it(self, ctx) -> None:
        """The other half of the rule: a movement that puts the unit somewhere
        is exactly what the position follows."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            first = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
            second = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            location_id=first,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=serial_id,
            location_id=first,
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_RETURN,
            serial_id=serial_id,
            location_id=second,
            unit_cost=Decimal("500"),
        )

        serial = await read_serial(session, serial_id)
        assert serial.status is SerialStatus.IN_STOCK
        assert serial.location_id == second

    async def test_a_transfer_places_the_unit_only_when_it_arrives(self, ctx) -> None:
        """Between the two legs the unit is on a vehicle. The outbound leg must
        not claim it has arrived anywhere."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, source_id = await seed_serialised_item(session, tenant)
            destination = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WH")
            )
            destination_id = destination.id
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=source_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=source_id,
            transaction_type=TransactionType.TRANSFER_OUT,
            serial_id=serial_id,
        )
        assert (await read_serial(session, serial_id)).warehouse_id == source_id

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=destination_id,
            transaction_type=TransactionType.TRANSFER_IN,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        assert (await read_serial(session, serial_id)).warehouse_id == destination_id

    async def test_a_shipped_unit_is_not_listed_at_the_warehouse_it_left(self, ctx) -> None:
        """The position is kept, so the read that asks "what is in my store?"
        has to consult the status - or the list fills with everything the
        warehouse ever shipped."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        here = await register_serial(session, tenant, item_id)
        gone = await register_serial(session, tenant, item_id)

        for serial_id in (here, gone):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.DISPATCH,
            serial_id=gone,
        )

        rows, _ = await inv.TraceabilityService(session, tenant).list_serials(
            item_id, warehouse_id, None, PageParams(page=1, limit=50)
        )
        assert [row.id for row in rows] == [here]
        # ...and the unit that shipped still knows where it shipped from.
        assert (await read_serial(session, gone)).warehouse_id == warehouse_id


class TestTheBinHalfOfTheAgreementRule:
    """The wrong-warehouse case was closed and the wrong-*bin* case was not.

    The rule waived the bin comparison outright, on the argument that assembly,
    dispatch and QC decisions post at warehouse level. True of a movement that
    names no bin, and a different statement about one that does: the posting has
    said which shelf it thinks the unit is on, the quantity moves on that shelf,
    and the unit's status is then decided by something that happened one aisle
    over. The waiver belongs to the unstated case only.
    """

    async def test_a_hold_raised_against_the_wrong_bin_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            here = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
            elsewhere = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        binned = await register_serial(session, tenant, item_id)
        other = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=binned,
            location_id=here,
            unit_cost=Decimal("500"),
        )
        # A unit really on the other shelf, so the hold below has a quantity to
        # work on at the position it names. The refusal must not be a shortfall.
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=other,
            location_id=elsewhere,
            unit_cost=Decimal("500"),
        )

        with pytest.raises(BusinessRuleError, match="another storage location") as excinfo:
            async with transaction(session):
                await inv.change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    location_id=elsewhere,
                    quantity=Decimal("1"),
                    from_state=inv.StockState.AVAILABLE,
                    to_state=inv.StockState.QC_HOLD,
                    reason="inspection raised on the wrong shelf",
                    serial_id=binned,
                )
        assert excinfo.value.details["serial_location_id"] == str(here)
        assert excinfo.value.details["movement_location_id"] == str(elsewhere)

        # Nothing moved: neither the unit nor either bin's quantity.
        assert (await read_serial(session, binned)).location_id == here
        wrong_bin = await inv.get_balance(
            session, tenant, item_id, warehouse_id, location_id=elsewhere
        )
        assert wrong_bin is not None
        assert wrong_bin.qc_hold_quantity == Decimal("0.0000")

    async def test_the_same_hold_on_the_unit_s_own_bin_is_accepted(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            here = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await register_serial(session, tenant, item_id)
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            location_id=here,
            unit_cost=Decimal("500"),
        )

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                location_id=here,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
                serial_id=serial_id,
            )

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=here)
        assert balance is not None
        assert balance.qc_hold_quantity == Decimal("1.0000")

    async def test_a_hold_that_names_no_bin_is_still_accepted(self, ctx) -> None:
        """The documented reason the comparison was by warehouse in the first
        place, and it is untouched: a QC decision raised at warehouse level says
        nothing about the shelf, so there is nothing to disagree with."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            here = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        binned = await register_serial(session, tenant, item_id)
        loose = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=binned,
            location_id=here,
            unit_cost=Decimal("500"),
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=loose,
            unit_cost=Decimal("500"),
        )

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="incoming inspection",
                serial_id=binned,
            )

        assert (await read_serial(session, binned)).location_id == here


class TestRelocationFollowsThePoolsNotTheDocumentType:
    """Which movements may rewrite a unit's position was read off the transaction
    type's *default* transition, and a caller can override the source.

    ``change_stock_state(JOB_WORK -> AVAILABLE)`` posts a ``JOB_WORK_RECEIPT``,
    whose default source is "outside this position" - so it counted as a
    relocation - while actually drawing on this position's own job-work pool.
    Nothing arrives from anywhere. Treating it as a relocation waived the
    "raise it where the unit is" check and let a return keyed against the wrong
    store rewrite the unit's position to that store.
    """

    async def _send_out(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        location_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """One unit received at a position and then issued to a job-work vendor.

        It leaves ``job_work_quantity`` of 1 standing at that exact position, so
        a mis-keyed return has a pool to draw on and is refused by the rule
        under test rather than by an accidental shortfall.
        """
        serial_id = await register_serial(session, tenant, item_id)
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            location_id=location_id,
            unit_cost=Decimal("500"),
        )
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                location_id=location_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.JOB_WORK,
                reason="out for machining",
                serial_id=serial_id,
            )
        return serial_id

    async def test_a_job_work_return_at_the_wrong_warehouse_is_refused(self, ctx) -> None:
        """Both stores have a unit out at a vendor, so the wrong store has a
        job-work pool to draw on: the refusal has to come from the position rule
        and not from the arithmetic happening to run out."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, here = await seed_serialised_item(session, tenant)
            elsewhere = (
                await seed_support.seed_warehouse(
                    session, company_id=tenant.company_id, code=_code("WH")
                )
            ).id
        mine = await self._send_out(session, tenant, item_id=item_id, warehouse_id=here)
        await self._send_out(session, tenant, item_id=item_id, warehouse_id=elsewhere)

        with pytest.raises(BusinessRuleError, match="held at another warehouse"):
            async with transaction(session):
                await inv.change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=elsewhere,
                    quantity=Decimal("1"),
                    from_state=inv.StockState.JOB_WORK,
                    to_state=inv.StockState.AVAILABLE,
                    reason="back from the vendor, keyed at the wrong store",
                    serial_id=mine,
                )

        # The unit was not teleported, and the other store's pool was not spent.
        assert (await read_serial(session, mine)).warehouse_id == here
        wrong = await inv.get_balance(session, tenant, item_id, elsewhere)
        assert wrong is not None
        assert wrong.job_work_quantity == Decimal("1.0000")

    async def test_a_job_work_return_at_the_wrong_bin_is_refused_too(self, ctx) -> None:
        """The bin half of the same mis-key, in the same movement."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            here = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
            elsewhere = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        mine = await self._send_out(
            session, tenant, item_id=item_id, warehouse_id=warehouse_id, location_id=here
        )
        await self._send_out(
            session, tenant, item_id=item_id, warehouse_id=warehouse_id, location_id=elsewhere
        )

        with pytest.raises(BusinessRuleError, match="another storage location"):
            async with transaction(session):
                await inv.change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    location_id=elsewhere,
                    quantity=Decimal("1"),
                    from_state=inv.StockState.JOB_WORK,
                    to_state=inv.StockState.AVAILABLE,
                    reason="back from the vendor, booked onto the wrong shelf",
                    serial_id=mine,
                )

        assert (await read_serial(session, mine)).location_id == here

    async def test_the_return_at_the_unit_s_own_position_is_accepted(self, ctx) -> None:
        """It moves a quantity between two pools of one position, so it places
        the unit nowhere - and leaves the shelf it was taken from alone."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await self._send_out(
            session, tenant, item_id=item_id, warehouse_id=warehouse_id, location_id=bin_id
        )

        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                location_id=bin_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.JOB_WORK,
                to_state=inv.StockState.AVAILABLE,
                reason="back from the vendor",
                serial_id=serial_id,
            )

        after = await read_serial(session, serial_id)
        assert after.status is SerialStatus.IN_STOCK
        assert after.warehouse_id == warehouse_id
        assert after.location_id == bin_id

    async def test_a_plain_job_work_receipt_still_places_the_unit(self, ctx) -> None:
        """The default is untouched: a *different* item coming back from the
        vendor is a genuine arrival and it decides where the unit now is."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.JOB_WORK_RECEIPT,
            serial_id=serial_id,
            location_id=bin_id,
            unit_cost=Decimal("500"),
        )

        placed = await read_serial(session, serial_id)
        assert placed.status is SerialStatus.IN_STOCK
        assert placed.warehouse_id == warehouse_id
        assert placed.location_id == bin_id


class TestAUnitCannotBeReceivedTwice:
    """The inbound half of the lifecycle.

    The ``IN_STOCK -> IN_STOCK`` self edge exists so a unit registered from a
    packing list can be received at all. Unqualified it also accepted the second
    receipt of a unit already on the shelf: a re-scanned goods receipt booked a
    second unit's worth of stock against one physical object, and the unit
    stayed cheerfully IN_STOCK.
    """

    async def test_a_second_receipt_of_the_same_unit_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=serial_id,
            unit_cost=Decimal("500"),
        )
        with pytest.raises(BusinessRuleError, match="already in stock"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )

        # One unit received, not two, and no second ledger row.
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1.0000")
        receipts = (
            await session.execute(
                text("SELECT count(*) FROM inventory_transactions WHERE serial_id = :serial"),
                {"serial": serial_id},
            )
        ).scalar_one()
        assert receipts == 1

    async def test_one_document_naming_the_same_unit_twice_is_refused(self, ctx) -> None:
        """The sibling line has not been flushed yet, so the ledger cannot see
        it - the batch has to remember what it has just received."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            first_bin = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
            second_bin = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
        serial_id = await register_serial(session, tenant, item_id)

        movements = [
            inv.MovementRequest(
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                reference_type="goods_receipt",
                reference_id=None,
                reference_code="GRN/DUP",
                reason="one unit, two lines",
                location_id=location_id,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
            for location_id in (first_bin, second_bin)
        ]
        with pytest.raises(BusinessRuleError, match="already in stock"):
            async with transaction(session):
                await inv.post_movements(session, tenant, movements)

        assert (
            await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=first_bin)
        ) is None

    async def test_a_unit_that_really_came_back_may_be_received(self, ctx) -> None:
        """The guard is about duplicates, not about re-entry. A unit that was
        issued to the floor and comes back is a genuine transition."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        for transaction_type in (
            TransactionType.PURCHASE_RECEIPT,
            TransactionType.MATERIAL_ISSUE,
            TransactionType.MATERIAL_RETURN,
        ):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=transaction_type,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )

        serial = await read_serial(session, serial_id)
        assert serial.status is SerialStatus.IN_STOCK
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1.0000")

    async def test_a_registration_that_states_a_bin_can_still_be_received(self, ctx) -> None:
        """A registration says where the unit is *expected* to land. It is not
        an arrival, so the receipt that follows it must not look like a
        duplicate."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id

        read = await inv.TraceabilityService(session, tenant).create_serial(
            StockSerialCreate(
                item_id=item_id,
                serial_number=_code("SN"),
                warehouse_id=warehouse_id,
                location_id=bin_id,
                unit_cost=Decimal("500"),
            )
        )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            serial_id=read.id,
            location_id=bin_id,
            unit_cost=Decimal("500"),
        )
        assert (await read_serial(session, read.id)).status is SerialStatus.IN_STOCK


class TestACustomerReturnCanBeSoldAgain:
    """RETURNED was a one-way door.

    Every movement type that targets IN_STOCK also adds a unit to on-hand, and
    the customer return had already added this one - so the RETURNED -> IN_STOCK
    edge the graph declares could not be driven by anything that did not invent
    a second unit. A QC release is neutral on on-hand and is the movement that
    resolves an inspection, so it resolves the unit's status too.
    """

    async def test_a_qc_release_puts_a_returned_unit_back_into_stock(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        for transaction_type in (
            TransactionType.PURCHASE_RECEIPT,
            TransactionType.DISPATCH,
            TransactionType.CUSTOMER_RETURN,
        ):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=transaction_type,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        assert (await read_serial(session, serial_id)).status is SerialStatus.RETURNED

        # Inspect it, then release it. Neither leg invents a unit.
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="return inspection",
                serial_id=serial_id,
            )
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.QC_HOLD,
                to_state=inv.StockState.AVAILABLE,
                reason="passed inspection, back on sale",
                serial_id=serial_id,
            )

        serial = await read_serial(session, serial_id)
        assert serial.status is SerialStatus.IN_STOCK
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # One physical unit, one unit of stock - the whole point of using the
        # release rather than a movement that adds one.
        assert balance.on_hand_quantity == Decimal("1.0000")
        assert balance.available_quantity == Decimal("1.0000")

    async def test_and_can_then_be_dispatched_again(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)

        for transaction_type in (
            TransactionType.PURCHASE_RECEIPT,
            TransactionType.DISPATCH,
            TransactionType.CUSTOMER_RETURN,
            TransactionType.DISPATCH,
        ):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=transaction_type,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        assert (await read_serial(session, serial_id)).status is SerialStatus.DISPATCHED


class TestTheStatusFollowsThePoolNotTheDocumentType:
    """Which pool a release lands in is what decides the unit's status.

    ``state_change_type`` maps more than one pool-to-pool move onto
    ``QC_RELEASE``: QC hold to available, damaged to available, *and* QC hold
    back to the return pool. Keyed on the transaction type alone, the rule that
    resolves a customer return fired for all of them - so a release that put the
    quantity back in the return pool still declared the unit IN_STOCK, and the
    unit's status and its quantity's classification disagreed about the same
    physical object. ``movement_effect`` knew the pool all along; the transition
    now reads the same answer.
    """

    async def _returned_and_held(
        self, session: AsyncSession, tenant: TenantContext
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """One unit at a customer, back again, and on inspection."""
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        serial_id = await register_serial(session, tenant, item_id)
        for transaction_type in (
            TransactionType.PURCHASE_RECEIPT,
            TransactionType.DISPATCH,
            TransactionType.CUSTOMER_RETURN,
        ):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=transaction_type,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await self._hold(session, tenant, item_id, warehouse_id, serial_id)
        return item_id, warehouse_id, serial_id

    @staticmethod
    async def _hold(
        session: AsyncSession,
        tenant: TenantContext,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        serial_id: uuid.UUID,
    ) -> None:
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.AVAILABLE,
                to_state=inv.StockState.QC_HOLD,
                reason="return inspection",
                serial_id=serial_id,
            )

    @staticmethod
    async def _release(
        session: AsyncSession,
        tenant: TenantContext,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        serial_id: uuid.UUID,
        to_state: Any,
    ) -> None:
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("1"),
                from_state=inv.StockState.QC_HOLD,
                to_state=to_state,
                reason="inspection resolved",
                serial_id=serial_id,
            )

    async def test_a_release_into_the_return_pool_leaves_the_unit_returned(self, ctx) -> None:
        """Both are ``QC_RELEASE`` rows in the ledger. Only the pool tells them
        apart, and the unit's status has to be told apart the same way."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id, serial_id = await self._returned_and_held(session, tenant)

        await self._release(
            session, tenant, item_id, warehouse_id, serial_id, inv.StockState.RETURN
        )

        assert (await read_serial(session, serial_id)).status is SerialStatus.RETURNED
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        # Out of the hold pool, still one physical unit, still on hand.
        assert balance.qc_hold_quantity == Decimal("0.0000")
        assert balance.on_hand_quantity == Decimal("1.0000")

    async def test_a_release_into_the_available_pool_puts_it_back_into_stock(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id, serial_id = await self._returned_and_held(session, tenant)

        await self._release(
            session, tenant, item_id, warehouse_id, serial_id, inv.StockState.AVAILABLE
        )

        assert (await read_serial(session, serial_id)).status is SerialStatus.IN_STOCK

    async def test_the_unit_can_still_be_resolved_after_a_release_into_return(self, ctx) -> None:
        """The narrower rule must not re-create the one-way door it replaced: a
        unit released into the return pool is still RETURNED, and a later
        release into the available pool still frees it."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id, serial_id = await self._returned_and_held(session, tenant)
        await self._release(
            session, tenant, item_id, warehouse_id, serial_id, inv.StockState.RETURN
        )

        await self._hold(session, tenant, item_id, warehouse_id, serial_id)
        await self._release(
            session, tenant, item_id, warehouse_id, serial_id, inv.StockState.AVAILABLE
        )

        assert (await read_serial(session, serial_id)).status is SerialStatus.IN_STOCK


# -----------------------------------------------------------------------------
# The defect: a unit that leaves twice
# -----------------------------------------------------------------------------


class TestAUnitCannotLeaveTwice:
    async def test_a_second_issue_of_the_same_unit_is_refused(self, ctx) -> None:
        """Two units on the shelf, one of them scanned twice.

        The quantity check cannot catch this and that is the whole point: the
        position holds two, so the second issue is affordable. Only the unit's
        own state knows that this particular object has already gone.
        """
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        first = await register_serial(session, tenant, item_id)
        second = await register_serial(session, tenant, item_id)

        for serial_id in (first, second):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=first,
        )

        with pytest.raises(InvalidStateTransitionError) as excinfo:
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.MATERIAL_ISSUE,
                serial_id=first,
            )
        assert excinfo.value.details["current_status"] == "ISSUED"

        # The refusal took nothing with it: the balance still holds the unit
        # that was not issued, and the other unit is untouched.
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("1.0000")
        assert (await read_serial(session, second)).status is SerialStatus.IN_STOCK

    async def test_a_dispatched_unit_cannot_be_issued_to_the_floor(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        gone = await register_serial(session, tenant, item_id)
        spare = await register_serial(session, tenant, item_id)

        for serial_id in (gone, spare):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.DISPATCH,
            serial_id=gone,
        )

        with pytest.raises(InvalidStateTransitionError):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.MATERIAL_ISSUE,
                serial_id=gone,
            )
        assert (await read_serial(session, gone)).status is SerialStatus.DISPATCHED

    async def test_a_unit_that_is_not_here_cannot_be_put_on_qc_hold(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        gone = await register_serial(session, tenant, item_id)
        spare = await register_serial(session, tenant, item_id)

        for serial_id in (gone, spare):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=gone,
        )

        with pytest.raises(BusinessRuleError, match="not in a warehouse"):
            async with transaction(session):
                await inv.change_stock_state(
                    session,
                    tenant,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("1"),
                    from_state=inv.StockState.AVAILABLE,
                    to_state=inv.StockState.QC_HOLD,
                    reason="inspection",
                    serial_id=gone,
                )

    async def test_a_refused_second_issue_writes_no_ledger_row(self, ctx) -> None:
        """The refusal has to take the whole posting with it, not half of it."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
        first = await register_serial(session, tenant, item_id)
        second = await register_serial(session, tenant, item_id)

        for serial_id in (first, second):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                serial_id=serial_id,
                unit_cost=Decimal("500"),
            )
        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            serial_id=first,
        )
        with pytest.raises(InvalidStateTransitionError):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.MATERIAL_ISSUE,
                serial_id=first,
            )

        issues = (
            await session.execute(
                text(
                    "SELECT count(*) FROM inventory_transactions "
                    "WHERE serial_id = :serial AND transaction_type = 'MATERIAL_ISSUE'"
                ),
                {"serial": first},
            )
        ).scalar_one()
        assert issues == 1


# -----------------------------------------------------------------------------
# The defect: two people scanning the same unit at the same moment
# -----------------------------------------------------------------------------


class TestConcurrentIssueOfOneUnit:
    """Two transactions, two connections, one serial. Exactly one may win.

    This is the serial's half of the scenario master rules section 10 states for
    quantities, and it cannot be faked: a single session, or two sessions sharing
    one connection inside an outer transaction, serialises on the client side and
    would pass with the row lock removed.

    The two issues are posted from **different bins**, and that is the whole
    design of the case. Two movements at the same grain queue on the balance
    row's lock, which would make this test pass even with the serial lock
    deleted - the balance would serialise them and the loser's re-read would see
    the new status anyway. Two grains means the balance rows never contend, both
    positions genuinely have a unit to give, and the only row the two
    transactions share is the serial. Whatever refuses the loser here is the
    serial's own lock.

    Realistically, it is two operators at two benches who both believe they are
    holding unit SN-x. One of them is wrong, and the system has to say so rather
    than book the same object out twice.
    """

    @pytest_asyncio.fixture
    async def committed_engine(
        self, _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]
    ) -> AsyncIterator[Any]:
        schema, _, _, _, _ = _schema
        engine = _schema_engine(schema)
        try:
            yield engine
        finally:
            await engine.dispose()

    @pytest.fixture
    def tenant(
        self, _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]
    ) -> TenantContext:
        _, company_id, user_id, _, _ = _schema
        return TenantContext(company_id=company_id, user_id=user_id)

    async def test_two_concurrent_issues_of_one_unit_and_exactly_one_wins(
        self, committed_engine: Any, tenant: TenantContext
    ) -> None:
        factory = async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session, session.begin():
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            first_bin = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id
            second_bin = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id

        async with factory() as session:
            scanned = await register_serial(session, tenant, item_id)
            spare = await register_serial(session, tenant, item_id)
            async with transaction(session):
                await inv.post_movements(
                    session,
                    tenant,
                    [
                        inv.MovementRequest(
                            item_id=item_id,
                            warehouse_id=warehouse_id,
                            quantity=Decimal("1"),
                            transaction_type=TransactionType.PURCHASE_RECEIPT,
                            reference_type="goods_receipt",
                            reference_id=None,
                            reference_code="GRN/CONC",
                            reason="concurrency fixture",
                            location_id=location_id,
                            serial_id=serial_id,
                            unit_cost=Decimal("500"),
                        )
                        for serial_id, location_id in (
                            (scanned, first_bin),
                            (spare, second_bin),
                        )
                    ],
                )
            await session.commit()

        async def issue(reference: str, location_id: uuid.UUID) -> None:
            """One issue, in its own session on its own connection, committed."""
            async with factory() as session:
                try:
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
                            reference_code=reference,
                            reason="two benches, one unit",
                            location_id=location_id,
                            serial_id=scanned,
                        )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        outcomes = await asyncio.gather(
            issue("MI/A", first_bin),
            issue("MI/B", second_bin),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        assert len(successes) == 1, f"the unit was issued twice: {outcomes}"
        assert len(failures) == 1
        refusal = failures[0]
        assert isinstance(refusal, AppError), f"unexpected failure: {refusal!r}"
        assert refusal.code in REFUSAL_CODES, f"wrong refusal: {refusal.code}"

        async with factory() as session:
            serial = await read_serial(session, scanned)
            assert serial.status is SerialStatus.ISSUED
            assert (await read_serial(session, spare)).status is SerialStatus.IN_STOCK

            # One issue posted, not two: exactly one of the two bins gave a unit
            # up, and the pair still holds one between them.
            on_hand = Decimal("0")
            for location_id in (first_bin, second_bin):
                position = await inv.get_balance(
                    session, tenant, item_id, warehouse_id, location_id=location_id
                )
                assert position is not None
                on_hand += Decimal(position.on_hand_quantity)
            assert on_hand == Decimal("1.0000")

            posted = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM inventory_transactions "
                        "WHERE serial_id = :serial "
                        "AND transaction_type = 'MATERIAL_ISSUE'"
                    ),
                    {"serial": scanned},
                )
            ).scalar_one()
            assert posted == 1

    # --- Why this case does not clean up after itself -------------------------
    #
    # It used to, from a ``finally:``, and its first two statements were::
    #
    #     DELETE FROM audit_logs WHERE company_id = :company
    #     DELETE FROM inventory_transactions WHERE item_id = :item
    #
    # The production schema forbids both, and always did. The initial migration
    # installs ``trg_audit_logs_immutable`` and
    # ``trg_inventory_transactions_immutable`` - ``BEFORE UPDATE OR DELETE ...
    # FOR EACH ROW EXECUTE FUNCTION atcraft_reject_row_mutation()`` - so a DELETE
    # that matches even one row raises SQLSTATE 42501. The audit log and the
    # stock ledger are append-only: a correction is a new offsetting row, never
    # an erased one (master rules section 41; docs/database.md 4.2 and 4.3).
    #
    # The DELETEs looked harmless only because this module builds its schema from
    # model metadata via ``create_all``, and metadata carries no triggers. Point
    # it at a migrated database - which is where the guarantee actually lives -
    # and the case fails in teardown. The tempting repair at that moment is to
    # weaken the trigger, trading a verified guarantee for a teardown
    # convenience. The convenience was given up instead, exactly as in
    # ``test_inventory_concurrency.py`` and ``test_allocation_concurrency.py``.
    #
    # Nothing needs deleting. ``seed_serialised_item`` mints a fresh item *and* a
    # fresh warehouse for this case, the bins and the serials are minted with it,
    # and every assertion is scoped to those ids. Rows left behind belong to an
    # item no other case names, so they are inert, and the module-scoped
    # ``_schema`` fixture drops the whole schema at the end - which disposes of
    # them without a statement any trigger could object to.


# -----------------------------------------------------------------------------
# Storage location validation
# -----------------------------------------------------------------------------


class TestLocationValidation:
    async def test_a_bin_that_does_not_exist_is_refused(self, ctx) -> None:
        """It has to be a typed error, not a foreign-key violation.

        The balance upsert would reach the constraint first and answer with an
        integrity error carrying the SQL, which is exactly what section 53
        forbids.
        """
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)

        with pytest.raises(NotFoundError, match="Storage location"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.OPENING_BALANCE,
                location_id=uuid.uuid4(),
                quantity="5",
                unit_cost=Decimal("10"),
            )

    async def test_another_tenants_bin_is_refused(self, ctx) -> None:
        """Not "forbidden" - not found. The other tenant's bins do not exist
        as far as this company is concerned, and saying otherwise confirms that
        an id is real."""
        session, tenant, other, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            other_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WH")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=other_warehouse.id
            )

        with pytest.raises(NotFoundError, match="Storage location"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.OPENING_BALANCE,
                location_id=foreign_bin.id,
                quantity="5",
                unit_cost=Decimal("10"),
            )

    async def test_a_deleted_bin_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=warehouse_id,
                deleted=True,
            )

        with pytest.raises(NotFoundError, match="Storage location"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.OPENING_BALANCE,
                location_id=retired.id,
                quantity="5",
                unit_cost=Decimal("10"),
            )

    async def test_a_bin_in_a_different_warehouse_is_refused(self, ctx) -> None:
        """The grain is (company, item, warehouse, location, batch). A bin from
        another warehouse makes that tuple describe a place that does not exist,
        and no warehouse view will ever show the stock booked into it."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WH")
            )
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=elsewhere.id
            )

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.OPENING_BALANCE,
                location_id=wrong_bin.id,
                quantity="5",
                unit_cost=Decimal("10"),
            )

    async def test_a_retired_bin_cannot_receive_stock(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            retired = await seed_bin(
                session,
                company_id=tenant.company_id,
                warehouse_id=warehouse_id,
                is_active=False,
            )

        with pytest.raises(BusinessRuleError, match="inactive"):
            await post(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                transaction_type=TransactionType.OPENING_BALANCE,
                location_id=retired.id,
                quantity="5",
                unit_cost=Decimal("10"),
            )

    async def test_a_retired_bin_can_still_give_its_stock_up(self, ctx) -> None:
        """The deliberate asymmetry, and the same one the item master and the
        warehouse already follow: refusing outbound movements would strand
        whatever is still in the bin behind the flag that retired it."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            bin_row = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=warehouse_id
            )
            bin_id = bin_row.id

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.OPENING_BALANCE,
            location_id=bin_id,
            quantity="5",
            unit_cost=Decimal("10"),
        )
        async with seeded(session):
            (
                await session.execute(select(StorageLocation).where(StorageLocation.id == bin_id))
            ).scalar_one().is_active = False

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.ADJUSTMENT_DECREASE,
            location_id=bin_id,
            quantity="2",
        )
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=bin_id)
        assert balance is not None
        assert balance.on_hand_quantity == Decimal("3.0000")

    async def test_a_valid_bin_keys_its_own_position(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id

        await post(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            transaction_type=TransactionType.OPENING_BALANCE,
            location_id=bin_id,
            quantity="7",
            unit_cost=Decimal("10"),
        )

        binned = await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=bin_id)
        assert binned is not None
        assert binned.on_hand_quantity == Decimal("7.0000")
        # A different grain, not the same row.
        assert await inv.get_balance(session, tenant, item_id, warehouse_id) is None

    async def test_one_bad_line_refuses_the_whole_batch(self, ctx) -> None:
        """A document is posted whole or not at all."""
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_plain_item(session, tenant)
            good_bin = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id

        movements = [
            inv.MovementRequest(
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("5"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OB/1",
                reason="opening",
                location_id=location_id,
                unit_cost=Decimal("10"),
            )
            for location_id in (good_bin, uuid.uuid4())
        ]
        with pytest.raises(NotFoundError, match="Storage location"):
            async with transaction(session):
                await inv.post_movements(session, tenant, movements)

        assert (
            await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=good_bin)
        ) is None


class TestSerialRegistration:
    """Where a unit is registered has to be a real position of this tenant.

    ``stock_serials.warehouse_id`` and ``.location_id`` carry the same
    company-blind foreign keys as a balance row, so the same phantom position
    can be written a day earlier by the goods-in bench keying a packing list.
    """

    async def test_a_unit_can_be_registered_before_its_stock_arrives(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bin_id = (
                await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
            ).id

        read = await inv.TraceabilityService(session, tenant).create_serial(
            StockSerialCreate(
                item_id=item_id,
                serial_number=_code("SN"),
                warehouse_id=warehouse_id,
                location_id=bin_id,
                unit_cost=Decimal("500"),
            )
        )
        assert read.status is SerialStatus.IN_STOCK
        assert read.location_id == bin_id

    async def test_another_tenants_bin_cannot_be_named(self, ctx) -> None:
        session, tenant, other, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WH")
            )
            foreign_bin = await seed_bin(
                session, company_id=other.company_id, warehouse_id=foreign_warehouse.id
            )

        with pytest.raises(NotFoundError, match="Storage location"):
            await inv.TraceabilityService(session, tenant).create_serial(
                StockSerialCreate(
                    item_id=item_id,
                    serial_number=_code("SN"),
                    warehouse_id=warehouse_id,
                    location_id=foreign_bin.id,
                )
            )

    async def test_another_tenants_warehouse_cannot_be_named(self, ctx) -> None:
        session, tenant, other, _ = ctx
        async with seeded(session):
            item_id, _ = await seed_serialised_item(session, tenant)
            foreign_warehouse = await seed_support.seed_warehouse(
                session, company_id=other.company_id, code=_code("WH")
            )

        with pytest.raises(NotFoundError, match="Warehouse"):
            await inv.TraceabilityService(session, tenant).create_serial(
                StockSerialCreate(
                    item_id=item_id,
                    serial_number=_code("SN"),
                    warehouse_id=foreign_warehouse.id,
                )
            )

    async def test_a_bin_from_a_different_warehouse_cannot_be_named(self, ctx) -> None:
        session, tenant, _, _ = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=_code("WH")
            )
            wrong_bin = await seed_bin(
                session, company_id=tenant.company_id, warehouse_id=elsewhere.id
            )

        with pytest.raises(BusinessRuleError, match="is not in that warehouse"):
            await inv.TraceabilityService(session, tenant).create_serial(
                StockSerialCreate(
                    item_id=item_id,
                    serial_number=_code("SN"),
                    warehouse_id=warehouse_id,
                    location_id=wrong_bin.id,
                )
            )


# -----------------------------------------------------------------------------
# Cost of a big document
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


class TestBatchCost:
    async def test_a_large_batch_reads_no_more_than_a_small_one(self, ctx) -> None:
        """The master-data reads and the two lock acquisitions are a fixed cost.

        One SELECT per line is the failure mode this guards: it is invisible on
        a two-line document and is a two-hundred-round-trip goods receipt in
        production (section 32). Inserts are not counted - one ledger row and
        one audit row per line is the work itself, not a lookup.
        """
        session, tenant, _, engine = ctx
        async with seeded(session):
            item_id, warehouse_id = await seed_serialised_item(session, tenant)
            bins = [
                (
                    await seed_bin(session, company_id=tenant.company_id, warehouse_id=warehouse_id)
                ).id
                for _ in range(40)
            ]
        serials = [await register_serial(session, tenant, item_id) for _ in range(40)]

        def receipts(count: int) -> list[inv.MovementRequest]:
            return [
                inv.MovementRequest(
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("1"),
                    transaction_type=TransactionType.PURCHASE_RECEIPT,
                    reference_type="goods_receipt",
                    reference_id=None,
                    reference_code="GRN/BIG",
                    reason="batch cost probe",
                    location_id=bins[index],
                    serial_id=serials[index],
                    unit_cost=Decimal("500"),
                )
                for index in range(count)
            ]

        with counted(engine) as small:
            async with transaction(session):
                await inv.post_movements(session, tenant, receipts(1))
        small_selects = [s for s in small if s.lstrip().upper().startswith("SELECT")]

        with counted(engine) as large:
            async with transaction(session):
                await inv.post_movements(session, tenant, receipts(40)[1:])
        large_selects = [s for s in large if s.lstrip().upper().startswith("SELECT")]

        assert len(large_selects) == len(small_selects), (
            f"a 39-line document read {len(large_selects)} times where a one-line "
            f"document read {len(small_selects)}:\n"
            + "\n".join(sorted(set(large_selects) - set(small_selects)))
        )
        # Four master-data lookups (the batch owners are not queried when no
        # line names a batch), the three locking reads - balances, FIFO cost
        # layers, serials - and the one that asks which of the units named have
        # been received before. Pinned so a new read added to the write path has
        # to be justified rather than noticed in production.
        #
        # It was seven before FIFO valuation. The eighth is the cost-layer lock,
        # and the property this test exists for is unchanged: it is one
        # statement for the whole document, keyed on the balance ids the lock
        # above just returned, so a thirty-nine-line receipt still reads exactly
        # as many times as a one-line one.
        assert len(small_selects) == 8, small_selects

        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is None  # every line landed in its own bin
        binned = await inv.get_balance(session, tenant, item_id, warehouse_id, location_id=bins[0])
        assert binned is not None
        assert binned.on_hand_quantity == Decimal("1.0000")
        assert (await read_serial(session, serials[39])).status is SerialStatus.IN_STOCK
