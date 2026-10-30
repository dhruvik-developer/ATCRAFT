"""Persist donor-to-demand MRP reallocation routes.

Revision ID: 20261107_0100
Revises: 20261106_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261107_0100"
down_revision = "20261106_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)


_ROUTE_CHECK = (
    "(suggestion_type = 'TRANSFER' AND from_warehouse_id IS NOT NULL "
    "AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id "
    "AND from_reservation_id IS NULL AND from_demand_type IS NULL "
    "AND from_demand_id IS NULL AND from_demand_code IS NULL "
    "AND to_demand_type IS NULL AND to_demand_id IS NULL "
    "AND to_demand_line_id IS NULL AND to_demand_code IS NULL "
    "AND to_demand_priority IS NULL) "
    "OR (suggestion_type = 'REALLOCATION' AND from_warehouse_id IS NOT NULL "
    "AND to_warehouse_id = from_warehouse_id AND from_reservation_id IS NOT NULL "
    "AND from_demand_type IN ('SALES_ORDER', 'PRODUCTION_ORDER', 'ASSEMBLY_ORDER', "
    "'JOB_WORK_ORDER', 'SPARE', 'TRANSFER') AND from_demand_id IS NOT NULL "
    "AND to_demand_type = 'SALES_ORDER' AND to_demand_id IS NOT NULL "
    "AND to_demand_line_id IS NOT NULL AND to_demand_priority IS NOT NULL "
    "AND to_demand_priority >= 0) "
    "OR (suggestion_type NOT IN ('TRANSFER', 'REALLOCATION') "
    "AND from_warehouse_id IS NULL AND to_warehouse_id IS NULL "
    "AND from_reservation_id IS NULL AND from_demand_type IS NULL "
    "AND from_demand_id IS NULL AND from_demand_code IS NULL "
    "AND to_demand_type IS NULL AND to_demand_id IS NULL "
    "AND to_demand_line_id IS NULL AND to_demand_code IS NULL "
    "AND to_demand_priority IS NULL)"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "mrp_requirements",
        sa.Column(
            "planned_reallocation_quantity",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "planned_reallocation_non_negative",
        "mrp_requirements",
        "planned_reallocation_quantity >= 0",
    )

    for name, column_type in (
        ("from_reservation_id", GUID),
        ("from_demand_type", sa.String(length=24)),
        ("from_demand_id", GUID),
        ("from_demand_code", sa.String(length=64)),
        ("to_demand_type", sa.String(length=24)),
        ("to_demand_id", GUID),
        ("to_demand_line_id", GUID),
        ("to_demand_code", sa.String(length=64)),
        ("to_demand_priority", sa.Integer()),
    ):
        op.add_column("mrp_suggestions", sa.Column(name, column_type, nullable=True))

    op.drop_constraint("suggestion_type_known", "mrp_suggestions", type_="check")
    op.create_check_constraint(
        "suggestion_type_known",
        "mrp_suggestions",
        "suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK', 'TRANSFER', 'REALLOCATION')",
    )
    op.drop_constraint("transfer_route_matches_type", "mrp_suggestions", type_="check")
    op.create_check_constraint("transfer_route_matches_type", "mrp_suggestions", _ROUTE_CHECK)
    op.create_index(
        "uq_mrp_suggestions_reallocation_route",
        "mrp_suggestions",
        [
            "run_id",
            "item_id",
            "from_reservation_id",
            "to_demand_id",
            "to_demand_line_id",
        ],
        unique=True,
        postgresql_where=sa.text("suggestion_type = 'REALLOCATION'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_reallocation_suggestions = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM mrp_suggestions WHERE suggestion_type = 'REALLOCATION')"
        )
    ).scalar_one()
    has_planned_reallocations = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM mrp_requirements WHERE planned_reallocation_quantity > 0)"
        )
    ).scalar_one()
    if has_reallocation_suggestions or has_planned_reallocations:
        raise RuntimeError(
            "Cannot downgrade MRP reallocation suggestions while reallocation routes "
            "or planned quantities exist."
        )

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("uq_mrp_suggestions_reallocation_route", table_name="mrp_suggestions")
    op.drop_constraint("transfer_route_matches_type", "mrp_suggestions", type_="check")
    op.drop_constraint("suggestion_type_known", "mrp_suggestions", type_="check")
    op.create_check_constraint(
        "suggestion_type_known",
        "mrp_suggestions",
        "suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK', 'TRANSFER')",
    )
    op.create_check_constraint(
        "transfer_route_matches_type",
        "mrp_suggestions",
        "(suggestion_type = 'TRANSFER' AND from_warehouse_id IS NOT NULL "
        "AND to_warehouse_id IS NOT NULL AND from_warehouse_id <> to_warehouse_id) "
        "OR (suggestion_type <> 'TRANSFER' AND from_warehouse_id IS NULL "
        "AND to_warehouse_id IS NULL)",
    )
    for name in (
        "to_demand_priority",
        "to_demand_code",
        "to_demand_line_id",
        "to_demand_id",
        "to_demand_type",
        "from_demand_code",
        "from_demand_id",
        "from_demand_type",
        "from_reservation_id",
    ):
        op.drop_column("mrp_suggestions", name)
    op.drop_constraint("planned_reallocation_non_negative", "mrp_requirements", type_="check")
    op.drop_column("mrp_requirements", "planned_reallocation_quantity")
