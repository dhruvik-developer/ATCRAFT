"""inventory operations: bin moves, scrap disposals, internal issues, scrap cost pool

Revision ID: 4ba9df3d2137
Revises: 159ffb5b9cb4
Created: 2026-10-12 17:00:00.000000+00:00

Adds the three documents work package E8a introduces, and the two columns they
need on existing tables:

* ``bin_moves`` / ``bin_move_lines`` - putaway between bins of one warehouse;
* ``scrap_disposals`` / ``scrap_disposal_lines`` - stock out of the SCRAP pool;
* ``internal_issues`` / ``internal_issue_lines`` - stock issued to a department
  or cost centre;
* ``inventory_cost_layers.pool`` - which of a position's two FIFO queues a
  layer belongs to: ``ON_HAND`` (every existing layer) or ``SCRAP``;
* ``items.default_location_id`` - the bin a picker pre-fills for an item.

The SCRAP queue is filled for stock already in scrap by the backfill revision
that follows this one; this revision only adds the column.

Review (master rules section 67):

* Locking impact:  the six ``CREATE TABLE`` statements contend with nothing.
                   ``ADD COLUMN pool ... NOT NULL DEFAULT 'ON_HAND'`` is
                   catalogue-only on PostgreSQL 11+ (the constant default is
                   stored, not written) but takes a brief ACCESS EXCLUSIVE lock
                   on ``inventory_cost_layers``. Its CHECK is added ``NOT VALID``
                   and validated separately under SHARE UPDATE EXCLUSIVE, which
                   blocks neither reads nor writes. ``ADD COLUMN
                   default_location_id NULL`` on ``items`` is catalogue-only;
                   its foreign key is added ``NOT VALID`` and validated
                   separately, and every value it checks is NULL.
* Data migration:  none. Every existing cost layer is an on-hand layer, which is
                   what the column default says; no item has a default bin.
* Rollback:        ``downgrade()`` drops the six tables, the item column and
                   the pool column. LOSSY for the documents raised since the
                   upgrade; the ledger rows they posted stay, because the
                   ledger is append-only. It REFUSES while any
                   ``SCRAP_DISPOSAL`` movement is on the ledger: the code below
                   this revision cannot read that transaction type, and the
                   rows cannot be removed. Restore from a backup instead. With
                   none posted, the scrap queue is deleted - nothing consumed
                   from it, so nothing references it - before its discriminator
                   goes, so no scrap layer can be mistaken for an on-hand one.
* Production size: ``inventory_cost_layers`` is the largest table touched, one
                   row per receipt; the column add and the validation scan are
                   seconds. ``items`` is small.
* Index creation:  plain ``CREATE INDEX`` on the six new, empty tables, and one
                   on ``items.default_location_id`` (NULL everywhere). Not
                   ``CONCURRENTLY``: it cannot run inside the migration's
                   transaction, and on these sizes the write pause is
                   negligible.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "4ba9df3d2137"
down_revision: str | None = "159ffb5b9cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _actor_columns() -> list[sa.Column[Any]]:
    """``created_at`` / ``updated_at`` / ``created_by_id`` / ``updated_by_id``."""
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
    ]


def _actor_keys(table: str) -> list[sa.ForeignKeyConstraint]:
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


def _tenant_indexes(table: str) -> None:
    """The two indexes ``TenantMixin`` and ``TimestampMixin`` declare."""
    op.create_index(op.f(f"ix_{table}_company_id"), table, ["company_id"], unique=False)
    op.create_index(op.f(f"ix_{table}_created_at"), table, ["created_at"], unique=False)


def _line_columns(parent: str) -> list[sa.Column[Any]]:
    """The columns every document line shares, after its parent key."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column(parent, sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
    ]


def _line_value_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
    ]


def _line_constraints(table: str) -> list[sa.Constraint]:
    return [
        sa.CheckConstraint("quantity > 0", name=op.f(f"ck_{table}_quantity_positive")),
        sa.CheckConstraint("unit_cost >= 0", name=op.f(f"ck_{table}_unit_cost_non_negative")),
        sa.CheckConstraint("total_cost >= 0", name=op.f(f"ck_{table}_total_cost_non_negative")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f(f"fk_{table}_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f(f"fk_{table}_item_id_items"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    ]


def upgrade() -> None:
    # --- bin moves ---------------------------------------------------------
    op.create_table(
        "bin_moves",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("move_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        *_actor_columns(),
        sa.CheckConstraint(
            "status <> 'POSTED' OR posted_at IS NOT NULL",
            name=op.f("ck_bin_moves_posted_moves_carry_a_time"),
        ),
        sa.CheckConstraint("status IN ('DRAFT', 'POSTED')", name=op.f("ck_bin_moves_status_known")),
        sa.CheckConstraint(
            "total_quantity >= 0", name=op.f("ck_bin_moves_total_quantity_non_negative")
        ),
        sa.CheckConstraint("total_value >= 0", name=op.f("ck_bin_moves_total_value_non_negative")),
        *_actor_keys("bin_moves"),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_bin_moves_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bin_moves")),
        sa.UniqueConstraint("company_id", "code", name="uq_bin_moves_company_id_code"),
    )
    _tenant_indexes("bin_moves")
    op.create_index("ix_bin_moves_date", "bin_moves", ["company_id", "move_date"], unique=False)
    op.create_index(
        "ix_bin_moves_warehouse",
        "bin_moves",
        ["company_id", "warehouse_id", "move_date"],
        unique=False,
    )

    op.create_table(
        "bin_move_lines",
        *_line_columns("bin_move_id"),
        sa.Column("from_location_id", sa.Uuid(), nullable=True),
        sa.Column("to_location_id", sa.Uuid(), nullable=True),
        *_line_value_columns(),
        *_actor_columns(),
        sa.CheckConstraint(
            "from_location_id IS DISTINCT FROM to_location_id",
            name=op.f("ck_bin_move_lines_bins_differ"),
        ),
        *_line_constraints("bin_move_lines"),
        *_actor_keys("bin_move_lines"),
        sa.ForeignKeyConstraint(
            ["bin_move_id"],
            ["bin_moves.id"],
            name=op.f("fk_bin_move_lines_bin_move_id_bin_moves"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"],
            ["storage_locations.id"],
            name=op.f("fk_bin_move_lines_from_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_location_id"],
            ["storage_locations.id"],
            name=op.f("fk_bin_move_lines_to_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "bin_move_id", "line_number", name="uq_bin_move_lines_bin_move_id_line_number"
        ),
    )
    _tenant_indexes("bin_move_lines")
    op.create_index("ix_bin_move_lines_bin_move", "bin_move_lines", ["bin_move_id"], unique=False)
    op.create_index(
        "ix_bin_move_lines_item", "bin_move_lines", ["company_id", "item_id"], unique=False
    )

    # --- scrap disposals ---------------------------------------------------
    op.create_table(
        "scrap_disposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("disposal_date", sa.Date(), nullable=False),
        sa.Column("disposal_kind", sa.String(length=16), nullable=False),
        sa.Column("buyer_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "realised_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        *_actor_columns(),
        sa.CheckConstraint(
            "disposal_kind <> 'WRITE_OFF' OR realised_amount = 0",
            name=op.f("ck_scrap_disposals_write_off_realises_nothing"),
        ),
        sa.CheckConstraint(
            "disposal_kind IN ('SALE', 'WRITE_OFF', 'RECYCLE')",
            name=op.f("ck_scrap_disposals_disposal_kind_known"),
        ),
        sa.CheckConstraint(
            "status <> 'POSTED' OR posted_at IS NOT NULL",
            name=op.f("ck_scrap_disposals_posted_disposals_carry_a_time"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'POSTED')", name=op.f("ck_scrap_disposals_status_known")
        ),
        sa.CheckConstraint(
            "realised_amount >= 0", name=op.f("ck_scrap_disposals_realised_amount_non_negative")
        ),
        sa.CheckConstraint(
            "total_quantity >= 0", name=op.f("ck_scrap_disposals_total_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "total_value >= 0", name=op.f("ck_scrap_disposals_total_value_non_negative")
        ),
        *_actor_keys("scrap_disposals"),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_scrap_disposals_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scrap_disposals")),
        sa.UniqueConstraint("company_id", "code", name="uq_scrap_disposals_company_id_code"),
    )
    _tenant_indexes("scrap_disposals")
    op.create_index(
        "ix_scrap_disposals_date", "scrap_disposals", ["company_id", "disposal_date"], unique=False
    )
    op.create_index(
        "ix_scrap_disposals_warehouse",
        "scrap_disposals",
        ["company_id", "warehouse_id", "disposal_date"],
        unique=False,
    )

    op.create_table(
        "scrap_disposal_lines",
        *_line_columns("scrap_disposal_id"),
        sa.Column("location_id", sa.Uuid(), nullable=True),
        *_line_value_columns(),
        *_actor_columns(),
        *_line_constraints("scrap_disposal_lines"),
        *_actor_keys("scrap_disposal_lines"),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_scrap_disposal_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scrap_disposal_id"],
            ["scrap_disposals.id"],
            name=op.f("fk_scrap_disposal_lines_scrap_disposal_id_scrap_disposals"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "scrap_disposal_id",
            "line_number",
            name="uq_scrap_disposal_lines_scrap_disposal_id_line_number",
        ),
    )
    _tenant_indexes("scrap_disposal_lines")
    op.create_index(
        "ix_scrap_disposal_lines_item",
        "scrap_disposal_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        "ix_scrap_disposal_lines_scrap_disposal",
        "scrap_disposal_lines",
        ["scrap_disposal_id"],
        unique=False,
    )

    # --- internal issues ---------------------------------------------------
    op.create_table(
        "internal_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("cost_centre_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=255), nullable=True),
        *_actor_columns(),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR decision_note IS NOT NULL",
            name=op.f("ck_internal_issues_cancelled_issues_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'POSTED' OR posted_at IS NOT NULL",
            name=op.f("ck_internal_issues_posted_issues_carry_a_time"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'POSTED', 'CANCELLED')",
            name=op.f("ck_internal_issues_status_known"),
        ),
        sa.CheckConstraint(
            "department_id IS NOT NULL OR cost_centre_id IS NOT NULL",
            name=op.f("ck_internal_issues_names_a_recipient"),
        ),
        sa.CheckConstraint(
            "total_quantity >= 0", name=op.f("ck_internal_issues_total_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "total_value >= 0", name=op.f("ck_internal_issues_total_value_non_negative")
        ),
        *_actor_keys("internal_issues"),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_internal_issues_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cost_centre_id"],
            ["cost_centres.id"],
            name=op.f("fk_internal_issues_cost_centre_id_cost_centres"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_internal_issues_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_internal_issues_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_internal_issues")),
        sa.UniqueConstraint("company_id", "code", name="uq_internal_issues_company_id_code"),
    )
    _tenant_indexes("internal_issues")
    op.create_index(
        op.f("ix_internal_issues_cost_centre_id"),
        "internal_issues",
        ["cost_centre_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_internal_issues_department_id"),
        "internal_issues",
        ["department_id"],
        unique=False,
    )
    op.create_index(
        "ix_internal_issues_pending",
        "internal_issues",
        ["company_id", "issue_date"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING_APPROVAL'"),
    )
    op.create_index(
        "ix_internal_issues_status",
        "internal_issues",
        ["company_id", "status", "issue_date"],
        unique=False,
    )
    op.create_index(
        "ix_internal_issues_warehouse",
        "internal_issues",
        ["company_id", "warehouse_id", "issue_date"],
        unique=False,
    )

    op.create_table(
        "internal_issue_lines",
        *_line_columns("internal_issue_id"),
        sa.Column("location_id", sa.Uuid(), nullable=True),
        *_line_value_columns(),
        sa.Column("notes", sa.Text(), nullable=True),
        *_actor_columns(),
        *_line_constraints("internal_issue_lines"),
        *_actor_keys("internal_issue_lines"),
        sa.ForeignKeyConstraint(
            ["internal_issue_id"],
            ["internal_issues.id"],
            name=op.f("fk_internal_issue_lines_internal_issue_id_internal_issues"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_internal_issue_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "internal_issue_id",
            "line_number",
            name="uq_internal_issue_lines_internal_issue_id_line_number",
        ),
    )
    _tenant_indexes("internal_issue_lines")
    op.create_index(
        "ix_internal_issue_lines_internal_issue",
        "internal_issue_lines",
        ["internal_issue_id"],
        unique=False,
    )
    op.create_index(
        "ix_internal_issue_lines_item",
        "internal_issue_lines",
        ["company_id", "item_id"],
        unique=False,
    )

    # --- the scrap cost pool on the FIFO layers ----------------------------
    op.add_column(
        "inventory_cost_layers",
        sa.Column("pool", sa.String(length=16), server_default="ON_HAND", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_inventory_cost_layers_pool_known"),
        "inventory_cost_layers",
        "pool IN ('ON_HAND', 'SCRAP')",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE inventory_cost_layers VALIDATE CONSTRAINT ck_inventory_cost_layers_pool_known"
    )

    # --- the item's default bin --------------------------------------------
    op.add_column("items", sa.Column("default_location_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_items_default_location_id"), "items", ["default_location_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_items_default_location_id_storage_locations"),
        "items",
        "storage_locations",
        ["default_location_id"],
        ["id"],
        ondelete="RESTRICT",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE items VALIDATE CONSTRAINT fk_items_default_location_id_storage_locations"
    )


def downgrade() -> None:
    bind = op.get_bind()
    disposals = bind.execute(
        sa.text(
            "SELECT count(*) FROM inventory_transactions WHERE transaction_type = 'SCRAP_DISPOSAL'"
        )
    ).scalar_one()
    if disposals:
        raise RuntimeError(
            f"Refusing to downgrade below {revision}: {disposals} SCRAP_DISPOSAL movement(s) "
            "are on the stock ledger. The code below this revision cannot read that "
            "transaction type, and the ledger is append-only, so the rows cannot be removed. "
            "Restore from a backup taken before the upgrade instead."
        )

    op.drop_constraint(
        op.f("fk_items_default_location_id_storage_locations"), "items", type_="foreignkey"
    )
    op.drop_index(op.f("ix_items_default_location_id"), table_name="items")
    op.drop_column("items", "default_location_id")

    # No disposal was ever posted, so no consumption row points at a scrap
    # layer. Delete the queue before its discriminator goes, or the code below
    # this revision would read every scrap layer as on-hand stock.
    op.execute("DELETE FROM inventory_cost_layers WHERE pool = 'SCRAP'")
    op.drop_constraint(
        op.f("ck_inventory_cost_layers_pool_known"), "inventory_cost_layers", type_="check"
    )
    op.drop_column("inventory_cost_layers", "pool")

    for table in (
        "internal_issue_lines",
        "internal_issues",
        "scrap_disposal_lines",
        "scrap_disposals",
        "bin_move_lines",
        "bin_moves",
    ):
        op.drop_table(table)
