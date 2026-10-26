"""The real migration chain agrees with every model and rolls back without data loss.

Application integration fixtures intentionally use ``metadata.create_all``. This
test instead starts with an empty, disposable database and verifies the schema
actually delivered by Alembic, including constraints' physical identity.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.core.registry import get_metadata
from src.modules.dealer_portal.models import DealerBanner
from src.modules.organization.models import Company
from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "20261026_0100"
REPAIR_REVISION = "20261027_0100"
REVISION = "20261031_0100"
API_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_TABLES = (
    "credit_note_lines",
    "credit_notes",
    "dealer_banners",
    "sales_return_lines",
    "sales_returns",
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

    def alembic(self, *arguments: str) -> None:
        environment = dict(os.environ)
        environment["DATABASE_SYNC_URL"] = self.url.render_as_string(hide_password=False)
        environment["DATABASE_URL"] = self.url.set(
            drivername="postgresql+asyncpg"
        ).render_as_string(hide_password=False)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=API_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        assert result.returncode == 0, (
            f"alembic {' '.join(arguments)} failed:\n{result.stdout}\n{result.stderr}"
        )


@pytest.fixture(scope="module")
def migration_database() -> Iterator[_MigrationDatabase]:
    database = _MigrationDatabase(f"atcraft_contract_mig_{uuid.uuid4().hex[:10]}")
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
        report_database_setup_failure(exc, purpose="schema contract migration")
    try:
        yield database
    finally:
        assert database.name.startswith("atcraft_contract_mig_")
        with psycopg.connect(**connection_options, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database.name))
            )


def _checks(connection: Connection) -> dict[int, tuple[str, str, str, bool]]:
    return {
        row.oid: (row.table_name, row.conname, row.definition, row.convalidated)
        for row in connection.execute(
            sa.text(
                "SELECT constraint_.oid, table_.relname AS table_name, "
                "constraint_.conname, pg_get_constraintdef(constraint_.oid) AS definition, "
                "constraint_.convalidated FROM pg_constraint constraint_ "
                "JOIN pg_class table_ ON table_.oid = constraint_.conrelid "
                "JOIN pg_namespace schema_ ON schema_.oid = table_.relnamespace "
                "WHERE schema_.nspname = 'public' AND constraint_.contype = 'c'"
            )
        )
    }


def _indexes(connection: Connection) -> dict[str, tuple[str, bool]]:
    return {
        row.relname: (row.definition, row.indisvalid)
        for row in connection.execute(
            sa.text(
                "SELECT index_.relname, pg_get_indexdef(index_.oid) AS definition, "
                "state_.indisvalid FROM pg_class index_ "
                "JOIN pg_index state_ ON state_.indexrelid = index_.oid "
                "JOIN pg_namespace schema_ ON schema_.oid = index_.relnamespace "
                "WHERE schema_.nspname = 'public'"
            )
        )
    }


def _defaults(connection: Connection) -> dict[tuple[str, str], str | None]:
    return {
        (row.table_name, row.column_name): row.column_default
        for row in connection.execute(
            sa.text(
                "SELECT table_name, column_name, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = ANY(:tables) AND column_name IN ('created_at', 'updated_at')"
            ),
            {"tables": list(CONTRACT_TABLES)},
        )
    }


def _tenant_foreign_keys(connection: Connection) -> dict[str, tuple[str, bool, str]]:
    return {
        row.table_name: (row.confdeltype, row.convalidated, row.definition)
        for row in connection.execute(
            sa.text(
                "SELECT table_.relname AS table_name, constraint_.confdeltype, "
                "constraint_.convalidated, pg_get_constraintdef(constraint_.oid) AS definition "
                "FROM pg_constraint constraint_ "
                "JOIN pg_class table_ ON table_.oid = constraint_.conrelid "
                "WHERE constraint_.contype = 'f' "
                "AND constraint_.confrelid = 'public.companies'::regclass "
                "AND table_.relname = ANY(:tables)"
            ),
            {"tables": list(CONTRACT_TABLES)},
        )
    }


def _assert_each_tenant_foreign_key_blocks_deletion(
    engine: Engine, foreign_keys: dict[str, tuple[str, bool, str]]
) -> None:
    # A complete credit/return graph has other company-owned parents whose FKs
    # would mask the FK under test. Exercise each real, reflected FK definition
    # on a transactional projection in its own test schema instead of inventing
    # application rows. PostgreSQL temporary tables cannot reference companies.
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA schema_contract_probe")
        connection.exec_driver_sql(
            "CREATE TABLE schema_contract_probe.tenant (company_id UUID NOT NULL)"
        )
        with Session(bind=connection) as session:
            for table, (action, validated, definition) in foreign_keys.items():
                assert action == "r" and validated, table
                company = Company(code=uuid.uuid4().hex[:10], name=f"FK probe: {table}")
                session.add(company)
                session.flush()
                company_id = company.id
                connection.exec_driver_sql(
                    f"ALTER TABLE schema_contract_probe.tenant "
                    f"ADD CONSTRAINT tenant_contract_probe_guard {definition}"
                )
                connection.execute(
                    sa.text("INSERT INTO schema_contract_probe.tenant VALUES (:company_id)"),
                    {"company_id": company_id},
                )
                with pytest.raises(IntegrityError) as failure, connection.begin_nested():
                    connection.execute(sa.delete(Company).where(Company.id == company_id))
                assert failure.value.orig.diag.constraint_name == "tenant_contract_probe_guard"
                connection.exec_driver_sql(
                    "ALTER TABLE schema_contract_probe.tenant "
                    "DROP CONSTRAINT tenant_contract_probe_guard"
                )
                connection.exec_driver_sql("DELETE FROM schema_contract_probe.tenant")
                session.delete(company)
                session.flush()
        connection.exec_driver_sql("DROP TABLE schema_contract_probe.tenant")
        connection.exec_driver_sql("DROP SCHEMA schema_contract_probe")


def _leave_interrupted_index_build(engine: Engine) -> None:
    # A writer forces CREATE INDEX CONCURRENTLY to wait after its first commit.
    # A server timeout then leaves the real invalid index that a retry must heal.
    with engine.connect() as writer:
        writer.exec_driver_sql("UPDATE credit_notes SET status = status WHERE false")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as builder:
            builder.exec_driver_sql("SET statement_timeout = '1s'")
            with pytest.raises(DBAPIError) as interrupted:
                builder.exec_driver_sql(
                    "CREATE INDEX CONCURRENTLY ix_credit_notes_created_at "
                    "ON credit_notes(created_at)"
                )
            assert interrupted.value.orig.sqlstate == "57014"
            assert (
                builder.scalar(
                    sa.text(
                        "SELECT indisvalid FROM pg_index "
                        "WHERE indexrelid = 'ix_credit_notes_created_at'::regclass"
                    )
                )
                is False
            )


def _create_banner(engine: Engine) -> tuple[uuid.UUID, uuid.UUID, Any, Any]:
    with Session(engine) as session, session.begin():
        company = Company(code=uuid.uuid4().hex[:10], name="Timestamp and tenant contract")
        session.add(company)
        session.flush()
        banner = DealerBanner(
            company_id=company.id,
            code="CONTRACT",
            title="Schema contract",
            image_document_id=uuid.uuid4(),
            updated_at=sa.text("DEFAULT"),
        )
        session.add(banner)
        session.flush()
        assert banner.created_at.tzinfo is not None and banner.updated_at.tzinfo is not None
        return company.id, banner.id, banner.created_at, banner.updated_at


def test_fresh_schema_alignment_preserves_data_constraints_and_rollback(
    migration_database: _MigrationDatabase,
) -> None:
    database = migration_database
    database.alembic("upgrade", PREVIOUS_REVISION)
    engine = database.engine()
    try:
        with engine.connect() as connection:
            previous_checks = _checks(connection)
            previous_indexes = _indexes(connection)
            previous_defaults = _defaults(connection)
            previous_foreign_keys = _tenant_foreign_keys(connection)
        assert len(previous_defaults) == 10
        assert all(default is None for default in previous_defaults.values())
        assert all(action == "c" for action, _, _ in previous_foreign_keys.values())

        # This uses the real model against the migrated database, so a missing
        # created_at default fails just as a production create would.
        with pytest.raises(IntegrityError) as missing_default:
            _create_banner(engine)
        assert missing_default.value.orig.diag.column_name == "created_at"

        _leave_interrupted_index_build(engine)
        database.alembic("upgrade", f"{PREVIOUS_REVISION}:{REPAIR_REVISION}", "--sql")
        database.alembic("upgrade", REPAIR_REVISION)
        with engine.connect() as connection:
            current_checks = _checks(connection)
            current_indexes = _indexes(connection)
            assert all(value == "now()" for value in _defaults(connection).values())
            foreign_keys = _tenant_foreign_keys(connection)
        assert set(foreign_keys) == set(CONTRACT_TABLES)
        _assert_each_tenant_foreign_key_blocks_deletion(engine, foreign_keys)

        # CHECK renames keep physical identity, expressions and validation. A
        # drop/recreate could admit unvalidated history or scan a large table.
        assert {
            oid: (table, definition, validated)
            for oid, (table, _, definition, validated) in current_checks.items()
        } == {
            oid: (table, definition, validated)
            for oid, (table, _, definition, validated) in previous_checks.items()
        }
        assert (
            sum(current_checks[oid][1] != before[1] for oid, before in previous_checks.items())
            == 24
        )
        assert len(current_indexes.keys() - previous_indexes.keys()) == 9
        assert previous_indexes.keys() <= current_indexes.keys()
        assert all(valid for _, valid in current_indexes.values())
        assert (
            current_indexes["ix_qc_inspection_decisions_created_at"]
            == previous_indexes["ix_qc_inspection_decisions_created_at"]
        )

        company_id, banner_id, created_at, updated_at = _create_banner(engine)
        with Session(engine) as session, pytest.raises(IntegrityError) as delete_company:
            session.execute(sa.delete(Company).where(Company.id == company_id))
        assert delete_company.value.orig.diag.constraint_name == (
            "fk_dealer_banners_company_id_companies"
        )

        database.alembic("downgrade", PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _checks(connection) == previous_checks
            assert _indexes(connection) == previous_indexes
            assert _defaults(connection) == previous_defaults
            assert _tenant_foreign_keys(connection) == previous_foreign_keys
        with Session(engine) as session:
            banner = session.get(DealerBanner, banner_id)
            assert banner is not None
            assert (banner.created_at, banner.updated_at) == (created_at, updated_at)

        database.alembic("upgrade", REVISION)
        database.alembic("check")
        # Historical revision 27 is checked independently above: newer revisions
        # legitimately add/replace constraints, so only HEAD matches current models.
        expected_checks = {
            (table.name, str(constraint.name))
            for table in get_metadata().tables.values()
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        with engine.connect() as connection:
            head_checks = _checks(connection)
        assert {(table, name) for table, name, _, _ in head_checks.values()} == expected_checks
        assert all(len(name.encode()) <= 63 for _, name in expected_checks)
    finally:
        engine.dispose()
