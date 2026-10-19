"""The purchase-return debit allocation migration matches its model and rolls back."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from src.modules.finance.models import PurchaseReturnDebitAllocation, VendorInvoice

pytestmark = pytest.mark.integration

PARENT = "20261018_0100"
REVISION = "20261019_0100"
DEBIT_NOTE_PDF_REVISION = "20261020_0100"
DB_NAME = f"atcraft_return_debit_mig_{uuid.uuid4().hex[:8]}"
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


def test_migration_matches_model_and_round_trips(migration_database: str) -> None:
    dsn = migration_database

    alembic("upgrade", PARENT)
    assert "debit_allocated_amount" not in column_names(dsn, "vendor_invoices")
    assert column_names(dsn, "purchase_return_debit_allocations") == set()

    alembic("upgrade", REVISION)
    assert "debit_allocated_amount" in column_names(dsn, "vendor_invoices")
    assert column_names(dsn, "purchase_return_debit_allocations") == {
        column.name for column in PurchaseReturnDebitAllocation.__table__.columns
    }

    alembic("upgrade", DEBIT_NOTE_PDF_REVISION)
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

    alembic("downgrade", REVISION)
    report_check = str(
        query(
            dsn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(%s) AND conname = %s",
            "public.report_snapshots",
            "ck_report_snapshots_report_key_is_known",
        )[0][0]
    )
    assert "PURCHASE_RETURN_DEBIT_NOTE" not in report_check

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

    alembic("downgrade", PARENT)
    assert "debit_allocated_amount" not in column_names(dsn, "vendor_invoices")
    assert column_names(dsn, "purchase_return_debit_allocations") == set()

    alembic("upgrade", REVISION)
    assert "debit_allocated_amount" in column_names(dsn, "vendor_invoices")
    assert column_names(dsn, "purchase_return_debit_allocations") == {
        column.name for column in PurchaseReturnDebitAllocation.__table__.columns
    }
