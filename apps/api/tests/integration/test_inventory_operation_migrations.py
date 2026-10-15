"""Build and reverse the inventory operation schema in an isolated database."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from src.modules.inventory.models import (
    BinMove,
    BinMoveLine,
    InternalIssue,
    InternalIssueLine,
    ScrapDisposal,
    ScrapDisposalLine,
)

pytestmark = pytest.mark.integration
PARENT = "159ffb5b9cb4"
HEAD = "a91d8a14b2c7"
API_ROOT = Path(__file__).resolve().parents[2]


def test_inventory_documents_round_trip_and_match_models():
    from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user

    database = f"atcraft_inventory_ops_mig_{uuid.uuid4().hex[:8]}"
    server = f"{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}"
    environment = dict(os.environ)
    environment["DATABASE_URL"] = f"postgresql+asyncpg://{server}/{database}"
    environment["DATABASE_SYNC_URL"] = f"postgresql+psycopg://{server}/{database}"
    admin_dsn = f"postgresql://{server}/postgres?connect_timeout=10"
    dsn = f"postgresql://{server}/{database}?connect_timeout=10"

    def migrate(action: str, revision: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=API_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        diagnostic = (result.stdout + result.stderr).replace(_pg_password, "[redacted]")
        assert result.returncode == 0, diagnostic

    def verify() -> None:
        with psycopg.connect(dsn) as connection:
            for model in (
                BinMove,
                BinMoveLine,
                ScrapDisposal,
                ScrapDisposalLine,
                InternalIssue,
                InternalIssueLine,
            ):
                columns = connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (model.__tablename__,),
                ).fetchall()
                assert {name for (name,) in columns} == set(model.__table__.columns.keys())
            check = connection.execute(
                "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                "WHERE conrelid = 'inventory_cost_layers'::regclass AND contype = 'c'",
            ).fetchall()
            assert any(
                "ON_HAND" in definition and "SCRAP" in definition and valid
                for definition, valid in check
            )
            reports = connection.execute(
                "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                "WHERE conrelid = 'report_snapshots'::regclass AND contype = 'c'",
            ).fetchall()
            assert any(
                "SCRAP_REGISTER" in definition
                and "CONSUMPTION_BY_DEPARTMENT" in definition
                and valid
                for definition, valid in reports
            )
            foreign_key = connection.execute(
                "SELECT convalidated FROM pg_constraint WHERE conrelid = 'items'::regclass "
                "AND contype = 'f' AND pg_get_constraintdef(oid) LIKE '%default_location_id%'",
            ).fetchall()
            assert foreign_key == [(True,)]

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        migrate("upgrade", PARENT)
        migrate("upgrade", HEAD)
        verify()
        migrate("downgrade", PARENT)
        with psycopg.connect(dsn) as connection:
            assert connection.execute("SELECT to_regclass('bin_moves')").fetchone() == (None,)
            assert connection.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'inventory_cost_layers' AND column_name = 'pool'",
            ).fetchone() == (0,)
        migrate("upgrade", HEAD)
        verify()
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
