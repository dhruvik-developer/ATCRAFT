"""Dispatch source-line attribution is backfilled only where it is provable.

The migration has three jobs and this test pins all three:

* trust ``dispatch_lines.inventory_transaction_id`` where it exists;
* attribute an unanchored movement only when exactly one line of its dispatch
  matches on item, batch and (when present) serial;
* leave every ambiguous movement NULL, so profitability reports pending cost
  instead of inventing an attribution.

It also proves the append-only trigger was re-enabled after the backfill and
that a downgrade carrying real attribution is refused rather than dropped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "20261110_0100"


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
    database = _MigrationDatabase(f"atcraft_source_line_mig_{uuid.uuid4().hex[:10]}")
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
        report_database_setup_failure(exc, purpose="source-line migration")
    try:
        yield database
    finally:
        assert database.name.startswith("atcraft_source_line_mig_")
        with psycopg.connect(**connection_options, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database.name))
            )


@dataclass(frozen=True)
class _Seed:
    company_id: uuid.UUID
    warehouse_id: uuid.UUID
    dispatch_id: uuid.UUID
    lines: dict[str, uuid.UUID]
    movements: dict[str, uuid.UUID]


def _seed_attribution_fixture(engine: Engine) -> _Seed:
    """Five movement shapes: anchored, unique, ambiguous, serial, non-dispatch."""
    company_id = uuid.uuid4()
    uom_id = uuid.uuid4()
    warehouse_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    dispatch_id = uuid.uuid4()
    item_ids = {name: uuid.uuid4() for name in ("A", "B", "C", "D", "E")}
    lines = {name: uuid.uuid4() for name in ("one", "two", "three", "four", "five", "six")}
    serial_five = uuid.uuid4()
    serial_six = uuid.uuid4()
    movements = {
        name: uuid.uuid4() for name in ("anchored", "unique", "ambiguous", "serial", "other_type")
    }

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO companies "
                "(id, code, name, country, currency, financial_year_start_month, "
                " timezone, is_active) "
                "VALUES (:id, :code, 'Source Line Migration', 'India', 'INR', 4, "
                "        'Asia/Kolkata', true)"
            ),
            {"id": company_id, "code": f"MIG{uuid.uuid4().hex[:8].upper()}"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO units_of_measure "
                "(id, company_id, code, name, uom_category, is_base) "
                "VALUES (:id, :company_id, 'NOS', 'Numbers', 'COUNT', true)"
            ),
            {"id": uom_id, "company_id": company_id},
        )
        for name, item_id in item_ids.items():
            connection.execute(
                sa.text(
                    "INSERT INTO items "
                    "(id, company_id, code, name, item_group, base_uom_id) "
                    "VALUES (:id, :company_id, :code, :name, 'FINISHED_GOOD', :uom_id)"
                ),
                {
                    "id": item_id,
                    "company_id": company_id,
                    "code": f"MIG-{name}",
                    "name": f"Migration item {name}",
                    "uom_id": uom_id,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO warehouses "
                "(id, company_id, code, name, warehouse_type, country, is_active, "
                " is_default) "
                "VALUES (:id, :company_id, 'WH1', 'Main store', 'FINISHED_GOODS', "
                "        'India', true, true)"
            ),
            {"id": warehouse_id, "company_id": company_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO customers "
                "(id, company_id, code, name, customer_type) "
                "VALUES (:id, :company_id, 'MIG-CU', 'Migration customer', 'CUSTOMER')"
            ),
            {"id": customer_id, "company_id": company_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO dispatches "
                "(id, company_id, code, customer_id, warehouse_id, dispatch_date, status) "
                "VALUES (:id, :company_id, 'DSP-MIG', :customer_id, :warehouse_id, "
                "        CURRENT_DATE, 'DISPATCHED')"
            ),
            {
                "id": dispatch_id,
                "company_id": company_id,
                "customer_id": customer_id,
                "warehouse_id": warehouse_id,
            },
        )

        line_rows = (
            ("one", 1, "A", []),
            ("two", 2, "B", []),
            # Three and four are deliberately indistinguishable by item and lot.
            ("three", 3, "C", []),
            ("four", 4, "C", []),
            # Five and six share item and lot too, but name different units.
            ("five", 5, "D", [serial_five]),
            ("six", 6, "D", [serial_six]),
        )
        for name, number, item, serials in line_rows:
            connection.execute(
                sa.text(
                    "INSERT INTO dispatch_lines "
                    "(id, company_id, dispatch_id, line_number, item_id, quantity, "
                    " serial_ids, inventory_transaction_id) "
                    "VALUES (:id, :company_id, :dispatch_id, :line_number, :item_id, "
                    "        1, CAST(:serial_ids AS uuid[]), :anchor)"
                ),
                {
                    "id": lines[name],
                    "company_id": company_id,
                    "dispatch_id": dispatch_id,
                    "line_number": number,
                    "item_id": item_ids[item],
                    "serial_ids": [str(serial) for serial in serials],
                    "anchor": movements["anchored"] if name == "one" else None,
                },
            )

        for serial, number in ((serial_five, "SER-5"), (serial_six, "SER-6")):
            connection.execute(
                sa.text(
                    "INSERT INTO stock_serials "
                    "(id, company_id, item_id, serial_number, warehouse_id) "
                    "VALUES (:id, :company_id, :item_id, :serial_number, :warehouse_id)"
                ),
                {
                    "id": serial,
                    "company_id": company_id,
                    "item_id": item_ids["D"],
                    "serial_number": number,
                    "warehouse_id": warehouse_id,
                },
            )

        movement_rows = (
            ("anchored", "DISPATCH", "A", None, -1),
            ("unique", "DISPATCH", "B", None, -1),
            ("ambiguous", "DISPATCH", "C", None, -1),
            ("serial", "DISPATCH", "D", serial_six, -1),
            ("other_type", "CUSTOMER_RETURN", "E", None, 1),
        )
        for name, transaction_type, item, serial_id, quantity in movement_rows:
            connection.execute(
                sa.text(
                    "INSERT INTO inventory_transactions "
                    "(id, company_id, transaction_type, item_id, warehouse_id, "
                    " serial_id, quantity, balance_after, reference_type, reference_id, "
                    " reason, occurred_at) "
                    "VALUES (:id, :company_id, :transaction_type, :item_id, :warehouse_id, "
                    "        :serial_id, :quantity, 0, 'dispatch', :dispatch_id, :reason, "
                    "        now())"
                ),
                {
                    "id": movements[name],
                    "company_id": company_id,
                    "transaction_type": transaction_type,
                    "item_id": item_ids[item],
                    "warehouse_id": warehouse_id,
                    "serial_id": serial_id,
                    "quantity": quantity,
                    "dispatch_id": dispatch_id,
                    "reason": f"migration-{name}",
                },
            )

    return _Seed(
        company_id=company_id,
        warehouse_id=warehouse_id,
        dispatch_id=dispatch_id,
        lines=lines,
        movements=movements,
    )


def _attribution(engine: Engine) -> dict[uuid.UUID, uuid.UUID | None]:
    with engine.connect() as connection:
        return {
            row.id: row.source_line_id
            for row in connection.execute(
                sa.text(
                    "SELECT id, source_line_id FROM inventory_transactions "
                    "WHERE reason LIKE 'migration-%'"
                )
            )
        }


def test_source_line_backfill_attributes_only_provable_movements(
    migration_database: _MigrationDatabase,
) -> None:
    database = migration_database
    database.alembic("upgrade", PREVIOUS_REVISION)
    engine = database.engine()
    try:
        seed = _seed_attribution_fixture(engine)

        database.alembic("upgrade", "head")
        database.alembic("check")

        assert _attribution(engine) == {
            seed.movements["anchored"]: seed.lines["one"],
            seed.movements["unique"]: seed.lines["two"],
            # Two lines share item C and lot NULL: no honest answer exists.
            seed.movements["ambiguous"]: None,
            # The serial picks line six out of the two item-D lines.
            seed.movements["serial"]: seed.lines["six"],
            # Only DISPATCH movements are attributed to dispatch lines.
            seed.movements["other_type"]: None,
        }

        with engine.connect() as connection:
            assert connection.scalar(
                sa.text(
                    "SELECT to_regclass('public.ix_inventory_transactions_source_line') IS NOT NULL"
                )
            )

        # The backfill disabled the append-only trigger for its own transaction;
        # it must be watching again afterwards.
        with pytest.raises(DBAPIError, match="not permitted"), engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE inventory_transactions SET reason = 'rewritten' WHERE id = :id"),
                {"id": seed.movements["anchored"]},
            )

        refused = database.run_alembic("downgrade", PREVIOUS_REVISION)
        assert refused.returncode != 0
        assert "would discard" in f"{refused.stdout}\n{refused.stderr}"

        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'inventory_transactions' "
                        "AND column_name = 'source_line_id'"
                    )
                )
                == 1
            )
    finally:
        engine.dispose()
