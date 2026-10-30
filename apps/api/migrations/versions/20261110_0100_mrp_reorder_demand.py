"""Allow reorder-point lines in MRP demand snapshots.

Revision ID: 20261110_0100
Revises: 20261109_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261110_0100"
down_revision = "20261109_0100"
branch_labels = None
depends_on = None

_DEMAND_SOURCE_CHECK = "ck_mrp_demands_demand_source_known"


def _replace_demand_source_check(values: str) -> None:
    op.drop_constraint(op.f(_DEMAND_SOURCE_CHECK), "mrp_demands", type_="check")
    op.execute(
        "ALTER TABLE mrp_demands ADD CONSTRAINT "
        f"{op.f(_DEMAND_SOURCE_CHECK)} CHECK (demand_source IN ({values})) NOT VALID"
    )
    op.execute(f'ALTER TABLE mrp_demands VALIDATE CONSTRAINT "{op.f(_DEMAND_SOURCE_CHECK)}"')


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace_demand_source_check("'SALES_ORDER', 'MANUAL', 'REORDER'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    has_reorder_demands = (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM mrp_demands WHERE demand_source = 'REORDER')")
        )
        .scalar_one()
    )
    if has_reorder_demands:
        raise RuntimeError(
            "Cannot remove REORDER demand support while mrp_demands contains REORDER rows."
        )
    _replace_demand_source_check("'SALES_ORDER', 'MANUAL'")
