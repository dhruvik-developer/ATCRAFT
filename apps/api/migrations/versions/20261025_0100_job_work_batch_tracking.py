"""Capture batch identity through job-work movements.

Revision ID: 20261025_0100
Revises: 20261024_0100
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261025_0100"
down_revision = "20261024_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "job_work_order_lines",
        sa.Column(
            "is_batch_tracked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("job_work_order_lines", sa.Column("batch_id", GUID, nullable=True))
    op.create_foreign_key(
        "fk_job_work_order_lines_batch_id_stock_batches",
        "job_work_order_lines",
        "stock_batches",
        ["batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    for table in ("job_work_challan_lines", "job_work_receipt_lines"):
        op.add_column(table, sa.Column("batch_id", GUID, nullable=True))
        op.create_foreign_key(
            f"fk_{table}_batch_id_stock_batches",
            table,
            "stock_batches",
            ["batch_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table in ("job_work_receipt_lines", "job_work_challan_lines"):
        op.drop_constraint(f"fk_{table}_batch_id_stock_batches", table, type_="foreignkey")
        op.drop_column(table, "batch_id")

    op.drop_constraint(
        "fk_job_work_order_lines_batch_id_stock_batches",
        "job_work_order_lines",
        type_="foreignkey",
    )
    op.drop_column("job_work_order_lines", "batch_id")
    op.drop_column("job_work_order_lines", "is_batch_tracked")
