"""Web Push opt-in and notification recovery.

Revision ID: c1bpush2026
Revises: 4480835a545e

Nullable marker and constant preference default require brief metadata locks,
without a table rewrite on supported PostgreSQL. Recovery indexes scan existing
notifications: schedule at low load. Downgrade removes device registrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1bpush2026"
down_revision: str | None = "4480835a545e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("push_delivered_at", sa.DateTime(timezone=True)))
    op.add_column(
        "notification_preferences",
        sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    for channel in ("push", "email"):
        op.create_index(
            f"ix_notifications_pending_{channel}",
            "notifications",
            ["created_at"],
            postgresql_where=sa.text(
                f"{channel}_delivered_at IS NULL AND '{channel.upper()}' = ANY(channels)"
            ),
        )
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("companies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("endpoint_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("endpoint_ciphertext", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(128), nullable=False),
        sa.Column("auth", sa.String(32), nullable=False),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("device_label", sa.String(120)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
    )
    op.create_index("ix_push_subscriptions_company_id", "push_subscriptions", ["company_id"])
    op.create_index("ix_push_subscriptions_created_at", "push_subscriptions", ["created_at"])
    op.create_index("ix_push_subscriptions_owner", "push_subscriptions", ["company_id", "user_id"])


def downgrade() -> None:
    op.drop_table("push_subscriptions")
    for channel in ("push", "email"):
        op.drop_index(f"ix_notifications_pending_{channel}", table_name="notifications")
    op.drop_column("notification_preferences", "push_enabled")
    op.drop_column("notifications", "push_delivered_at")
