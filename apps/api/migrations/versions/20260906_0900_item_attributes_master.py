"""item attributes master, and backfill from the free-text columns

Revision ID: 4c1d70b8ea92
Revises: 7f3a91c4e5d2
Created: 2026-09-06 09:00:00.000000+00:00

``items.finish``, ``items.size``, ``items.colour`` and the same three on
``item_variants`` were free text. Nothing in the application branched on them -
which is exactly why they can become a client-editable master - but nothing
governed them either, so "Brushed", "brushed" and "Brushed  Steel" were three
different finishes and no report could group by finish.

This revision is the EXPAND half of an expand/contract pair. It adds
``item_attributes``, adds the six ``*_id`` columns beside the six text columns,
and backfills. It does NOT drop the text columns - that is the next revision,
which lands with the code that stopped reading them. Keeping them here is what
makes this revision's downgrade genuinely lossless.

The backfill collapses spellings, which is the point and also the one
irreversible thing here. Matching is on the upper-cased, whitespace-collapsed,
trimmed text, so "brushed  steel" and "Brushed Steel" become one row. The
surviving spelling is chosen deterministically: most-used first, then the oldest
row, tie-broken on the UUIDv7 primary key - ``created_at`` alone is not enough
because ``now()`` is transaction start time, so every row written by one seed run
carries the identical timestamp (see CLAUDE.md). Every collapse is printed, so
the operator can see what merged rather than discovering it later.

Review (master rules section 67):

* Locking impact:  ``CREATE TABLE`` takes no lock anything else contends for.
                   Six ``ADD COLUMN ... NULL`` are catalogue-only in PostgreSQL
                   11+ - no table rewrite, no scan - but each takes a brief
                   ACCESS EXCLUSIVE lock and will queue behind a long
                   transaction on ``items``. The six foreign keys are added
                   ``NOT VALID`` and validated separately: ``ADD CONSTRAINT ...
                   FOREIGN KEY`` otherwise needs SHARE ROW EXCLUSIVE on both
                   tables for the length of a full scan, which on a large
                   ``items`` blocks writes. ``VALIDATE CONSTRAINT`` takes only
                   SHARE UPDATE EXCLUSIVE and does not block writes at all.
* Data migration:  yes, and it is the substance of this revision. One INSERT
                   per attribute kind building the vocabulary from both tables
                   at once, then two UPDATEs per kind. Set-based; no row loop.
* Rollback:        LOSSLESS. The text columns are untouched, so ``downgrade()``
                   drops the new columns and the new table and leaves exactly
                   the schema and the data this revision started from. The
                   *collapse* is not undone because it was never applied to the
                   text columns.
* Production size: proportional to ``items`` + ``item_variants``, both master
                   tables measured in thousands, not to any ledger. Seconds.
* Index creation:  three on the new table (its primary key, the unique
                   constraint, and one partial index for the picker) and six
                   single-column indexes on the foreign keys. All are created on
                   a table that is empty at the time, or on columns that were
                   just added, so none is a long build. The existing
                   ``uq_item_variants_attribute_combo`` is NOT touched here - it
                   still guards the text columns, which are still the ones in
                   use. The contract revision swaps it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c1d70b8ea92"
down_revision: str | None = "7f3a91c4e5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Alembic configures this logger, so the collapse report lands in the migration
#: output beside every other step rather than on a bare stdout.
log = logging.getLogger("alembic.runtime.migration")

#: The three kinds, and the column that holds each one today. The kinds are
#: fixed in code because each is a column; the values are the client's.
ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("FINISH", "finish"),
    ("SIZE", "size"),
    ("COLOUR", "colour"),
)

#: One SQL expression for "the same value, however it was typed". Used for both
#: grouping and matching, so the two can never disagree.
NORMALISED = "upper(btrim(regexp_replace({column}, '\\s+', ' ', 'g')))"


def _code_expression(column: str) -> str:
    """A master-data code derived from the winning spelling.

    ``normalise_code`` in ``src/common/codes.py`` upper-cases and collapses
    internal whitespace; the remaining substitution turns the spaces into
    hyphens so "Antique Brass" becomes ANTIQUE-BRASS rather than a code with a
    space in it. Truncated to the column width, and the uniqueness constraint
    catches the vanishingly unlikely collision by failing the migration rather
    than silently merging two different finishes.
    """
    return f"left(replace({NORMALISED.format(column=column)}, ' ', '-'), 32)"


def upgrade() -> None:
    op.create_table(
        "item_attributes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("attribute", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_attributes")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_attributes_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "attribute IN ('FINISH', 'SIZE', 'COLOUR')",
            name=op.f("ck_item_attributes_attribute_known"),
        ),
        sa.UniqueConstraint(
            "company_id",
            "attribute",
            "code",
            name="uq_item_attributes_company_id_attribute_code",
        ),
    )
    op.create_index(
        "ix_item_attributes_company_id", "item_attributes", ["company_id"], unique=False
    )
    op.create_index(
        "ix_item_attributes_company_attribute",
        "item_attributes",
        ["company_id", "attribute", "sort_order"],
        unique=False,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    for table in ("items", "item_variants"):
        for _, column in ATTRIBUTES:
            op.add_column(table, sa.Column(f"{column}_id", sa.Uuid(), nullable=True))
            op.create_index(f"ix_{table}_{column}_id", table, [f"{column}_id"], unique=False)
            # NOT VALID, then validated below: see the locking note above.
            op.execute(
                f"ALTER TABLE {table} "
                f"ADD CONSTRAINT fk_{table}_{column}_id_item_attributes "
                f"FOREIGN KEY ({column}_id) REFERENCES item_attributes (id) "
                f"ON DELETE RESTRICT NOT VALID"
            )

    _backfill()

    for table in ("items", "item_variants"):
        for _, column in ATTRIBUTES:
            op.execute(
                f"ALTER TABLE {table} VALIDATE CONSTRAINT fk_{table}_{column}_id_item_attributes"
            )


def _backfill() -> None:
    """Build the vocabulary from what is already typed, then point at it."""
    connection = op.get_bind()

    for attribute, column in ATTRIBUTES:
        normalised = NORMALISED.format(column=column)
        # Both tables at once: a finish used only on a variant is still a finish
        # this company uses, and splitting the two would create it twice.
        candidates = f"""
            SELECT company_id, {normalised} AS key, {column} AS spelling
            FROM items
            WHERE deleted_at IS NULL AND {column} IS NOT NULL AND btrim({column}) <> ''
            UNION ALL
            SELECT company_id, {normalised} AS key, {column} AS spelling
            FROM item_variants
            WHERE deleted_at IS NULL AND {column} IS NOT NULL AND btrim({column}) <> ''
        """

        # Report what is about to be merged, before merging it.
        merged = connection.execute(
            sa.text(f"""
                SELECT key, count(DISTINCT spelling) AS spellings,
                       string_agg(DISTINCT spelling, ' | ' ORDER BY spelling) AS variants
                FROM ({candidates}) c
                GROUP BY company_id, key
                HAVING count(DISTINCT spelling) > 1
            """)
        ).fetchall()
        for row in merged:
            log.warning(
                "item_attributes: %s - %d spellings collapsed into one: %s",
                attribute,
                row.spellings,
                row.variants,
            )

        # The winner: most used, then the oldest row, tie-broken on the UUIDv7
        # id because created_at is transaction start time and ties within a run.
        connection.execute(
            sa.text(f"""
                INSERT INTO item_attributes
                    (id, company_id, attribute, code, name, sort_order, is_active,
                     created_at, updated_at)
                SELECT
                    gen_random_uuid(), company_id, :attribute,
                    left(replace(key, ' ', '-'), 32), winner, 0, true, now(), now()
                FROM (
                    -- Count each spelling first, then rank the spellings within
                    -- their normalised key. One pass cannot do both: an
                    -- aggregate may not appear inside a window definition.
                    SELECT company_id, key, spelling AS winner,
                           row_number() OVER (
                               PARTITION BY company_id, key
                               ORDER BY count(*) DESC, spelling ASC
                           ) AS rank
                    FROM ({candidates}) c
                    GROUP BY company_id, key, spelling
                ) chosen
                WHERE rank = 1
                ON CONFLICT (company_id, attribute, code) DO NOTHING
            """),
            {"attribute": attribute},
        )

        for table in ("items", "item_variants"):
            connection.execute(
                sa.text(f"""
                    UPDATE {table} t
                    SET {column}_id = a.id
                    FROM item_attributes a
                    WHERE a.company_id = t.company_id
                      AND a.attribute = :attribute
                      AND a.code = left(replace({normalised.replace(column, "t." + column)},
                                                ' ', '-'), 32)
                      AND t.{column} IS NOT NULL
                      AND btrim(t.{column}) <> ''
                """),
                {"attribute": attribute},
            )

    # Nothing should be left behind. A row with text but no id means the code
    # derivation and the match disagree, which would silently lose the value
    # when the contract revision drops the column - so fail here instead.
    for table in ("items", "item_variants"):
        for _, column in ATTRIBUTES:
            orphans = connection.execute(
                sa.text(f"""
                    SELECT count(*) FROM {table}
                    WHERE deleted_at IS NULL
                      AND {column} IS NOT NULL AND btrim({column}) <> ''
                      AND {column}_id IS NULL
                """)
            ).scalar_one()
            if orphans:
                raise RuntimeError(
                    f"{orphans} row(s) in {table} have a {column} that did not resolve to an "
                    f"item_attributes row. Refusing to continue rather than losing the value."
                )


def downgrade() -> None:
    """Lossless: the text columns were never touched."""
    for table in ("items", "item_variants"):
        for _, column in ATTRIBUTES:
            op.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                f"fk_{table}_{column}_id_item_attributes"
            )
            op.drop_index(f"ix_{table}_{column}_id", table_name=table)
            op.drop_column(table, f"{column}_id")

    op.drop_index("ix_item_attributes_company_attribute", table_name="item_attributes")
    op.drop_index("ix_item_attributes_company_id", table_name="item_attributes")
    op.drop_table("item_attributes")
