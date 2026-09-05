"""The test that was missing when every HTTP write was being discarded.

A service-level test supplies its own session. An API test that overrides
``get_db`` supplies the *test's* session. Neither exercises the one arrangement
production actually uses:

    get_db opens a request-scoped session
      -> the auth dependency reads the user on it (SQLAlchemy AUTOBEGINS here)
        -> the route calls a service
          -> the service opens `transaction(session)`
        -> get_db's finally closes the session

That autobegin is what broke it. ``transaction()`` inferred nesting from
``session.in_transaction()``, saw True, and opened a SAVEPOINT under a
transaction nobody owned. The savepoint released, the outer transaction was
never committed, and ``close()`` rolled the request back. 2857 tests passed
while no write through any route reached the database.

So these tests deliberately do NOT override ``get_db``. They run the real
dependency chain against a real database and then look for the row on a
different connection - the only way to tell a commit from a savepoint release.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import SessionFactory, transaction
from src.modules.organization.models import Company

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest_asyncio.fixture
async def checker(_database: None) -> AsyncIterator[AsyncSession]:
    """A session on its own connection, for reading what actually committed.

    Reading back through the same session would see uncommitted work in its own
    transaction and report success either way.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _insert_company(session: AsyncSession, company_id: uuid.UUID) -> None:
    """The one fixture row this file needs, built through the ORM.

    A hand-written INSERT names a column list that goes stale the moment the
    organisation module adds or removes one, and it skips the Python-side
    defaults many NOT NULL columns rely on (``country``, ``currency``,
    ``financial_year_start_month``, ``timezone``, ``is_active``) - the trap
    CLAUDE.md records having cost this suite 43 failures twice.
    """
    session.add(Company(id=company_id, code=f"RP{company_id.hex[:8].upper()}", name="Route Probe"))
    await session.flush()


async def _count(checker: AsyncSession, company_id: uuid.UUID) -> int:
    # A fresh transaction each time: a long-lived REPEATABLE READ snapshot on the
    # checker would hide a commit that landed after it started.
    await checker.rollback()
    return int(
        await checker.scalar(
            text("SELECT count(*) FROM companies WHERE id = :id"), {"id": company_id}
        )
        or 0
    )


async def _cleanup(checker: AsyncSession, company_id: uuid.UUID) -> None:
    await checker.execute(text("DELETE FROM companies WHERE id = :id"), {"id": company_id})
    await checker.commit()


async def test_a_write_after_a_dependency_read_actually_commits(checker: AsyncSession) -> None:
    """The regression itself: read first (as auth does), then write, then close."""
    company_id = uuid.uuid4()
    session = SessionFactory()
    try:
        # Stands in for get_current_principal loading the user. This is the
        # statement that autobegins the transaction.
        await session.execute(text("SELECT 1"))
        assert session.in_transaction(), (
            "expected SQLAlchemy to have autobegun - if this ever stops being "
            "true the bug this test guards has changed shape"
        )

        async with transaction(session):
            await _insert_company(session, company_id)
    finally:
        await session.close()  # exactly what get_db does

    assert await _count(checker, company_id) == 1, (
        "the row did not survive the request: transaction() opened a savepoint "
        "under the dependency's autobegun transaction and nothing committed it"
    )
    await _cleanup(checker, company_id)


async def test_a_failed_transaction_still_rolls_back(checker: AsyncSession) -> None:
    """The fix must not have turned rollback into commit."""
    company_id = uuid.uuid4()
    session = SessionFactory()
    try:
        await session.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError):
            async with transaction(session):
                await _insert_company(session, company_id)
                raise RuntimeError("a business rule refused this operation")
    finally:
        await session.close()

    assert await _count(checker, company_id) == 0, "a failed operation left a row behind"


async def test_a_nested_service_call_cannot_commit_past_a_failing_caller(
    checker: AsyncSession,
) -> None:
    """Nesting must still be a savepoint, not an independent commit.

    Two services composed into one operation have to succeed or fail together -
    master rules section 29's "never a partial business state".
    """
    company_id = uuid.uuid4()
    session = SessionFactory()
    try:
        await session.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError):
            async with transaction(session):
                await _insert_company(session, company_id)
                # An inner service opening its own boundary.
                async with transaction(session):
                    await session.execute(text("SELECT 1"))
                raise RuntimeError("the caller failed after the inner call returned")
    finally:
        await session.close()

    assert await _count(checker, company_id) == 0, (
        "the inner transaction() committed independently of its caller"
    )


async def test_two_sequential_operations_on_one_session_both_persist(
    checker: AsyncSession,
) -> None:
    """The flag must reset, or only the first write per request would commit."""
    first, second = uuid.uuid4(), uuid.uuid4()
    session = SessionFactory()
    try:
        await session.execute(text("SELECT 1"))
        async with transaction(session):
            await _insert_company(session, first)
        async with transaction(session):
            await _insert_company(session, second)
    finally:
        await session.close()

    assert await _count(checker, first) == 1, "the first operation did not persist"
    assert await _count(checker, second) == 1, (
        "the second operation did not persist - the business-transaction flag "
        "was left set by the first"
    )
    await _cleanup(checker, first)
    await _cleanup(checker, second)
