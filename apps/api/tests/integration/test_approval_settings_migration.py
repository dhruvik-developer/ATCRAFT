"""Approval thresholds work on an Alembic-built database, not only ORM fixtures."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.core.deps import TenantContext
from src.core.registry import get_metadata
from src.modules.audit.models import AuditAction, AuditLog, CompanyApprovalSettings
from src.modules.audit.schemas import ApprovalSettingsUpdate
from src.modules.audit.service import (
    DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
    DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD,
    DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
    DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
    get_company_approval_settings,
    update_company_approval_settings,
)
from src.modules.inventory.service import (
    adjustment_approval_threshold,
    internal_issue_approval_threshold,
)
from src.modules.organization.models import Company
from src.modules.users.models import User
from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "20261123_0100"
REPAIR_REVISION = "20261124_0100"
API_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_FIELDS = (
    "po_dual_approval_amount",
    "adjustment_approval_threshold",
    "internal_issue_approval_threshold",
    "requisition_auto_approve_limit",
)


@dataclass(frozen=True)
class _MigrationDatabase:
    name: str

    @property
    def url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=_pg_user,
            password=_pg_password,
            host=_pg_host,
            port=_pg_port,
            database=self.name,
        )

    def engine(self) -> Engine:
        return sa.create_engine(self.url, poolclass=NullPool)

    def run_alembic(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["DATABASE_SYNC_URL"] = self.url.render_as_string(hide_password=False)
        environment["DATABASE_URL"] = self.url.set(
            drivername="postgresql+asyncpg"
        ).render_as_string(hide_password=False)
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=API_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )

    def alembic(self, *arguments: str) -> None:
        result = self.run_alembic(*arguments)
        assert result.returncode == 0, (
            f"alembic {' '.join(arguments)} failed:\n{result.stdout}\n{result.stderr}"
        )


@pytest.fixture(scope="module")
def migration_database() -> Iterator[_MigrationDatabase]:
    database = _MigrationDatabase(f"atcraft_approval_mig_{uuid.uuid4().hex[:10]}")
    connection_options = {
        "host": _pg_host,
        "port": _pg_port,
        "user": _pg_user,
        "password": _pg_password,
        "dbname": "postgres",
        "connect_timeout": 10,
    }
    try:
        with psycopg.connect(**connection_options, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database.name)))
    except Exception as exc:
        report_database_setup_failure(exc, purpose="approval settings migration")
    try:
        yield database
    finally:
        assert database.name.startswith("atcraft_approval_mig_")
        with psycopg.connect(**connection_options, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database.name))
            )


def _legacy_tenants(engine: Engine) -> tuple[TenantContext, ...]:
    get_metadata()
    tenants = []
    with Session(engine) as session, session.begin():
        for suffix in ("OWNER", "OTHER", "FK"):
            company = Company(code=f"APPROVAL-{suffix}", name=f"Existing company {suffix}")
            session.add(company)
            session.flush()
            user = User(
                company_id=company.id,
                email=f"{suffix.lower()}@approval-migration.test",
                full_name=f"Existing user {suffix}",
                password_hash="existing-password-must-be-preserved",
            )
            session.add(user)
            session.flush()
            tenants.append(TenantContext(company_id=company.id, user_id=user.id))
    return tuple(tenants)


def _master_rows(engine: Engine) -> dict[str, list[tuple]]:
    with engine.connect() as connection:
        return {
            table.name: [
                tuple(row) for row in connection.execute(sa.select(table).order_by(table.c.id))
            ]
            for table in (Company.__table__, User.__table__)
        }


async def _verify_service_reads_and_tenant_overrides(
    database: _MigrationDatabase, tenants: tuple[TenantContext, ...]
) -> None:
    engine = create_async_engine(
        database.url.set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    owner, other, _ = tenants
    defaults = {
        "po_dual_approval_amount": DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
        "adjustment_approval_threshold": DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
        "internal_issue_approval_threshold": DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD,
        "requisition_auto_approve_limit": DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
        "is_default": True,
    }
    overrides = {
        "po_dual_approval_amount": Decimal("35000.1250"),
        "adjustment_approval_threshold": Decimal("12000.2500"),
        "internal_issue_approval_threshold": Decimal("8000.5000"),
        "requisition_auto_approve_limit": Decimal("500.7500"),
    }
    try:
        async with maker() as session:
            assert (await get_company_approval_settings(session, owner)).model_dump() == defaults
            assert (
                await adjustment_approval_threshold(session, owner)
                == defaults["adjustment_approval_threshold"]
            )
            assert (
                await internal_issue_approval_threshold(session, owner)
                == defaults["internal_issue_approval_threshold"]
            )
            # A default read must succeed without manufacturing an override row.
            assert (
                await session.scalar(
                    sa.select(sa.func.count()).select_from(CompanyApprovalSettings)
                )
                == 0
            )
            saved = await update_company_approval_settings(
                session, owner, ApprovalSettingsUpdate(**overrides)
            )
            assert saved.model_dump() == {**overrides, "is_default": False}

        # A new connection proves the service committed, rather than leaving a
        # value visible only in its own identity map or transaction.
        async with maker() as session:
            assert (await get_company_approval_settings(session, owner)).model_dump() == {
                **overrides,
                "is_default": False,
            }
            assert (await get_company_approval_settings(session, other)).model_dump() == defaults
            assert (
                await adjustment_approval_threshold(session, owner)
                == overrides["adjustment_approval_threshold"]
            )
            assert (
                await internal_issue_approval_threshold(session, owner)
                == overrides["internal_issue_approval_threshold"]
            )
            await update_company_approval_settings(
                session,
                other,
                ApprovalSettingsUpdate(adjustment_approval_threshold=Decimal("99.1250")),
            )

        async with maker() as session:
            assert await adjustment_approval_threshold(session, other) == Decimal("99.1250")
            assert (
                await adjustment_approval_threshold(session, owner)
                == overrides["adjustment_approval_threshold"]
            )
            assert (
                await internal_issue_approval_threshold(session, other)
                == defaults["internal_issue_approval_threshold"]
            )
            for tenant in (owner, other):
                row = await session.scalar(
                    sa.select(CompanyApprovalSettings).where(
                        CompanyApprovalSettings.company_id == tenant.company_id
                    )
                )
                assert row is not None and row.created_at.tzinfo is not None
                assert row.created_by_id == tenant.user_id
                audit = await session.scalar(
                    sa.select(AuditLog).where(
                        AuditLog.company_id == tenant.company_id,
                        AuditLog.entity_id == row.id,
                        AuditLog.action == AuditAction.CONFIG_CHANGE,
                    )
                )
                assert audit is not None and audit.created_by_id == tenant.user_id

        async with maker() as session:
            reset = await update_company_approval_settings(
                session, owner, ApprovalSettingsUpdate(adjustment_approval_threshold=None)
            )
            assert reset.model_dump() == {
                **overrides,
                "adjustment_approval_threshold": DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
                "is_default": False,
            }
        async with maker() as session:
            assert (
                await adjustment_approval_threshold(session, owner)
                == DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD
            )
            assert await adjustment_approval_threshold(session, other) == Decimal("99.1250")
            assert (
                await internal_issue_approval_threshold(session, owner)
                == overrides["internal_issue_approval_threshold"]
            )
    finally:
        await engine.dispose()


def _assert_database_constraints(engine: Engine, tenants: tuple[TenantContext, ...]) -> None:
    owner, _, unused = tenants
    cases = (
        (
            CompanyApprovalSettings(company_id=owner.company_id),
            "uq_company_approval_settings_company",
        ),
        (
            CompanyApprovalSettings(company_id=uuid.uuid4()),
            "fk_company_approval_settings_company_id_companies",
        ),
        (
            CompanyApprovalSettings(company_id=unused.company_id, created_by_id=uuid.uuid4()),
            "fk_company_approval_settings_created_by_id_users",
        ),
    )
    with Session(engine) as session:
        for row, constraint in cases:
            with pytest.raises(IntegrityError) as failure, session.begin_nested():
                session.add(row)
                session.flush()
            assert failure.value.orig.diag.constraint_name == constraint
    with engine.connect() as connection:
        foreign_keys = {
            fk["constrained_columns"][0]: fk["options"]["ondelete"]
            for fk in sa.inspect(connection).get_foreign_keys("company_approval_settings")
        }
        assert foreign_keys == {"company_id": "RESTRICT", "created_by_id": "SET NULL"}


def _assert_downgrade_refused(database: _MigrationDatabase, engine: Engine) -> None:
    with engine.connect() as connection:
        before = list(
            connection.execute(
                sa.select(CompanyApprovalSettings.__table__).order_by(CompanyApprovalSettings.id)
            )
        )
    failed = database.run_alembic("downgrade", PREVIOUS_REVISION)
    assert failed.returncode != 0
    assert "approval settings" in (failed.stdout + failed.stderr).lower()
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == REPAIR_REVISION
        )
        assert (
            list(
                connection.execute(
                    sa.select(CompanyApprovalSettings.__table__).order_by(
                        CompanyApprovalSettings.id
                    )
                )
            )
            == before
        )


def _assert_lock_timeout(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0
    assert "lock timeout" in (result.stdout + result.stderr).lower()


def test_migrated_approval_settings_preserve_history_and_round_trip(
    migration_database: _MigrationDatabase,
) -> None:
    database = migration_database
    database.alembic("upgrade", PREVIOUS_REVISION)
    engine = database.engine()
    try:
        tenants = _legacy_tenants(engine)
        previous_rows = _master_rows(engine)
        with engine.connect() as connection:
            assert not sa.inspect(connection).has_table("company_approval_settings")

        # The upgrade must fail atomically rather than waiting indefinitely on
        # a busy parent table. The held lock establishes contention directly;
        # the subprocess's migration lock_timeout supplies the bounded wait.
        with engine.begin() as writer:
            writer.execute(sa.text("LOCK TABLE companies IN ACCESS EXCLUSIVE MODE"))
            _assert_lock_timeout(database.run_alembic("upgrade", REPAIR_REVISION))
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == PREVIOUS_REVISION
            )
            assert not sa.inspect(connection).has_table("company_approval_settings")
        database.alembic("upgrade", "head")
        database.alembic("check")
        assert _master_rows(engine) == previous_rows
        asyncio.run(_verify_service_reads_and_tenant_overrides(database, tenants))
        _assert_database_constraints(engine, tenants)

        # Isolate this revision's downgrade contract if later migrations exist.
        database.alembic("downgrade", REPAIR_REVISION)
        _assert_downgrade_refused(database, engine)
        with engine.begin() as connection:
            connection.execute(
                sa.update(CompanyApprovalSettings).values(dict.fromkeys(OVERRIDE_FIELDS))
            )
        # An all-NULL override row still carries identity, author and history;
        # restoring defaults must not turn a destructive downgrade into success.
        _assert_downgrade_refused(database, engine)

        with engine.begin() as connection:
            connection.execute(sa.delete(CompanyApprovalSettings))
        with engine.begin() as reader:
            reader.execute(sa.text("LOCK TABLE company_approval_settings IN ACCESS SHARE MODE"))
            _assert_lock_timeout(database.run_alembic("downgrade", PREVIOUS_REVISION))
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == REPAIR_REVISION
            )
            assert sa.inspect(connection).has_table("company_approval_settings")
        database.alembic("downgrade", PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert not sa.inspect(connection).has_table("company_approval_settings")
        assert _master_rows(engine) == previous_rows
        database.alembic("upgrade", "head")
        database.alembic("check")
        assert _master_rows(engine) == previous_rows
    finally:
        engine.dispose()
