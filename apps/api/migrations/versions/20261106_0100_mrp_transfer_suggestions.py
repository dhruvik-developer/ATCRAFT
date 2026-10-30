"""Persist MRP transfer routes and allow multiple route suggestions per item.

Revision ID: 20261106_0100
Revises: 20261105_0100
Create Date: 2026-11-06

The shared item-level requirement remains unique. Action rows differ: one
ordinary supply suggestion per item, plus one transfer suggestion per distinct
source/destination route.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261106_0100"
down_revision = "20261105_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("mrp_suggestions", sa.Column("from_warehouse_id", GUID, nullable=True))
    op.add_column("mrp_suggestions", sa.Column("to_warehouse_id", GUID, nullable=True))
    op.create_foreign_key(
        "fk_mrp_suggestions_from_warehouse_id_warehouses",
        "mrp_suggestions",
        "warehouses",
        ["from_warehouse_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_mrp_suggestions_to_warehouse_id_warehouses",
        "mrp_suggestions",
        "warehouses",
        ["to_warehouse_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_mrp_suggestions_run_id_item_id", "mrp_suggestions", type_="unique")
    op.create_index(
        "uq_mrp_suggestions_supply_run_item",
        "mrp_suggestions",
        ["run_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK')"),
    )
    op.create_index(
        "uq_mrp_suggestions_transfer_route",
        "mrp_suggestions",
        ["run_id", "item_id", "from_warehouse_id", "to_warehouse_id"],
        unique=True,
        postgresql_where=sa.text("suggestion_type = 'TRANSFER'"),
    )
    op.create_check_constraint(
        "transfer_route_matches_type",
        "mrp_suggestions",
        "(suggestion_type = 'TRANSFER' AND from_warehouse_id IS NOT NULL "
        "AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id) "
        "OR (suggestion_type <> 'TRANSFER' AND from_warehouse_id IS NULL "
        "AND to_warehouse_id IS NULL)",
    )
    op.drop_constraint("source_type_known", "stock_transfers", type_="check")
    op.create_check_constraint(
        "source_type_known",
        "stock_transfers",
        "source_type IN ('MANUAL', 'SALES_ORDER', 'REALLOCATION', 'MRP_SUGGESTION')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_transfer_suggestions = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM mrp_suggestions WHERE suggestion_type = 'TRANSFER')")
    ).scalar_one()
    has_mrp_transfers = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM stock_transfers WHERE source_type = 'MRP_SUGGESTION')"
        )
    ).scalar_one()
    duplicates = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM mrp_suggestions "
            "GROUP BY run_id, item_id HAVING count(*) > 1)"
        )
    ).scalar_one()
    if has_transfer_suggestions or has_mrp_transfers or duplicates:
        raise RuntimeError(
            "Cannot downgrade MRP transfer suggestions while transfer routes, "
            "MRP-originated transfers, or multiple suggestions for an item exist."
        )

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("source_type_known", "stock_transfers", type_="check")
    op.create_check_constraint(
        "source_type_known",
        "stock_transfers",
        "source_type IN ('MANUAL', 'SALES_ORDER', 'REALLOCATION')",
    )
    op.drop_constraint("transfer_route_matches_type", "mrp_suggestions", type_="check")
    op.drop_index("uq_mrp_suggestions_transfer_route", table_name="mrp_suggestions")
    op.drop_index("uq_mrp_suggestions_supply_run_item", table_name="mrp_suggestions")
    op.create_unique_constraint(
        "uq_mrp_suggestions_run_id_item_id", "mrp_suggestions", ["run_id", "item_id"]
    )
    op.drop_constraint(
        "fk_mrp_suggestions_to_warehouse_id_warehouses", "mrp_suggestions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_mrp_suggestions_from_warehouse_id_warehouses", "mrp_suggestions", type_="foreignkey"
    )
    op.drop_column("mrp_suggestions", "to_warehouse_id")
    op.drop_column("mrp_suggestions", "from_warehouse_id")
