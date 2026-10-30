"""Add per-company settings for nightly MRP runs.

Revision ID: 20261109_0100
Revises: 20261108_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261109_0100"
down_revision = "20261108_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "mrp_schedule_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "mrp_scheduled_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("horizon_days", sa.Integer(), server_default="90", nullable=False),
        sa.Column(
            "warehouse_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "respect_safety_stock",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "horizon_days BETWEEN 1 AND 1095",
            name=op.f("ck_mrp_schedule_settings_horizon_days_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_mrp_schedule_settings_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_mrp_schedule_settings_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_mrp_schedule_settings_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mrp_schedule_settings"),
        sa.UniqueConstraint("company_id", name="uq_mrp_schedule_settings_company"),
    )
    op.create_index(
        "ix_mrp_schedule_settings_company_id",
        "mrp_schedule_settings",
        ["company_id"],
    )
    op.create_index(
        "ix_mrp_schedule_settings_created_at",
        "mrp_schedule_settings",
        ["created_at"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("mrp_schedule_settings")
