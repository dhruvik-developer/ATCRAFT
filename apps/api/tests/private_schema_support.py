"""A private PostgreSQL schema per test module, and a session in it that rolls back.

A suite that seeds once per module - two companies, the users who act, a unit or
a shift - cannot use the conftest ``session``, which starts every test from an
empty database. It builds a schema of its own instead: an engine whose search
path puts that schema first, a module-scoped fixture that creates the schema,
runs ``create_all`` in it, seeds it and drops it afterwards, and a per-test
session whose writes roll back - which a suite whose sessions really commit, as
the concurrency suites' do, goes without. Thirty-three modules wrote that
pattern out for themselves, and the copies had drifted where it mattered. Four
never called ``prepare_metadata()``, so their schema held only the tables their
own imports had registered - 83 of 156 for the quality suite run alone - and a
statement against any other table fell through the search path to ``public``.
One created ``btree_gist``, which the shift-roster EXCLUDE constraints in every
metadata cannot be created without; the other thirty-two worked because an
earlier run had installed it in the database.

``private_schema`` creates, seeds and drops the schema, ``schema_engine``
connects to it, and ``rolled_back_session`` hands a test a session whose writes
roll back. What stays in each module is what differs between them: the schema's
prefix, the rows it seeds, and the shape its fixtures yield.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.models import Base
from tests.schema_support import (
    DATABASE_EXTENSIONS,
    prepare_metadata,
    report_database_setup_failure,
)

#: Writes a schema's committed rows through the session it is handed.
Seed = Callable[[AsyncSession], Awaitable[None]]


def schema_engine(schema: str) -> AsyncEngine:
    """An engine whose connections look in ``schema`` first, one backend each.

    ``public`` stays on the search path because the extensions' operator classes
    live there. ``NullPool`` gives every session its own PostgreSQL backend -
    which a concurrency test needs, so that one session can genuinely block on
    another's row lock - and costs one connect per session rather than a pool to
    discard, since an asyncpg connection cannot outlive the event loop that
    opened it and each test runs on a fresh one.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@contextmanager
def private_schema(prefix: str, seed: Seed, *, best_effort_drop: bool = False) -> Iterator[str]:
    """Create ``<prefix>_<10 hex digits>`` with every table and ``seed``'s rows; drop it after.

    For a module-scoped fixture, which yields inside the block::

        with private_schema("it_quality", seed) as schema:
            yield schema, company_id, user_id

    Synchronous, driving its own throwaway event loop: a module-scoped *async*
    fixture would need a module-scoped loop, and asyncpg connections cannot be
    shared across the function-scoped loops pytest-asyncio gives each test.

    ``create_all`` runs with ``checkfirst=False``, and that is load-bearing: the
    conftest database already holds every table in ``public``, which stays on the
    search path, so ``checkfirst=True`` would find them there, create nothing
    here, and every write would land in the shared schema instead.

    ``seed`` runs in one session that commits, so its rows are what every test in
    the module starts from. Any set-up failure goes to
    ``report_database_setup_failure``, which skips only when PostgreSQL cannot be
    reached and re-raises a model that will not create or a seed the constraints
    refuse.

    ``best_effort_drop`` turns a failed ``DROP SCHEMA`` into a warning. The drop
    takes one lock per object in a single transaction, and a server left at the
    default ``max_locks_per_transaction`` can run the shared lock table out while
    another schema-per-module suite is up. A schema left behind is disposable,
    and the concurrency suites set it rather than report an environment limit as
    a defect in the code under test.
    """
    prepare_metadata()
    schema = f"{prefix}_{uuid.uuid4().hex[:10]}"
    try:
        asyncio.run(_create(schema, seed))
    except Exception as exc:  # pragma: no cover - the reporter decides what it means
        report_database_setup_failure(exc)
    yield schema
    asyncio.run(_drop(schema, best_effort=best_effort_drop))


@asynccontextmanager
async def rolled_back_session(schema: str, *, autoflush: bool) -> AsyncIterator[AsyncSession]:
    """A session in ``schema`` whose writes roll back when the block ends.

    The outer transaction is owned here and never committed, and
    ``join_transaction_mode="create_savepoint"`` nests a service's own
    ``transaction(...)`` blocks as savepoints - so a service commits normally
    while the test still leaves nothing behind, as with the conftest ``session``.

    ``autoflush`` has no default because the copies this replaced disagreed, and
    a suite's assertions can lean on either: most ran with SQLAlchemy's default,
    ``True``; the procurement, production and E5 quality suites and the serial
    boundaries run with ``False``, as the conftest ``session`` does.

    The session is bound to one connection, so ``session.bind.engine`` is the
    engine behind it - what ``tests/statement_support.counted`` listens on.
    """
    engine = schema_engine(schema)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            maker = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                autoflush=autoflush,
                join_transaction_mode="create_savepoint",
            )
            async with maker() as session:
                yield session
            await outer.rollback()
    finally:
        await engine.dispose()


async def _create(schema: str, seed: Seed) -> None:
    admin = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            for extension in DATABASE_EXTENSIONS:
                await connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
    finally:
        await admin.dispose()

    engine = schema_engine(schema)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(sync_connection, checkfirst=False)
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await seed(session)
    finally:
        await engine.dispose()


async def _drop(schema: str, *, best_effort: bool) -> None:
    admin = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        if not best_effort:
            raise
        warnings.warn(f"could not drop test schema {schema}: {exc}", stacklevel=1)
    finally:
        await admin.dispose()


__all__ = ["Seed", "private_schema", "rolled_back_session", "schema_engine"]
