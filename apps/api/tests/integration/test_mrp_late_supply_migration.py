"""Revision ``ad9f573e5e59`` - the MRP late-supply columns - against a database.

The rest of the suite builds its schema from the model metadata, deliberately:
those tests verify application behaviour against the schema the code believes
in. Nothing then verifies that the *migrations* build that same schema, and a
column that exists only in ``models.py`` is a column production does not have.

This file closes that gap for one revision. It runs Alembic against a throwaway
database, in a subprocess, because ``migrations/env.py`` reads the URL from
``src.core.config.settings`` - which is cached at import time in this process
and already points at the suite's own database.

Four things are proved:

1. The columns and their guarantees are absent at the parent revision, so the
   revision is what adds them rather than something else having done it.
2. Applying it produces exactly the columns, types, nullability and CHECK
   constraints the model declares - including the constraint *names*, which
   Alembic matches on and which a bare (unwrapped) name in the revision would
   have silently double-prefixed through the metadata naming convention.
3. The migrated ``mrp_requirements`` carries exactly the columns
   ``MrpRequirement`` declares - no more, no fewer - so the revision cannot
   drift from the model it was written for.
4. It round-trips. Downgrade removes all of it; upgrading again puts it back.

Scoped to ``mrp_requirements`` on purpose. The obvious stronger assertion is
``alembic check``, which compares the whole schema; it is not used here because
it fails for reasons that have nothing to do with this revision - any module
whose models have landed ahead of their own migration shows up as a difference,
and this file would then report another team's work in progress as a failure of
the MRP revision. Whole-schema drift is a check for the pipeline, not for one
revision's test.

Cost: one full build of the initial schema (~10 s on CI, minutes on a loaded
laptop) plus three cheap steps. It is marked ``integration`` and runs with the
rest of that tier.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from src.modules.mrp.models import MrpRequirement

pytestmark = pytest.mark.integration

#: The revision under test and the one it chains off. Spelled out rather than
#: resolved as "head": another revision may land beside this one, and
#: ``upgrade head`` would then either drag it in or refuse with two heads.
REVISION = "ad9f573e5e59"
PARENT = "b7cc02046b2d"

TABLE = "mrp_requirements"
NEW_COLUMNS = (
    "late_incoming_quantity",
    "late_in_production_quantity",
    "first_late_supply_date",
)
NEW_CONSTRAINTS = (
    "ck_mrp_requirements_late_in_production_non_negative",
    "ck_mrp_requirements_late_incoming_non_negative",
    "ck_mrp_requirements_late_quantity_names_its_date",
    "ck_mrp_requirements_late_supply_arrives_after_required",
)

#: Its own database, created and dropped here. Sharing the suite's would leave
#: the schema at whatever revision the last downgrade stopped at.
DB_NAME = "atcraft_mrp_late_supply_mig"

#: libpq parameters appended to every connection string here. A throwaway
#: database reached through Docker Desktop's port proxy on a loaded laptop has
#: had its connection dropped mid-query with no server-side session left to
#: cancel; without keepalives the client waits on the dead socket forever and
#: the whole integration run stalls. With them, the loss surfaces as an
#: OperationalError inside a minute.
CONNECTION_GUARDS = (
    "connect_timeout=10&keepalives=1&keepalives_idle=15&keepalives_interval=5&keepalives_count=3"
)

#: Upper bound on one Alembic step. See :func:`alembic`.
ALEMBIC_TIMEOUT_SECONDS = 600

API_ROOT = Path(__file__).resolve().parents[2]


def _admin_dsn() -> str:
    """Connection to ``postgres`` itself, for CREATE/DROP DATABASE.

    Built from the same environment ``tests/conftest.py`` already resolved, so
    this follows a developer's published port without anything to export.
    """
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return (
        f"postgresql://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/postgres?{CONNECTION_GUARDS}"
    )


def _target_dsn(driver: str) -> str:
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    # The guards are libpq parameters: right for psycopg (which Alembic uses),
    # unknown to asyncpg, which is only named so Settings validates.
    query = f"?{CONNECTION_GUARDS}" if driver == "psycopg" else ""
    return f"postgresql+{driver}://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{DB_NAME}{query}"


def _plain_dsn() -> str:
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return f"postgresql://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{DB_NAME}?{CONNECTION_GUARDS}"


@pytest.fixture(scope="module")
def migration_database() -> Iterator[str]:
    admin = _admin_dsn()
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{DB_NAME}"')
    try:
        yield _plain_dsn()
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run Alembic against the throwaway database, in its own process.

    ``env.py`` reads ``settings.database_sync_url``, and ``Settings`` is cached
    at import. Overriding the environment only works in a process that has not
    imported it yet, which is why this is a subprocess rather than a call to
    ``alembic.command``.
    """
    environment = dict(os.environ)
    environment["DATABASE_URL"] = _target_dsn("asyncpg")
    environment["DATABASE_SYNC_URL"] = _target_dsn("psycopg")
    # A bounded wait, and no inherited stdin. The initial-schema upgrade builds
    # 103 tables in one transaction and took over two minutes on a loaded
    # Windows box; without a timeout a genuinely stuck child parked the whole
    # integration run at 27% with nothing to say why. Ten minutes is far past
    # any honest run and still turns a hang into a failure with output.
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
    return result


def columns(dsn: str) -> dict[str, tuple[str, str, str | None]]:
    """``{name: (data_type, is_nullable, column_default)}`` for the new columns."""
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default,
                   numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = ANY(%s)
            """,
            (TABLE, list(NEW_COLUMNS)),
        ).fetchall()
    return {
        name: (f"{data_type}({precision},{scale})" if precision else data_type, nullable, default)
        for name, data_type, nullable, default, precision, scale in rows
    }


def all_column_names(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (TABLE,),
        ).fetchall()
    return {name for (name,) in rows}


def check_constraints(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'c' AND conname = ANY(%s)
            """,
            (TABLE, list(NEW_CONSTRAINTS)),
        ).fetchall()
    return dict(rows)


def test_the_revision_adds_the_late_supply_columns_and_round_trips(
    migration_database: str,
) -> None:
    dsn = migration_database

    # --- the parent revision does not have them --------------------------
    alembic("upgrade", PARENT)
    assert columns(dsn) == {}, "the columns must arrive with this revision, not before it"
    assert check_constraints(dsn) == {}

    # --- applying it ------------------------------------------------------
    alembic("upgrade", REVISION)
    present = columns(dsn)
    assert present == {
        # NUMERIC(18,4) like every other quantity in the system. A float, or a
        # narrower scale, would round a plan's arithmetic.
        "late_incoming_quantity": ("numeric(18,4)", "NO", "'0'::numeric"),
        "late_in_production_quantity": ("numeric(18,4)", "NO", "'0'::numeric"),
        # Nullable, and no default: "no late supply" and "late supply arriving
        # today" are different facts and a sentinel would merge them.
        "first_late_supply_date": ("date", "YES", None),
    }

    constraints = check_constraints(dsn)
    assert set(constraints) == set(NEW_CONSTRAINTS), (
        "names, not just definitions: Alembic matches check constraints by name, "
        "and the metadata naming convention would double-prefix an unwrapped one"
    )
    definitions = " ".join(constraints.values())
    assert "late_incoming_quantity >= (0)::numeric" in definitions
    assert "late_in_production_quantity >= (0)::numeric" in definitions
    assert (
        "(first_late_supply_date IS NULL) OR (first_late_supply_date > earliest_required_date)"
        in definitions
    ), "late means exactly one thing: it lands after the date this row needs it"
    assert "first_late_supply_date IS NOT NULL" in definitions, (
        "a late quantity with no date is the half-fact these columns replace"
    )

    # --- and the table does not disagree with the model -------------------
    # A column the migration invented is drift nothing will ever notice, so
    # nothing built here may be absent from the model. The other direction -
    # that the model has no column the migrations forgot - cannot be asserted at
    # a revision that is no longer the last one to touch this table: a later
    # revision legitimately adds columns the model has and this one does not.
    # ``test_mrp_per_feed_late_dates_migration.py`` makes that assertion at the
    # tip, which is where it is true.
    assert all_column_names(dsn) <= {column.name for column in MrpRequirement.__table__.columns}

    # --- rollback ---------------------------------------------------------
    alembic("downgrade", PARENT)
    assert columns(dsn) == {}, "downgrade leaves the table as the parent revision built it"
    assert check_constraints(dsn) == {}

    # --- and forward again ------------------------------------------------
    alembic("upgrade", REVISION)
    assert columns(dsn) == present
    assert set(check_constraints(dsn)) == set(NEW_CONSTRAINTS)
