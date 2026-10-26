"""Packing migration preserves known QC identities and never invents legacy ones."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.deps import TenantContext
from src.modules.packing import service as packing
from src.modules.packing.models import PackingOrderLine
from src.modules.quality.models import QcInspection
from tests.integration.test_packing_final_serials import make_packing_kit, start
from tests.integration.test_schema_contract_migration import _MigrationDatabase
from tests.integration.test_schema_contract_migration import (
    migration_database as migration_database,
)
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration


def test_packing_selection_upgrade_preserves_identity_and_protects_downgrade(
    migration_database: _MigrationDatabase,
):
    db = migration_database
    db.alembic("upgrade", "head")
    db.alembic("check")

    async def prepare_previous_revision_history():
        engine = create_async_engine(
            db.url.set(drivername="postgresql+asyncpg"), poolclass=NullPool
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                company = await seed_company(session)
                user = await seed_user(session, company_id=company.id)
                await session.commit()
                tenant = TenantContext(company_id=company.id, user_id=user.id)
                named = await make_packing_kit(session, tenant)
                await start(named)
                legacy = await make_packing_kit(session, tenant)
                legacy_inspection = await start(legacy)
                # Stage the prior revision's representation: line selections did
                # not exist, and some historical inspections named only quantities.
                await session.execute(
                    sa.update(PackingOrderLine).values(qc_serial_ids=[], qc_batch_id=None)
                )
                await session.execute(
                    sa.update(QcInspection)
                    .where(QcInspection.id == legacy_inspection)
                    .values(serial_ids=[], serial_dispositions={})
                )
                await session.commit()
                return named.line_id, named.ids[:2], legacy.line_id
        finally:
            await engine.dispose()

    named_id, selected_ids, legacy_id = asyncio.run(prepare_previous_revision_history())
    db.alembic("downgrade", "20261029_0100")
    engine = db.engine()
    try:
        assert "qc_serial_ids" not in {
            column["name"] for column in sa.inspect(engine).get_columns("packing_order_lines")
        }
        db.alembic("upgrade", "head")
        db.alembic("check")
        with engine.connect() as connection:
            rows = dict(
                connection.execute(
                    sa.select(PackingOrderLine.id, PackingOrderLine.qc_serial_ids)
                ).all()
            )
            assert rows[named_id] == selected_ids
            assert rows[legacy_id] == []
        with pytest.raises(AssertionError, match="downgrade would discard traceability"):
            db.alembic("downgrade", "20261029_0100")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(PackingOrderLine.qc_serial_ids).where(PackingOrderLine.id == named_id)
                )
                == selected_ids
            )
        # Revision 31 may have rolled back before protected revision 30 refused.
        db.alembic("upgrade", "head")
        db.alembic("check")
    finally:
        engine.dispose()


def test_withdrawn_qc_history_cannot_be_downgraded(migration_database: _MigrationDatabase):
    db = migration_database
    db.alembic("upgrade", "head")

    async def withdraw_real_inspection():
        engine = create_async_engine(
            db.url.set(drivername="postgresql+asyncpg"), poolclass=NullPool
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                company = await seed_company(session)
                user = await seed_user(session, company_id=company.id)
                await session.commit()
                tenant = TenantContext(company_id=company.id, user_id=user.id)
                kit = await make_packing_kit(session, tenant)
                inspection_id = await start(kit)
                await packing.cancel_order(
                    session,
                    tenant,
                    kit.order_id,
                    reason="Withdraw unstarted packing for migration test",
                )
                return inspection_id
        finally:
            await engine.dispose()

    inspection_id = asyncio.run(withdraw_real_inspection())
    with pytest.raises(AssertionError, match="downgrade would discard their audit meaning"):
        db.alembic("downgrade", "20261030_0100")
    engine = db.engine()
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(QcInspection.status, QcInspection.result, QcInspection.serial_ids).where(
                    QcInspection.id == inspection_id
                )
            ).one()
            assert row.status == "WITHDRAWN"
            assert row.result == "PENDING"
            assert len(row.serial_ids) == 2
        db.alembic("check")
    finally:
        engine.dispose()
