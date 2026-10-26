"""The shipped migration preserves traceability and enforces append-only links."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.inventory.models import StockSerialConversion
from tests.integration.test_schema_contract_migration import (
    _MigrationDatabase,
)
from tests.integration.test_schema_contract_migration import (
    migration_database as migration_database,
)
from tests.integration.test_serial_job_work import _issued, _receive, make_serial_kit
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration


def test_tracking_upgrade_roundtrip_has_no_model_drift(migration_database: _MigrationDatabase):
    db = migration_database
    db.alembic("upgrade", "head")
    db.alembic("check")
    engine = db.engine()
    try:
        inspector = sa.inspect(engine)
        assert "stock_serial_conversions" in inspector.get_table_names()
        assert "serial_dispositions" in {
            column["name"] for column in inspector.get_columns("qc_inspections")
        }
        db.alembic("downgrade", "20261027_0100")
        assert "stock_serial_conversions" not in sa.inspect(engine).get_table_names()
        db.alembic("upgrade", "head")
        db.alembic("check")
    finally:
        engine.dispose()


def test_converted_history_cannot_be_mutated_replayed_or_downgraded(
    migration_database: _MigrationDatabase,
):
    db = migration_database

    async def convert_real_units():
        engine = create_async_engine(
            db.url.set(drivername="postgresql+asyncpg"), poolclass=NullPool
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                company = await seed_company(session)
                user = await seed_user(session, company_id=company.id)
                await session.commit()
                tenant = TenantContext(company_id=company.id, user_id=user.id)
                principal = Principal(
                    company_id=company.id,
                    user_id=user.id,
                    email=user.email,
                    full_name=user.full_name,
                    is_superuser=False,
                    permissions=frozenset(str(permission) for permission in Permission),
                )
                kit = await make_serial_kit(session, tenant)
                order = await _issued(kit, convert=True)
                await _receive(
                    kit, principal, order.id, output_quantity=4, output_serial_ids=kit.ids
                )
        finally:
            await engine.dispose()

    asyncio.run(convert_real_units())
    engine = db.engine()
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.select(StockSerialConversion)).mappings().first()
            assert row is not None
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(StockSerialConversion))
                == 4
            )
        for statement in (
            sa.update(StockSerialConversion).values(output_serial_id=row["output_serial_id"]),
            sa.delete(StockSerialConversion),
        ):
            with pytest.raises(DBAPIError) as immutable, engine.begin() as connection:
                connection.execute(statement)
            assert immutable.value.orig.sqlstate == "42501"
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.insert(StockSerialConversion).values(
                    company_id=row["company_id"],
                    source_serial_id=row["source_serial_id"],
                    output_serial_id=row["output_serial_id"],
                    job_work_receipt_line_id=row["job_work_receipt_line_id"],
                )
            )
        with pytest.raises(AssertionError, match="downgrade would discard its traceability"):
            db.alembic("downgrade", "20261027_0100")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(StockSerialConversion))
                == 4
            )
        # Alembic commits each revision. A newer, reversible revision may have
        # rolled back before the protected tracking revision refuses removal.
        # Reapply that newer DDL before comparing the full current metadata.
        db.alembic("upgrade", "head")
        db.alembic("check")
    finally:
        engine.dispose()
