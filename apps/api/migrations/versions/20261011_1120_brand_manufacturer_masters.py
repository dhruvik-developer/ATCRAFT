"""brand and manufacturer masters, and where items and vendor links name them

Revision ID: 22a566492b2c
Revises: 2f5e08c7ba61
Created: 2026-10-11 11:20:00.000000+00:00

The proposal lists "category, brand, manufacturer & specification masters" as
modules of the existing system, and neither a brand nor a manufacturer existed
anywhere - no table, no column, not even a free-text field. This adds both as
client-maintained master tables in ``products``, and the columns that point at
them:

* ``manufacturers`` - who makes a part. Deliberately not a vendor: a maker's
  cylinder is routinely bought through a distributor, and a vendor carries
  payables, approval and a portal login that a maker does not.
* ``brands`` - the name a product is sold under, optionally owned by one
  manufacturer.
* ``items.brand_id``, ``items.manufacturer_id``, ``items.manufacturer_part_number``.
* ``item_vendor_links.manufacturer_id`` and ``.manufacturer_part_number`` - which
  maker's part a particular source supplies.

Nothing in the application may branch on either value (``docs/database.md``
section 3, "Brands and manufacturers").

Unlike ``4c1d70b8ea92`` (the item attributes), the two new tables are created
with the ``users`` foreign keys ``ActorMixin`` declares on ``created_by_id`` and
``updated_by_id``, and with the ``created_at`` / ``deleted_at`` indexes the
mixins declare. That revision omitted them, which is drift between its table and
the model; it is left alone here, because fixing it is a separate change with
its own review, but it is not repeated.

Review (master rules section 67):

* Locking impact:  the two ``CREATE TABLE`` statements take no lock anything else
                   contends for. The five ``ADD COLUMN ... NULL`` on ``items``
                   and ``item_vendor_links`` are catalogue-only in PostgreSQL 11+
                   - no rewrite, no scan - but each takes a brief ACCESS
                   EXCLUSIVE lock and will queue behind a long transaction on
                   its table. The three foreign keys on those existing tables
                   are added ``NOT VALID`` and validated separately:
                   ``ADD CONSTRAINT ... FOREIGN KEY`` otherwise holds SHARE ROW
                   EXCLUSIVE on both tables for a full scan, while ``VALIDATE
                   CONSTRAINT`` takes only SHARE UPDATE EXCLUSIVE and does not
                   block writes. (Every new column is NULL here, so validation
                   has nothing to check; the split is about the lock, not the
                   data.)
* Data migration:  none. Every new column starts NULL - "no brand recorded" is
                   the truthful state of every existing item, and inventing one
                   would be a guess presented as a fact. The demo loader assigns
                   brands to its own items; a client assigns theirs.
* Rollback:        LOSSLESS for the schema this revision started from.
                   ``downgrade()`` drops the five columns and the two tables, and
                   with them any brand or manufacturer data entered since - that
                   loss is inherent to removing the feature and is stated rather
                   than hidden. The grant migration that follows must be
                   downgraded first (Alembic's order does that).
* Production size: two empty tables; five NULL columns on ``items`` (thousands of
                   rows) and ``item_vendor_links`` (fewer). Milliseconds, plus
                   the index builds below.
* Index creation:  on the new tables, the primary keys, the two unique
                   constraints, one partial picker index each, and the
                   ``company_id`` / ``created_at`` / ``deleted_at`` /
                   ``manufacturer_id`` indexes the models declare - all on empty
                   tables. On ``items``: ``ix_items_brand_id``,
                   ``ix_items_manufacturer_id`` and the partial
                   ``ix_items_company_brand``; on ``item_vendor_links``:
                   ``ix_item_vendor_links_manufacturer_id``. These are built
                   inside the migration transaction rather than CONCURRENTLY, as
                   ``4c1d70b8ea92`` built its six: the columns were added a
                   statement earlier and hold only NULLs, and ``items`` is a
                   master table measured in thousands of rows, so each build is
                   a short scan rather than a long write block.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "22a566492b2c"
down_revision: str | None = "2f5e08c7ba61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(table, column, referred table)`` for the foreign keys added to tables that
#: already hold rows. Each is added NOT VALID and then validated - see the
#: locking note above.
EXISTING_TABLE_FKS: tuple[tuple[str, str, str], ...] = (
    ("items", "brand_id", "brands"),
    ("items", "manufacturer_id", "manufacturers"),
    ("item_vendor_links", "manufacturer_id", "manufacturers"),
)


def _actor_columns() -> list[sa.Column]:
    """The timestamp, soft-delete and actor columns every master table carries."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
    ]


def _actor_foreign_keys(table: str) -> list[sa.ForeignKeyConstraint]:
    """``company_id`` RESTRICT and the two ``users`` SET NULL keys ActorMixin declares."""
    return [
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f(f"fk_{table}_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_updated_by_id_users"),
            ondelete="SET NULL",
        ),
    ]


def _mixin_indexes(table: str) -> None:
    """The indexes ``TenantMixin``, ``TimestampMixin`` and ``SoftDeleteMixin`` declare."""
    for column in ("company_id", "created_at", "deleted_at"):
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)


def upgrade() -> None:
    op.create_table(
        "manufacturers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=80), server_default="India", nullable=False),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_actor_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manufacturers")),
        *_actor_foreign_keys("manufacturers"),
        sa.UniqueConstraint("company_id", "code", name="uq_manufacturers_company_id_code"),
    )
    _mixin_indexes("manufacturers")
    op.create_index(
        "ix_manufacturers_company_active",
        "manufacturers",
        ["company_id", "name"],
        unique=False,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    op.create_table(
        "brands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_actor_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brands")),
        *_actor_foreign_keys("brands"),
        sa.ForeignKeyConstraint(
            ["manufacturer_id"],
            ["manufacturers.id"],
            name=op.f("fk_brands_manufacturer_id_manufacturers"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sort_order >= 0 AND sort_order <= 9999",
            name=op.f("ck_brands_sort_order_bounded"),
        ),
        sa.UniqueConstraint("company_id", "code", name="uq_brands_company_id_code"),
    )
    _mixin_indexes("brands")
    op.create_index(op.f("ix_brands_manufacturer_id"), "brands", ["manufacturer_id"], unique=False)
    op.create_index(
        "ix_brands_company_active",
        "brands",
        ["company_id", "sort_order"],
        unique=False,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    # --- the references, on tables that already hold rows ---------------------
    op.add_column("items", sa.Column("brand_id", sa.Uuid(), nullable=True))
    op.add_column("items", sa.Column("manufacturer_id", sa.Uuid(), nullable=True))
    op.add_column(
        "items", sa.Column("manufacturer_part_number", sa.String(length=64), nullable=True)
    )
    op.add_column("item_vendor_links", sa.Column("manufacturer_id", sa.Uuid(), nullable=True))
    op.add_column(
        "item_vendor_links",
        sa.Column("manufacturer_part_number", sa.String(length=64), nullable=True),
    )

    op.create_index(op.f("ix_items_brand_id"), "items", ["brand_id"], unique=False)
    op.create_index(op.f("ix_items_manufacturer_id"), "items", ["manufacturer_id"], unique=False)
    op.create_index(
        "ix_items_company_brand",
        "items",
        ["company_id", "brand_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        op.f("ix_item_vendor_links_manufacturer_id"),
        "item_vendor_links",
        ["manufacturer_id"],
        unique=False,
    )

    for table, column, referred in EXISTING_TABLE_FKS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT fk_{table}_{column}_{referred} "
            f"FOREIGN KEY ({column}) REFERENCES {referred} (id) "
            f"ON DELETE RESTRICT NOT VALID"
        )
    for table, column, referred in EXISTING_TABLE_FKS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT fk_{table}_{column}_{referred}")


def downgrade() -> None:
    """Drops the feature: the five columns, then the two tables, in FK order."""
    for table, column, referred in reversed(EXISTING_TABLE_FKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_{column}_{referred}")

    op.drop_index(op.f("ix_item_vendor_links_manufacturer_id"), table_name="item_vendor_links")
    op.drop_index("ix_items_company_brand", table_name="items")
    op.drop_index(op.f("ix_items_manufacturer_id"), table_name="items")
    op.drop_index(op.f("ix_items_brand_id"), table_name="items")

    op.drop_column("item_vendor_links", "manufacturer_part_number")
    op.drop_column("item_vendor_links", "manufacturer_id")
    op.drop_column("items", "manufacturer_part_number")
    op.drop_column("items", "manufacturer_id")
    op.drop_column("items", "brand_id")

    op.drop_index("ix_brands_company_active", table_name="brands")
    op.drop_index(op.f("ix_brands_manufacturer_id"), table_name="brands")
    for column in ("deleted_at", "created_at", "company_id"):
        op.drop_index(op.f(f"ix_brands_{column}"), table_name="brands")
    op.drop_table("brands")

    op.drop_index("ix_manufacturers_company_active", table_name="manufacturers")
    for column in ("deleted_at", "created_at", "company_id"):
        op.drop_index(op.f(f"ix_manufacturers_{column}"), table_name="manufacturers")
    op.drop_table("manufacturers")
