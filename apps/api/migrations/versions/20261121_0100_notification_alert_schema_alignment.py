"""align smart-alert foreign keys and resolved-state index

Revision ID: 20261121_0100
Revises: 20261120_0100
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261121_0100"
down_revision: str | None = "20261120_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FOREIGN_KEYS = (
    (
        "notification_alert_rules",
        "fk_notification_alert_rules_company_id_companies",
        "company_id",
        "companies",
        "RESTRICT",
    ),
    (
        "notification_alert_rules",
        "fk_notification_alert_rules_created_by_id_users",
        "created_by_id",
        "users",
        "SET NULL",
    ),
    (
        "notification_alert_rules",
        "fk_notification_alert_rules_updated_by_id_users",
        "updated_by_id",
        "users",
        "SET NULL",
    ),
    (
        "notification_alert_states",
        "fk_notification_alert_states_company_id_companies",
        "company_id",
        "companies",
        "RESTRICT",
    ),
    (
        "notification_alert_states",
        "fk_notification_alert_states_created_by_id_users",
        "created_by_id",
        "users",
        "SET NULL",
    ),
    (
        "notification_alert_states",
        "fk_notification_alert_states_updated_by_id_users",
        "updated_by_id",
        "users",
        "SET NULL",
    ),
)


def upgrade() -> None:
    for table, name, column, target_table, ondelete in _FOREIGN_KEYS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            target_table,
            [column],
            ["id"],
            ondelete=ondelete,
        )

    op.drop_index(
        "ix_notification_alert_states_company_id_resolved_at_las_01f2",
        table_name="notification_alert_states",
    )
    op.create_index(
        "ix_notification_alert_states_resolved",
        "notification_alert_states",
        ["company_id", "resolved_at", "last_detected_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_alert_states_resolved",
        table_name="notification_alert_states",
    )
    op.create_index(
        op.f("ix_notification_alert_states_company_id_resolved_at_last_detected_at"),
        "notification_alert_states",
        ["company_id", "resolved_at", "last_detected_at"],
    )

    for table, name, column, target_table, _ondelete in reversed(_FOREIGN_KEYS):
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, target_table, [column], ["id"])
