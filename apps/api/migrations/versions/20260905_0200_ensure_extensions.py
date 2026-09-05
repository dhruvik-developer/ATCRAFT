"""Ensure every required extension exists, on existing clusters too

`infrastructure/docker/postgres/init/01-extensions.sql` creates them, but a
`docker-entrypoint-initdb.d` script runs **once**, when the data directory is
first initialised. Any extension added to that file afterwards therefore reaches
new deployments and silently never reaches existing ones - which is what
happened to `pg_stat_statements`: `shared_preload_libraries` loads it and
`pg_stat_statements.track=all` pays to record every statement, while the view
that reads it does not exist, so the cost is incurred and nothing can see the
result. `SELECT * FROM pg_stat_statements` answered "relation does not exist" on
a cluster whose other three extensions were present.

A migration runs on every deployment, new and old, which is the difference that
matters. Every statement is `IF NOT EXISTS`, so this is a no-op where the init
script already did the work.

Creating an extension needs rights the application role may not have on a
managed Postgres. Failing the whole upgrade over an *observability* extension
would be the wrong trade, so each one is attempted separately and a refusal is
reported rather than raised. A missing `pg_trgm` or `btree_gist` would already
have failed the initial schema migration, so the only one that can realistically
be skipped here is the one this migration exists for.

Revision ID: 7f3a91c4e5d2
Revises: 5e2b8c41f7a3
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision: str = "7f3a91c4e5d2"
down_revision: str | None = "5e2b8c41f7a3"
branch_labels: str | None = None
depends_on: str | None = None

#: Alembic configures this logger, so a note here lands in the migration output
#: alongside every other step rather than on a bare stdout.
log = logging.getLogger("alembic.runtime.migration")

#: Every extension the schema or the operator depends on, with why it is here.
EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("pgcrypto", "gen_random_uuid, digest"),
    ("pg_trgm", "trigram search - the reason there is no Elasticsearch"),
    ("btree_gist", "exclusion constraints"),
    ("pg_stat_statements", "slow-query visibility"),
)


def upgrade() -> None:
    connection = op.get_bind()
    for name, purpose in EXTENSIONS:
        try:
            # A separate transaction per extension: on PostgreSQL a failed
            # statement poisons the surrounding one, so without this a single
            # permission refusal would roll back the extensions that did work.
            with connection.begin_nested():
                connection.execute(sa.text(f'CREATE EXTENSION IF NOT EXISTS "{name}"'))
        except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - depends on server rights
            log.warning(
                "could not create extension %r (%s): %s. A superuser can add it later with: "
                'CREATE EXTENSION IF NOT EXISTS "%s";',
                name,
                purpose,
                str(exc).splitlines()[0],
                name,
            )


def downgrade() -> None:
    """Deliberately empty.

    Dropping an extension the schema depends on would break the database, and
    dropping the observability one would discard collected statistics for no
    gain. There is nothing to reverse: the upgrade is idempotent and additive.
    """
