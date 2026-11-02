"""vendor job-work reporting and returned-reject rework linkage

Revision ID: 20261115_0100
Revises: 20261114_0200
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import src.core.types

revision: str = "20261115_0100"
down_revision: str | None = "20261114_0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_WORK_PORTAL_PERMISSION = "vendor_portal.job_work"
VENDOR_PORTAL_ROLES = ("VENDOR_OWNER", "VENDOR_MANAGER")


def upgrade() -> None:
    op.add_column(
        "job_work_orders",
        sa.Column("vendor_overdue_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_job_work_orders_overdue_notice",
        "job_work_orders",
        ["expected_return_date", "id"],
        unique=False,
        postgresql_where=sa.text(
            "vendor_overdue_notified_at IS NULL AND status IN ('ISSUED', 'PARTIALLY_RECEIVED')"
        ),
    )
    op.add_column(
        "job_work_order_lines",
        sa.Column("source_receipt_line_id", src.core.types.GUID(), nullable=True),
    )
    op.create_index(
        "ix_job_work_order_lines_rework_source",
        "job_work_order_lines",
        ["company_id", "source_receipt_line_id"],
        unique=False,
        postgresql_where=sa.text("source_receipt_line_id IS NOT NULL"),
    )

    op.create_table(
        "job_work_vendor_updates",
        sa.Column("id", src.core.types.GUID(), nullable=False),
        sa.Column("company_id", src.core.types.GUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", src.core.types.GUID(), nullable=True),
        sa.Column("job_work_order_id", src.core.types.GUID(), nullable=False),
        sa.Column("vendor_id", src.core.types.GUID(), nullable=False),
        sa.Column("update_type", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_date", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "update_type IN ('IN_PROCESS', 'READY_TO_RETURN', 'DISPATCHED_BACK')",
            name=op.f("ck_job_work_vendor_updates_update_type_is_known"),
        ),
        sa.CheckConstraint(
            "length(btrim(note)) > 0 OR note IS NULL",
            name=op.f("ck_job_work_vendor_updates_note_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_vendor_updates_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_vendor_updates_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_id"],
            ["job_work_orders.id"],
            name=op.f("fk_job_work_vendor_updates_job_work_order_id_job_work_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_job_work_vendor_updates_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_vendor_updates")),
    )
    op.create_index(
        op.f("ix_job_work_vendor_updates_company_id"),
        "job_work_vendor_updates",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_vendor_updates_order",
        "job_work_vendor_updates",
        ["company_id", "job_work_order_id", "event_time", "id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_vendor_updates_vendor",
        "job_work_vendor_updates",
        ["company_id", "vendor_id", "event_time"],
        unique=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE roles
               SET permissions = array_append(permissions, :permission),
                   updated_at = now()
             WHERE code = ANY(:codes)
               AND is_vendor_role = true
               AND NOT (:permission = ANY(permissions))
            """
        ).bindparams(
            sa.bindparam("permission", JOB_WORK_PORTAL_PERMISSION, type_=sa.String()),
            sa.bindparam(
                "codes",
                list(VENDOR_PORTAL_ROLES),
                type_=sa.ARRAY(sa.String()),
            ),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE roles
               SET permissions = array_remove(permissions, :permission),
                   updated_at = now()
             WHERE :permission = ANY(permissions)
            """
        ).bindparams(sa.bindparam("permission", JOB_WORK_PORTAL_PERMISSION, type_=sa.String()))
    )
    op.drop_index("ix_job_work_vendor_updates_vendor", table_name="job_work_vendor_updates")
    op.drop_index("ix_job_work_vendor_updates_order", table_name="job_work_vendor_updates")
    op.drop_index(
        op.f("ix_job_work_vendor_updates_company_id"),
        table_name="job_work_vendor_updates",
    )
    op.drop_table("job_work_vendor_updates")
    op.drop_index(
        "ix_job_work_order_lines_rework_source",
        table_name="job_work_order_lines",
    )
    op.drop_column("job_work_order_lines", "source_receipt_line_id")
    op.drop_index(
        "ix_job_work_orders_overdue_notice",
        table_name="job_work_orders",
    )
    op.drop_column("job_work_orders", "vendor_overdue_notified_at")
