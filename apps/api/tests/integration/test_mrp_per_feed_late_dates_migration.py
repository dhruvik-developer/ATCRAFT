"""Revision ``66b1c0e2d1f4`` - one late-supply date per feed - against a database.

The companion to ``test_mrp_late_supply_migration.py``, for the revision that
splits the single ``first_late_supply_date`` into a date per supply feed. Same
reasoning for existing at all: the rest of the suite builds its schema from the
model metadata, so nothing else proves the *migrations* build that same schema,
and a column that exists only in ``models.py`` is a column production does not
have.

Five things are proved:

1. The two columns and the three constraints are absent at the parent revision.
2. Applying it produces exactly the columns, types and nullability the model
   declares, and the CHECK constraints under the names Alembic matches on.
3. The rows the *previous* revision could write - a summary date with no
   per-feed dates - still satisfy the new constraints, because they cannot be
   backfilled and the revision therefore has to keep admitting them.
4. The new constraints actually reject the shape this revision exists to end:
   a summary date the feed dates do not support.
5. It round-trips: downgrade removes all of it, upgrading again puts it back.

This file used to carry the exact-equality check between the migrated table and
``MrpRequirement``, because it was then the last revision to touch
``mrp_requirements``. ``2438d960a6e4`` has since added the closing halves of the
late-supply intervals, so - exactly as this note prescribed - that assertion has
moved to ``test_mrp_late_closing_dates_migration.py`` and this file keeps the
subset half: the migration must invent no column the model does not declare.

Cost: one full build of the initial schema (~10 s) plus a few cheap steps. It is
marked ``integration`` and runs with the rest of that tier.
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

#: The revision under test and the one it chains off. Spelled out rather than
#: resolved as "head": another revision may land beside this one, and
#: ``upgrade head`` would then either drag it in or refuse with two heads.
REVISION = "66b1c0e2d1f4"
PARENT = "65bcadcc236b"

TABLE = "mrp_requirements"
NEW_COLUMNS = (
    "first_late_incoming_date",
    "first_late_in_production_date",
)
NEW_CONSTRAINTS = (
    "ck_mrp_requirements_late_incoming_arrives_after_required",
    "ck_mrp_requirements_late_in_production_arrives_after_required",
    "ck_mrp_requirements_first_late_supply_is_the_earlier",
)

#: Its own database, created and dropped here. Sharing the suite's would leave
#: the schema at whatever revision the last downgrade stopped at.
DB_NAME = "atcraft_mrp_per_feed_late_mig"

API_ROOT = Path(__file__).resolve().parents[2]


def _admin_dsn() -> str:
    """Connection to ``postgres`` itself, for CREATE/DROP DATABASE."""
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return f"postgresql://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/postgres"


def _target_dsn(driver: str) -> str:
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return f"postgresql+{driver}://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{DB_NAME}"


def _plain_dsn() -> str:
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    return f"postgresql://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{DB_NAME}"


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
    imported it yet, which is why this is a subprocess.
    """
    environment = dict(os.environ)
    environment["DATABASE_URL"] = _target_dsn("asyncpg")
    environment["DATABASE_SYNC_URL"] = _target_dsn("psycopg")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
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


def test_the_revision_adds_a_date_per_feed_and_round_trips(
    migration_database: str,
) -> None:
    dsn = migration_database

    # --- the parent revision does not have them ---------------------------
    alembic("upgrade", PARENT)
    assert columns(dsn) == {}, "the columns must arrive with this revision, not before it"
    assert check_constraints(dsn) == {}

    # --- applying it -------------------------------------------------------
    alembic("upgrade", REVISION)
    present = columns(dsn)
    assert present == {
        # Nullable, no default: NULL is "this feed excluded nothing", and a
        # sentinel date would merge that with "it lands today".
        "first_late_incoming_date": ("date", "YES", None),
        "first_late_in_production_date": ("date", "YES", None),
    }

    constraints = check_constraints(dsn)
    assert set(constraints) == set(NEW_CONSTRAINTS), (
        "names, not just definitions: Alembic matches check constraints by name, "
        "and the metadata naming convention would double-prefix an unwrapped one"
    )
    definitions = " ".join(constraints.values())
    assert (
        "(first_late_incoming_date IS NULL) OR "
        "(first_late_incoming_date > earliest_required_date)" in definitions
    ), "late means one thing per feed too: it lands after the date this row needs it"
    assert (
        "(first_late_in_production_date IS NULL) OR "
        "(first_late_in_production_date > earliest_required_date)" in definitions
    )
    assert "LEAST(first_late_incoming_date, first_late_in_production_date)" in definitions, (
        "the summary date is the earlier of the two feeds, enforced rather than intended"
    )

    # --- and the table carries no column the model does not declare ---------
    # A column the migration invented is drift nothing will ever notice, so that
    # half is still assertable at this revision.
    #
    # The converse - that the model has no column the table lacks - is NOT
    # assertable here any more, and the note this replaces said why: it held only
    # while this was the last revision to touch the table. `2438d960a6e4` now
    # adds the closing halves of the late-supply intervals
    # (`last_late_*_date`), so at THIS revision the model legitimately declares
    # two columns this migration does not create. Asserting equality here would
    # fail for the one reason that is not a defect: a later migration exists.
    # `test_mrp_late_closing_dates_migration.py` inherits the full agreement
    # check, exactly as this comment originally prescribed.
    model_columns = {column.name for column in MrpRequirement.__table__.columns}
    assert all_column_names(dsn) <= model_columns, (
        "the migration created a column the model does not declare: "
        f"{sorted(all_column_names(dsn) - model_columns)}"
    )
    assert {
        "late_incoming_quantity",
        "late_in_production_quantity",
        "first_late_incoming_date",
        "first_late_in_production_date",
    } <= all_column_names(dsn), "this revision's own columns must all be present"

    # --- rollback ----------------------------------------------------------
    alembic("downgrade", PARENT)
    assert columns(dsn) == {}, "downgrade leaves the table as the parent revision built it"
    assert check_constraints(dsn) == {}
    assert "first_late_supply_date" in all_column_names(dsn), (
        "the previous revision's columns are not this one's to remove"
    )

    # --- and forward again -------------------------------------------------
    alembic("upgrade", REVISION)
    assert columns(dsn) == present
    assert set(check_constraints(dsn)) == set(NEW_CONSTRAINTS)


#: The four date columns every new constraint reads, in the order the row
#: shapes below spell them.
DATE_COLUMNS = (
    "first_late_incoming_date",
    "first_late_in_production_date",
    "first_late_supply_date",
    "earliest_required_date",
)

NEEDED_BY = date(2026, 7, 15)
ONE_DAY_LATE = NEEDED_BY + timedelta(days=1)
FOUR_MONTHS_LATE = NEEDED_BY + timedelta(days=120)


def admits(dsn: str, definitions: dict[str, str], row: tuple[date | None, ...]) -> bool:
    """Would every new constraint accept a row with these four dates?

    The constraint expressions are read back out of ``pg_constraint`` and
    evaluated against the candidate row, rather than a row being INSERTed: a
    hand-written INSERT would have to name every NOT NULL column on the table
    and would go stale the moment another module adds one. This names only the
    columns the constraints themselves read, which cannot go stale without the
    constraints changing.

    ``NOT FALSE``, not ``IS TRUE``: that is PostgreSQL's own rule for a CHECK,
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


def test_the_constraints_admit_the_rows_the_previous_revision_wrote(
    migration_database: str,
) -> None:
    """A summary date with no per-feed dates has to stay legal.

    Rows planned under ``ad9f573e5e59`` carry a late quantity, a summary date
    and nothing per feed, and nothing can honestly say now which feed that date
    came from - so the revision deliberately does not backfill, and therefore
    cannot demand that a per-feed date be present. This is that decision
    asserted rather than assumed: a stricter constraint would fail against a
    real database on ``alembic upgrade``, which is the kind of thing this test
    exists to catch before it does.

    The rejected shapes are here too, at the level the database enforces them.
    The engine-level equivalents - a real run, real rows, an UPDATE through the
    ORM - are in ``tests/integration/test_supply_netting.py``.
    """
    dsn = migration_database
    alembic("upgrade", REVISION)
    definitions = check_constraints(dsn)
    assert set(definitions) == set(NEW_CONSTRAINTS)

    # (incoming, in production, summary, needed by)
    assert admits(dsn, definitions, (None, None, ONE_DAY_LATE, NEEDED_BY)), (
        "a row this revision's parent wrote: a summary date and no feed dates"
    )
    assert admits(dsn, definitions, (None, None, None, NEEDED_BY)), "nothing late at all"
    assert admits(dsn, definitions, (ONE_DAY_LATE, None, ONE_DAY_LATE, NEEDED_BY)), (
        "one feed late: LEAST ignores the NULL, so the summary is that feed's date"
    )
    assert admits(dsn, definitions, (FOUR_MONTHS_LATE, ONE_DAY_LATE, ONE_DAY_LATE, NEEDED_BY)), (
        "both feeds late: the summary is the earlier of the two"
    )

    # ...and the shapes this revision exists to end.
    assert not admits(dsn, definitions, (FOUR_MONTHS_LATE, None, ONE_DAY_LATE, NEEDED_BY)), (
        "the reproduction: 5 000 due in four months, summarised with a job's date"
    )
    assert not admits(dsn, definitions, (NEEDED_BY, None, NEEDED_BY, NEEDED_BY)), (
        "a feed date on the required date itself is in-time supply, not late"
    )
    assert not admits(
        dsn, definitions, (ONE_DAY_LATE, FOUR_MONTHS_LATE, FOUR_MONTHS_LATE, NEEDED_BY)
    ), "the summary is the earlier feed date, never the later one"
