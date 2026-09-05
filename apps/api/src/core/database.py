"""Async database engine, session lifecycle and locking primitives.

Master rules sections 29/30: multi-step business operations are transactional,
connection pools are bounded, and no code creates a connection per request.

Session policy
--------------
One session per HTTP request, opened by :func:`get_db` and closed when the
response is finished. The *request* does not implicitly commit - services
declare their own transaction boundaries with :func:`transaction`, so a partial
business state can never be committed by accident.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, cast

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.config import AppEnv, settings


class _DMLResult(Protocol):
    """Structural type for SQLAlchemy results returned by write statements."""

    rowcount: int


def affected_rows(result: object) -> int:
    """Return the driver-reported row count for a completed DML statement.

    SQLAlchemy deliberately exposes ``rowcount`` only on cursor-backed write
    results, while ``AsyncSession.execute`` is typed as the broader ``Result``.
    Keeping the narrow cast here makes write repositories explicit without
    scattering unsafe casts throughout the codebase.
    """
    return int(cast(_DMLResult, result).rowcount or 0)


# -----------------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------------


def _create_engine() -> AsyncEngine:
    # Tests run against a single connection so a transaction can wrap the whole
    # test; production uses a bounded QueuePool.
    if settings.app_env is AppEnv.test:
        return create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            poolclass=NullPool,
            future=True,
        )

    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,  # a recycled connection killed by the DB must not surface as a 500
        future=True,
        connect_args={
            "server_settings": {
                # Server-side guard: a runaway report can never pin a backend
                # indefinitely (section 30).
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "idle_in_transaction_session_timeout": "60000",
                "application_name": settings.otel_service_name,
                "jit": "off",  # JIT costs more than it saves on OLTP-shaped queries
            },
        },
    )


engine: AsyncEngine = _create_engine()

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # response serialisation must not trigger lazy reloads
    autoflush=False,  # flushes happen where the service decides, not implicitly
)


# -----------------------------------------------------------------------------
# Request-scoped session dependency
# -----------------------------------------------------------------------------


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back anything left uncommitted. Services own their commits; a route
    that forgets to commit gets no silent write.
    """
    session = SessionFactory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone session for Celery workers, seeds and scripts."""
    session = SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# Marks a session that is already inside a business transaction. It is an
# attribute on the session rather than a ContextVar because nesting follows the
# session, not the task: two concurrent requests hold two sessions and must not
# see each other's depth.
_BUSINESS_TRANSACTION = "_atcraft_business_transaction"


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Explicit business transaction boundary.

    Nests safely: an inner call joins the outer business transaction as a
    savepoint instead of opening a second one, so a service can be called
    standalone or as part of a larger operation without changing its code.

        async with transaction(session):
            await create_production_order(...)
            await reserve_material(...)
            await write_audit(...)
        # one COMMIT, or one ROLLBACK - never a partial business state

    Nesting is decided by an explicit flag, NOT by ``session.in_transaction()``.

    That distinction is the whole point. SQLAlchemy *autobegins* a transaction on
    the first statement of any kind, so by the time a route handler runs, the
    authentication dependency has already read the user on the same
    request-scoped session and ``in_transaction()`` is True. Inferring nesting
    from it meant every service opened a SAVEPOINT under a transaction nobody
    owned; the savepoint released, the outer transaction was never committed,
    and ``get_db``'s ``close()`` rolled the whole request back. Every write
    through every HTTP route was silently discarded, and no test caught it
    because service tests supply their own session and API tests override the
    dependency.

    Committing here also commits whatever reads autobegan before it, which is
    harmless: those statements changed nothing.

    The body always runs inside a SAVEPOINT, even at the outermost level, so a
    failure unwinds *this operation* and nothing else. ``session.rollback()``
    would be wrong here: it discards everything uncommitted on the session,
    including work a caller did before this call. Under the test ``session``
    fixture - which joins the connection's transaction with
    ``join_transaction_mode="create_savepoint"`` - that reaches all the way back
    to the fixture's own savepoint and deletes the rows the test seeded, so an
    *expected* failure makes the next line fail with a bogus ``NotFoundError``.
    Only the outermost frame promotes the released savepoint to a real COMMIT.
    """
    if getattr(session, _BUSINESS_TRANSACTION, False):
        async with session.begin_nested():
            yield session
        return

    setattr(session, _BUSINESS_TRANSACTION, True)
    try:
        # On exception this context manager rolls back to its own savepoint and
        # re-raises, which is the whole of the cleanup we want. Anything written
        # before this call survives; the caller decides what to do next.
        async with session.begin_nested():
            yield session
        await session.commit()
    finally:
        setattr(session, _BUSINESS_TRANSACTION, False)


# -----------------------------------------------------------------------------
# Locking primitives - inventory correctness depends on these (section 10)
# -----------------------------------------------------------------------------

# Namespace keeps advisory-lock keys from colliding with any other subsystem
# that might use PostgreSQL advisory locks in the same database.
_ADVISORY_NAMESPACE = 0x4143  # "AC"


async def advisory_xact_lock(session: AsyncSession, *parts: str | uuid.UUID | int) -> None:
    """Take a transaction-scoped advisory lock on a logical key.

    Released automatically at COMMIT or ROLLBACK - there is no unlock path to
    forget. Used to serialise operations that are not a single row update, such
    as allocating one sales order across several warehouses, or generating the
    next document number in a series.

    Ordering note: callers that take several advisory locks must acquire them in
    a deterministic order (sort the keys) or they will deadlock each other.
    """
    key = "|".join(str(p) for p in parts)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": _ADVISORY_NAMESPACE, "key": key},
    )


async def try_advisory_xact_lock(session: AsyncSession, *parts: str | uuid.UUID | int) -> bool:
    """Non-blocking variant. Returns False if another transaction holds the lock."""
    key = "|".join(str(p) for p in parts)
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": _ADVISORY_NAMESPACE, "key": key},
    )
    return bool(result.scalar_one())


async def set_lock_timeout(session: AsyncSession, milliseconds: int) -> None:
    """Bound how long the current transaction waits for a row lock.

    An allocation request that cannot get its lock quickly should fail fast with
    a retryable error rather than holding an HTTP worker hostage.
    """
    await session.execute(text(f"SET LOCAL lock_timeout = '{int(milliseconds)}ms'"))


# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------


async def check_database_ready() -> bool:
    """Readiness probe: can we actually round-trip a query?"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    """Close the pool on application shutdown."""
    await engine.dispose()


@event.listens_for(engine.sync_engine, "connect")
def _on_connect(dbapi_connection: object, connection_record: object) -> None:
    """Hook point for per-connection setup; kept for observability wiring."""


__all__ = [
    "SessionFactory",
    "advisory_xact_lock",
    "affected_rows",
    "check_database_ready",
    "dispose_engine",
    "engine",
    "get_db",
    "session_scope",
    "set_lock_timeout",
    "transaction",
    "try_advisory_xact_lock",
]
