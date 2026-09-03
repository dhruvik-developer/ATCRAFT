"""The scenario master rules section 10 names explicitly.

    "Available = 100; Order A requests 80; Order B requests 50 - the system must
    never allow both."

This is the test the whole locking design exists for, and it cannot be faked:
two genuinely concurrent transactions, on two separate connections, contending
for the same balance row. A single session, or two sessions sharing one
connection inside an outer transaction, would serialise on the client side and
the test would pass with the locking removed.

So this module commits for real. It works inside its own PostgreSQL schema,
seeds each case with fresh identifiers, and drops the schema at the end.

Four properties are asserted, in order of severity:

1. exactly one of two competing requests succeeds;
2. on-hand never goes negative, whatever the interleaving;
3. the ledger reconciles to the balance - the sum of the movements that changed
   on-hand equals what the balance table says is there;
4. no FIFO cost layer is spent twice. That one is quieter than the others and
   needs its own class: two issues that both drew fifty units from the same
   sixty-unit layer leave the *quantities* perfectly correct and understate the
   cost of goods issued, so it is asserted on the money.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, ErrorCode
from src.core.models import Base
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.inventory.repository import LedgerRepository
from tests import seed_support
from tests.schema_support import prepare_metadata

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
    ErrorCode.NEGATIVE_STOCK_BLOCKED,
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
    schema = f"it_inv_conc_{uuid.uuid4().hex[:10]}"
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
                await seed_support.seed_company(session, company_id=company_id, prefix="CONC")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"conc-{uuid.uuid4().hex[:8]}@atcraft.test",
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
        pytest.skip(f"PostgreSQL is not available for concurrency tests: {exc}")

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
    engine: Any, tenant: TenantContext, *, quantity: str, items: int = 1
) -> tuple[list[uuid.UUID], uuid.UUID]:
    """Create fresh items and a warehouse holding ``quantity`` of each. Committed.

    Minting both per case is what makes "no cleanup" (see the note below the
    assertions) correct: no case can ever see another case's rows. Do not make
    this reuse a position.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    item_ids: list[uuid.UUID] = []
    async with factory() as session, session.begin():
        warehouse = await seed_support.seed_warehouse(
            session,
            company_id=tenant.company_id,
            code=f"WH-{uuid.uuid4().hex[:8].upper()}",
            name="Store",
        )
        warehouse_id = warehouse.id
        for _ in range(items):
            item = await seed_support.seed_item(
                session,
                company_id=tenant.company_id,
                code=f"RM-{uuid.uuid4().hex[:8].upper()}",
                name="Rod",
                gst_rate=Decimal("18"),
                standard_cost=Decimal("10"),
            )
            item_ids.append(item.id)

    async with factory() as session:
        async with transaction(session):
            await inv.post_movements(
                session,
                tenant,
                [
                    inv.MovementRequest(
                        item_id=item_id,
                        warehouse_id=warehouse_id,
                        quantity=Decimal(quantity),
                        transaction_type=TransactionType.OPENING_BALANCE,
                        reference_type="opening",
                        reference_id=None,
                        reference_code="OPENING",
                        reason="concurrency fixture opening stock",
                        unit_cost=Decimal("10"),
                    )
                    for item_id in item_ids
                ],
            )
        await session.commit()

    return item_ids, warehouse_id


async def _issue(
    engine: Any,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    reference_code: str,
) -> None:
    """One issue, in its own session on its own connection, committed."""
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
                    reference_code=reference_code,
                    reason="concurrent draw",
                )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _seed_layers(
    engine: Any, tenant: TenantContext, layers: Sequence[tuple[str, str]]
) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh item and warehouse holding one cost layer per ``(quantity, price)``.

    Each receipt is its own committed transaction, so the layers get distinct
    ``occurred_at`` stamps and the FIFO order under test is the one the prices
    were listed in. Fresh identifiers per case, for the same reason
    :func:`_seed_stock` mints them: no case may see another case's rows.
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
            code=f"RM-{uuid.uuid4().hex[:8].upper()}",
            name="Rod",
            gst_rate=Decimal("18"),
            standard_cost=Decimal("0"),
        )
        warehouse_id, item_id = warehouse.id, item.id

    for quantity, unit_cost in layers:
        async with factory() as session:
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
                    reference_code="GRN/LAYER",
                    reason="layered fixture stock",
                    unit_cost=Decimal(unit_cost),
                )
            await session.commit()

    return item_id, warehouse_id


async def _assert_layers_hold(
    engine: Any, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> None:
    """No layer gave up more than it held, and the layers add up to the position.

    The first check is the direct one: a layer that was consumed twice has more
    quantity drawn from it than it ever contained. The database's own
    ``remaining_quantity >= 0`` check would also have caught it, but only if the
    two transactions serialised - the point of asserting it here is that it is
    checked after the fact, against what actually committed.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None

        over_drawn = (
            await session.execute(
                text(
                    "SELECT l.id, l.original_quantity, sum(c.quantity) AS drawn "
                    "FROM inventory_cost_layers l "
                    "JOIN inventory_cost_consumptions c ON c.layer_id = l.id "
                    "WHERE l.balance_id = :balance "
                    "GROUP BY l.id, l.original_quantity "
                    "HAVING sum(c.quantity) > l.original_quantity"
                ),
                {"balance": balance.id},
            )
        ).all()
        assert not over_drawn, f"a cost layer was consumed more than once: {over_drawn}"

        quantity, value = (
            await session.execute(
                text(
                    "SELECT coalesce(sum(remaining_quantity), 0), "
                    "coalesce(sum(remaining_quantity * unit_cost), 0) "
                    "FROM inventory_cost_layers WHERE balance_id = :balance"
                ),
                {"balance": balance.id},
            )
        ).one()
        assert Decimal(quantity) == Decimal(balance.on_hand_quantity), (
            f"layers hold {quantity} but the position holds {balance.on_hand_quantity}"
        )
        assert Decimal(value).quantize(Decimal("0.0001")) == Decimal(balance.stock_value), (
            f"layers are worth {value} but the position claims {balance.stock_value}"
        )


async def _cost_of_goods_issued(
    engine: Any, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Decimal:
    """What every outbound movement at this position cost, from the layers it drew."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        total = (
            await session.execute(
                text(
                    "SELECT coalesce(sum(c.total_cost), 0) "
                    "FROM inventory_cost_consumptions c "
                    "JOIN inventory_transactions t ON t.id = c.transaction_id "
                    "WHERE t.item_id = :item AND t.warehouse_id = :warehouse"
                ),
                {"item": item_id, "warehouse": warehouse_id},
            )
        ).scalar_one()
    return Decimal(total)


async def _assert_consistent(
    engine: Any,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
) -> Decimal:
    """On-hand is non-negative and equals the ledger's net movement. Returns on-hand."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert balance.on_hand_quantity >= 0, "on-hand went negative"
        assert balance.available_quantity >= 0, "available went negative"

        posted = await LedgerRepository(session, tenant).posted_quantity(
            item_id=item_id, warehouse_id=warehouse_id
        )
        assert posted == balance.on_hand_quantity, (
            f"ledger sum {posted} does not match balance {balance.on_hand_quantity}"
        )
        return Decimal(balance.on_hand_quantity)


# --- Why no case cleans up after itself --------------------------------------
#
# There was a ``_cleanup`` helper here, called from a ``finally:`` in every
# case, whose first two statements were::
#
#     DELETE FROM audit_logs WHERE entity_type = 'inventory_transaction'
#     DELETE FROM inventory_transactions WHERE item_id = ANY(:ids)
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
# Nothing needs deleting. ``_seed_stock`` mints fresh items *and* a fresh
# warehouse per case, and every assertion is scoped to those positions:
# ``get_balance(item_id, warehouse_id)``, ``posted_quantity(item_id=...,
# warehouse_id=...)`` and the balance-row count below all filter on
# ``company_id`` and/or ``item_id`` + ``warehouse_id``. Rows an earlier case
# left behind belong to items no later case ever names, so they are inert. The
# module-scoped fixture drops the entire schema at the end, which disposes of
# them without a statement any trigger could object to.
#
# A partial cleanup - deleting only ``inventory_balances``, the one table of the
# three the triggers do permit - would be worse than none: it leaves ledger rows
# with no balance to reconcile against, which is precisely the inconsistency
# ``_assert_consistent`` exists to catch.
#
# So: do not reintroduce a DELETE here, and do not touch the triggers.


class TestCompetingRequests:
    async def test_available_100_requests_80_and_50_only_one_wins(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """The scenario from master rules section 10, verbatim."""
        item_ids, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        item_id = item_ids[0]
        outcomes = await asyncio.gather(
            _issue(engine, tenant, item_id, warehouse_id, "80", "MI/A"),
            _issue(engine, tenant, item_id, warehouse_id, "50", "MI/B"),
            return_exceptions=True,
        )

        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        assert len(successes) == 1, f"both requests were allowed: {outcomes}"
        assert len(failures) == 1

        refusal = failures[0]
        assert isinstance(refusal, AppError), f"unexpected failure: {refusal!r}"
        assert refusal.code in REFUSAL_CODES, f"wrong refusal: {refusal.code}"

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        # Whichever won, the survivor's arithmetic has to be exact.
        assert remaining in (Decimal("20.0000"), Decimal("50.0000"))

    async def test_five_way_contention_never_oversells(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Five requests for 30 against 100: at most three can be satisfied.

        The two-party case can pass by luck if the second request simply happens
        to run after the first has committed. Five contenders make a genuine
        queue, and the invariant still has to hold at the end of it.
        """
        item_ids, warehouse_id = await _seed_stock(engine, tenant, quantity="100")
        item_id = item_ids[0]
        outcomes = await asyncio.gather(
            *(
                _issue(engine, tenant, item_id, warehouse_id, "30", f"MI/{index}")
                for index in range(5)
            ),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(successes) == 3, f"expected exactly three winners, got {outcomes}"

        for failure in (o for o in outcomes if isinstance(o, BaseException)):
            assert isinstance(failure, AppError), f"unexpected failure: {failure!r}"
            assert failure.code in REFUSAL_CODES

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert remaining == Decimal("10.0000")

    async def test_concurrent_receipts_do_not_lose_a_movement(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Two first receipts race to create the same balance row.

        Without the ``INSERT ... ON CONFLICT`` upsert one of them would fail on
        the unique index, or worse, both would insert and the position would be
        split across two rows that nothing ever adds together.
        """
        item_ids, warehouse_id = await _seed_stock(engine, tenant, quantity="1")
        item_id = item_ids[0]

        async def receive(quantity: str, code: str) -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
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
                        reference_code=code,
                        reason="concurrent receipt",
                        unit_cost=Decimal("10"),
                    )
                await session.commit()

        outcomes = await asyncio.gather(
            receive("40", "GRN/A"), receive("60", "GRN/B"), return_exceptions=True
        )
        assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert remaining == Decimal("101.0000")

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM inventory_balances "
                        "WHERE item_id = :item AND warehouse_id = :wh"
                    ),
                    {"item": item_id, "wh": warehouse_id},
                )
            ).scalar_one()
        assert rows == 1, "the position was split across two balance rows"


class TestCostLayerContention:
    """Two issues must not spend the same cost layer twice.

    Consuming a layer is a read-modify-write on ``remaining_quantity``, which is
    the same shape of race the balance row lock exists for and needs the same
    answer. It is also a *quieter* failure than overselling: two issues that
    both drew 50 units from a 60-unit layer would leave the quantities perfectly
    correct - 100 issued, nothing on hand - while the cost of goods issued was
    understated and the value left in the layers was a number nothing in the
    system could explain.

    So the assertion is on the money, not on the quantity. The total cost of the
    winning issues is *fixed* by FIFO whatever order they run in - the first
    ninety units of a queue cost what they cost - and any double consumption
    changes it.
    """

    async def test_two_issues_cannot_draw_the_same_layer_twice(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Both succeed - there is enough stock - and together they cost 1 400.

        60 @ 10 then 40 @ 20. Whichever issue wins the lock first takes 50 @ 10;
        the other takes the last 10 @ 10 and then 40 @ 20. 500 + 900 = 1 400,
        which is exactly the value that was in the layers. Two issues both
        reading "60 remaining at 10" would come to 500 + 500 = 1 000 and leave
        400 of value stranded in a layer nothing owns.
        """
        item_id, warehouse_id = await _seed_layers(
            engine, tenant, [("60", "10"), ("40", "20")]
        )
        outcomes = await asyncio.gather(
            _issue(engine, tenant, item_id, warehouse_id, "50", "MI/L1"),
            _issue(engine, tenant, item_id, warehouse_id, "50", "MI/L2"),
            return_exceptions=True,
        )
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert not failures, f"a request that had stock was refused: {failures}"

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert remaining == Decimal("0.0000")
        await _assert_layers_hold(engine, tenant, item_id, warehouse_id)
        assert await _cost_of_goods_issued(engine, tenant, item_id, warehouse_id) == Decimal(
            "1400.0000"
        )

    async def test_five_contenders_still_cost_the_first_ninety_units(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Three of five requests for 30 win, and FIFO fixes what they cost.

        40 @ 10, 30 @ 20, 30 @ 30 - 1 900 of value over 100 units. Exactly
        ninety units can be issued, and the first ninety in FIFO order cost
        400 + 600 + 600 = 1 600 regardless of which three requests won or in
        what order they interleaved. The 10 units left are the dearest ones, at
        300.
        """
        item_id, warehouse_id = await _seed_layers(
            engine, tenant, [("40", "10"), ("30", "20"), ("30", "30")]
        )
        outcomes = await asyncio.gather(
            *(
                _issue(engine, tenant, item_id, warehouse_id, "30", f"MI/LAYER{index}")
                for index in range(5)
            ),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(successes) == 3, f"expected exactly three winners, got {outcomes}"
        for failure in (o for o in outcomes if isinstance(o, BaseException)):
            assert isinstance(failure, AppError), f"unexpected failure: {failure!r}"
            assert failure.code in REFUSAL_CODES

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert remaining == Decimal("10.0000")
        await _assert_layers_hold(engine, tenant, item_id, warehouse_id)
        assert await _cost_of_goods_issued(engine, tenant, item_id, warehouse_id) == Decimal(
            "1600.0000"
        )

    async def test_a_receipt_racing_an_issue_leaves_the_layers_reconciled(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """The other direction: one posting opens a layer while another spends one.

        Both succeed, and the invariant has to survive either interleaving - the
        issue drawing on the receipt's new layer, or finishing before it exists.
        """
        item_id, warehouse_id = await _seed_layers(engine, tenant, [("50", "10")])

        async def receive(quantity: str, unit_cost: str, code: str) -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
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
                        reference_code=code,
                        reason="concurrent receipt",
                        unit_cost=Decimal(unit_cost),
                    )
                await session.commit()

        outcomes = await asyncio.gather(
            receive("50", "30", "GRN/RACE"),
            _issue(engine, tenant, item_id, warehouse_id, "50", "MI/RACE"),
            return_exceptions=True,
        )
        assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes

        remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
        assert remaining == Decimal("50.0000")
        await _assert_layers_hold(engine, tenant, item_id, warehouse_id)
        # The issue takes the cheap layer whether it ran before or after the
        # receipt, because FIFO orders on arrival time and the receipt is newer.
        assert await _cost_of_goods_issued(engine, tenant, item_id, warehouse_id) == Decimal(
            "500.0000"
        )


class TestDeadlockAvoidance:
    async def test_two_documents_touching_the_same_items_in_opposite_order(
        self, engine: Any, tenant: TenantContext
    ) -> None:
        """Both must succeed: the engine locks by primary key, not by line order.

        This is the failure the ordered locking exists to prevent. Document A
        lists item 1 then item 2; document B lists item 2 then item 1. If each
        locked its lines in the order they were written, the two would hold one
        row each and wait forever for the other - PostgreSQL would break the tie
        by aborting one of them with a deadlock error.
        """
        item_ids, warehouse_id = await _seed_stock(engine, tenant, quantity="100", items=2)
        first, second = item_ids

        async def issue_pair(order: list[uuid.UUID], code: str) -> None:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                async with transaction(session):
                    await inv.post_movements(
                        session,
                        tenant,
                        [
                            inv.MovementRequest(
                                item_id=item_id,
                                warehouse_id=warehouse_id,
                                quantity=Decimal("10"),
                                transaction_type=TransactionType.MATERIAL_ISSUE,
                                reference_type="material_issue",
                                reference_id=None,
                                reference_code=code,
                                reason="kit issue",
                            )
                            for item_id in order
                        ],
                    )
                await session.commit()

        outcomes = await asyncio.gather(
            issue_pair([first, second], "MI/FWD"),
            issue_pair([second, first], "MI/REV"),
            return_exceptions=True,
        )
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert not failures, f"a document was aborted: {failures}"

        for item_id in item_ids:
            remaining = await _assert_consistent(engine, tenant, item_id, warehouse_id)
            assert remaining == Decimal("80.0000")
