"""Index causal demand lookups in planning history.

Revision ID: 20261102_0100
Revises: 20261101_0100
Create Date: 2026-10-27

No rows or columns change. The ordinary GIN build is transactional, so an
interrupted upgrade rolls back cleanly. It holds a write-conflicting table
lock while building: use a maintenance window for large planning histories.
Lock acquisition is bounded rather than waiting indefinitely behind traffic.
"""

from alembic import op

revision = "20261102_0100"
down_revision = "20261101_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index(
        "ix_mrp_suggestions_source_demands",
        "mrp_suggestions",
        ["source_demands"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_mrp_suggestions_source_demands", table_name="mrp_suggestions")
