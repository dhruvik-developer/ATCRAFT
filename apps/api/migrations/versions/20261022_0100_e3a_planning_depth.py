"""Add low-level codes and expected availability dates to the MRP tables.

Revision ID: 20261022_0100
Revises: 20261021_0100
Create Date: 2026-10-19

The E3a engine-depth step plans level by level and reports when material is
expected to be available, so three tables gain five columns between them:

* ``mrp_requirements.low_level_code`` - the deepest level the item appears at
  across the structures in scope, which is the level the planner netted it at;
* ``mrp_requirements.expected_available_date`` - when the gross requirement is
  fully covered by stock, dated supply or the plan's own order, after component
  slippage is rolled up;
* ``mrp_suggestions.low_level_code`` / ``expected_available_date`` - the same two
  facts on the work queue;
* ``mrp_demands.expected_available_date`` - the per-line answer, stamped when the
  run completes because it depends on the plan.

Every column is nullable on purpose: rows written before this revision predate
the calculation, and "not planned level by level" is honestly NULL rather than a
default that would read as a fact. Application code always writes the new values.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261022_0100"
down_revision = "20261021_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mrp_requirements", sa.Column("low_level_code", sa.Integer(), nullable=True))
    op.add_column(
        "mrp_requirements", sa.Column("expected_available_date", sa.Date(), nullable=True)
    )
    op.add_column("mrp_suggestions", sa.Column("low_level_code", sa.Integer(), nullable=True))
    op.add_column("mrp_suggestions", sa.Column("expected_available_date", sa.Date(), nullable=True))
    op.add_column("mrp_demands", sa.Column("expected_available_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("mrp_demands", "expected_available_date")
    op.drop_column("mrp_suggestions", "expected_available_date")
    op.drop_column("mrp_suggestions", "low_level_code")
    op.drop_column("mrp_requirements", "expected_available_date")
    op.drop_column("mrp_requirements", "low_level_code")
