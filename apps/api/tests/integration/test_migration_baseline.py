"""A fresh database builds the current head schema and rolls back cleanly.

The 58 pre-squash revisions were replaced by one baseline generated from a
``pg_dump --schema-only`` of the verified head database, and post-squash
revisions sit on top of it. This test proves the fresh-install path: an empty
database reaches the full schema through the migration chain, the schema still
matches the models and constraint vocabulary that the application depends on,
and a disposable database can be downgraded and rebuilt.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest

from src.modules.finance.models import PurchaseReturnDebitAllocation, VendorInvoice
from src.modules.inventory.models import (
    BinMove,
    BinMoveLine,
    InternalIssue,
    InternalIssueLine,
    ScrapDisposal,
    ScrapDisposalLine,
)
from src.modules.mrp.models import MrpRequirement
from src.modules.routing.models import Shift, ShiftAssignment

pytestmark = pytest.mark.integration

HEAD_REVISION = "20261022_0100"
DB_NAME = f"atcraft_baseline_mig_{uuid.uuid4().hex[:8]}"
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


def base_table_names(dsn: str) -> set[str]:
    rows = query(
        dsn,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
    )
    return {str(name) for (name,) in rows}


def table_constraints(dsn: str, table: str, kind: str | None = None) -> dict[str, tuple[str, bool]]:
    statement = (
        "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint "
        "WHERE conrelid = to_regclass(%s)"
    )
    params: list[object] = [f"public.{table}"]
    if kind is not None:
        statement += " AND contype = %s"
        params.append(kind)
    return {
        str(name): (str(definition), bool(valid))
        for name, definition, valid in query(dsn, statement, *params)
    }


def mrp_late_constraints(dsn: str) -> dict[str, tuple[str, bool]]:
    rows = query(
        dsn,
        "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint "
        "WHERE conrelid = to_regclass('public.mrp_requirements') AND contype = 'c'",
    )
    return {
        str(name): (str(definition), bool(valid))
        for name, definition, valid in rows
        if str(name).startswith("ck_mrp_requirements_late")
        or name == "ck_mrp_requirements_first_late_supply_is_the_earlier"
    }


MRP_LATE_COLUMNS = (
    ("late_incoming_quantity", "numeric"),
    ("late_in_production_quantity", "numeric"),
    ("first_late_incoming_date", "date"),
    ("last_late_incoming_date", "date"),
    ("first_late_in_production_date", "date"),
    ("last_late_in_production_date", "date"),
    ("first_late_supply_date", "date"),
    ("earliest_required_date", "date"),
)


def admits_mrp_row(
    dsn: str,
    definitions: dict[str, tuple[str, bool]],
    row: tuple[object, ...],
) -> bool:
    """Ask PostgreSQL whether every late-supply CHECK accepts this candidate row.

    The constraints are read back from ``pg_constraint`` and evaluated by the
    database, so this cannot pass merely because a test restated the rule.
    """
    names = ", ".join(name for name, _ in MRP_LATE_COLUMNS)
    placeholders = ", ".join(f"%s::{kind}" for _, kind in MRP_LATE_COLUMNS)
    with psycopg.connect(dsn) as connection:
        for definition, _ in definitions.values():
            expression = definition.removeprefix("CHECK ")
            (verdict,) = connection.execute(
                f"SELECT ({expression}) IS NOT FALSE "
                f"FROM (VALUES ({placeholders})) AS candidate({names})",
                row,
            ).fetchone()
            if not verdict:
                return False
    return True


def test_baseline_matches_models_and_round_trips(migration_database: str) -> None:
    dsn = migration_database

    assert base_table_names(dsn) == set()

    alembic("upgrade", "head")

    assert query(dsn, "SELECT version_num FROM alembic_version")[0][0] == HEAD_REVISION
    assert {
        "pgcrypto",
        "pg_trgm",
        "btree_gist",
        "pg_stat_statements",
    } <= {str(name) for (name,) in query(dsn, "SELECT extname FROM pg_extension")}

    assert column_names(dsn, "purchase_return_debit_allocations") == {
        column.name for column in PurchaseReturnDebitAllocation.__table__.columns
    }
    # The E3a revision's columns are part of the head schema: migratable in a
    # fresh database before the application reads them.
    assert {"low_level_code", "expected_available_date"} <= column_names(dsn, "mrp_requirements")
    assert {"low_level_code", "expected_available_date"} <= column_names(dsn, "mrp_suggestions")
    assert "expected_available_date" in column_names(dsn, "mrp_demands")

    report_check = str(
        query(
            dsn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(%s) AND conname = %s",
            "public.report_snapshots",
            "ck_report_snapshots_report_key_is_known",
        )[0][0]
    )
    assert "PURCHASE_RETURN_DEBIT_NOTE" in report_check
    assert "SHIFT_PRODUCTION" in report_check
    assert "COST_CENTRE_EXPENSE" in report_check
    assert "SCRAP_REGISTER" in report_check
    assert "CONSUMPTION_BY_DEPARTMENT" in report_check

    constraints = {
        str(name): str(definition)
        for name, definition in query(
            dsn,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(%s)",
            "public.purchase_return_debit_allocations",
        )
    }
    normalized_checks = {
        name: re.sub(r"[\s()]", "", definition).lower() for name, definition in constraints.items()
    }
    assert (
        "amount>0::numeric"
        in normalized_checks["ck_purchase_return_debit_allocations_amount_positive"]
    )
    assert "purchase_return_id" in constraints["uq_return_debit_allocations_target"]
    assert "vendor_invoice_id" in constraints["uq_return_debit_allocations_target"]
    assert {
        "fk_pr_debit_allocations_return_id",
        "fk_pr_debit_allocations_invoice_id",
    } <= constraints.keys()
    invoice_constraints = {
        str(name): re.sub(r"[\s()]", "", str(definition)).lower()
        for name, definition in query(
            dsn,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(%s)",
            "public.vendor_invoices",
        )
    }
    assert (
        "debit_allocated_amount>=0::numeric"
        in invoice_constraints["ck_vendor_invoices_debit_allocated_non_negative"]
    )

    invoice_columns = {
        str(name): (str(nullable), default)
        for name, nullable, default in query(
            dsn,
            "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'vendor_invoices'",
        )
    }
    debit_amount_nullable, debit_amount_default = invoice_columns["debit_allocated_amount"]
    assert debit_amount_nullable == "NO"
    assert re.sub(r"\D", "", str(debit_amount_default)) == "0"
    assert "debit_allocated_amount" in {column.name for column in VendorInvoice.__table__.columns}

    for model in (
        BinMove,
        BinMoveLine,
        ScrapDisposal,
        ScrapDisposalLine,
        InternalIssue,
        InternalIssueLine,
    ):
        assert column_names(dsn, model.__tablename__) == {
            column.name for column in model.__table__.columns
        }
    inventory_checks = table_constraints(dsn, "inventory_cost_layers", "c")
    pool_definition, pool_valid = inventory_checks["ck_inventory_cost_layers_pool_known"]
    assert "ON_HAND" in pool_definition and "SCRAP" in pool_definition
    assert pool_valid
    location_definition, location_valid = table_constraints(dsn, "items", "f")[
        "fk_items_default_location_id_storage_locations"
    ]
    assert "default_location_id" in location_definition
    assert location_valid

    assert column_names(dsn, "mrp_requirements") == {
        column.name for column in MrpRequirement.__table__.columns
    }
    mrp_checks = mrp_late_constraints(dsn)
    assert set(mrp_checks) == {
        "ck_mrp_requirements_first_late_supply_is_the_earlier",
        "ck_mrp_requirements_late_incoming_arrives_after_required",
        "ck_mrp_requirements_late_incoming_interval_ordered",
        "ck_mrp_requirements_late_incoming_non_negative",
        "ck_mrp_requirements_late_in_production_arrives_after_required",
        "ck_mrp_requirements_late_production_interval_ordered",
        "ck_mrp_requirements_late_in_production_non_negative",
        "ck_mrp_requirements_late_quantity_names_its_date",
        "ck_mrp_requirements_late_supply_arrives_after_required",
    }
    assert all(valid for _, valid in mrp_checks.values())
    normalized_mrp = " ".join(
        re.sub(r"[\s()]", "", definition).lower() for definition, _ in mrp_checks.values()
    )
    assert "late_incoming_quantity>=0::numeric" in normalized_mrp
    assert "late_in_production_quantity>=0::numeric" in normalized_mrp
    assert "last_late_incoming_date>=first_late_incoming_date" in normalized_mrp
    assert "last_late_in_production_date>=first_late_in_production_date" in normalized_mrp
    assert "first_late_supply_date=leastfirst_late_incoming_date" in normalized_mrp
    assert "first_late_supply_date>earliest_required_date" in normalized_mrp
    assert "first_late_incoming_date>earliest_required_date" in normalized_mrp
    assert "first_late_in_production_date>earliest_required_date" in normalized_mrp
    assert "first_late_supply_dateisnotnull" in normalized_mrp

    needed = date(2026, 7, 15)
    opened = date(2026, 7, 30)
    closed = opened + timedelta(days=138)
    four_months_late = needed + timedelta(days=120)
    rows = {
        "nothing late": (0, 0, None, None, None, None, None, needed),
        "summary only": (5, 0, None, None, None, None, opened, needed),
        "one interval": (5, 0, opened, closed, None, None, opened, needed),
        "two intervals": (5, 5, opened, closed, opened, closed, opened, needed),
        "closing before opening": (
            5,
            0,
            opened,
            opened - timedelta(days=1),
            None,
            None,
            opened,
            needed,
        ),
        "closing without opening": (5, 0, None, closed, None, None, None, needed),
        "quantity without date": (5, 0, None, None, None, None, None, needed),
        "summary after a feed": (
            5,
            5,
            opened,
            closed,
            four_months_late,
            None,
            four_months_late,
            needed,
        ),
        "date on required day": (5, 0, needed, needed, None, None, needed, needed),
        "production interval reversed": (
            0,
            0,
            None,
            None,
            opened,
            opened - timedelta(days=1),
            None,
            needed,
        ),
    }
    assert admits_mrp_row(dsn, mrp_checks, rows["nothing late"])
    assert admits_mrp_row(dsn, mrp_checks, rows["summary only"])
    assert admits_mrp_row(dsn, mrp_checks, rows["one interval"])
    assert admits_mrp_row(dsn, mrp_checks, rows["two intervals"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["closing before opening"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["closing without opening"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["quantity without date"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["summary after a feed"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["date on required day"])
    assert not admits_mrp_row(dsn, mrp_checks, rows["production interval reversed"])

    assert column_names(dsn, "shifts") == {column.name for column in Shift.__table__.columns}
    assert column_names(dsn, "shift_assignments") == {
        column.name for column in ShiftAssignment.__table__.columns
    }
    exclusions = table_constraints(dsn, "shift_assignments", "x")
    assert set(exclusions) == {
        "ex_shift_assignments_operator_window",
        "ex_shift_assignments_machine_window",
    }
    operator_rule, operator_valid = exclusions["ex_shift_assignments_operator_window"]
    machine_rule, machine_valid = exclusions["ex_shift_assignments_machine_window"]
    assert operator_valid and machine_valid
    assert "operator_user_id WITH =" in operator_rule
    assert "tstzrange(starts_at, ends_at, '[)'::text) WITH &&" in operator_rule
    assert "machine_id IS NOT NULL" in machine_rule
    shift_fks = table_constraints(dsn, "shift_assignments", "f")
    assert "fk_shift_assignments_machine_id_work_center_id_machines" in shift_fks
    for table in ("work_orders", "production_entries"):
        assert {"shift", "shift_id"} <= column_names(dsn, table)
        definition, valid = table_constraints(dsn, table, "f")[f"fk_{table}_shift_id_shifts"]
        assert "REFERENCES shifts(id) ON DELETE RESTRICT" in definition
        assert valid
    shift_checks = table_constraints(dsn, "shifts", "c")
    assert {"ck_shifts_midnight_flag_matches_times", "ck_shifts_break_minutes_bounded"} <= set(
        shift_checks
    )

    guards = {
        (str(table), str(trigger))
        for table, trigger in query(
            dsn,
            "SELECT c.relname, t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal",
        )
    }
    assert ("audit_logs", "trg_audit_logs_immutable") in guards
    assert ("inventory_transactions", "trg_inventory_transactions_immutable") in guards
    assert (
        "inventory_cost_consumptions",
        "trg_inventory_cost_consumptions_immutable",
    ) in guards
    assert ("login_attempts", "trg_login_attempts_no_update") in guards
    assert (
        query(
            dsn,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'atcraft_reject_row_mutation'",
        )[0][0]
        == 1
    )

    alembic("downgrade", "base")
    assert base_table_names(dsn) <= {"alembic_version"}
    assert (
        query(
            dsn,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'atcraft_reject_row_mutation'",
        )[0][0]
        == 0
    )

    alembic("upgrade", "head")
    assert {
        "purchase_return_debit_allocations",
        "vendor_invoices",
        "report_snapshots",
        "audit_logs",
        "bin_moves",
        "internal_issues",
        "mrp_requirements",
        "shifts",
        "shift_assignments",
    } <= base_table_names(dsn)
    assert query(dsn, "SELECT version_num FROM alembic_version")[0][0] == HEAD_REVISION
