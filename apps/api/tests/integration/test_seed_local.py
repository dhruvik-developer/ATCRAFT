"""The additive local entrypoint builds a real business and preserves it on rerun."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL

from tests.conftest import _pg_host, _pg_password, _pg_port, _pg_user
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration
API_ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Cedar!8427Example"


@pytest.fixture
def seed_database() -> Iterator[URL]:
    database = URL.create(
        "postgresql+psycopg",
        username=_pg_user,
        password=_pg_password,
        host=_pg_host,
        port=int(_pg_port),
        database=f"atcraft_seed_cli_{uuid.uuid4().hex[:10]}",
    )
    administrative_url = database.set(database="postgres", drivername="postgresql")
    try:
        with psycopg.connect(
            administrative_url.render_as_string(hide_password=False),
            autocommit=True,
            connect_timeout=10,
        ) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database.database))
            )
    except Exception as exc:
        report_database_setup_failure(exc, purpose="local seed entrypoint integration test")
    try:
        yield database
    finally:
        with psycopg.connect(
            administrative_url.render_as_string(hide_password=False),
            autocommit=True,
            connect_timeout=10,
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database.database)
                )
            )


def _table_counts(connection: psycopg.Connection) -> dict[str, int]:
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    return {
        table: connection.execute(
            sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table))
        ).fetchone()[0]
        for (table,) in tables
    }


def test_fresh_local_seed_rerun_preserves_all_rows_and_existing_password(seed_database, tmp_path):
    environment = dict(os.environ)
    environment.update(
        APP_ENV="test",
        DATABASE_URL=seed_database.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        ),
        DATABASE_SYNC_URL=seed_database.render_as_string(hide_password=False),
        STORAGE_PATH=str(tmp_path / "storage"),
        # The wrapper must override these process-only delivery settings; neither
        # a Redis server nor an SMTP connection is needed for the complete seed.
        CELERY_BROKER_URL="redis://127.0.0.1:1/1",
        CELERY_RESULT_BACKEND="redis://127.0.0.1:1/2",
        EMAIL_PROVIDER="smtp",
        EMAIL_FROM_ADDRESS="seed@example.com",
        SMTP_HOST="127.0.0.1",
        SMTP_PORT="1",
        WEB_PUSH_ENABLED="true",
        DB_ECHO="true",
    )
    command = [
        sys.executable,
        "-m",
        "scripts.seed_local",
        "--company",
        "Seed CLI Example",
        "--code",
        "SEEDCLI",
        "--admin-email",
        "Seed.Admin@Example.com",
        "--admin-password",
        PASSWORD,
    ]

    def run() -> None:
        result = subprocess.run(
            command,
            cwd=API_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        output = result.stdout + result.stderr
        assert command[-1] not in output, (
            "An explicitly supplied administrator secret was displayed."
        )
        assert result.returncode == 0, output
        assert "Local seed complete" in output

    plain_url = seed_database.set(drivername="postgresql").render_as_string(hide_password=False)
    run()
    with psycopg.connect(plain_url) as connection:
        before = _table_counts(connection)
        company = connection.execute(
            "SELECT id, name FROM companies WHERE code = %s", ("SEEDCLI",)
        ).fetchone()
        assert company is not None and company[1] == "Seed CLI Example"
        administrator = connection.execute(
            "SELECT password_hash, must_change_password FROM users "
            "WHERE company_id = %s AND email = %s",
            (company[0], "seed.admin@example.com"),
        ).fetchone()
        assert administrator is not None and administrator[1] is True
        assert before["items"] > 0
        assert before["sales_orders"] > 0
        assert before["purchase_orders"] > 0
        assert before["inventory_transactions"] > 0
        assert before["documents"] > 0
        assert before["report_snapshots"] > 0
        for table in (
            "plants",
            "financial_years",
            "item_variants",
            "specification_attributes",
            "category_specifications",
            "item_specifications",
            "item_accessories",
            "stock_batches",
            "stock_serials",
            "stock_transfers",
            "stock_adjustments",
            "physical_counts",
            "sales_returns",
            "credit_notes",
            "inventory_summary",
        ):
            assert before[table] > 0, f"Fresh seed left {table} empty."
        for table in ("sales_invoices", "credit_notes"):
            assert (
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE pdf_document_id IS NULL").format(
                        sql.Identifier(table)
                    )
                ).fetchone()[0]
                == 0
            ), f"Fresh seed left {table} without PDF downloads."
        assert (
            connection.execute(
                "SELECT count(*) FROM report_snapshots WHERE status = 'QUEUED'"
            ).fetchone()[0]
            == 0
        )

    def stored_files() -> dict[str, str]:
        root = tmp_path / "storage"
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        }

    files_before = stored_files()
    assert len(files_before) >= 11  # Seven sample uploads, two invoices and two credits.

    # A rerun may repair existing system permissions but never resets credentials
    # or adds a second copy of any successful business workflow.
    command[-1] = "Maple!9358Alternate"
    run()
    with psycopg.connect(plain_url) as connection:
        assert _table_counts(connection) == before
        assert (
            connection.execute(
                "SELECT password_hash, must_change_password FROM users "
                "WHERE company_id = %s AND email = %s",
                (company[0], "seed.admin@example.com"),
            ).fetchone()
            == administrator
        )
    assert stored_files() == files_before
