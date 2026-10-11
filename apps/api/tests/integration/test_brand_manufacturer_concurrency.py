"""Assigning a brand or a maker while someone else retires it.

Both sides are check-then-act. Deactivating or retiring asks "does anything
live still name this value?" and then writes; assigning asks "is this value
still offered?" and then writes. Interleave them and each check can pass
against a world the other is about to change:

* the deactivation's probe cannot see an item whose INSERT has not committed
  yet, so it finds nothing, deactivates, and the item then commits carrying a
  brand nothing offers any more - the exact state the probe exists to refuse;
* the assignment reads the brand's last *committed* version, which is still
  active, while an uncommitted deactivation of it sits one row lock away.

The foreign key does not save either case: its check takes ``FOR KEY SHARE``,
which an ``UPDATE`` that changes no key column does not conflict with. So the
service serialises the two itself - an assignment reads the row ``FOR SHARE``,
a deactivation or retirement reads it ``FOR NO KEY UPDATE`` - and whichever
arrives second waits, then decides on the first one's committed result.

This cannot be faked with one session, or with two sessions sharing one
connection inside an outer transaction: both serialise on the client and the
test would pass with the locking removed. So, like ``test_bom_concurrency.py``,
this module commits for real in a schema of its own and drops it at the end,
and each test *proves* that the second operation queued behind the first by
watching for it in ``pg_stat_activity`` before letting the first commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ErrorCode, NotFoundError, ValidationError
from src.core.models import Base
from src.modules.products import service
from src.modules.products.models import Brand, Item, ItemGroup, Manufacturer
from src.modules.products.schemas import BrandCreate, BrandUpdate, ItemCreate, ManufacturerCreate
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

prepare_metadata()

#: How long the second operation is given to queue behind the first. Generous,
#: because a loaded four-core box shares this database with other suites; the
#: wait normally shows up within a few milliseconds.
LOCK_WAIT_DEADLINE_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.05

Operation = Callable[[AsyncSession], Awaitable[Any]]


def _schema_engine(schema: str) -> Any:
    """One PostgreSQL backend per session (NullPool), pinned to this module's schema."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def conc_schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """A committed company, user and unit of measure, in a schema of their own."""
    schema = f"it_brand_conc_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    unit_ids: list[uuid.UUID] = []

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
            async with factory() as session, session.begin():
                await seed_support.seed_company(session, company_id=company_id, prefix="BRC")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email=f"brand-race-{uuid.uuid4().hex[:8]}@atcraft.test",
                )
                unit = await seed_support.seed_uom(session, company_id=company_id)
                unit_ids.append(unit.id)
        finally:
            await eng.dispose()

    async def teardown() -> None:
        # Best effort, for the reason test_bom_concurrency.py gives: a schema
        # this run failed to drop is disposable, and failing the suite over it
        # would report an environment limit as a defect in the code under test.
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception as exc:  # pragma: no cover - environment, not a test failure
            warnings.warn(f"could not drop test schema {schema}: {exc}", stacklevel=1)
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, user_id, unit_ids[0]
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def engine(conc_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]) -> AsyncIterator[Any]:
    schema, _, _, _ = conc_schema
    eng = _schema_engine(schema)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def tenant(conc_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]) -> TenantContext:
    _, company_id, user_id, _ = conc_schema
    return TenantContext(company_id=company_id, user_id=user_id)


@pytest.fixture
def unit_id(conc_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]) -> uuid.UUID:
    return conc_schema[3]


def _factory(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


def _item(unit_id: uuid.UUID, **overrides: Any) -> ItemCreate:
    data: dict[str, Any] = {
        "code": _code("RACE"),
        "name": "Raced item",
        "item_group": ItemGroup.COMPONENT,
        "base_uom_id": unit_id,
        "can_purchase": True,
    }
    data.update(overrides)
    return ItemCreate(**data)


async def _committed_brand(engine: Any, tenant: TenantContext) -> uuid.UUID:
    async with _factory(engine)() as session:
        brand = await service.create_brand(
            session, tenant, BrandCreate(code=_code("BR"), name="Raced brand")
        )
    return brand.id


async def _committed_maker(engine: Any, tenant: TenantContext) -> uuid.UUID:
    async with _factory(engine)() as session:
        maker = await service.create_manufacturer(
            session, tenant, ManufacturerCreate(code=_code("MK"), name="Raced maker")
        )
    return maker.id


async def _until_waiting_on_a_lock(engine: Any, pid: int, contender: asyncio.Task[Any]) -> None:
    """Return once backend ``pid`` is queued on a lock; fail if it finishes first.

    A contender that completes while the first operation is still uncommitted
    did not wait for it - which is the race, not a slow machine.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_WAIT_DEADLINE_SECONDS
    async with engine.connect() as probe:
        while True:
            if contender.done():
                pytest.fail(
                    "the second operation finished while the first was still uncommitted: "
                    "it never waited for the row lock, so nothing serialises assigning a "
                    "brand or maker against deactivating or retiring it"
                )
            waiting_on = (
                await probe.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one_or_none()
            # pg_stat_activity is snapshotted per transaction: end this one so
            # the next poll reads the backend's state afresh.
            await probe.rollback()
            if waiting_on == "Lock":
                return
            if loop.time() > deadline:
                pytest.fail(f"backend {pid} never queued behind the first operation's row lock")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _race(engine: Any, *, first: Operation, second: Operation) -> BaseException | None:
    """Run ``first`` up to its commit, prove ``second`` queues behind it, then commit.

    Returns what ``second`` raised once ``first`` had committed, or None if it
    succeeded.
    """
    factory = _factory(engine)
    async with factory() as holder, factory() as waiter:
        # The contender's own backend, read on the connection its transaction
        # will use: the session keeps it until that transaction ends.
        pid = (await waiter.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        contender: asyncio.Task[Any] | None = None
        try:
            async with transaction(holder):
                await first(holder)
                contender = asyncio.create_task(second(waiter))
                await _until_waiting_on_a_lock(engine, pid, contender)
            # `first` has committed. Only now may `second` read and decide.
            try:
                await contender
            except Exception as exc:  # the outcome under test
                return exc
            return None
        finally:
            await holder.rollback()
            if contender is not None:
                if not contender.done():
                    # Reached only after a failure above. The holder has rolled
                    # back, so the contender is released; collect it rather than
                    # abandon a task mid-statement.
                    with contextlib.suppress(Exception):
                        await contender
                elif not contender.cancelled():
                    contender.exception()  # retrieved, so asyncio does not warn
            await waiter.rollback()


async def _brand_row(engine: Any, brand_id: uuid.UUID) -> tuple[bool, bool]:
    """``(is_active, archived)`` as committed."""
    async with _factory(engine)() as session:
        row = (
            await session.execute(
                select(Brand.is_active, Brand.deleted_at).where(Brand.id == brand_id)
            )
        ).one()
    return bool(row.is_active), row.deleted_at is not None


async def _maker_row(engine: Any, maker_id: uuid.UUID) -> tuple[bool, bool]:
    async with _factory(engine)() as session:
        row = (
            await session.execute(
                select(Manufacturer.is_active, Manufacturer.deleted_at).where(
                    Manufacturer.id == maker_id
                )
            )
        ).one()
    return bool(row.is_active), row.deleted_at is not None


async def _count(engine: Any, statement: Any) -> int:
    async with _factory(engine)() as session:
        return int((await session.execute(statement)).scalar_one())


# --------------------------------------------------------------- the brand --


async def test_a_deactivation_waits_for_an_assignment_in_flight_and_then_refuses(
    engine: Any, tenant: TenantContext, unit_id: uuid.UUID
) -> None:
    brand_id = await _committed_brand(engine, tenant)

    outcome = await _race(
        engine,
        first=lambda s: service.create_item(s, tenant, _item(unit_id, brand_id=brand_id)),
        second=lambda s: service.update_brand(s, tenant, brand_id, BrandUpdate(is_active=False)),
    )

    assert isinstance(outcome, BusinessRuleError), outcome
    assert outcome.code == ErrorCode.REFERENCED_RECORD_IN_USE
    assert await _brand_row(engine, brand_id) == (True, False), "the brand stays offered"
    carrying = select(func.count()).select_from(Item).where(Item.brand_id == brand_id)
    assert await _count(engine, carrying) == 1


async def test_an_assignment_waits_for_a_deactivation_in_flight_and_then_refuses(
    engine: Any, tenant: TenantContext, unit_id: uuid.UUID
) -> None:
    brand_id = await _committed_brand(engine, tenant)

    outcome = await _race(
        engine,
        first=lambda s: service.update_brand(s, tenant, brand_id, BrandUpdate(is_active=False)),
        second=lambda s: service.create_item(s, tenant, _item(unit_id, brand_id=brand_id)),
    )

    # It read the brand after the deactivation committed, not the version
    # before it - so it was told the truth.
    assert isinstance(outcome, ValidationError), outcome
    assert await _brand_row(engine, brand_id) == (False, False)
    carrying = select(func.count()).select_from(Item).where(Item.brand_id == brand_id)
    assert await _count(engine, carrying) == 0, "no item may carry a brand nothing offers"


async def test_the_locked_read_is_a_real_re_read_when_the_session_already_holds_the_brand(
    engine: Any, tenant: TenantContext, unit_id: uuid.UUID
) -> None:
    """``populate_existing``: otherwise the lock is waited for and its answer ignored.

    SQLAlchemy hands back the identity map's copy of a row the session already
    holds. A session that read the brand before the deactivation committed would
    take the share lock, wait, re-read the row under it - and still decide on the
    active copy it had, so the item would land on a brand nothing offers.
    """
    brand_id = await _committed_brand(engine, tenant)

    async def assign_after_reading_it(session: AsyncSession) -> Any:
        held = await session.get(Brand, brand_id)
        assert held is not None and held.is_active, "read before the deactivation committed"
        return await service.create_item(session, tenant, _item(unit_id, brand_id=brand_id))

    outcome = await _race(
        engine,
        first=lambda s: service.update_brand(s, tenant, brand_id, BrandUpdate(is_active=False)),
        second=assign_after_reading_it,
    )

    assert isinstance(outcome, ValidationError), outcome
    carrying = select(func.count()).select_from(Item).where(Item.brand_id == brand_id)
    assert await _count(engine, carrying) == 0


# ---------------------------------------------------------------- the maker --


async def test_a_retirement_waits_for_an_assignment_in_flight_and_then_refuses(
    engine: Any, tenant: TenantContext, unit_id: uuid.UUID
) -> None:
    maker_id = await _committed_maker(engine, tenant)

    outcome = await _race(
        engine,
        first=lambda s: service.create_item(s, tenant, _item(unit_id, manufacturer_id=maker_id)),
        second=lambda s: service.delete_manufacturer(s, tenant, maker_id, reason="Closed down"),
    )

    assert isinstance(outcome, BusinessRuleError), outcome
    assert outcome.code == ErrorCode.REFERENCED_RECORD_IN_USE
    assert await _maker_row(engine, maker_id) == (True, False), "the maker stays live"


async def test_an_assignment_waits_for_a_retirement_in_flight_and_then_finds_nothing(
    engine: Any, tenant: TenantContext
) -> None:
    maker_id = await _committed_maker(engine, tenant)
    brand_code = _code("OWNED")

    outcome = await _race(
        engine,
        first=lambda s: service.delete_manufacturer(s, tenant, maker_id, reason="Closed down"),
        second=lambda s: service.create_brand(
            s,
            tenant,
            BrandCreate(code=brand_code, name="Owned by a retired maker", manufacturer_id=maker_id),
        ),
    )

    # The locked re-read no longer matches a live row: a retired maker is gone,
    # exactly as it would be to a request that arrived a moment later.
    assert isinstance(outcome, NotFoundError), outcome
    assert await _maker_row(engine, maker_id) == (False, True)
    owned = select(func.count()).select_from(Brand).where(Brand.code == brand_code)
    assert await _count(engine, owned) == 0
