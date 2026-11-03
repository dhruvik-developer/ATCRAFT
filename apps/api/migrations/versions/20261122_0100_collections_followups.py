"""create the payment follow-up register and grant collection permissions

Revision ID: 20261122_0100
Revises: 20261121_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import src.core.types

revision: str = "20261122_0100"
down_revision: str | None = "20261121_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLLECTION_PERMISSIONS = ("collection.view", "collection.manage")
COLLECTION_ROLE_CODES = ("SALES_EXECUTIVE", "ACCOUNTS")
REPORT_SNAPSHOT_KEY_CONSTRAINT = "ck_report_snapshots_report_key_is_known"
REPORT_SNAPSHOT_KEYS = (
    "SALES_ORDER_STATUS",
    "STOCK_AVAILABILITY",
    "MRP_SHORTAGE",
    "PURCHASE_PENDING",
    "PRODUCTION_PENDING",
    "JOB_WORK_PENDING",
    "QC_REJECTION",
    "VENDOR_PERFORMANCE",
    "DEALER_PERFORMANCE",
    "PRODUCT_PROFITABILITY",
    "FAST_MOVING",
    "SLOW_MOVING",
    "DEAD_STOCK",
    "ORDER_DELAY",
    "PRODUCTION_EFFICIENCY",
    "MACHINE_UTILISATION",
    "SHIFT_PRODUCTION",
    "INVENTORY_VALUATION",
    "DISPATCH",
    "COURIER_TRACKING",
    "VENDOR_DISPATCH_HISTORY",
    "COST_CENTRE_EXPENSE",
    "SCRAP_REGISTER",
    "CONSUMPTION_BY_DEPARTMENT",
    "SALES_REGISTER",
    "PURCHASE_REGISTER",
    "GSTR1",
    "TRIAL_BALANCE",
    "PURCHASE_ORDER",
    "PACKING_LIST",
    "SALES_INVOICE",
    "CREDIT_NOTE",
    "PURCHASE_RETURN_DEBIT_NOTE",
    "REWORK_RATE",
    "PRODUCTION_COST",
    "JOB_WORK_COST",
    "JOB_WORK_CHALLAN",
)


def _swap_report_snapshot_keys(keys: tuple[str, ...]) -> None:
    spelled = ", ".join(f"'{key}'" for key in keys)
    op.drop_constraint(op.f(REPORT_SNAPSHOT_KEY_CONSTRAINT), "report_snapshots", type_="check")
    op.execute(
        sa.text(
            f"ALTER TABLE report_snapshots ADD CONSTRAINT "
            f"{REPORT_SNAPSHOT_KEY_CONSTRAINT} CHECK (report_key IN ({spelled})) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE report_snapshots VALIDATE CONSTRAINT {REPORT_SNAPSHOT_KEY_CONSTRAINT}"
        )
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap_report_snapshot_keys(REPORT_SNAPSHOT_KEYS)
    op.create_table(
        "payment_follow_ups",
        sa.Column("id", src.core.types.GUID(), nullable=False),
        sa.Column("company_id", src.core.types.GUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("customer_id", src.core.types.GUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="OPEN", nullable=False),
        sa.Column("assigned_to_id", src.core.types.GUID(), nullable=True),
        sa.Column("opened_overdue_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("current_overdue_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("oldest_due_date", sa.Date(), nullable=False),
        sa.Column("next_action_date", sa.Date(), nullable=True),
        sa.Column("promised_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("promised_date", sa.Date(), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", src.core.types.GUID(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
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
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_follow_ups")),
        sa.CheckConstraint(
            "status IN ('OPEN', 'PROMISED', 'ESCALATED', 'RESOLVED', 'CANCELLED')",
            name=op.f("ck_payment_follow_ups_status_known"),
        ),
        sa.CheckConstraint(
            "(promised_amount IS NULL) = (promised_date IS NULL)",
            name=op.f("ck_payment_follow_ups_promise_complete"),
        ),
        sa.CheckConstraint(
            "promised_amount IS NULL OR promised_amount > 0",
            name=op.f("ck_payment_follow_ups_promised_amount_positive"),
        ),
        sa.CheckConstraint(
            "opened_overdue_amount >= 0",
            name=op.f("ck_payment_follow_ups_opened_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "current_overdue_amount >= 0",
            name=op.f("ck_payment_follow_ups_current_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "status <> 'RESOLVED' OR resolved_at IS NOT NULL",
            name=op.f("ck_payment_follow_ups_resolved_has_timestamp"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_payment_follow_ups_cancelled_has_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'ESCALATED' OR escalated_at IS NOT NULL",
            name=op.f("ck_payment_follow_ups_escalated_has_timestamp"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_payment_follow_ups_currency_is_iso_code"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_payment_follow_ups_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_payment_follow_ups_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name=op.f("fk_payment_follow_ups_assigned_to_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["users.id"],
            name=op.f("fk_payment_follow_ups_resolved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_payment_follow_ups_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_payment_follow_ups_updated_by_id_users"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(op.f("ix_payment_follow_ups_company_id"), "payment_follow_ups", ["company_id"])
    op.create_index(op.f("ix_payment_follow_ups_created_at"), "payment_follow_ups", ["created_at"])
    op.create_index(
        "ix_payment_follow_ups_status_due",
        "payment_follow_ups",
        ["company_id", "status", "next_action_date"],
    )
    op.create_index(
        "uq_payment_follow_ups_live_party",
        "payment_follow_ups",
        ["company_id", "customer_id", "currency"],
        unique=True,
        postgresql_where=sa.text("status IN ('ESCALATED', 'OPEN', 'PROMISED')"),
    )
    op.create_index(
        "ix_payment_follow_ups_assignee",
        "payment_follow_ups",
        ["company_id", "assigned_to_id", "status"],
    )
    op.create_index(
        "ix_payment_follow_ups_customer",
        "payment_follow_ups",
        ["company_id", "customer_id", "created_at"],
    )

    op.create_table(
        "payment_follow_up_notes",
        sa.Column("id", src.core.types.GUID(), nullable=False),
        sa.Column("company_id", src.core.types.GUID(), nullable=False),
        sa.Column("follow_up_id", src.core.types.GUID(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("next_action_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", src.core.types.GUID(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_follow_up_notes")),
        sa.CheckConstraint(
            "channel IN ('CALL', 'EMAIL', 'VISIT', 'MESSAGE', 'SYSTEM')",
            name=op.f("ck_payment_follow_up_notes_channel_known"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('PROMISED', 'DISPUTED', 'NO_RESPONSE', 'PARTIAL_PAYMENT', 'OTHER')",
            name=op.f("ck_payment_follow_up_notes_outcome_known"),
        ),
        sa.CheckConstraint(
            "length(btrim(note)) > 0",
            name=op.f("ck_payment_follow_up_notes_note_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_payment_follow_up_notes_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["follow_up_id"],
            ["payment_follow_ups.id"],
            name=op.f("fk_payment_follow_up_notes_follow_up_id_payment_follow_ups"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_payment_follow_up_notes_created_by_id_users"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        op.f("ix_payment_follow_up_notes_company_id"),
        "payment_follow_up_notes",
        ["company_id"],
    )
    op.create_index(
        "ix_payment_follow_up_notes_timeline",
        "payment_follow_up_notes",
        ["follow_up_id", "created_at", "id"],
    )
    op.execute(
        "CREATE TRIGGER trg_payment_follow_up_notes_immutable "
        "BEFORE DELETE OR UPDATE ON public.payment_follow_up_notes "
        "FOR EACH ROW EXECUTE FUNCTION public.atcraft_reject_row_mutation()"
    )

    for permission in COLLECTION_PERMISSIONS:
        op.execute(
            sa.text(
                """
                UPDATE roles
                   SET permissions = array_append(permissions, :permission),
                       updated_at = now()
                 WHERE code = ANY(:codes)
                   AND is_vendor_role IS FALSE
                   AND is_dealer_role IS FALSE
                   AND NOT (:permission = ANY(permissions))
                """
            ).bindparams(
                sa.bindparam("permission", permission, type_=sa.String()),
                sa.bindparam("codes", list(COLLECTION_ROLE_CODES), type_=sa.ARRAY(sa.String())),
            )
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM report_snapshots WHERE report_key = 'DEALER_PERFORMANCE'")
    _swap_report_snapshot_keys(
        tuple(key for key in REPORT_SNAPSHOT_KEYS if key != "DEALER_PERFORMANCE")
    )
    for permission in COLLECTION_PERMISSIONS:
        op.execute(
            sa.text(
                """
                UPDATE roles
                   SET permissions = array_remove(permissions, :permission),
                       updated_at = now()
                 WHERE :permission = ANY(permissions)
                """
            ).bindparams(sa.bindparam("permission", permission, type_=sa.String()))
        )
    op.execute(
        "DROP TRIGGER trg_payment_follow_up_notes_immutable ON public.payment_follow_up_notes"
    )
    op.drop_index("ix_payment_follow_up_notes_timeline", table_name="payment_follow_up_notes")
    op.drop_index(
        op.f("ix_payment_follow_up_notes_company_id"), table_name="payment_follow_up_notes"
    )
    op.drop_table("payment_follow_up_notes")
    op.drop_index("ix_payment_follow_ups_customer", table_name="payment_follow_ups")
    op.drop_index("ix_payment_follow_ups_assignee", table_name="payment_follow_ups")
    op.drop_index("uq_payment_follow_ups_live_party", table_name="payment_follow_ups")
    op.drop_index("ix_payment_follow_ups_status_due", table_name="payment_follow_ups")
    op.drop_index(op.f("ix_payment_follow_ups_created_at"), table_name="payment_follow_ups")
    op.drop_index(op.f("ix_payment_follow_ups_company_id"), table_name="payment_follow_ups")
    op.drop_table("payment_follow_ups")
