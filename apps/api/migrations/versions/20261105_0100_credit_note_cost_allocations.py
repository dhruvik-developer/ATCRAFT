"""Freeze returned FIFO cost allocations on issued customer-return credits.

Revision ID: 20261105_0100
Revises: 20261104_0100

Only ordinary receipt anchors with a one-to-one legacy match are backfilled.
Ambiguous receipts and historic credits stay unknown; no cost is invented.
Writers should be drained for the bounded-lock schema/backfill operation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261105_0100"
down_revision = "20261104_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "sales_return_lines", sa.Column("receipt_transaction_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_sales_return_lines_receipt_transaction",
        "sales_return_lines",
        "inventory_transactions",
        ["receipt_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_sales_return_lines_receipt",
        "sales_return_lines",
        ["company_id", "receipt_transaction_id"],
        unique=True,
    )
    op.execute(
        """
        WITH candidates AS (
            SELECT line.id AS line_id, movement.id AS transaction_id,
                   count(*) OVER (PARTITION BY line.id) AS line_matches,
                   count(*) OVER (PARTITION BY movement.id) AS movement_matches
            FROM sales_return_lines AS line
            JOIN sales_returns AS returned
              ON returned.id = line.sales_return_id AND returned.company_id = line.company_id
            JOIN dispatch_lines AS dispatched
              ON dispatched.id = line.dispatch_line_id AND dispatched.company_id = line.company_id
            JOIN inventory_transactions AS movement
              ON movement.company_id = line.company_id
             AND movement.reference_type = 'sales_return'
             AND movement.reference_id = returned.id
             AND movement.transaction_type = 'CUSTOMER_RETURN'
             AND movement.item_id = line.item_id
             AND movement.batch_id IS NOT DISTINCT FROM dispatched.batch_id
             AND movement.quantity = line.quantity
             AND movement.serial_id IS NULL
            WHERE line.receipt_transaction_id IS NULL
              AND cardinality(line.serial_ids) = 0
              AND returned.status IN ('RECEIVED', 'CLOSED')
        )
        UPDATE sales_return_lines AS line
           SET receipt_transaction_id = candidates.transaction_id
          FROM candidates
         WHERE line.id = candidates.line_id
           AND candidates.line_matches = 1 AND candidates.movement_matches = 1
        """
    )
    op.create_table(
        "credit_note_cost_allocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("credit_note_line_id", sa.UUID(), nullable=False),
        sa.Column("sales_return_line_id", sa.UUID(), nullable=False),
        sa.Column("receipt_transaction_id", sa.UUID(), nullable=False),
        sa.Column("source_quantity_offset", sa.Numeric(18, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("cost_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_cost_allocations")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="RESTRICT",
            name=op.f("fk_credit_note_cost_allocations_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["credit_note_line_id"],
            ["credit_note_lines.id"],
            ondelete="RESTRICT",
            name=op.f("fk_credit_cost_allocations_credit_line"),
        ),
        sa.ForeignKeyConstraint(
            ["sales_return_line_id"],
            ["sales_return_lines.id"],
            ondelete="RESTRICT",
            name=op.f("fk_credit_cost_allocations_return_line"),
        ),
        sa.ForeignKeyConstraint(
            ["receipt_transaction_id"],
            ["inventory_transactions.id"],
            ondelete="RESTRICT",
            name=op.f("fk_credit_cost_allocations_receipt_txn"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
            name=op.f("fk_credit_note_cost_allocations_created_by_id_users"),
        ),
        sa.UniqueConstraint(
            "credit_note_line_id",
            "receipt_transaction_id",
            "source_quantity_offset",
            name="uq_credit_cost_allocation_piece",
        ),
        sa.CheckConstraint(
            "source_quantity_offset >= 0",
            name=op.f("ck_credit_note_cost_allocations_source_offset_non_negative"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_credit_note_cost_allocations_allocated_quantity_positive")
        ),
        sa.CheckConstraint(
            "cost_amount >= 0",
            name=op.f("ck_credit_note_cost_allocations_allocated_cost_non_negative"),
        ),
    )
    for name, columns in (
        ("ix_credit_note_cost_allocations_company_id", ["company_id"]),
        ("ix_credit_cost_allocations_line", ["company_id", "credit_note_line_id"]),
        ("ix_credit_cost_allocations_return", ["company_id", "sales_return_line_id"]),
        ("ix_credit_cost_allocations_receipt", ["company_id", "receipt_transaction_id"]),
    ):
        op.create_index(name, "credit_note_cost_allocations", columns)
    op.execute(
        "CREATE TRIGGER trg_credit_note_cost_allocations_immutable "
        "BEFORE DELETE OR UPDATE ON credit_note_cost_allocations "
        "FOR EACH ROW EXECUTE FUNCTION public.atcraft_reject_row_mutation()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Freeze writers before the check so a concurrent credit cannot append
    # evidence between the emptiness check and dropping its history table.
    op.execute(
        "LOCK TABLE credit_note_cost_allocations, sales_return_lines IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM credit_note_cost_allocations)")):
        raise RuntimeError(
            "Return-credit FIFO allocation history exists; downgrade would discard it."
        )
    op.drop_table("credit_note_cost_allocations")
    op.drop_index("uq_sales_return_lines_receipt", table_name="sales_return_lines")
    op.drop_constraint(
        op.f("fk_sales_return_lines_receipt_transaction"),
        "sales_return_lines",
        type_="foreignkey",
    )
    op.drop_column("sales_return_lines", "receipt_transaction_id")
