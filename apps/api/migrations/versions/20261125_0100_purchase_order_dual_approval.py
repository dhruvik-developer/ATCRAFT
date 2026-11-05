"""Record the first of two approvals for a purchase order above the dual-approval limit.

Revision ID: 20261125_0100
Revises: 20261124_0100

The company approval settings already name the amount above which a purchase
order needs a second approver. This adds the evidence for the first signature:
the order stays PENDING_APPROVAL until a different user completes the approval,
so the pair (who approved first, when) has to live on the document itself.

Columns are nullable and start empty: every existing order keeps its current
state, and an order that never crossed the threshold simply leaves them NULL.
The foreign key is ``ON DELETE SET NULL`` like every other actor reference, but
the check constraint keeps the user and the timestamp together; deleting an
approver therefore nulls only the user and would violate the pair. That is the
same tension the existing ``approved_by_id`` pair has, and is handled the same
way: approval evidence is retained rather than silently halved.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261125_0100"
down_revision = "20261124_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "purchase_orders",
        sa.Column("first_approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "purchase_orders",
        sa.Column("first_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_purchase_orders_first_approved_by_id_users"),
        "purchase_orders",
        "users",
        ["first_approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_purchase_orders_first_approval_evidence_complete"),
        "purchase_orders",
        "(first_approved_by_id IS NULL) = (first_approved_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_purchase_orders_first_approval_evidence_complete"),
        "purchase_orders",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_purchase_orders_first_approved_by_id_users"),
        "purchase_orders",
        type_="foreignkey",
    )
    op.drop_column("purchase_orders", "first_approved_at")
    op.drop_column("purchase_orders", "first_approved_by_id")
