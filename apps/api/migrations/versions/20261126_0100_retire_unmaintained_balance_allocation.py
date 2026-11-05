"""Remove the unmaintained inventory-balance allocation figure.

Revision ID: 20261126_0100
Revises: 20261125_0100

The application has no generic, per-position pick/commit operation. The
balance-level column therefore always remained zero and was misleading on the
inventory screen. Sales-order-line allocation is a separate, maintained
quantity and is not changed here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261126_0100"
down_revision = "20261125_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        op.f("ck_inventory_balances_allocated_non_negative"),
        "inventory_balances",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_balances_allocated_within_reserved"),
        "inventory_balances",
        type_="check",
    )
    op.drop_column("inventory_balances", "allocated_quantity")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "inventory_balances",
        sa.Column(
            "allocated_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_inventory_balances_allocated_non_negative"),
        "inventory_balances",
        "allocated_quantity >= 0",
    )
    op.create_check_constraint(
        op.f("ck_inventory_balances_allocated_within_reserved"),
        "inventory_balances",
        "allocated_quantity <= reserved_quantity",
    )
