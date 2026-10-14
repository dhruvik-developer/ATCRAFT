"""Revisions ``0ac18597894f``, ``0785162d1e47`` and ``48e4df04913e`` against a database.

The suite builds its schema from the model metadata, so nothing else verifies
that the *migrations* build the shift tables the models declare. These three
revisions are the shift master and roster, the grant of their permissions to
the system roles, and the report key for daily production by shift. They run
here in a subprocess against a throwaway database, for the reason
``test_mrp_late_supply_migration.py`` gives: ``env.py`` reads the URL from
settings cached at import time in this process.

Proved:

1. At the parent revision none of it exists.
2. After the shift revision, ``shifts`` and ``shift_assignments`` carry exactly
   the columns their models declare; both EXCLUDE constraints exist with the
   ranges and predicates the models state; the two new foreign keys on
   ``work_orders`` and ``production_entries`` are validated; the old ``shift``
   text columns are untouched.
3. After the report-key revision the CHECK admits ``SHIFT_PRODUCTION``.
4. It round-trips: down to the parent removes all of it and keeps the text
   columns; up again restores it.

The database name carries a random suffix: lanes share one PostgreSQL server,
and two runs of this file at once must not drop each other's database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from src.modules.routing.models import Shift, ShiftAssignment

pytestmark = pytest.mark.integration

#: Spelled out rather than resolved as "head", for the precedent's reason.
PARENT = "159ffb5b9cb4"
SHIFT_REVISION = "0ac18597894f"
REPORT_REVISION = "48e4df04913e"

DB_NAME = f"atcraft_shift_roster_mig_{uuid.uuid4().hex[:8]}"
CONNECTION_GUARDS = (
    "connect_timeout=10&keepalives=1&keepalives_idle=15&keepalives_interval=5&keepalives_count=3"
)
ALEMBIC_TIMEOUT_SECONDS = 600
API_ROOT = Path(__file__).resolve().parents[2]


def _server() -> str:
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return f"{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}"


def _admin_dsn() -> str:
    return f"postgresql://{_server()}/postgres?{CONNECTION_GUARDS}"


def _plain_dsn() -> str:
    return f"postgresql://{_server()}/{DB_NAME}?{CONNECTION_GUARDS}"


@pytest.fixture(scope="module")
def migration_database() -> Iterator[str]:
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{DB_NAME}"')
    try:
        yield _plain_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def alembic(*args: str) -> None:
    environment = dict(os.environ)
    environment["DATABASE_URL"] = f"postgresql+asyncpg://{_server()}/{DB_NAME}"
    environment["DATABASE_SYNC_URL"] = (
        f"postgresql+psycopg://{_server()}/{DB_NAME}?{CONNECTION_GUARDS}"
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )


def query(dsn: str, sql: str, *params: object) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as connection:
        return list(connection.execute(sql, params).fetchall())


def column_names(dsn: str, table: str) -> set[str]:
    rows = query(
        dsn,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        table,
    )
    return {str(name) for (name,) in rows}


def constraints(dsn: str, table: str, kind: str) -> dict[str, tuple[str, bool]]:
    """``{name: (definition, validated)}`` for one constraint type on a table."""
    rows = query(
        dsn,
        "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint "
        "WHERE conrelid = to_regclass(%s) AND contype = %s",
        f"public.{table}",
        kind,
    )
    return {str(name): (str(definition), bool(valid)) for name, definition, valid in rows}


def test_the_shift_revisions_build_the_model_schema_and_round_trip(
    migration_database: str,
) -> None:
    dsn = migration_database

    alembic("upgrade", PARENT)
    assert column_names(dsn, "shifts") == set()
    assert column_names(dsn, "shift_assignments") == set()
    assert "shift_id" not in column_names(dsn, "work_orders")
    assert "shift" in column_names(dsn, "production_entries")

    alembic("upgrade", SHIFT_REVISION)
    assert column_names(dsn, "shifts") == {column.name for column in Shift.__table__.columns}
    assert column_names(dsn, "shift_assignments") == {
        column.name for column in ShiftAssignment.__table__.columns
    }

    exclusions = constraints(dsn, "shift_assignments", "x")
    assert set(exclusions) == {
        "ex_shift_assignments_operator_window",
        "ex_shift_assignments_machine_window",
    }
    operator_rule, _ = exclusions["ex_shift_assignments_operator_window"]
    assert "operator_user_id WITH =" in operator_rule
    assert "tstzrange(starts_at, ends_at, '[)'::text) WITH &&" in operator_rule
    machine_rule, _ = exclusions["ex_shift_assignments_machine_window"]
    assert "machine_id IS NOT NULL" in machine_rule

    foreign_keys = constraints(dsn, "shift_assignments", "f")
    assert "fk_shift_assignments_machine_id_work_center_id_machines" in foreign_keys
    for table in ("work_orders", "production_entries"):
        assert {"shift", "shift_id"} <= column_names(dsn, table)
        definition, validated = constraints(dsn, table, "f")[f"fk_{table}_shift_id_shifts"]
        assert "REFERENCES shifts(id) ON DELETE RESTRICT" in definition
        assert validated, f"{table}.shift_id was left NOT VALID"

    checks = constraints(dsn, "shifts", "c")
    assert {"ck_shifts_midnight_flag_matches_times", "ck_shifts_break_minutes_bounded"} <= set(
        checks
    )

    alembic("upgrade", REPORT_REVISION)
    report_check, _ = constraints(dsn, "report_snapshots", "c")[
        "ck_report_snapshots_report_key_is_known"
    ]
    assert "'SHIFT_PRODUCTION'" in report_check
    assert "'COST_CENTRE_EXPENSE'" in report_check

    alembic("downgrade", PARENT)
    assert column_names(dsn, "shifts") == set()
    assert column_names(dsn, "shift_assignments") == set()
    for table in ("work_orders", "production_entries"):
        columns = column_names(dsn, table)
        assert "shift_id" not in columns and "shift" in columns
    report_check, _ = constraints(dsn, "report_snapshots", "c")[
        "ck_report_snapshots_report_key_is_known"
    ]
    assert "'SHIFT_PRODUCTION'" not in report_check

    alembic("upgrade", REPORT_REVISION)
    assert column_names(dsn, "shift_assignments") == {
        column.name for column in ShiftAssignment.__table__.columns
    }
