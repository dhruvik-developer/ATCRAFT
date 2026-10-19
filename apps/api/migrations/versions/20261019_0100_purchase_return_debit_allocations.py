"""Track purchase-return debit allocations against vendor invoices."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_0100"
down_revision = "20261018_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_invoices",
        sa.Column(
            "debit_allocated_amount",
            sa.Numeric(18, 4),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_vendor_invoices_debit_allocated_non_negative"),
        "vendor_invoices",
        "debit_allocated_amount >= 0",
    )
    op.create_table(
        "purchase_return_debit_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_return_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "amount > 0",
            name=op.f("ck_purchase_return_debit_allocations_amount_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_purchase_return_debit_allocations_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_purchase_return_debit_allocations_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_purchase_return_debit_allocations_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_return_id"],
            ["purchase_returns.id"],
            name="fk_pr_debit_allocations_return_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_invoice_id"],
            ["vendor_invoices.id"],
            name="fk_pr_debit_allocations_invoice_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_purchase_return_debit_allocations"),
        sa.UniqueConstraint(
            "company_id",
            "purchase_return_id",
            "vendor_invoice_id",
            name="uq_return_debit_allocations_target",
        ),
    )
    op.create_index(
        "ix_purchase_return_debit_allocations_company_id",
        "purchase_return_debit_allocations",
        ["company_id"],
    )
    op.create_index(
        "ix_purchase_return_debit_allocations_created_at",
        "purchase_return_debit_allocations",
        ["created_at"],
    )
    op.create_index(
        "ix_purchase_return_debit_allocations_invoice",
        "purchase_return_debit_allocations",
        ["company_id", "vendor_invoice_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_purchase_return_debit_allocations_created_at",
        table_name="purchase_return_debit_allocations",
    )
    op.drop_index(
        "ix_purchase_return_debit_allocations_company_id",
        table_name="purchase_return_debit_allocations",
    )
    op.drop_index(
        "ix_purchase_return_debit_allocations_invoice",
        table_name="purchase_return_debit_allocations",
    )
    op.drop_table("purchase_return_debit_allocations")
    op.drop_constraint(
        op.f("ck_vendor_invoices_debit_allocated_non_negative"),
        "vendor_invoices",
        type_="check",
    )
    op.drop_column("vendor_invoices", "debit_allocated_amount")
