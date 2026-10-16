"""Add sales returns and credit notes.

Revision ID: 20261016_1400
Revises: 20261016_1310
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_1400"
down_revision: str | None = "20261016_1310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dispatch_lines",
        sa.Column("returned_quantity", sa.Numeric(18, 4), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_dispatch_lines_returned_quantity_bounded",
        "dispatch_lines",
        "returned_quantity >= 0 AND returned_quantity <= quantity",
    )
    op.add_column(
        "sales_invoice_lines",
        sa.Column("credited_quantity", sa.Numeric(18, 4), server_default="0", nullable=False),
    )
    op.add_column(
        "sales_invoice_lines",
        sa.Column("credited_amount", sa.Numeric(18, 4), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_sales_invoice_lines_credited_quantity_bounded",
        "sales_invoice_lines",
        "credited_quantity >= 0 AND credited_quantity <= quantity",
    )
    op.create_check_constraint(
        "ck_sales_invoice_lines_credited_amount_bounded",
        "sales_invoice_lines",
        "credited_amount >= 0 AND credited_amount <= taxable_amount",
    )

    op.create_table(
        "sales_returns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("dispatch_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("return_warehouse_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(16), server_default="DRAFT", nullable=False),
        sa.Column("source", sa.String(16), server_default="ERP", nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dispatch_id"], ["dispatches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["return_warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "code", name="uq_sales_returns_company_code"),
        sa.CheckConstraint(
            "status IN ('DRAFT','RECEIVED','CLOSED','CANCELLED')",
            name="ck_sales_returns_status_known",
        ),
        sa.CheckConstraint(
            "source IN ('ERP','DEALER_PORTAL')", name="ck_sales_returns_source_known"
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR "
            "(cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)",
            name="ck_sales_returns_cancellation_evidence",
        ),
    )
    op.create_index(
        "ix_sales_returns_customer_date",
        "sales_returns",
        ["company_id", "customer_id", "created_at", "id"],
    )
    op.create_index("ix_sales_returns_dispatch", "sales_returns", ["company_id", "dispatch_id"])
    op.create_index(
        "ix_sales_returns_status", "sales_returns", ["company_id", "status", "created_at", "id"]
    )

    op.create_table(
        "sales_return_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("sales_return_id", sa.UUID(), nullable=False),
        sa.Column("dispatch_line_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("credited_quantity", sa.Numeric(18, 4), server_default="0", nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requires_qc", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sales_return_id"], ["sales_returns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dispatch_line_id"], ["dispatch_lines.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sales_return_id", "dispatch_line_id", name="uq_sales_return_lines_dispatch"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_sales_return_lines_quantity_positive"),
        sa.CheckConstraint(
            "credited_quantity >= 0 AND credited_quantity <= quantity",
            name="ck_sales_return_lines_credited_bounded",
        ),
    )
    op.create_index(
        "ix_sales_return_lines_return",
        "sales_return_lines",
        ["company_id", "sales_return_id"],
    )

    op.create_table(
        "credit_notes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("sales_invoice_id", sa.UUID(), nullable=False),
        sa.Column("sales_return_id", sa.UUID(), nullable=True),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("note_date", sa.Date(), nullable=False),
        sa.Column("reason_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), server_default="ISSUED", nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("subtotal", sa.Numeric(18, 4), nullable=False),
        sa.Column("taxable_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("cgst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("sgst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("igst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("round_off_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("buyer_name", sa.String(255), nullable=False),
        sa.Column("buyer_gstin", sa.String(15), nullable=True),
        sa.Column("seller_name", sa.String(255), nullable=False),
        sa.Column("seller_gstin", sa.String(15), nullable=True),
        sa.Column("seller_address", sa.Text(), nullable=False),
        sa.Column("billing_address", sa.Text(), nullable=False),
        sa.Column("place_of_supply_state_code", sa.String(4), nullable=True),
        sa.Column("is_interstate", sa.Boolean(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by_id", sa.UUID(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by_id", sa.UUID(), nullable=True),
        sa.Column("journal_id", sa.UUID(), nullable=False),
        sa.Column("pdf_document_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sales_invoice_id"], ["sales_invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sales_return_id"], ["sales_returns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["issued_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cancelled_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pdf_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "code", name="uq_credit_notes_company_code"),
        sa.CheckConstraint("status IN ('ISSUED','CANCELLED')", name="ck_credit_notes_status_known"),
        sa.CheckConstraint(
            "reason_type IN ('RETURN','PRICE_ADJUSTMENT','DISCOUNT','OTHER')",
            name="ck_credit_notes_reason_known",
        ),
        sa.CheckConstraint(
            "total_amount = taxable_amount + tax_amount + round_off_amount",
            name="ck_credit_notes_total_reconciles",
        ),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name="ck_credit_notes_tax_reconciles",
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR "
            "(cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)",
            name="ck_credit_notes_cancellation_evidence",
        ),
    )
    op.create_index(
        "ix_credit_notes_invoice", "credit_notes", ["company_id", "sales_invoice_id", "note_date"]
    )
    op.create_index(
        "ix_credit_notes_customer", "credit_notes", ["company_id", "customer_id", "note_date"]
    )

    op.create_table(
        "credit_note_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("credit_note_id", sa.UUID(), nullable=False),
        sa.Column("sales_invoice_line_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("hsn_code", sa.String(16), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("taxable_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("tax_rate", sa.Numeric(9, 4), nullable=False),
        sa.Column("cgst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("sgst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("igst_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("line_total", sa.Numeric(18, 4), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["credit_note_id"], ["credit_notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sales_invoice_line_id"], ["sales_invoice_lines.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "credit_note_id",
            "sales_invoice_line_id",
            name="uq_credit_note_lines_invoice_line",
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name="ck_credit_note_lines_quantity_positive"
        ),
        sa.CheckConstraint(
            "taxable_amount >= 0 AND tax_rate >= 0 AND tax_rate <= 100",
            name="ck_credit_note_lines_amounts_valid",
        ),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name="ck_credit_note_lines_tax_reconciles",
        ),
        sa.CheckConstraint(
            "line_total = taxable_amount + tax_amount",
            name="ck_credit_note_lines_total_reconciles",
        ),
    )


def downgrade() -> None:
    op.drop_table("credit_note_lines")
    op.drop_index("ix_credit_notes_customer", table_name="credit_notes")
    op.drop_index("ix_credit_notes_invoice", table_name="credit_notes")
    op.drop_table("credit_notes")
    op.drop_index("ix_sales_return_lines_return", table_name="sales_return_lines")
    op.drop_table("sales_return_lines")
    op.drop_index("ix_sales_returns_status", table_name="sales_returns")
    op.drop_index("ix_sales_returns_dispatch", table_name="sales_returns")
    op.drop_index("ix_sales_returns_customer_date", table_name="sales_returns")
    op.drop_table("sales_returns")
    op.drop_constraint(
        "ck_sales_invoice_lines_credited_amount_bounded", "sales_invoice_lines", type_="check"
    )
    op.drop_constraint(
        "ck_sales_invoice_lines_credited_quantity_bounded", "sales_invoice_lines", type_="check"
    )
    op.drop_column("sales_invoice_lines", "credited_amount")
    op.drop_column("sales_invoice_lines", "credited_quantity")
    op.drop_constraint(
        "ck_dispatch_lines_returned_quantity_bounded", "dispatch_lines", type_="check"
    )
    op.drop_column("dispatch_lines", "returned_quantity")
