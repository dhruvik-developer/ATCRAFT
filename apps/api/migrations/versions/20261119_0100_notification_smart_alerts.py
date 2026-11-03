"""add configurable smart-alert rules and episode state

Revision ID: 20261119_0100
Revises: 20261118_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import src.core.types

revision: str = "20261119_0100"
down_revision: str | None = "20261118_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_alert_rules",
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("threshold_minutes", sa.Integer(), nullable=False),
        sa.Column("remind_every_minutes", sa.Integer(), nullable=False),
        sa.Column("id", src.core.types.GUID(), nullable=False),
        sa.Column("company_id", src.core.types.GUID(), nullable=False),
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
        sa.Column("created_by_id", src.core.types.GUID(), nullable=True),
        sa.Column("updated_by_id", src.core.types.GUID(), nullable=True),
        sa.CheckConstraint(
            "threshold_minutes >= 0",
            name=op.f("ck_notification_alert_rules_threshold_nonnegative"),
        ),
        sa.CheckConstraint(
            "remind_every_minutes > 0",
            name=op.f("ck_notification_alert_rules_remind_every_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notification_alert_rules_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notification_alert_rules_created_by_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notification_alert_rules_updated_by_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_alert_rules")),
        sa.UniqueConstraint(
            "company_id",
            "rule_key",
            name="uq_notification_alert_rules_company_key",
        ),
    )
    op.create_index(
        op.f("ix_notification_alert_rules_company_id"),
        "notification_alert_rules",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_alert_rules_created_at"),
        "notification_alert_rules",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "notification_alert_states",
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("entity_id", src.core.types.GUID(), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", src.core.types.GUID(), nullable=False),
        sa.Column("company_id", src.core.types.GUID(), nullable=False),
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
        sa.Column("created_by_id", src.core.types.GUID(), nullable=True),
        sa.Column("updated_by_id", src.core.types.GUID(), nullable=True),
        sa.CheckConstraint(
            "notified_count >= 0",
            name=op.f("ck_notification_alert_states_notified_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notification_alert_states_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notification_alert_states_created_by_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notification_alert_states_updated_by_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_alert_states")),
    )
    op.create_index(
        "uq_notification_alert_states_open",
        "notification_alert_states",
        ["company_id", "rule_key", "entity_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        op.f("ix_notification_alert_states_company_id"),
        "notification_alert_states",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_alert_states_created_at"),
        "notification_alert_states",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_alert_states_company_id_resolved_at_last_detected_at"),
        "notification_alert_states",
        ["company_id", "resolved_at", "last_detected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notification_alert_states_company_id_resolved_at_last_detected_at"),
        table_name="notification_alert_states",
    )
    op.drop_index(
        op.f("ix_notification_alert_states_created_at"),
        table_name="notification_alert_states",
    )
    op.drop_index(
        op.f("ix_notification_alert_states_company_id"),
        table_name="notification_alert_states",
    )
    op.drop_index(
        "uq_notification_alert_states_open",
        table_name="notification_alert_states",
    )
    op.drop_table("notification_alert_states")
    op.drop_index(
        op.f("ix_notification_alert_rules_created_at"),
        table_name="notification_alert_rules",
    )
    op.drop_index(
        op.f("ix_notification_alert_rules_company_id"),
        table_name="notification_alert_rules",
    )
    op.drop_table("notification_alert_rules")
