"""Align the migrated schema with the shared tenant and timestamp contracts.

Revision ID: 20261027_0100
Revises: 20261026_0100
Create Date: 2026-10-22

The baseline omitted timestamp defaults and indexes from five tables and gave
their tenant foreign keys CASCADE rather than TenantMixin's RESTRICT. Existing
financial history must not disappear when a company is removed. CHECK expressions
already agree with the models; renaming them preserves their OIDs and validation
state rather than dropping and rechecking business rules.

Indexes are built/dropped concurrently outside the DDL transaction. The remaining
metadata changes and FK replacement are atomic, use a bounded lock wait, and do
not rewrite rows. FK validation scans the five affected tables while their DDL
locks are held; schedule the migration away from heavy writes to those tables.
An interrupted concurrent index build is removed before retrying that build.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261027_0100"
down_revision = "20261026_0100"
branch_labels = None
depends_on = None

_TENANT_TABLES = (
    "credit_note_lines",
    "credit_notes",
    "dealer_banners",
    "sales_return_lines",
    "sales_returns",
)

_INDEXES = (
    ("ix_credit_note_lines_company_id", "credit_note_lines", "company_id"),
    ("ix_credit_note_lines_created_at", "credit_note_lines", "created_at"),
    ("ix_credit_notes_company_id", "credit_notes", "company_id"),
    ("ix_credit_notes_created_at", "credit_notes", "created_at"),
    ("ix_dealer_banners_created_at", "dealer_banners", "created_at"),
    ("ix_sales_return_lines_company_id", "sales_return_lines", "company_id"),
    ("ix_sales_return_lines_created_at", "sales_return_lines", "created_at"),
    ("ix_sales_returns_company_id", "sales_returns", "company_id"),
    ("ix_sales_returns_created_at", "sales_returns", "created_at"),
)

# Explicit historical names include PostgreSQL's deterministic truncation hashes.
# No migration imports current models: future model edits cannot change this DDL.
_CHECK_RENAMES = (
    (
        "credit_note_lines",
        "ck_credit_note_lines_ck_credit_note_lines_amounts_valid",
        "ck_credit_note_lines_credit_note_line_amounts_valid",
    ),
    (
        "credit_note_lines",
        "ck_credit_note_lines_ck_credit_note_lines_quantity_positive",
        "ck_credit_note_lines_credit_note_quantity_positive",
    ),
    (
        "credit_note_lines",
        "ck_credit_note_lines_ck_credit_note_lines_tax_reconciles",
        "ck_credit_note_lines_credit_note_line_tax_reconciles",
    ),
    (
        "credit_note_lines",
        "ck_credit_note_lines_ck_credit_note_lines_total_reconciles",
        "ck_credit_note_lines_credit_note_line_total_reconciles",
    ),
    (
        "credit_notes",
        "ck_credit_notes_ck_credit_notes_cancellation_evidence",
        "ck_credit_notes_credit_note_cancellation_evidence",
    ),
    (
        "credit_notes",
        "ck_credit_notes_ck_credit_notes_reason_known",
        "ck_credit_notes_credit_note_reason_known",
    ),
    (
        "credit_notes",
        "ck_credit_notes_ck_credit_notes_status_known",
        "ck_credit_notes_credit_note_status_known",
    ),
    (
        "credit_notes",
        "ck_credit_notes_ck_credit_notes_tax_reconciles",
        "ck_credit_notes_credit_note_tax_reconciles",
    ),
    (
        "credit_notes",
        "ck_credit_notes_ck_credit_notes_total_reconciles",
        "ck_credit_notes_credit_note_total_reconciles",
    ),
    (
        "dealer_banners",
        "ck_dealer_banners_ck_dealer_banners_audience_known",
        "ck_dealer_banners_banner_audience_known",
    ),
    (
        "dealer_banners",
        "ck_dealer_banners_ck_dealer_banners_dates_ordered",
        "ck_dealer_banners_banner_dates_ordered",
    ),
    (
        "dealer_banners",
        "ck_dealer_banners_ck_dealer_banners_link_kind_known",
        "ck_dealer_banners_link_kind_known",
    ),
    (
        "dealer_banners",
        "ck_dealer_banners_ck_dealer_banners_link_target_matches",
        "ck_dealer_banners_link_target_matches",
    ),
    (
        "dealer_banners",
        "ck_dealer_banners_ck_dealer_banners_status_known",
        "ck_dealer_banners_banner_status_known",
    ),
    (
        "dispatch_lines",
        "ck_dispatch_lines_ck_dispatch_lines_returned_quantity_bounded",
        "ck_dispatch_lines_returned_quantity_bounded",
    ),
    (
        "sales_invoice_lines",
        "ck_sales_invoice_lines_ck_sales_invoice_lines_credited__72c0",
        "ck_sales_invoice_lines_credited_amount_bounded",
    ),
    (
        "sales_invoice_lines",
        "ck_sales_invoice_lines_ck_sales_invoice_lines_credited__b0a2",
        "ck_sales_invoice_lines_credited_quantity_bounded",
    ),
    (
        "sales_return_lines",
        "ck_sales_return_lines_ck_sales_return_lines_credited_bounded",
        "ck_sales_return_lines_sales_return_line_credited_bounded",
    ),
    (
        "sales_return_lines",
        "ck_sales_return_lines_ck_sales_return_lines_quantity_positive",
        "ck_sales_return_lines_sales_return_line_quantity_positive",
    ),
    (
        "sales_returns",
        "ck_sales_returns_ck_sales_returns_cancellation_evidence",
        "ck_sales_returns_sales_return_cancellation_evidence",
    ),
    (
        "sales_returns",
        "ck_sales_returns_ck_sales_returns_source_known",
        "ck_sales_returns_sales_return_source_known",
    ),
    (
        "sales_returns",
        "ck_sales_returns_ck_sales_returns_status_known",
        "ck_sales_returns_sales_return_status_known",
    ),
    (
        "stock_adjustment_lines",
        "ck_stock_adjustment_lines_ck_stock_adjustment_lines_ser_91d3",
        "ck_stock_adjustment_lines_serial_count_matches_quantity",
    ),
    (
        "stock_transfer_lines",
        "ck_stock_transfer_lines_ck_stock_transfer_lines_serial__0916",
        "ck_stock_transfer_lines_serial_count_matches_quantity",
    ),
)


def _change_indexes(*, create: bool) -> None:
    # Concurrent builds cannot run in a transaction. Do them before the atomic
    # metadata changes so no ALTER TABLE lock is held during an index scan.
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            for name, table, column in _INDEXES:
                if create:
                    valid = None
                    if not op.get_context().as_sql:
                        valid = op.get_bind().scalar(
                            sa.text(
                                "SELECT indisvalid FROM pg_index "
                                "WHERE indexrelid = to_regclass(:name)"
                            ),
                            {"name": f"public.{name}"},
                        )
                    if valid is False:
                        op.drop_index(name, table_name=table, postgresql_concurrently=True)
                    op.create_index(
                        name,
                        table,
                        [column],
                        unique=False,
                        postgresql_concurrently=True,
                        if_not_exists=True,
                    )
                else:
                    op.drop_index(
                        name,
                        table_name=table,
                        postgresql_concurrently=True,
                        if_exists=True,
                    )
        finally:
            op.execute("RESET lock_timeout")


def _change_metadata(*, upgrade: bool) -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _TENANT_TABLES:
        for column in ("created_at", "updated_at"):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=sa.func.now() if upgrade else None,
            )
        name = f"fk_{table}_company_id_companies"
        op.drop_constraint(op.f(name), table, type_="foreignkey")
        op.create_foreign_key(
            op.f(name),
            table,
            "companies",
            ["company_id"],
            ["id"],
            ondelete="RESTRICT" if upgrade else "CASCADE",
            postgresql_not_valid=True,
        )
        op.execute(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"')

    for table, previous, current in _CHECK_RENAMES:
        before, after = (previous, current) if upgrade else (current, previous)
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{before}" TO "{after}"')


def upgrade() -> None:
    _change_indexes(create=True)
    _change_metadata(upgrade=True)


def downgrade() -> None:
    _change_indexes(create=False)
    _change_metadata(upgrade=False)
