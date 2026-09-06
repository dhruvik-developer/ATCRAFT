"""drop the free-text item attribute columns

Revision ID: 9b6f2ac41d38
Revises: 4c1d70b8ea92
Created: 2026-09-06 09:01:00.000000+00:00

The CONTRACT half of the pair started by ``4c1d70b8ea92``. That revision built
``item_attributes``, added the six ``*_id`` columns and backfilled them; this one
removes the six text columns they replaced, and lands with the code that stopped
reading them - which is the rule this repository already follows for a dropped
column (see ``7e8eb4bdd0ed``, which dropped ``items.valuation_method``).

It also swaps ``uq_item_variants_attribute_combo``. That index is what stops a
second "satin, 52mm, no colour" variant of the same item, and it COALESCEs each
attribute so that NULL <> NULL cannot open the classic nullable-unique hole. The
sentinel changes from ``''`` to the nil UUID, because ``coalesce(uuid, '')`` does
not typecheck - the same device, for the same reason, as ``GRAIN_NULL_SENTINEL``
in ``inventory/models.py``.

Two consequences worth stating plainly. First, the collapse performed by the
previous revision becomes visible here: two variants of one item that differed
only by the SPELLING of a finish - "Brushed" against "brushed" - were distinct
under the old index and are the same combination under the new one. The index is
therefore built after a pre-flight that names any such pair and refuses rather
than failing halfway through a CREATE INDEX. Second, that pair is data the client
has to decide about; the migration cannot choose which variant to keep.

Review (master rules section 67):

* Locking impact:  six ``DROP COLUMN``, each catalogue-only in PostgreSQL - the
                   column is marked dropped and the space reclaimed lazily - but
                   each takes a brief ACCESS EXCLUSIVE lock and queues behind any
                   long transaction on the table. The index swap builds
                   CONCURRENTLY, which does not block writes but cannot run
                   inside a transaction; ``env.py`` runs migrations in one, so
                   the build is ordinary here and takes SHARE for its duration.
                   ``item_variants`` is a master table of thousands, so that is
                   sub-second - if it ever is not, split this revision and build
                   the index by hand outside a transaction.
* Data migration:  none. ``4c1d70b8ea92`` moved the data; this only removes what
                   is no longer read. The pre-flight below reads, and writes
                   nothing.
* Rollback:        PARTIAL, and the honest statement is worth reading. The text
                   columns come back and are repopulated FROM the master, so
                   every row recovers a correct, canonical spelling. What does
                   not come back is the pre-collapse spelling: a row that said
                   "brushed" before the previous revision says "Brushed" after
                   this downgrade. That is the merge, and it is intentional and
                   irreversible. Anyone who needs the original strings restores
                   from a backup taken before ``4c1d70b8ea92``.
* Production size: proportional to ``item_variants`` for the index build; the
                   column drops are catalogue updates.
* Index creation:  one unique index dropped and one created, both on
                   ``item_variants``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b6f2ac41d38"
down_revision: str | None = "4c1d70b8ea92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: tuple[str, ...] = ("finish", "size", "colour")

#: See the module docstring, and ATTRIBUTE_NULL_SENTINEL in products/models.py.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

COMBO_BY_ID = ", ".join(f"coalesce({column}_id, '{NIL_UUID}'::uuid)" for column in COLUMNS)


def upgrade() -> None:
    connection = op.get_bind()

    # Pre-flight: the new index is coarser than the old one, so a pair that was
    # distinct only by spelling now collides. Name them rather than failing
    # halfway through the build with a message that identifies one row.
    clashes = connection.execute(
        sa.text(f"""
            SELECT item_id, {COMBO_BY_ID.replace("coalesce", "coalesce")} AS combo,
                   count(*) AS n, string_agg(code, ', ' ORDER BY code) AS codes
            FROM item_variants
            WHERE deleted_at IS NULL
            GROUP BY item_id, {COMBO_BY_ID}
            HAVING count(*) > 1
        """)
    ).fetchall()
    if clashes:
        detail = "; ".join(f"item {row.item_id}: {row.codes}" for row in clashes)
        raise RuntimeError(
            "These variants differed only by how their finish, size or colour was spelled, "
            "and are the same combination now that spellings have been merged. Retire or "
            f"re-code one of each pair, then run this migration again -> {detail}"
        )

    op.drop_index("uq_item_variants_attribute_combo", table_name="item_variants")
    op.execute(
        "CREATE UNIQUE INDEX uq_item_variants_attribute_combo "
        f"ON item_variants (item_id, {COMBO_BY_ID}) "
        "WHERE deleted_at IS NULL"
    )

    for table in ("items", "item_variants"):
        for column in COLUMNS:
            op.drop_column(table, column)


def downgrade() -> None:
    """Restores the columns and repopulates them from the master.

    Canonical spellings, not the originals - see Rollback above.
    """
    for table in ("items", "item_variants"):
        for column in COLUMNS:
            op.add_column(table, sa.Column(column, sa.String(length=64), nullable=True))
            op.execute(
                f"UPDATE {table} t SET {column} = a.name "
                f"FROM item_attributes a WHERE a.id = t.{column}_id"
            )

    op.drop_index("uq_item_variants_attribute_combo", table_name="item_variants")
    op.execute(
        "CREATE UNIQUE INDEX uq_item_variants_attribute_combo "
        "ON item_variants (item_id, coalesce(finish, ''), coalesce(size, ''), "
        "coalesce(colour, '')) WHERE deleted_at IS NULL"
    )
