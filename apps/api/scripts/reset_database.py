"""Drop everything and rebuild the schema from the migrations.

For development and evaluation only. This exists because the alternative -
unpicking demonstration data row by row - cannot be made reliable: the 299 rows
:mod:`src.core.demo_data` creates span 33 tables joined by foreign keys, and
child rows (order lines, addresses, ledger entries, cost layers) carry no ``DM-``
code to recognise them by. A delete pass would leave orphans behind and call
itself clean.

Rebuilding is exact. What comes out is a database containing precisely what the
migrations define and nothing else, which is what a handover wants.

The intended cycle::

    python -m scripts.reset_database --seed --demo    # a working test system
    ... test against it ...
    python -m scripts.reset_database --seed           # pristine, for handover

The second command leaves the baseline every install needs - roles, permissions,
units, warehouse types, one administrator - and none of the invented business.

REFUSES to run against a database whose name looks like production, and refuses
without ``--yes``. Neither guard is clever; both are there because the cost of
being wrong is somebody's data.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings
from src.core.logging import configure_logging, get_logger

log = get_logger(__name__)

API_DIR = Path(__file__).resolve().parents[1]

#: A database whose name contains one of these is not a scratch database.
#: Deliberately blunt: a false refusal costs a rename, a false permission costs
#: everything.
PRODUCTION_MARKERS = ("prod", "live", "production", "staging")


def _database_name() -> str:
    return settings.database_url.rsplit("/", 1)[-1].split("?")[0]


def _assert_safe(name: str, *, force: bool) -> None:
    marker = next((m for m in PRODUCTION_MARKERS if m in name.lower()), None)
    if marker and not force:
        raise SystemExit(
            f"\n  Refusing to reset {name!r}: the name contains {marker!r}.\n"
            "  If this really is a scratch database, rename it or pass --i-know-what-i-am-doing.\n"
        )
    is_local = "localhost" in settings.database_url or "127.0.0.1" in settings.database_url
    if not is_local and not force:
        raise SystemExit(
            "\n  Refusing to reset a database that is not on localhost.\n"
            "  DATABASE_URL points at a remote host.\n"
        )


async def _drop_and_recreate_schema() -> None:
    """Empty the database by dropping its public schema.

    Dropping the schema rather than the database itself means no reconnection
    dance and no need for a second database to issue the command from - and it
    takes the sequences, types and indexes with it, which a table-by-table
    ``DROP TABLE`` loop is easy to get wrong.
    """
    engine = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            log.info("reset.schema_dropped")
    finally:
        await engine.dispose()


def _run(command: list[str], label: str) -> None:
    log.info("reset.step", step=label)
    result = subprocess.run(command, cwd=API_DIR, check=False)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(f"\n  {label} failed with exit code {result.returncode}.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="Required. Confirms the drop.")
    parser.add_argument("--seed", action="store_true", help="Load baseline reference data after.")
    parser.add_argument(
        "--demo", action="store_true", help="Also load demonstration data (implies --seed)."
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Administrator password for --seed. Omit to have one generated and printed once.",
    )
    parser.add_argument(
        "--i-know-what-i-am-doing",
        action="store_true",
        dest="force",
        help="Override the production-name and remote-host guards.",
    )
    args = parser.parse_args()

    configure_logging()
    name = _database_name()
    _assert_safe(name, force=args.force)

    if not args.yes:
        raise SystemExit(
            f"\n  This DESTROYS every table and row in {name!r}.\n"
            "  Re-run with --yes if that is what you want.\n"
        )

    print(f"\n  Resetting {name} ...")
    asyncio.run(_drop_and_recreate_schema())

    python = sys.executable
    _run([python, "-m", "alembic", "upgrade", "head"], "alembic upgrade head")

    if args.seed or args.demo:
        seed_command = [python, "-m", "src.core.seed"]
        if args.admin_password:
            seed_command += ["--admin-password", args.admin_password]
        _run(seed_command, "seed")

    if args.demo:
        _run([python, "-m", "src.core.demo_data"], "demo data")

    print(
        f"\n  {name} rebuilt"
        + (" with baseline data" if args.seed or args.demo else " (schema only)")
        + (" and demonstration data" if args.demo else "")
        + ".\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
