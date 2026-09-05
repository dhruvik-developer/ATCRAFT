"""Revision ``2438d960a6e4`` - the closing half of each late-supply interval.

The rest of the suite builds its schema from the model metadata, so nothing else
proves the *migrations* build that same schema. A column that exists only in
``models.py`` is a column production does not have.

What this revision is for. A late quantity is a SUM over that feed's late
tranches, and ``first_late_*_date`` is the MINIMUM over the same tranches, so
the pair described the whole quantity only when the feed had one tranche. With
several - 10 landing in July and 5 000 in December - the row said "5 010" beside
the July date, which reads as nearly here. That is the direction that stops a
line: a planner defers an order they needed to place. These two columns close
the interval, so a consumer can state "the first of 5 010 on 30 Jul, all of it
by 15 Dec" and imply nothing untrue.

Four things are proved:

1. The columns are absent at the parent revision.
2. Applying it produces the columns and the ordering constraints the model
   declares, under the names Alembic matches on.
3. The constraints admit what they must and reject what they exist to reject:
   rows from before this revision (no closing date at all), a single-tranche
   feed whose interval is a point, and a genuine interval - but never a closing
   date before its own opening one, nor one standing without an opening date.
4. It round-trips: downgrade removes both columns and both constraints, and
   leaves the previous revisions' columns alone.

It also inherits the exact-equality check between the migrated table and
``MrpRequirement``, which ``test_mrp_per_feed_late_dates_migration.py`` handed
over when this revision landed: this is now the last one to touch
``mrp_requirements``. When another lands, that assertion moves again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest

from src.modules.mrp.models import MrpRequirement

pytestmark = pytest.mark.integration

REVISION = "2438d960a6e4"
PARENT = "7a1e4c9b2d55"

TABLE = "mrp_requirements"
NEW_COLUMNS = (
    "last_late_incoming_date",
    "last_late_in_production_date",
)
NEW_CONSTRAINTS = (
    "ck_mrp_requirements_late_incoming_interval_ordered",
    "ck_mrp_requirements_late_production_interval_ordered",
)

DB_NAME = "atcraft_mrp_late_closing_mig"

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

    ``env.py`` reads ``settings.database_sync_url`` and ``Settings`` is cached at
    import, so overriding the environment only works in a process that has not
    imported it yet.
    """
    environment = dict(os.environ)
    environment["DATABASE_URL"] = _target_dsn("asyncpg")
    environment["DATABASE_SYNC_URL"] = _target_dsn("psycopg")
    # Bounded, and with no inherited stdin: the initial-schema upgrade took over
    # two minutes on a loaded laptop, and an unbounded wait turned a stuck
    # child into a silent hang of the whole run.
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
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = ANY(%s)
            """,
            (TABLE, list(NEW_COLUMNS)),
        ).fetchall()
    return {name: (data_type, nullable, default) for name, data_type, nullable, default in rows}


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


#: The columns the two new constraints read. Only these, so the helper cannot go
#: stale when another module adds a NOT NULL column to ``mrp_requirements``.
DATE_COLUMNS = (
    "first_late_incoming_date",
    "last_late_incoming_date",
    "first_late_in_production_date",
    "last_late_in_production_date",
)


def admits(dsn: str, definitions: dict[str, str], row: tuple[date | None, ...]) -> bool:
    """Would every new constraint accept a row with these four dates?

    The constraint expressions are read back out of ``pg_constraint`` and
    evaluated by PostgreSQL against the candidate row, rather than a row being
    INSERTed: a hand-written INSERT would have to name every NOT NULL column on
    the table and would go stale the moment another module adds one. This is
    still the database's own verdict on its own stored expression - not this
    test restating the rule in Python, which would pass even if the migration
    had written a different constraint.

    ``IS NOT FALSE``, not ``IS TRUE``: that is PostgreSQL's rule for a CHECK,
    and the difference is exactly the NULL cases these constraints turn on.
    """
    columns = ", ".join(DATE_COLUMNS)
    placeholders = ", ".join("%s::date" for _ in DATE_COLUMNS)
    with psycopg.connect(dsn) as connection:
        for definition in definitions.values():
            expression = definition.removeprefix("CHECK ")
            (verdict,) = connection.execute(
                f"SELECT ({expression}) IS NOT FALSE "
                f"FROM (VALUES ({placeholders})) AS candidate({columns})",
                row,
            ).fetchone()
            if not verdict:
                return False
    return True


def test_the_revision_closes_the_late_interval_and_round_trips(
    migration_database: str,
) -> None:
    dsn = migration_database

    # --- absent at the parent ----------------------------------------------
    alembic("upgrade", PARENT)
    assert columns(dsn) == {}, "the columns must arrive with this revision, not before it"
    assert check_constraints(dsn) == {}

    # --- applying it --------------------------------------------------------
    alembic("upgrade", REVISION)
    assert columns(dsn) == {
        # Nullable, no default. NULL means "not recorded": rows planned before
        # this revision cannot be backfilled, because the tranche list that
        # produced the quantity is not stored, and setting last = first would
        # assert a single-tranche feed nobody can know was single-tranche.
        "last_late_incoming_date": ("date", "YES", None),
        "last_late_in_production_date": ("date", "YES", None),
    }

    constraints = check_constraints(dsn)
    assert set(constraints) == set(NEW_CONSTRAINTS), (
        "names, not just definitions: Alembic matches check constraints by name"
    )
    definitions = " ".join(constraints.values())
    assert "last_late_incoming_date >= first_late_incoming_date" in definitions
    assert "last_late_in_production_date >= first_late_in_production_date" in definitions

    # --- what the interval constraint admits and rejects --------------------
    opened = date(2026, 7, 30)
    closed = opened + timedelta(days=138)

    # (first_incoming, last_incoming, first_production, last_production)
    assert admits(dsn, constraints, (None, None, None, None)), (
        "a row planned before this revision carries neither closing date"
    )
    assert admits(dsn, constraints, (opened, opened, None, None)), (
        "a single late tranche is an interval of one day, not an error"
    )
    assert admits(dsn, constraints, (opened, closed, None, None)), (
        "the ordinary case: several tranches, a real span"
    )
    assert admits(dsn, constraints, (opened, closed, opened, closed)), (
        "both feeds late, each with its own interval"
    )
    assert not admits(dsn, constraints, (opened, opened - timedelta(days=1), None, None)), (
        "an interval that closes before it opens is not an interval"
    )
    assert not admits(dsn, constraints, (None, closed, None, None)), (
        "a closing date with no opening one is a worse half-fact than the single "
        "date these columns replace"
    )
    assert not admits(dsn, constraints, (None, None, None, closed)), (
        "the production feed is held to the same rule as the incoming one"
    )

    # --- the table agrees with the model, both directions -------------------
    # Inherited from test_mrp_per_feed_late_dates_migration.py, which held this
    # while it was the last revision to touch the table. A column the migration
    # forgot is a production 500 on the first INSERT; a column it invented is
    # drift nothing will ever notice.
    assert all_column_names(dsn) == {column.name for column in MrpRequirement.__table__.columns}

    # --- rollback -----------------------------------------------------------
    alembic("downgrade", PARENT)
    assert columns(dsn) == {}, "downgrade leaves the table as the parent revision built it"
    assert check_constraints(dsn) == {}
    assert "first_late_incoming_date" in all_column_names(dsn), (
        "the previous revisions' columns are not this one's to remove"
    )

    # --- and back again -----------------------------------------------------
    alembic("upgrade", REVISION)
    assert set(columns(dsn)) == set(NEW_COLUMNS)
    assert set(check_constraints(dsn)) == set(NEW_CONSTRAINTS)
