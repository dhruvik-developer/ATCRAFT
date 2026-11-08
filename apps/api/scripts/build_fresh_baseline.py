"""Build a single Alembic baseline from ``pg_dump --schema-only`` of head.

Usage (from ``apps/api``, with Docker postgres running)::

    .venv/Scripts/python.exe -m scripts.build_fresh_baseline

Creates a disposable database, upgrades it through the current chain, dumps
the schema, and writes ``migrations/versions/<revision>_fresh_baseline.py``.
Does not delete older revision files; the caller removes them after review.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
REVISION = "20261127_0100"
OUT_PATH = API_ROOT / "migrations" / "versions" / f"{REVISION}_fresh_baseline.py"
SCRATCH_DB = "atcraft_erp_squash_scratch"
DUMP_PATH = API_ROOT / "migrations" / "_squash_schema.sql"
PG_USER = "atcraft"
CONTAINER = "atcraft-erp-postgres-1"

SKIP_PREFIXES = (
    "--",
    "SET ",
    "SELECT pg_catalog",
    "\\restrict",
    "\\unrestrict",
)
SKIP_CONTAINS = (
    "alembic_version",
    "OWNER TO",
    "GRANT ",
    "REVOKE ",
    "ALTER DEFAULT PRIVILEGES",
)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    # Commands are fixed literals in this script (docker / alembic), not user input.
    return subprocess.run(cmd, check=check, text=True, capture_output=True)  # noqa: S603


def psql(sql: str, *, database: str = "postgres") -> None:
    result = run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-U",
            PG_USER,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
    )
    if result.stdout.strip():
        print(result.stdout, end="")
    if result.stderr.strip():
        print(result.stderr, end="", file=sys.stderr)


def _dotenv_values() -> dict[str, str]:
    values: dict[str, str] = {}
    dotenv = API_ROOT.parents[1] / ".env"
    if not dotenv.exists():
        return values
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split("#", 1)[0].strip().strip('"').strip("'")
    return values


def alembic_upgrade() -> None:
    dotenv = _dotenv_values()
    user = dotenv.get("POSTGRES_USER", PG_USER)
    password = dotenv.get("POSTGRES_PASSWORD", "atcraft_dev_password")
    host = dotenv.get("POSTGRES_HOST", "127.0.0.1")
    port = dotenv.get("POSTGRES_PORT", "5432")
    sync_url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{SCRATCH_DB}"
    async_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{SCRATCH_DB}"
    env = {
        **dict(__import__("os").environ),
        "DATABASE_SYNC_URL": sync_url,
        "DATABASE_URL": async_url,
        "POSTGRES_HOST": host,
        "POSTGRES_PORT": port,
        "POSTGRES_USER": user,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": SCRATCH_DB,
    }
    print(f"+ alembic upgrade head -> {host}:{port}/{SCRATCH_DB}", flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout)
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        raise SystemExit(f"alembic upgrade failed: {completed.returncode}")


def dump_schema() -> str:
    result = run(
        [
            "docker",
            "exec",
            CONTAINER,
            "pg_dump",
            "-U",
            PG_USER,
            "-d",
            SCRATCH_DB,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--no-tablespaces",
        ]
    )
    DUMP_PATH.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def _keep_statement(text: str) -> bool:
    if not text:
        return False
    if any(text.startswith(p) for p in SKIP_PREFIXES):
        return False
    if any(s in text for s in SKIP_CONTAINS):
        return False
    return not text.upper().startswith("SET ")


def split_statements(dump: str) -> list[str]:
    """Split pg_dump output into executable SQL statements."""
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    dollar_tag = ""

    def commit() -> None:
        text = "\n".join(buf).strip()
        buf.clear()
        if text.endswith(";"):
            text = text[:-1].rstrip()
        if _keep_statement(text):
            statements.append(text)

    for raw_line in dump.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if in_dollar:
            buf.append(line)
            if dollar_tag and (stripped == f"{dollar_tag};" or stripped.endswith(f"{dollar_tag};")):
                commit()
                in_dollar = False
                dollar_tag = ""
            continue

        # Standalone comments and blanks never start a statement.
        if not stripped or stripped.startswith("--") or stripped.startswith("\\"):
            continue

        buf.append(line)
        if re.search(r"\bAS\s+\$([A-Za-z0-9_]*)\$", line, flags=re.IGNORECASE):
            match = re.search(r"\$([A-Za-z0-9_]*)\$", line)
            assert match is not None
            dollar_tag = match.group(0)
            in_dollar = True
            continue

        if stripped.endswith(";"):
            commit()

    if buf:
        commit()
    return statements


def table_names(statements: list[str]) -> list[str]:
    names: list[str] = []
    for stmt in statements:
        match = re.match(
            r'CREATE TABLE (?:IF NOT EXISTS )?public\.("?)([a-z0-9_]+)\1',
            stmt,
            flags=re.IGNORECASE,
        )
        if match:
            names.append(match.group(2))
    return names


def py_string(value: str) -> str:
    return repr(value)


def _normalise_statement(text: str) -> str:
    """Make idempotent-safe forms for objects pg_dump emits as plain CREATE."""
    if text.startswith("CREATE FUNCTION "):
        return "CREATE OR REPLACE FUNCTION " + text[len("CREATE FUNCTION ") :]
    return text


def write_migration(statements: list[str], tables: list[str]) -> None:
    header = f'''"""Fresh baseline schema, squashed from the entire revision chain.

Revision ID: {REVISION}
Revises: (none)
Create Date: 2026-11-08

This single revision is the complete schema at the former head
``20261126_0100`` (baseline plus forward revisions through 2026-11-26). It was
produced from ``pg_dump --schema-only`` of a database migrated through the old
chain, so every constraint, CHECK, index, server default, trigger and the
append-only mutation guard are exactly the ones the application has been
running. The application's extensions (pgcrypto, pg_trgm, btree_gist and
pg_stat_statements) are included; only Alembic's own ``alembic_version`` table
and the psql/SET preamble are omitted.

Existing databases must not be upgraded through this revision. After verifying
the database schema matches this baseline, mark it with::

    alembic stamp --purge {REVISION}

``--purge`` clears the retired revision id first; a plain ``stamp`` cannot
resolve the previous head now that the historical files are gone. That keeps
every historical data backfill, permission grant, report-key cleanup and CHECK
swap already applied without replaying them. A fresh database runs
``alembic upgrade head`` once.

The pre-squash chain remains recoverable at git tag
``pre-migration-squash-20261127``. See
``docs/implementation/migration-squash-20261127.md`` for the stamp runbook and
recovery plan.
"""

from __future__ import annotations

from alembic import op

revision: str = "{REVISION}"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

#: Every statement needed to build the schema, in dependency order. They came
#: from the verified head schema, not from model metadata, so database-level
#: constraints the models do not declare are preserved.
SCHEMA_STATEMENTS: tuple[str, ...] = (
'''
    body_parts = [f"    {py_string(stmt)},\n" for stmt in statements]
    mid = ")\n\nDOWNGRADE_TABLES: tuple[str, ...] = (\n"
    # Drop in reverse create order for fewer dependency surprises.
    for name in reversed(tables):
        mid += f"    {py_string(name)},\n"
    mid += ')\n\nMUTATION_GUARD = "atcraft_reject_row_mutation"\n\n\n'
    footer = """def upgrade() -> None:
    bind = op.get_bind()
    for statement in SCHEMA_STATEMENTS:
        # psycopg treats ``%`` as a placeholder marker even when no parameters
        # are bound, so literal percents in the captured DDL must be doubled.
        bind.exec_driver_sql(statement.replace("%", "%%"))


def downgrade() -> None:
    # A baseline is rolled back by restoring the pre-squash backup, not by
    # replaying historical revisions. This destructive path exists for
    # disposable databases and tests only; it leaves the extensions installed.
    bind = op.get_bind()
    for table in DOWNGRADE_TABLES:
        bind.exec_driver_sql(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS public.{MUTATION_GUARD}() CASCADE")
"""
    OUT_PATH.write_text(header + "".join(body_parts) + mid + footer, encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(statements)} statements, {len(tables)} tables)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="Reuse an existing dump file instead of recreating the scratch DB",
    )
    args = parser.parse_args()

    if not args.dump_only:
        psql(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE);")
        psql(f"CREATE DATABASE {SCRATCH_DB} OWNER {PG_USER};")
        alembic_upgrade()
        dump = dump_schema()
    else:
        dump = DUMP_PATH.read_text(encoding="utf-8")

    statements = [_normalise_statement(s) for s in split_statements(dump)]
    if not statements:
        raise SystemExit("No schema statements parsed from dump")
    tables = table_names(statements)
    if not tables:
        raise SystemExit("No CREATE TABLE statements found")
    write_migration(statements, tables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
