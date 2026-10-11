"""cost centres: the master table, and the carriers that name one

Revision ID: 3746fadd0b86
Revises: 2f5e08c7ba61
Created: 2026-10-11 12:00:00.000000+00:00

Adds ``cost_centres`` (organisation) and a nullable ``cost_centre_id`` on the
eight tables that carry one in this release:

* masters, where it is a default or a cost pool: ``departments``,
  ``work_centers``;
* documents, where it is the allocation: ``production_orders``,
  ``job_work_orders``, ``stock_adjustments``, ``vendor_invoices``,
  ``vendor_invoice_lines``;
* ``ledger_entries``, the analytic source of truth the cost-centre statement
  reads, with a partial index over the legs that carry a centre.

Every carrier is NULL for every existing row - NULL means "unallocated" - so
there is no backfill: nothing in the data says which centre an old document
belonged to, and inventing one would put fiction on a cost-centre statement.

``cost_centres`` is created WITH its two actor foreign keys
(``created_by_id`` / ``updated_by_id`` -> ``users``), which the
``item_attributes`` table in ``4c1d70b8ea92`` was created without; the metadata
declares them through ``ActorMixin`` and a copied ``create_table`` would have
repeated the drift. The ``created_at`` and ``deleted_at`` indexes the mixins
declare are created too, for the same reason.

Review (master rules section 67):

* Locking impact:  ``CREATE TABLE`` contends with nothing. Each ``ADD COLUMN
                   ... NULL`` is catalogue-only in PostgreSQL 11+ (no rewrite,
                   no scan) but takes a brief ACCESS EXCLUSIVE lock and queues
                   behind a long transaction on its table. The eight foreign
                   keys are added ``NOT VALID`` and validated separately:
                   ``VALIDATE CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE, which
                   blocks neither reads nor writes, and every value it checks is
                   NULL. Each new index is a plain ``CREATE INDEX``, which holds
                   a SHARE lock (reads continue, writes wait) while it scans its
                   table - see "Index creation".
* Data migration:  none. Every existing row is unallocated by definition.
* Rollback:        ``downgrade()`` drops the eight columns, their indexes and
                   foreign keys, then the table. LOSSY for data written after
                   the upgrade: the cost centres themselves, every assignment
                   on a document, and the cost-centre tag on posted ledger legs
                   (the debit and credit amounts and balances are untouched -
                   only the analytic tag goes).
* Production size: ``ledger_entries`` and ``vendor_invoice_lines`` are the
                   largest tables touched, in the hundreds of thousands of rows
                   for a plant of this size. Seconds.
* Index creation:  eight single-column indexes on the new carrier columns and a
                   partial ``(company_id, cost_centre_id, entry_date) WHERE
                   cost_centre_id IS NOT NULL`` on ``ledger_entries``. All index
                   a column that is NULL in every existing row, so the partial
                   one is empty and the others are trivial to sort; the build
                   is one scan of each table. Not ``CONCURRENTLY``: that cannot
                   run inside the migration's transaction, and at these sizes
                   a write pause of seconds is a better trade than a revision
                   that can half-apply.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3746fadd0b86"
down_revision: str | None = "2f5e08c7ba61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables that gain a plain, single-column-indexed ``cost_centre_id``.
#: ``ledger_entries`` is handled separately: its index is composite and partial.
CARRIERS: tuple[str, ...] = (
    "departments",
    "work_centers",
    "production_orders",
    "job_work_orders",
    "stock_adjustments",
    "vendor_invoices",
    "vendor_invoice_lines",
)

LEDGER = "ledger_entries"
LEDGER_INDEX = "ix_ledger_entries_cost_centre"


def _fk_name(table: str) -> str:
    return f"fk_{table}_cost_centre_id_cost_centres"


def upgrade() -> None:
    op.create_table(
        "cost_centres",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("cost_centre_type", sa.String(length=24), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=True),
        sa.Column("head_user_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(
            "cost_centre_type IN ('PRODUCTION', 'SERVICE', 'ADMINISTRATION', 'SELLING')",
            name=op.f("ck_cost_centres_cost_centre_type_known"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name=op.f("ck_cost_centres_parent_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_cost_centres_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["cost_centres.id"],
            name=op.f("fk_cost_centres_parent_id_cost_centres"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name=op.f("fk_cost_centres_plant_id_plants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["head_user_id"],
            ["users.id"],
            name=op.f("fk_cost_centres_head_user_id_users"),
            ondelete="SET NULL",
        ),
        # The actor keys 4c1d70b8ea92 forgot for item_attributes - see the
        # module docstring.
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_cost_centres_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_cost_centres_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_centres")),
        sa.UniqueConstraint("company_id", "code", name="uq_cost_centres_company_id_code"),
    )
    op.create_index("ix_cost_centres_company_id", "cost_centres", ["company_id"], unique=False)
    op.create_index("ix_cost_centres_created_at", "cost_centres", ["created_at"], unique=False)
    op.create_index("ix_cost_centres_deleted_at", "cost_centres", ["deleted_at"], unique=False)
    op.create_index("ix_cost_centres_parent_id", "cost_centres", ["parent_id"], unique=False)
    op.create_index("ix_cost_centres_plant_id", "cost_centres", ["plant_id"], unique=False)
    op.create_index(
        "ix_cost_centres_company_active",
        "cost_centres",
        ["company_id", "is_active"],
        unique=False,
    )

    for table in (*CARRIERS, LEDGER):
        op.add_column(table, sa.Column("cost_centre_id", sa.Uuid(), nullable=True))
        # NOT VALID, then validated below: see the locking note above.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {_fk_name(table)} "
            "FOREIGN KEY (cost_centre_id) REFERENCES cost_centres (id) "
            "ON DELETE RESTRICT NOT VALID"
        )

    for table in CARRIERS:
        op.create_index(f"ix_{table}_cost_centre_id", table, ["cost_centre_id"], unique=False)
    op.create_index(
        LEDGER_INDEX,
        LEDGER,
        ["company_id", "cost_centre_id", "entry_date"],
        unique=False,
        postgresql_where=sa.text("cost_centre_id IS NOT NULL"),
    )

    for table in (*CARRIERS, LEDGER):
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {_fk_name(table)}")


def downgrade() -> None:
    """Lossy for post-upgrade data - see the module docstring."""
    op.drop_index(LEDGER_INDEX, table_name=LEDGER)
    for table in CARRIERS:
        op.drop_index(f"ix_{table}_cost_centre_id", table_name=table)
    for table in (*CARRIERS, LEDGER):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {_fk_name(table)}")
        op.drop_column(table, "cost_centre_id")

    op.drop_index("ix_cost_centres_company_active", table_name="cost_centres")
    op.drop_index("ix_cost_centres_plant_id", table_name="cost_centres")
    op.drop_index("ix_cost_centres_parent_id", table_name="cost_centres")
    op.drop_index("ix_cost_centres_deleted_at", table_name="cost_centres")
    op.drop_index("ix_cost_centres_created_at", table_name="cost_centres")
    op.drop_index("ix_cost_centres_company_id", table_name="cost_centres")
    op.drop_table("cost_centres")
