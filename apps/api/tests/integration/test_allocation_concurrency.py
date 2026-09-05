"""Two orders, one lot of stock, and the promise that only one of them gets it.

Master rules section 10 names the scenario for movements; a reservation is the
same problem one step earlier, and gets it wrong more quietly. An over-issue
fails at the loading bay. An over-*reservation* succeeds, and two customers are
given the same delivery date - which nobody discovers until the second one is
short.

This cannot be faked. Two genuinely concurrent transactions, on two separate
connections, contending for the same ``inventory_balances`` row. A single
session, or two sessions sharing one connection inside an outer transaction,
would serialise on the client side and the test would pass with the locking
removed.

So this module commits for real: its own PostgreSQL schema, fresh identifiers per
case, schema dropped at the end.

Three properties are asserted:

1. exactly one of two competing reservations succeeds, and the loser's refusal is
   a typed business error rather than a constraint violation;
2. the balance never ends up promising more than it holds, whatever the
   interleaving;
3. reserve-then-consume leaves ``reserved_quantity`` and ``on_hand_quantity``
   both exactly right - the pair that must never be allowed to disagree.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, ErrorCode
from src.core.models import Base
from src.modules.allocation import service as alloc
from src.modules.allocation.models import (
    DemandType,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.inventory.repository import LedgerRepository
from src.modules.products.models import ItemGroup
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()

#: Error codes that mean "this request correctly did not get the stock".
REFUSAL_CODES = {
    ErrorCode.INSUFFICIENT_AVAILABLE_STOCK,
    ErrorCode.INSUFFICIENT_STOCK,
    ErrorCode.STOCK_LOCK_TIMEOUT,
}


def _schema_engine(schema: str):
    """Engine pinned to the test schema, one connection per session (NullPool).

    NullPool matters here: the point of the test is that two sessions hold two
    *different* PostgreSQL backends, so one can genuinely block on the other's
    row lock.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def concurrency_schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    schema = f"it_alloc_conc_{uuid.uuid4().hex[:10]}"
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
                await seed_support.seed_company(session, company_id=company_id, prefix="ACC")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"alloc-conc-{uuid.uuid4().hex[:8]}@atcraft.test",
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
async def engine(concurrency_schema: tuple[str, uuid.UUID, uuid.UUID]) -> AsyncIterator[Any]:
    schema, _, _ = concurrency_schema
    eng = _schema_engine(schema)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
def tenant(concurrency_schema: tuple[str, uuid.UUID, uuid.UUID]) -> TenantContext:
    _, company_id, user_id = concurrency_schema
    return TenantContext(company_id=company_id, user_id=user_id)


async def _seed_stock(
    engine: Any, tenant: TenantContext, *, quantity: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh item and warehouse holding ``quantity``. Committed for real.

    Minting both per case is what makes "no cleanup" (see the note below the
    assertions) correct: no case can ever see another case's rows. Do not make
    this reuse a position.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session, session.begin():
        warehouse = await seed_support.seed_warehouse(
            session,
            company_id=tenant.company_id,
            code=f"WH-{uuid.uuid4().hex[:8].upper()}",
            name="Store",
        )
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=f"CMP-{uuid.uuid4().hex[:8].upper()}",
            name="Handle",
            item_group=ItemGroup.COMPONENT,
            gst_rate=Decimal("18"),
            standard_cost=Decimal("10"),
        )
        warehouse_id, item_id = warehouse.id, item.id

    async with factory() as session:
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
                reason="allocation concurrency fixture opening stock",
                unit_cost=Decimal("10"),
            )
        await session.commit()

    return item_id, warehouse_id


async def _reserve(
    engine: Any,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    demand_code: str,
) -> uuid.UUID:
    """One reservation, in its own session on its own connection, committed."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            reservation = await alloc.reserve(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal(quantity),
                demand_type=DemandType.SALES_ORDER,
                demand_id=uuid.uuid4(),
                demand_code=demand_code,
                reservation_type=ReservationType.HARD,
                priority=100,
                reason="concurrent allocation",
            )
            await session.commit()
            return reservation.id
        except BaseException:
            await session.rollback()
            raise


async def _assert_consistent(
    engine: Any, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> tuple[Decimal, Decimal]:
    """The invariants that must hold whatever the interleaving.

    Returns ``(on_hand, reserved)``.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        on_hand = Decimal(balance.on_hand_quantity)
        reserved = Decimal(balance.reserved_quantity)

        assert on_hand >= 0, "on-hand went negative"
        assert reserved >= 0, "reserved went negative"
        assert reserved <= on_hand, "the balance promises more than it holds"
        assert balance.available_quantity >= 0, "available went negative"

        # The reservation rows and the balance must tell the same story: the sum
        # of what open reservations still hold is what the balance says is
        # reserved. This is the check that catches a lost update.
        outstanding = (
            await session.execute(
                select(func.coalesce(func.sum(StockReservation.remaining_quantity), 0)).where(
                    StockReservation.company_id == tenant.company_id,
                    StockReservation.item_id == item_id,
                    StockReservation.warehouse_id == warehouse_id,
                    StockReservation.status.in_(
                        [ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CONSUMED]
                    ),
                )
            )
        ).scalar_one()
        assert Decimal(str(outstanding)) == reserved, (
            f"reservations hold {outstanding} but the balance says {reserved}"
        )

        posted = await LedgerRepository(session, tenant).posted_quantity(
            item_id=item_id, warehouse_id=warehouse_id
        )
        assert posted == on_hand, f"ledger sum {posted} does not match balance {on_hand}"
        return on_hand, reserved


# --- Why no case cleans up after itself --------------------------------------
#
# There was a ``_cleanup`` helper here, called from a ``finally:`` in every
# case, whose first two statements were::
#
#     DELETE FROM audit_logs WHERE entity_type IN (...)
#     DELETE FROM inventory_transactions WHERE item_id = :item
#
# The production schema forbids both, and always did. The initial migration
# installs ``trg_audit_logs_immutable`` and
# ``trg_inventory_transactions_immutable`` - ``BEFORE UPDATE OR DELETE ... FOR
# EACH ROW EXECUTE FUNCTION atcraft_reject_row_mutation()`` - so a DELETE that
# matches even one row raises SQLSTATE 42501. The audit log and the stock ledger
# are append-only: a correction is a new offsetting row, never an erased one
# (master rules section 41; docs/database.md 4.2 and 4.3).
#
# The DELETEs looked harmless only because this module builds its schema from
# model metadata via ``create_all``, and metadata carries no triggers. Point the
# module at a migrated database - which is where these guarantees actually live
# - and every case fails in teardown. The tempting repair at that moment is to
# weaken the trigger, trading a real, verified guarantee for a teardown
# convenience. The convenience was given up instead.
#
# Nothing needs deleting. ``_seed_stock`` mints a fresh item *and* a fresh
# warehouse per case, and every assertion is scoped to that one position:
# ``get_balance(item_id, warehouse_id)``, ``posted_quantity(item_id=...,
# warehouse_id=...)`` and the ``StockReservation`` sum all filter on
# ``company_id`` + ``item_id`` + ``warehouse_id``. Rows an earlier case left
# behind belong to an item no later case ever names, so they are inert. The
# module-scoped fixture drops the entire schema at the end, which disposes of
# them without a statement any trigger could object to.
#
# A partial cleanup - deleting only ``stock_reservations`` and
# ``inventory_balances``, the two tables the triggers do permit - would be worse
# than none: it leaves ledger rows with no balance to reconcile against, which
# is precisely the inconsistency ``_assert_consistent`` exists to catch.
#
# So: do not reintroduce a DELETE here, and do not touch the triggers.


class TestCompetingReservations:
    async def test_available_100_reserve_80_and_50_only_one_wins(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Master rules section 10, applied to the promise rather than the issue.

        Both requests read "100 available" before either commits. Only the row
        lock stops both from succeeding, and the loser must come back with a
        typed refusal rather than a check-constraint violation.
        """
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        outcomes = await asyncio.gather(
            _reserve(engine, tenant, item_id, warehouse_id, "80", "SO/A"),
            _reserve(engine, tenant, item_id, warehouse_id, "50", "SO/B"),
            return_exceptions=True,
        )

        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        assert len(successes) == 1, f"both reservations were allowed: {outcomes}"
        assert len(failures) == 1

        refusal = failures[0]
        assert isinstance(refusal, AppError), f"unexpected failure: {refusal!r}"
        assert refusal.code in REFUSAL_CODES, f"wrong refusal: {refusal.code}"

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("100.0000"), "a reservation must not move on-hand"
        # Whichever won, the survivor's arithmetic has to be exact.
        assert reserved in (Decimal("80.0000"), Decimal("50.0000"))

    async def test_five_way_contention_never_over_promises(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Five requests for 30 against 100: at most three can be satisfied.

        The two-party case can pass by luck if the second request happens to run
        after the first has committed. Five contenders make a genuine queue, and
        the invariant still has to hold at the end of it.
        """
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        outcomes = await asyncio.gather(
            *(
                _reserve(engine, tenant, item_id, warehouse_id, "30", f"SO/{index}")
                for index in range(5)
            ),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(successes) == 3, f"expected exactly three winners, got {outcomes}"

        for failure in (o for o in outcomes if isinstance(o, BaseException)):
            assert isinstance(failure, AppError), f"unexpected failure: {failure!r}"
            assert failure.code in REFUSAL_CODES

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("100.0000")
        assert reserved == Decimal("90.0000")

    async def test_a_reservation_and_an_issue_contend_for_the_same_row(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """The reason reserve() borrows the movement engine's lock rather than
        inventing its own: a promise and a withdrawal are competing for the same
        100 units, and a second locking scheme would let both through."""
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")

        async def issue(quantity: str) -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                try:
                    async with transaction(session):
                        await inv.post_movement(
                            session,
                            tenant,
                            item_id=item_id,
                            warehouse_id=warehouse_id,
                            quantity=Decimal(quantity),
                            transaction_type=TransactionType.MATERIAL_ISSUE,
                            reference_type="material_issue",
                            reference_id=None,
                            reference_code="MI/RACE",
                            reason="concurrent draw",
                        )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        outcomes = await asyncio.gather(
            _reserve(engine, tenant, item_id, warehouse_id, "70", "SO/RACE"),
            issue("70"),
            return_exceptions=True,
        )
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(failures) == 1, f"both the promise and the issue succeeded: {outcomes}"
        assert isinstance(failures[0], AppError)
        assert failures[0].code in REFUSAL_CODES

        await _assert_consistent(engine, tenant, item_id, warehouse_id)


class TestReserveThenConsume:
    async def test_reserved_and_on_hand_are_both_correct_after_consuming(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """The pair that must never disagree.

        Reserve 60 of 100, then issue all 60. On-hand falls to 40 and reserved
        returns to 0 - and crucially the *ledger* agrees with on-hand, so the
        movement really was posted rather than the reservation quietly vanishing.
        """
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        reservation_id = await _reserve(engine, tenant, item_id, warehouse_id, "60", "SO/CONSUME")

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("100.0000"), "reserving must not move stock"
        assert reserved == Decimal("60.0000")

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            await alloc.consume(
                session,
                tenant,
                reservation_id=reservation_id,
                quantity=Decimal("60"),
                reference_type="dispatch",
                reference_id=None,
                reference_code="DSP/CONSUME",
            )
            await session.commit()

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("40.0000"), "the issue did not reach the balance"
        assert reserved == Decimal("0.0000"), "the reservation was not given up"

        async with factory() as session:
            reservation = (
                await session.execute(
                    select(StockReservation).where(StockReservation.id == reservation_id)
                )
            ).scalar_one()
            assert reservation.status is ReservationStatus.CONSUMED
            assert reservation.consumed_quantity == Decimal("60.0000")

    async def test_a_partial_consumption_leaves_the_two_columns_consistent(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Reserve 60, issue 25. On-hand 75, reserved 35, available 40 - and the
        three have to be mutually consistent, not merely individually plausible."""
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        reservation_id = await _reserve(engine, tenant, item_id, warehouse_id, "60", "SO/PARTIAL")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            await alloc.consume(
                session,
                tenant,
                reservation_id=reservation_id,
                quantity=Decimal("25"),
                reference_type="dispatch",
                reference_id=None,
                reference_code="DSP/PARTIAL",
            )
            await session.commit()

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("75.0000")
        assert reserved == Decimal("35.0000")

        async with factory() as session:
            balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
            assert balance is not None
            assert balance.available_quantity == Decimal("40.0000")

    async def test_concurrent_consumption_of_one_reservation_cannot_double_issue(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Two dispatch documents drawing the same reservation at once. The
        reservation row lock is what makes the second one see the first one's
        consumption rather than the value it read before."""
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        reservation_id = await _reserve(engine, tenant, item_id, warehouse_id, "40", "SO/DOUBLE")

        async def draw(code: str) -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                try:
                    await alloc.consume(
                        session,
                        tenant,
                        reservation_id=reservation_id,
                        quantity=Decimal("40"),
                        reference_type="dispatch",
                        reference_id=None,
                        reference_code=code,
                    )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        outcomes = await asyncio.gather(draw("DSP/X"), draw("DSP/Y"), return_exceptions=True)
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(successes) == 1, f"the reservation was consumed twice: {outcomes}"

        failure = next(o for o in outcomes if isinstance(o, BaseException))
        assert isinstance(failure, AppError)
        assert failure.code in REFUSAL_CODES | {ErrorCode.RESERVATION_ALREADY_CONSUMED}

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("60.0000"), "only one issue of 40 may have posted"
        assert reserved == Decimal("0.0000")


class TestConcurrentRelease:
    async def test_two_releases_of_one_reservation_cannot_double_refund(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Releasing the same reservation twice would lower reserved_quantity
        twice and hand the same stock to two other demands."""
        item_id, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        first = await _reserve(engine, tenant, item_id, warehouse_id, "40", "SO/REL1")
        await _reserve(engine, tenant, item_id, warehouse_id, "40", "SO/REL2")

        async def give_back() -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                try:
                    await alloc.release(
                        session, tenant, reservation_id=first, reason="order cancelled"
                    )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise

        outcomes = await asyncio.gather(give_back(), give_back(), return_exceptions=True)
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(successes) == 1, f"the reservation was released twice: {outcomes}"

        on_hand, reserved = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert on_hand == Decimal("100.0000")
        assert reserved == Decimal("40.0000"), "only the second reservation still holds stock"
