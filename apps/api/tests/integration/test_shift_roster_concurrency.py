"""Two supervisors planning the same operator into the same window at once.

The rule at stake: **an operator holds at most one PLANNED window at a time.**
The roster service checks it before writing, which is a check-then-act; it only
holds if nothing can be planned between the check and the write. Two defences
make it hold, and each is raced here on its own:

1. the per-company roster advisory lock, which serialises the two service calls
   so the second one *sees* the first and refuses with the clash named;
2. the ``ex_shift_assignments_operator_window`` EXCLUDE constraint, which refuses
   the loser even when the lock is bypassed - two raw writes that never asked
   the service anything.

This cannot be faked with one session, or with two sessions sharing a connection
inside an outer transaction: both serialise on the client side and the test
would pass with the locking removed. So this module commits for real, in its own
schema, and drops it at the end.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from datetime import time, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.core.messages import SHIFT_ASSIGNMENT_OVERLAP
from src.core.models import Base
from src.modules.routing.models import ShiftAssignment, ShiftAssignmentStatus
from src.modules.routing.schemas import ShiftAssignmentCreate
from src.modules.routing.service import ShiftRosterService, shift_window
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

prepare_metadata()

DAY = today() + timedelta(days=3)


def _schema_engine(schema: str) -> Any:
    """Engine pinned to the test schema, one connection per session (NullPool).

    NullPool matters: the two plans must hold two *different* PostgreSQL
    backends, so one can genuinely block on the other.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def roster_schema() -> Iterator[tuple[str, dict[str, uuid.UUID]]]:
    schema = f"it_roster_conc_{uuid.uuid4().hex[:10]}"
    ids = {
        name: uuid.uuid4()
        for name in ("company", "planner_a", "planner_b", "operator", "centre", "shift", "general")
    }

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                # The EXCLUDE constraints compare uuids with GiST.
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
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
                company = ids["company"]
                await seed_support.seed_company(session, company_id=company, prefix="ROST")
                for name in ("planner_a", "planner_b", "operator"):
                    await seed_support.seed_user(
                        session,
                        company_id=company,
                        user_id=ids[name],
                        email=f"{name}-{uuid.uuid4().hex[:8]}@atcraft.test",
                    )
                centre = await seed_support.seed_work_center(session, company_id=company)
                ids["centre"] = centre.id
                shift = await seed_support.seed_shift(session, company_id=company, code="A")
                ids["shift"] = shift.id
                general = await seed_support.seed_shift(
                    session,
                    company_id=company,
                    code="GEN",
                    start_time=time(9),
                    end_time=time(17, 30),
                    break_minutes=30,
                )
                ids["general"] = general.id
        finally:
            await eng.dispose()

    async def teardown() -> None:
        # Best effort, for the reason test_bom_concurrency.py gives: a schema
        # this run failed to drop is disposable, and an environment limit is not
        # a defect in the code under test.
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

    yield schema, ids
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def engine(roster_schema: tuple[str, dict[str, uuid.UUID]]) -> AsyncIterator[Any]:
    schema, _ = roster_schema
    eng = _schema_engine(schema)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _planned(engine: Any, ids: dict[str, uuid.UUID]) -> int:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        stmt = select(func.count()).where(
            ShiftAssignment.company_id == ids["company"],
            ShiftAssignment.operator_user_id == ids["operator"],
            ShiftAssignment.status == ShiftAssignmentStatus.PLANNED,
        )
        return int((await session.execute(stmt)).scalar_one())


async def _cancel_everything(engine: Any, ids: dict[str, uuid.UUID]) -> None:
    """Free the operator again, so each test starts from an empty plan."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session, session.begin():
        for row in (
            await session.execute(
                select(ShiftAssignment).where(ShiftAssignment.company_id == ids["company"])
            )
        ).scalars():
            row.status = ShiftAssignmentStatus.CANCELLED


async def test_two_supervisors_planning_one_operator_leave_exactly_one_plan(
    engine: Any, roster_schema: tuple[str, dict[str, uuid.UUID]]
) -> None:
    _, ids = roster_schema
    await _cancel_everything(engine, ids)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    gate = asyncio.Event()

    async def plan(planner: str, shift: str) -> str:
        tenant = TenantContext(company_id=ids["company"], user_id=ids[planner])
        async with factory() as session:
            await gate.wait()
            try:
                await ShiftRosterService(session, tenant).plan(
                    [
                        ShiftAssignmentCreate(
                            roster_date=DAY,
                            shift_id=ids[shift],
                            work_center_id=ids["centre"],
                            operator_user_id=ids["operator"],
                        )
                    ]
                )
            except BusinessRuleError as exc:
                return exc.message
            return "planned"

    # A on one screen, the general shift on the other: different shifts, the
    # same person, overlapping windows.
    first = asyncio.create_task(plan("planner_a", "shift"))
    second = asyncio.create_task(plan("planner_b", "general"))
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), timeout=60)

    assert sorted(outcomes) == sorted(["planned", SHIFT_ASSIGNMENT_OVERLAP])
    assert await _planned(engine, ids) == 1


async def test_two_raw_writes_past_the_service_still_leave_exactly_one_plan(
    engine: Any, roster_schema: tuple[str, dict[str, uuid.UUID]]
) -> None:
    """The EXCLUDE constraint is the backstop for a writer that took no lock.

    Two truly simultaneous inserts can also make PostgreSQL abort one of them
    with a deadlock while it resolves the exclusion check. Both outcomes are a
    refusal; the invariant under test is that the operator has exactly one
    PLANNED window. The service path, which takes the roster advisory lock, is
    tested above to return the named overlap error instead.
    """
    _, ids = roster_schema
    await _cancel_everything(engine, ids)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    gate = asyncio.Event()

    class _Times:
        start_time = time(6)
        end_time = time(14)
        break_minutes = 0

    starts_at, ends_at = shift_window(DAY, _Times(), "Asia/Kolkata")

    async def write(offset_minutes: int) -> str:
        async with factory() as session:
            await gate.wait()
            try:
                async with session.begin():
                    session.add(
                        ShiftAssignment(
                            company_id=ids["company"],
                            roster_date=DAY,
                            shift_id=ids["shift"],
                            work_center_id=ids["centre"],
                            operator_user_id=ids["operator"],
                            starts_at=starts_at + timedelta(minutes=offset_minutes),
                            ends_at=ends_at + timedelta(minutes=offset_minutes),
                        )
                    )
            except IntegrityError as exc:
                return "refused" if "ex_shift_assignments_operator_window" in str(exc) else "other"
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "40P01":
                    return "deadlocked"
                return "other"
            return "written"

    first = asyncio.create_task(write(0))
    second = asyncio.create_task(write(30))
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), timeout=60)

    assert sorted(outcomes) in (["deadlocked", "written"], ["refused", "written"])
    assert await _planned(engine, ids) == 1
