"""One-to-one batch SKU conversion lineage for job-work receipts.

Revision ID: 20261128_0100
Revises: 20261127_0100
Create Date: 2026-11-08

Mirrors ``stock_serial_conversions``: an immutable join from the issued input
lot to a new lot on the output SKU when job work changes identity one-to-one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261128_0100"
down_revision: str | None = "20261127_0100"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "stock_batch_conversions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("output_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_work_receipt_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "source_batch_id <> output_batch_id",
            name="batch_conversion_changes_identity",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_batch_conversions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_batch_conversions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_receipt_line_id"],
            ["job_work_receipt_lines.id"],
            name="fk_batch_conversion_receipt_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["output_batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_batch_conversions_output_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_batch_conversions_source_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_batch_conversions")),
        sa.UniqueConstraint(
            "company_id",
            "output_batch_id",
            name="uq_stock_batch_conversions_output",
        ),
        sa.UniqueConstraint(
            "company_id",
            "source_batch_id",
            name="uq_stock_batch_conversions_source",
        ),
    )
    op.create_index(
        "ix_stock_batch_conversions_company_id",
        "stock_batch_conversions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_stock_batch_conversions_receipt",
        "stock_batch_conversions",
        ["company_id", "job_work_receipt_line_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_stock_batch_conversions_receipt", table_name="stock_batch_conversions")
    op.drop_index("ix_stock_batch_conversions_company_id", table_name="stock_batch_conversions")
    op.drop_table("stock_batch_conversions")
