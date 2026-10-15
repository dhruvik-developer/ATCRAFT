"""Snapshot production standards and close-time variance.

Revision ID: 20261016_1000
Revises: 10f21bb9339c
Created: 2026-10-16

New monetary columns are nullable because historical orders have no trustworthy
release-time standard snapshot. ``work_orders.is_outsourced`` is non-null with a
false server default, preserving the existing classification of released work.
The change is metadata-only: no table rewrite or historical cost backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_1000"
down_revision: str | Sequence[str] | None = "10f21bb9339c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    op.add_column("production_orders", sa.Column("standard_unit_cost", _MONEY, nullable=True))
    op.add_column(
        "production_orders", sa.Column("standard_material_unit_cost", _MONEY, nullable=True)
    )
    op.add_column(
        "production_orders", sa.Column("standard_operation_unit_cost", _MONEY, nullable=True)
    )
    op.add_column(
        "production_orders", sa.Column("standard_subcontract_unit_cost", _MONEY, nullable=True)
    )
    op.add_column("production_orders", sa.Column("actual_subcontract_cost", _MONEY, nullable=True))
    op.add_column("production_orders", sa.Column("actual_unit_cost", _MONEY, nullable=True))
    op.add_column("production_orders", sa.Column("material_variance", _MONEY, nullable=True))
    op.add_column("production_orders", sa.Column("operation_variance", _MONEY, nullable=True))
    op.add_column("production_orders", sa.Column("subcontract_variance", _MONEY, nullable=True))
    op.add_column("production_orders", sa.Column("total_variance", _MONEY, nullable=True))
    op.add_column(
        "work_orders",
        sa.Column("is_outsourced", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("work_orders", "is_outsourced")
    op.drop_column("production_orders", "total_variance")
    op.drop_column("production_orders", "subcontract_variance")
    op.drop_column("production_orders", "operation_variance")
    op.drop_column("production_orders", "material_variance")
    op.drop_column("production_orders", "actual_unit_cost")
    op.drop_column("production_orders", "actual_subcontract_cost")
    op.drop_column("production_orders", "standard_subcontract_unit_cost")
    op.drop_column("production_orders", "standard_operation_unit_cost")
    op.drop_column("production_orders", "standard_material_unit_cost")
    op.drop_column("production_orders", "standard_unit_cost")
