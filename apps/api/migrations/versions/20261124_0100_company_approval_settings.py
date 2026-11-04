"""Create the company approval settings used by inventory workflows.

Revision ID: 20261124_0100
Revises: 20261123_0100

The table starts empty: absent rows and NULL thresholds retain the application's
existing defaults. No company, inventory or audit history is rewritten. Foreign
key lock acquisition is bounded to five seconds; a timeout rolls the migration
back so it can be retried during a quieter maintenance window.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261124_0100"
down_revision = "20261123_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "company_approval_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("po_dual_approval_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("adjustment_approval_threshold", sa.Numeric(18, 4), nullable=True),
        sa.Column("internal_issue_approval_threshold", sa.Numeric(18, 4), nullable=True),
        sa.Column("requisition_auto_approve_limit", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_company_approval_settings_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_company_approval_settings_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_company_approval_settings"),
        sa.UniqueConstraint("company_id", name="uq_company_approval_settings_company"),
    )
    op.create_index(
        "ix_company_approval_settings_company_id",
        "company_approval_settings",
        ["company_id"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Hold the lock through the emptiness check and DROP so a concurrent first
    # settings save cannot be committed and then silently discarded.
    op.execute("LOCK TABLE company_approval_settings IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM company_approval_settings) THEN
                RAISE EXCEPTION 'Company approval settings exist; downgrade would discard them. Preserve the settings and keep this revision installed.';
            END IF;
        END;
        $$
        """
    )
    op.drop_table("company_approval_settings")
