"""Add consent, templates and delivery evidence for WhatsApp/SMS.

Revision ID: 20261114_0100
Revises: 20261113_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261114_0100"
down_revision = "20261113_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column("users", sa.Column("phone_e164", sa.String(16), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_phone_e164_format"),
        "users",
        "phone_e164 IS NULL OR phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
    )
    for column in ("phone_verified_at", "whatsapp_opt_in_at", "sms_opt_in_at"):
        op.add_column("users", sa.Column(column, sa.DateTime(timezone=True), nullable=True))

    op.add_column(
        "notification_preferences",
        sa.Column(
            "whatsapp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("sms_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("notifications", sa.Column("whatsapp_delivered_at", sa.DateTime(timezone=True)))
    op.add_column("notifications", sa.Column("sms_delivered_at", sa.DateTime(timezone=True)))
    op.add_column(
        "notifications",
        sa.Column("template_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_notifications_pending_whatsapp",
        "notifications",
        ["created_at"],
        postgresql_where=sa.text("whatsapp_delivered_at IS NULL AND 'WHATSAPP' = ANY(channels)"),
    )
    op.create_index(
        "ix_notifications_pending_sms",
        "notifications",
        ["created_at"],
        postgresql_where=sa.text("sms_delivered_at IS NULL AND 'SMS' = ANY(channels)"),
    )

    op.create_table(
        "user_phone_verification_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.String(16), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("send_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("send_window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 5",
            name=op.f("ck_user_phone_verification_challenges_attempt_count_bounded"),
        ),
        sa.CheckConstraint(
            "phone_e164 IS NULL OR phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
            name=op.f("ck_user_phone_verification_challenges_phone_e164_format"),
        ),
        sa.CheckConstraint(
            "send_count >= 0 AND send_count <= 3",
            name=op.f("ck_user_phone_verification_challenges_send_count_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_user_phone_verification_challenges_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_phone_verification_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_user_phone_verification_challenges_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_user_phone_verification_challenges_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_phone_verification_challenges")),
        sa.UniqueConstraint(
            "company_id",
            "user_id",
            name="uq_phone_verification_company_user",
        ),
    )
    op.create_index(
        "ix_phone_verification_pending",
        "user_phone_verification_challenges",
        ["company_id", "expires_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index(
        op.f("ix_user_phone_verification_challenges_company_id"),
        "user_phone_verification_challenges",
        ["company_id"],
    )
    op.create_index(
        op.f("ix_user_phone_verification_challenges_created_at"),
        "user_phone_verification_challenges",
        ["created_at"],
    )

    op.create_table(
        "notification_channel_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("notification_type", sa.String(48), nullable=False),
        sa.Column("template_name", sa.String(128), nullable=True),
        sa.Column("template_id", sa.String(128), nullable=True),
        sa.Column("language", sa.String(16), server_default="en", nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sms_text", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('WHATSAPP', 'SMS')",
            name=op.f("ck_notification_channel_templates_channel_supported"),
        ),
        sa.CheckConstraint(
            "(channel = 'WHATSAPP' AND template_name IS NOT NULL AND template_id IS NULL) OR "
            "(channel = 'SMS' AND template_id IS NOT NULL AND template_name IS NULL)",
            name=op.f("ck_notification_channel_templates_channel_template_fields_match"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notification_channel_templates_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notification_channel_templates_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notification_channel_templates_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channel_templates")),
        sa.UniqueConstraint(
            "company_id",
            "channel",
            "notification_type",
            name="uq_notification_channel_template_key",
        ),
    )
    op.create_index(
        op.f("ix_notification_channel_templates_company_id"),
        "notification_channel_templates",
        ["company_id"],
    )
    op.create_index(
        op.f("ix_notification_channel_templates_created_at"),
        "notification_channel_templates",
        ["created_at"],
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('WHATSAPP', 'SMS')",
            name=op.f("ck_notification_deliveries_channel_supported"),
        ),
        sa.CheckConstraint(
            "outcome IN ('DELIVERED', 'FAILED', 'RETRY')",
            name=op.f("ck_notification_deliveries_outcome_known"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notification_deliveries_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notification_deliveries_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_deliveries_notification",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        "ix_notification_deliveries_notification",
        "notification_deliveries",
        ["company_id", "notification_id"],
    )
    op.create_index(
        op.f("ix_notification_deliveries_company_id"),
        "notification_deliveries",
        ["company_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_notification_deliveries_immutable "
        "BEFORE DELETE OR UPDATE ON notification_deliveries "
        "FOR EACH ROW EXECUTE FUNCTION public.atcraft_reject_row_mutation()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    has_delivery = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM notification_deliveries)")
    )
    has_templates = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM notification_channel_templates)")
    )
    has_consent = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM users WHERE phone_e164 IS NOT NULL "
            "OR phone_verified_at IS NOT NULL OR whatsapp_opt_in_at IS NOT NULL "
            "OR sms_opt_in_at IS NOT NULL)"
        )
    )
    has_challenges = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM user_phone_verification_challenges)")
    )
    has_channel_preferences = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM notification_preferences "
            "WHERE whatsapp_enabled OR sms_enabled)"
        )
    )
    has_channel_notifications = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM notifications "
            "WHERE 'WHATSAPP' = ANY(channels) OR 'SMS' = ANY(channels) "
            "OR whatsapp_delivered_at IS NOT NULL OR sms_delivered_at IS NOT NULL "
            "OR template_data IS NOT NULL)"
        )
    )
    if (
        has_delivery
        or has_templates
        or has_consent
        or has_challenges
        or has_channel_preferences
        or has_channel_notifications
    ):
        raise RuntimeError(
            "Cannot remove messaging settings or delivery evidence while rows exist."
        )

    op.execute("DROP TRIGGER trg_notification_deliveries_immutable ON notification_deliveries")
    op.drop_index(
        op.f("ix_notification_deliveries_company_id"),
        table_name="notification_deliveries",
    )
    op.drop_index("ix_notification_deliveries_notification", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index(
        op.f("ix_notification_channel_templates_created_at"),
        table_name="notification_channel_templates",
    )
    op.drop_index(
        op.f("ix_notification_channel_templates_company_id"),
        table_name="notification_channel_templates",
    )
    op.drop_table("notification_channel_templates")
    op.drop_index(
        op.f("ix_user_phone_verification_challenges_created_at"),
        table_name="user_phone_verification_challenges",
    )
    op.drop_index(
        op.f("ix_user_phone_verification_challenges_company_id"),
        table_name="user_phone_verification_challenges",
    )
    op.drop_index("ix_phone_verification_pending", table_name="user_phone_verification_challenges")
    op.drop_table("user_phone_verification_challenges")
    op.drop_index("ix_notifications_pending_sms", table_name="notifications")
    op.drop_index("ix_notifications_pending_whatsapp", table_name="notifications")
    op.drop_column("notifications", "sms_delivered_at")
    op.drop_column("notifications", "whatsapp_delivered_at")
    op.drop_column("notifications", "template_data")
    op.drop_constraint(op.f("ck_users_phone_e164_format"), "users", type_="check")
    op.drop_column("notification_preferences", "sms_enabled")
    op.drop_column("notification_preferences", "whatsapp_enabled")
    op.drop_column("users", "sms_opt_in_at")
    op.drop_column("users", "whatsapp_opt_in_at")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "phone_e164")
