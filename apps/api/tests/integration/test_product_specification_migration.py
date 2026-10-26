"""Revision 32 adds typed description tables without rewriting existing items."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.deps import TenantContext
from src.core.models import Base
from src.modules.products.models import Item, ItemSpecification, SpecificationValueType
from src.modules.products.specification_schemas import (
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    SpecificationAttributeCreate,
)
from src.modules.products.specification_service import SpecificationService
from tests import seed_support
from tests.conftest import TEST_DB_NAME
from tests.schema_support import prepare_metadata

pytestmark = pytest.mark.integration
prepare_metadata()
NEW_TABLES = {
    "specification_attributes",
    "category_specifications",
    "item_specifications",
    "item_accessories",
}
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/20261101_0100_product_specifications_accessories.py"
)


def _revision():
    spec = importlib.util.spec_from_file_location("product_specifications_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(connection: sa.Connection, command: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(_revision(), command)()


def _differences(connection: sa.Connection):
    # SQLAlchemy caches the default at first connect. Autogenerate must inspect
    # this isolated namespace, rather than another suite's public schema.
    previous = connection.dialect.default_schema_name
    connection.dialect.default_schema_name = connection.scalar(sa.text("SELECT current_schema()"))
    try:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        return compare_metadata(context, Base.metadata)
    finally:
        connection.dialect.default_schema_name = previous


@pytest.fixture(scope="module")
def _migration_schema() -> Iterator[str]:
    assert sa.engine.make_url(settings.database_url).database == TEST_DB_NAME
    schema = f"it_d5_migration_{uuid.uuid4().hex[:10]}"

    async def setup():
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}",public'))
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(
                        sync,
                        tables=[
                            table
                            for name, table in Base.metadata.tables.items()
                            if name not in NEW_TABLES
                        ],
                        checkfirst=False,
                    )
                )
        finally:
            await engine.dispose()

    async def teardown():
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    asyncio.run(setup())
    yield schema
    asyncio.run(teardown())


@pytest.fixture
async def migrated(
    _migration_schema: str,
) -> AsyncIterator[tuple[AsyncConnection, AsyncSession, TenantContext, uuid.UUID]]:
    engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{_migration_schema},public"}},
    )
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            async with factory() as session:
                company = await seed_support.seed_company(session)
                user = await seed_support.seed_user(session, company_id=company.id)
                item = await seed_support.seed_item(
                    session,
                    company_id=company.id,
                    code="D5-PRESERVED",
                    name="Existing item before specification tables",
                )
                tenant = TenantContext(company_id=company.id, user_id=user.id)
                item_id = item.id
                await session.commit()
                await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
                yield connection, session, tenant, item_id
            await outer.rollback()
    finally:
        await engine.dispose()


async def test_revision_adds_four_tables_matches_metadata_and_empty_round_trip_preserves_items(
    migrated,
):
    connection, session, _tenant, item_id = migrated
    assert _revision().revision == "20261101_0100"
    assert _revision().down_revision == "20261031_0100"
    assert await connection.run_sync(_differences) == []
    before = (
        (await session.execute(sa.select(Item.__table__).where(Item.id == item_id)))
        .mappings()
        .one()
    )
    await session.commit()
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    # Visibility is checked in the intended schema: public holds the shared
    # fixture's tables too and must never be mistaken for this revision's copy.
    schema = await connection.scalar(sa.text("SELECT current_schema()"))
    names = set(
        (
            await connection.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),
                {"schema": schema},
            )
        ).scalars()
    )
    assert names.isdisjoint(NEW_TABLES)
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    assert await connection.run_sync(_differences) == []
    after = (
        (await session.execute(sa.select(Item.__table__).where(Item.id == item_id)))
        .mappings()
        .one()
    )
    assert dict(after) == dict(before)


async def test_database_preserves_false_zero_enforces_master_type_and_refuses_populated_downgrade(
    migrated,
):
    connection, session, tenant, item_id = migrated
    service = SpecificationService(session, tenant)
    boolean = await service.create_attribute(
        SpecificationAttributeCreate(
            code="FIRE", name="Fire rated", value_type=SpecificationValueType.BOOLEAN
        )
    )
    number = await service.create_attribute(
        SpecificationAttributeCreate(
            code="KEYS", name="Keys", value_type=SpecificationValueType.NUMBER
        )
    )
    read = await service.replace_item_specifications(
        item_id,
        ItemSpecificationsReplace(
            values=[
                ItemSpecificationValueIn(specification_attribute_id=boolean.id, value_bool=False),
                ItemSpecificationValueIn(specification_attribute_id=number.id, value_number="0"),
            ]
        ),
    )
    assert {
        value.specification_attribute_id: value.value_bool
        if value.value_type is SpecificationValueType.BOOLEAN
        else value.value_number
        for value in read.values
    } == {boolean.id: False, number.id: 0}
    await session.commit()
    other = await seed_support.seed_item(session, company_id=tenant.company_id, code="D5-CHECK")
    other_id = other.id
    await session.commit()
    with pytest.raises(IntegrityError) as mismatch, session.no_autoflush:
        async with session.begin_nested():
            session.add(
                ItemSpecification(
                    company_id=tenant.company_id,
                    item_id=other_id,
                    specification_attribute_id=boolean.id,
                    value_type=SpecificationValueType.TEXT,
                    value_text="false",
                )
            )
            await session.flush()
    assert mismatch.value.orig.sqlstate == "23503"
    with pytest.raises(IntegrityError) as multiple_values:
        async with session.begin_nested():
            session.add(
                ItemSpecification(
                    company_id=tenant.company_id,
                    item_id=other_id,
                    specification_attribute_id=boolean.id,
                    value_type=SpecificationValueType.BOOLEAN,
                    value_bool=False,
                    value_number=0,
                )
            )
            await session.flush()
    assert multiple_values.value.orig.sqlstate == "23514"
    with pytest.raises(sa.exc.DBAPIError, match="downgrade would discard descriptive data"):
        async with connection.begin_nested():
            await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    after = await service.get_item_specifications(item_id)
    assert after == read
    assert await connection.run_sync(_differences) == []
