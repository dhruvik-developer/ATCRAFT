"""Shared test fixtures.

Two tiers, and the distinction matters for how fast the suite runs:

* **Unit tests** (`tests/unit`) import nothing from here beyond the pure helpers.
  They exercise business logic — BOM explosion, allocation ranking, GST maths,
  state machines — with no database at all, and must stay fast enough to run on
  every save.

* **Integration tests** (`tests/integration`, `tests/api`) need a real
  PostgreSQL. Not SQLite: the schema depends on `NUMERIC` semantics, generated
  columns, `ARRAY`, `JSONB`, partial indexes, `SELECT ... FOR UPDATE` and
  advisory locks. A SQLite stand-in would pass while the production schema was
  broken, which is worse than no test.

Start the infrastructure first:

    make dev      # or: docker compose ... up -d postgres redis
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

# Must be set before src.core.config is imported anywhere, since Settings is
# cached at import time.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-used-outside-the-suite-0123456789")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")



def _dotenv_defaults() -> dict[str, str]:
    """Read POSTGRES_*/REDIS_* defaults from the repository-root .env.

    The host ports are environment-specific — on a machine where 5432 is already
    taken, the compose stack publishes elsewhere. Reading the same file the
    application reads means `pytest` connects to the same database the developer
    is running, without anyone having to export variables first.
    """
    values: dict[str, str] = {}
    root = pathlib.Path(__file__).resolve().parents[3]
    for name in (".env",):
        path = root / name
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.split("#", 1)[0].strip().strip('"'))
    return values


_dotenv = _dotenv_defaults()


def _cfg(key: str, fallback: str) -> str:
    return os.environ.get(key) or _dotenv.get(key) or fallback


TEST_DB_NAME = _cfg("TEST_POSTGRES_DB", "atcraft_erp_test")
_pg_user = _cfg("POSTGRES_USER", "atcraft")
_pg_password = _cfg("POSTGRES_PASSWORD", "atcraft_dev_password")
_pg_host = _cfg("POSTGRES_HOST", "localhost")
_pg_port = _cfg("POSTGRES_PORT", "5432")

os.environ["DATABASE_URL"] = (
    f"postgresql+asyncpg://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{TEST_DB_NAME}"
)
os.environ["DATABASE_SYNC_URL"] = (
    f"postgresql+psycopg://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{TEST_DB_NAME}"
)

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.deps import Principal, TenantContext
from src.core.models import Base
from src.core.permissions import Permission
from src.core.security import hash_password
from tests.schema_support import prepare_metadata

ADMIN_DSN = f"postgresql+asyncpg://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/postgres"
TEST_DSN = os.environ["DATABASE_URL"]


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _database() -> None:
    """Create the test database and its schema once per session.

    Synchronous on purpose. A session-scoped *async* fixture would need a
    session-scoped event loop, which then has to be shared with every
    function-scoped test - and asyncpg pools are bound to the loop that created
    them. Running the setup in its own throwaway loop sidesteps all of that.

    The schema is built from the model metadata rather than by running Alembic.
    That is deliberate: these tests verify application behaviour against the
    schema the code believes in, while the migrations are verified separately by
    running them against a database. Conflating the two hides drift in both
    directions.
    """
    placeholders = prepare_metadata()
    if placeholders:
        print(  # noqa: T201 - visible in test output on purpose
            f"conftest: placeholder tables for modules not yet built: {sorted(placeholders)}"
        )

    async def _setup() -> None:
        admin_engine = create_async_engine(ADMIN_DSN, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": TEST_DB_NAME},
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        finally:
            await admin_engine.dispose()

        engine = create_async_engine(TEST_DSN, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                for extension in ("pgcrypto", "pg_trgm", "btree_gist"):
                    await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_setup())
    except Exception as exc:  # pragma: no cover - environment problem, not a failure
        pytest.skip(
            f"PostgreSQL is not reachable at {_pg_host}:{_pg_port} ({exc}). "
            "Start it with `make dev` before running integration tests.",
            allow_module_level=True,
        )


@pytest.fixture
async def engine(_database: None) -> AsyncIterator[Any]:
    """Per-test engine with NullPool.

    Function-scoped because pytest-asyncio gives each test a fresh event loop and
    an asyncpg connection cannot outlive the loop that opened it. NullPool keeps
    the cost to one connect per test rather than a pool that must be discarded.
    """
    eng = create_async_engine(TEST_DSN, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back at the end of the test.

    The outer transaction is owned here and never committed. Services under test
    use ``transaction(session)``, which nests as a SAVEPOINT thanks to
    ``join_transaction_mode="create_savepoint"`` — so a service can commit
    normally while the test still leaves the database untouched.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as test_session:
            yield test_session
        await transaction.rollback()


@pytest.fixture
async def committed_session(engine) -> AsyncIterator[AsyncSession]:
    """A session that really commits.

    Concurrency tests need this: two transactions cannot contend for a row lock
    if both live inside one outer transaction. Tests using it are responsible for
    their own cleanup, and should use unique codes so a leftover row cannot
    collide with the next run.
    """
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with maker() as real_session:
        yield real_session


# -----------------------------------------------------------------------------
# Baseline tenant fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
async def company(session: AsyncSession):
    from src.modules.organization.models import Company

    record = Company(
        code=f"TEST{uuid.uuid4().hex[:6].upper()}",
        name="Test Manufacturing Co",
        country="India",
        currency="INR",
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def other_company(session: AsyncSession):
    """A second tenant.

    Every module should have at least one test asserting that its queries cannot
    see this company's rows. Tenant isolation that is never tested is a claim,
    not a property.
    """
    from src.modules.organization.models import Company

    record = Company(
        code=f"OTHER{uuid.uuid4().hex[:5].upper()}",
        name="Other Manufacturing Co",
        country="India",
        currency="INR",
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def admin_role(session: AsyncSession, company):
    from src.modules.users.models import Role

    role = Role(
        company_id=company.id,
        code="TEST_ADMIN",
        name="Test Administrator",
        permissions=sorted(str(p) for p in Permission),
        is_system=False,
        is_active=True,
    )
    session.add(role)
    await session.flush()
    return role


@pytest.fixture
async def user(session: AsyncSession, company, admin_role):
    from src.modules.users.models import User

    record = User(
        company_id=company.id,
        email=f"tester-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Test User",
        password_hash=hash_password("TestPassword!2026"),
        is_active=True,
        is_superuser=False,
    )
    record.roles.append(admin_role)
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
def tenant(company, user) -> TenantContext:
    """The scope object every repository and service takes."""
    return TenantContext(company_id=company.id, user_id=user.id, is_superuser=False)


@pytest.fixture
def principal(company, user) -> Principal:
    return Principal(
        user_id=user.id,
        company_id=company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("TEST_ADMIN",),
    )


@pytest.fixture
def limited_principal(company, user) -> Principal:
    """A principal holding only read permissions.

    Use it to assert that a write endpoint refuses — a permission check that is
    only ever exercised by an all-powerful principal is untested.
    """
    return Principal(
        user_id=user.id,
        company_id=company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission if str(p).endswith(".view")),
        role_codes=("VIEWER",),
    )


# -----------------------------------------------------------------------------
# API client
# -----------------------------------------------------------------------------


@pytest.fixture
async def api_client(session: AsyncSession, principal: Principal):
    """HTTP client against the ASGI app, with auth and the DB session overridden.

    Authentication is bypassed on purpose: `tests/api` exists to exercise routes,
    serialisation and permission *decisions*. The login flow itself is covered by
    the auth module's own tests, and repeating it per request would add an Argon2
    verification to every case.
    """
    from httpx import ASGITransport, AsyncClient

    from src.core.database import get_db
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_principal] = lambda: principal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
        yield client

    app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def unique_code():
    """Collision-free business codes, so a leaked row cannot fail the next run."""

    def _make(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

    return _make


def assert_decimal(actual: object, expected: str, message: str = "") -> None:
    """Compare decimals by value, ignoring scale.

    ``Decimal("10.00") != Decimal("10.0000")`` under ``==`` on the string form,
    but they are the same quantity, and a scale difference is not a business
    difference.
    """
    actual_decimal = Decimal(str(actual))
    expected_decimal = Decimal(expected)
    assert actual_decimal == expected_decimal, (
        message or f"expected {expected_decimal}, got {actual_decimal}"
    )


@pytest.fixture
def decimal_equal():
    return assert_decimal
