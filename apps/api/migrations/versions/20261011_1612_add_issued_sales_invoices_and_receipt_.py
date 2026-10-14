"""Add issued sales invoices and receipt allocations

Revision ID: b28b4dab11a4
Revises: 3b7a1c9e4f2a
Created: 2026-10-11 16:12:05.510389+00:00

Review: new tables take no existing-table lock. Nullable payment linkage and its
CHECK/FK take brief DDL locks; the FK and CHECK are installed NOT VALID then
validated. Existing vendor allocations are unchanged. Tenant-leading and partial
unique indexes bound invoice lists and live dispatches. Payment indexes are built
on the small allocation table in this release. Downgrade refuses issued invoices
because dropping statutory documents while retaining their journals is unsafe;
empty-schema rollback restores vendor-only allocations. No financial backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b28b4dab11a4"
down_revision: str | None = "3b7a1c9e4f2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM document_sequences WHERE doc_type='sales_invoice') THEN
            RAISE EXCEPTION 'sales_invoice sequence already exists; review numbering before SI rollout';
        END IF;
    END $$""")
    op.create_table(
        "sales_invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("sales_order_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("is_interstate", sa.Boolean(), nullable=False),
        sa.Column("place_of_supply_state_code", sa.String(length=4), nullable=True),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "discount_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "taxable_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "cgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "sgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "igst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "round_off_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "paid_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("seller_name", sa.String(length=255), nullable=False),
        sa.Column("seller_gstin", sa.String(length=15), nullable=True),
        sa.Column("seller_state_code", sa.String(length=4), nullable=True),
        sa.Column("seller_state_name", sa.String(length=120), nullable=True),
        sa.Column("buyer_name", sa.String(length=255), nullable=False),
        sa.Column("buyer_gstin", sa.String(length=15), nullable=True),
        sa.Column("buyer_state_code", sa.String(length=4), nullable=True),
        sa.Column("buyer_state_name", sa.String(length=120), nullable=True),
        sa.Column("transporter_name", sa.String(length=120), nullable=True),
        sa.Column("vehicle_number", sa.String(length=24), nullable=True),
        sa.Column("lr_number", sa.String(length=64), nullable=True),
        sa.Column("eway_bill_number", sa.String(length=64), nullable=True),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("bank_account_name", sa.String(length=255), nullable=True),
        sa.Column("bank_account_number", sa.String(length=34), nullable=True),
        sa.Column("bank_ifsc", sa.String(length=11), nullable=True),
        sa.Column("bank_branch", sa.String(length=120), nullable=True),
        sa.Column("seller_address", sa.Text(), nullable=False),
        sa.Column("billing_address", sa.Text(), nullable=False),
        sa.Column("shipping_address", sa.Text(), nullable=False),
        sa.Column("invoice_terms", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by_id", sa.Uuid(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_id", sa.Uuid(), nullable=True),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("reversal_journal_id", sa.Uuid(), nullable=True),
        sa.Column("pdf_document_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR (cancellation_reason IS NOT NULL AND cancelled_at IS NOT NULL AND reversal_journal_id IS NOT NULL)",
            name=op.f("ck_sales_invoices_cancellation_evidence"),
        ),
        sa.CheckConstraint(
            "status IN ('ISSUED', 'PARTIALLY_PAID', 'PAID', 'CANCELLED')",
            name=op.f("ck_sales_invoices_status_known"),
        ),
        sa.CheckConstraint(
            "cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0",
            name=op.f("ck_sales_invoices_tax_non_negative"),
        ),
        sa.CheckConstraint(
            "due_date >= invoice_date", name=op.f("ck_sales_invoices_due_not_before_invoice")
        ),
        sa.CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name=op.f("ck_sales_invoices_tax_exclusive"),
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_sales_invoices_currency_iso")),
        sa.CheckConstraint(
            "paid_amount >= 0 AND paid_amount <= total_amount",
            name=op.f("ck_sales_invoices_paid_bounded"),
        ),
        sa.CheckConstraint(
            "subtotal = taxable_amount + discount_amount",
            name=op.f("ck_sales_invoices_subtotal_reconciles"),
        ),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name=op.f("ck_sales_invoices_tax_split_reconciles"),
        ),
        sa.CheckConstraint(
            "total_amount = taxable_amount + tax_amount + round_off_amount",
            name=op.f("ck_sales_invoices_total_reconciles"),
        ),
        sa.CheckConstraint(
            "total_amount > 0 AND taxable_amount >= 0 AND discount_amount >= 0",
            name=op.f("ck_sales_invoices_amounts_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoices_cancelled_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_sales_invoices_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoices_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_sales_invoices_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["dispatches.id"],
            name=op.f("fk_sales_invoices_dispatch_id_dispatches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoices_issued_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pdf_document_id"],
            ["documents.id"],
            name=op.f("fk_sales_invoices_pdf_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"],
            ["sales_orders.id"],
            name=op.f("fk_sales_invoices_sales_order_id_sales_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoices_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales_invoices")),
        sa.UniqueConstraint("company_id", "code", name="uq_sales_invoices_company_code"),
    )
    op.create_index(
        op.f("ix_sales_invoices_company_id"), "sales_invoices", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_sales_invoices_created_at"), "sales_invoices", ["created_at"], unique=False
    )
    op.create_index(
        "ix_sales_invoices_customer_due",
        "sales_invoices",
        ["company_id", "customer_id", "due_date"],
        unique=False,
    )
    op.create_index(
        "ix_sales_invoices_order", "sales_invoices", ["company_id", "sales_order_id"], unique=False
    )
    op.create_index(
        "ix_sales_invoices_status_date",
        "sales_invoices",
        ["company_id", "status", "invoice_date"],
        unique=False,
    )
    op.create_index(
        "uq_sales_invoices_live_dispatch",
        "sales_invoices",
        ["company_id", "dispatch_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CANCELLED'"),
    )
    op.create_table(
        "sales_invoice_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("sales_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_line_id", sa.Uuid(), nullable=False),
        sa.Column("sales_order_line_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("uom_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("hsn_code", sa.String(length=16), nullable=True),
        sa.Column("uom_code", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("discount_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("taxable_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("cgst_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("sgst_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("igst_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("tax_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=18, scale=4), nullable=False),
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
        sa.CheckConstraint(
            "cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0",
            name=op.f("ck_sales_invoice_lines_tax_non_negative"),
        ),
        sa.CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name=op.f("ck_sales_invoice_lines_tax_exclusive"),
        ),
        sa.CheckConstraint(
            "line_number > 0 AND quantity > 0",
            name=op.f("ck_sales_invoice_lines_positive_quantity_number"),
        ),
        sa.CheckConstraint(
            "line_total = taxable_amount + tax_amount",
            name=op.f("ck_sales_invoice_lines_total_reconciles"),
        ),
        sa.CheckConstraint(
            "subtotal = taxable_amount + discount_amount",
            name=op.f("ck_sales_invoice_lines_subtotal_reconciles"),
        ),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name=op.f("ck_sales_invoice_lines_tax_split_reconciles"),
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100",
            name=op.f("ck_sales_invoice_lines_tax_rate_bounded"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0 AND discount_amount >= 0 AND taxable_amount >= 0",
            name=op.f("ck_sales_invoice_lines_amounts_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_sales_invoice_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoice_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_line_id"],
            ["dispatch_lines.id"],
            name=op.f("fk_sales_invoice_lines_dispatch_line_id_dispatch_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_sales_invoice_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_invoice_id"],
            ["sales_invoices.id"],
            name=op.f("fk_sales_invoice_lines_sales_invoice_id_sales_invoices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_line_id"],
            ["sales_order_lines.id"],
            name=op.f("fk_sales_invoice_lines_sales_order_line_id_sales_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_sales_invoice_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_sales_invoice_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales_invoice_lines")),
        sa.UniqueConstraint(
            "sales_invoice_id", "dispatch_line_id", name="uq_sales_invoice_lines_dispatch"
        ),
        sa.UniqueConstraint(
            "sales_invoice_id", "line_number", name="uq_sales_invoice_lines_number"
        ),
    )
    op.create_index(
        op.f("ix_sales_invoice_lines_company_id"),
        "sales_invoice_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sales_invoice_lines_created_at"),
        "sales_invoice_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_sales_invoice_lines_hsn",
        "sales_invoice_lines",
        ["company_id", "hsn_code", "tax_rate"],
        unique=False,
    )
    op.create_index(
        "ix_sales_invoice_lines_invoice",
        "sales_invoice_lines",
        ["company_id", "sales_invoice_id", "line_number"],
        unique=False,
    )
    op.create_index(
        "ix_sales_invoice_lines_order_line",
        "sales_invoice_lines",
        ["company_id", "sales_order_line_id"],
        unique=False,
    )
    op.add_column("payment_allocations", sa.Column("sales_invoice_id", sa.Uuid(), nullable=True))
    op.alter_column(
        "payment_allocations", "vendor_invoice_id", existing_type=sa.UUID(), nullable=True
    )
    op.drop_constraint(
        op.f("uq_payment_allocations_payment_invoice"), "payment_allocations", type_="unique"
    )
    op.create_index(
        op.f("ix_payment_allocations_sales_invoice_id"),
        "payment_allocations",
        ["sales_invoice_id"],
        unique=False,
    )
    op.create_index(
        "uq_payment_allocations_sales",
        "payment_allocations",
        ["payment_id", "sales_invoice_id"],
        unique=True,
        postgresql_where=sa.text("sales_invoice_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payment_allocations_vendor",
        "payment_allocations",
        ["payment_id", "vendor_invoice_id"],
        unique=True,
        postgresql_where=sa.text("vendor_invoice_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_payment_allocations_sales_invoice_id_sales_invoices"),
        "payment_allocations",
        "sales_invoices",
        ["sales_invoice_id"],
        ["id"],
        ondelete="RESTRICT",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE payment_allocations VALIDATE CONSTRAINT fk_payment_allocations_sales_invoice_id_sales_invoices"
    )
    op.create_check_constraint(
        op.f("ck_payment_allocations_exactly_one_invoice"),
        "payment_allocations",
        "(vendor_invoice_id IS NULL) <> (sales_invoice_id IS NULL)",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE payment_allocations VALIDATE CONSTRAINT ck_payment_allocations_exactly_one_invoice"
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM sales_invoices) THEN
            RAISE EXCEPTION 'cannot downgrade while statutory sales invoices exist';
        END IF;
    END $$""")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(
        op.f("ck_payment_allocations_exactly_one_invoice"), "payment_allocations", type_="check"
    )
    op.drop_constraint(
        op.f("fk_payment_allocations_sales_invoice_id_sales_invoices"),
        "payment_allocations",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_payment_allocations_vendor",
        table_name="payment_allocations",
        postgresql_where=sa.text("vendor_invoice_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_payment_allocations_sales",
        table_name="payment_allocations",
        postgresql_where=sa.text("sales_invoice_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_payment_allocations_sales_invoice_id"), table_name="payment_allocations")
    op.create_unique_constraint(
        op.f("uq_payment_allocations_payment_invoice"),
        "payment_allocations",
        ["payment_id", "vendor_invoice_id"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        "payment_allocations", "vendor_invoice_id", existing_type=sa.UUID(), nullable=False
    )
    op.drop_column("payment_allocations", "sales_invoice_id")
    op.drop_index("ix_sales_invoice_lines_order_line", table_name="sales_invoice_lines")
    op.drop_index("ix_sales_invoice_lines_invoice", table_name="sales_invoice_lines")
    op.drop_index("ix_sales_invoice_lines_hsn", table_name="sales_invoice_lines")
    op.drop_index(op.f("ix_sales_invoice_lines_created_at"), table_name="sales_invoice_lines")
    op.drop_index(op.f("ix_sales_invoice_lines_company_id"), table_name="sales_invoice_lines")
    op.drop_table("sales_invoice_lines")
    op.drop_index(
        "uq_sales_invoices_live_dispatch",
        table_name="sales_invoices",
        postgresql_where=sa.text("status <> 'CANCELLED'"),
    )
    op.drop_index("ix_sales_invoices_status_date", table_name="sales_invoices")
    op.drop_index("ix_sales_invoices_order", table_name="sales_invoices")
    op.drop_index("ix_sales_invoices_customer_due", table_name="sales_invoices")
    op.drop_index(op.f("ix_sales_invoices_created_at"), table_name="sales_invoices")
    op.drop_index(op.f("ix_sales_invoices_company_id"), table_name="sales_invoices")
    op.drop_table("sales_invoices")
    # ### end Alembic commands ###
