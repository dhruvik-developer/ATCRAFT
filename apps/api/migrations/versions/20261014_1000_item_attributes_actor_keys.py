"""item_attributes: the actor foreign keys and timestamp indexes 4c1d70b8ea92 forgot

Revision ID: d4e10a01aa01
Revises: 7f3070625160
Created: 2026-10-14 10:00:00.000000+00:00

Migration ``4c1d70b8ea92`` built ``item_attributes`` with ``created_by_id`` and
``updated_by_id`` columns but no foreign keys to ``users``, and with no
``ix_item_attributes_created_at`` / ``ix_item_attributes_deleted_at`` indexes -
every other master table carries all four (see e.g. ``cost_centres`` in
``20261011_1200``). A metadata compare flags exactly those four objects, so
this revision adds them and nothing else.

The foreign keys are added ``NOT VALID`` and validated separately, after
nulling any actor id that matches no user. A dangling actor id can exist: the
columns were writable for a month with no constraint, and the seeder stamps
them from whatever administrator ran it. Nulling is logged, and it loses
nothing the application reads - the actor columns are provenance, never
authority.

Review (master rules section 67):

* Locking impact:  two ``CREATE INDEX`` (one concurrent-safe build each on a
                    master table measured in hundreds of rows) and two
                    ``ADD CONSTRAINT ... NOT VALID`` (a brief lock, no scan).
                    ``VALIDATE CONSTRAINT`` takes only SHARE UPDATE EXCLUSIVE
                    and does not block writes.
* Data migration:  yes - nulls actor ids with no matching user, counted and
                    logged per column. Re-running changes nothing.
* Rollback:        drops the two constraints and the two indexes. The nulled
                    actor ids are not restored: they matched no user, so there
                    is nothing to restore them to.
* Production size: proportional to ``item_attributes``. Milliseconds.
* Index creation:  the two named below, on an unindexed master table.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e10a01aa01"
down_revision: str | None = "7f3070625160"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Alembic configures this logger, so the null report lands in the migration
#: output beside every other step rather than on a bare stdout.
log = logging.getLogger("alembic.runtime.migration")

#: The two actor columns, and the constraint each one gains.
ACTOR_KEYS: tuple[tuple[str, str], ...] = (
    ("created_by_id", "fk_item_attributes_created_by_id_users"),
    ("updated_by_id", "fk_item_attributes_updated_by_id_users"),
)


def upgrade() -> None:
    connection = op.get_bind()

    for column, _ in ACTOR_KEYS:
        dangling = connection.execute(
            sa.text(
                "SELECT count(*) FROM item_attributes "
                f"WHERE {column} IS NOT NULL AND {column} NOT IN (SELECT id FROM users)"
            )
        ).scalar_one()
        if dangling:
            log.warning(
                "item_attributes: %s row(s) carry a %s that matches no user; "
                "nulling before the foreign key is added",
                dangling,
                column,
            )
            connection.execute(
                sa.text(
                    "UPDATE item_attributes SET "
                    f"{column} = NULL WHERE {column} IS NOT NULL "
                    f"AND {column} NOT IN (SELECT id FROM users)"
                )
            )

    for column, constraint in ACTOR_KEYS:
        # NOT VALID, then validated below: the ADD takes a brief lock and no
        # scan, and VALIDATE does not block writes.
        op.execute(
            f"ALTER TABLE item_attributes ADD CONSTRAINT {constraint} "
            f"FOREIGN KEY ({column}) REFERENCES users (id) "
            f"ON DELETE SET NULL NOT VALID"
        )

    op.create_index(
        "ix_item_attributes_created_at", "item_attributes", ["created_at"], unique=False
    )
    op.create_index(
        "ix_item_attributes_deleted_at", "item_attributes", ["deleted_at"], unique=False
    )

    for _, constraint in ACTOR_KEYS:
        op.execute(f"ALTER TABLE item_attributes VALIDATE CONSTRAINT {constraint}")


def downgrade() -> None:
    for _, constraint in reversed(ACTOR_KEYS):
        op.execute(f"ALTER TABLE item_attributes DROP CONSTRAINT IF EXISTS {constraint}")
    op.drop_index("ix_item_attributes_deleted_at", table_name="item_attributes")
    op.drop_index("ix_item_attributes_created_at", table_name="item_attributes")
