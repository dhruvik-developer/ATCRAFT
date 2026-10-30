"""Persist MRP assembly supply and the BOM snapshot for assembly conversion.

Revision ID: 20261108_0100
Revises: 20261107_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261108_0100"
down_revision = "20261107_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "mrp_requirements",
        sa.Column("assembly_quantity", sa.Numeric(18, 4), nullable=False, server_default="0"),
    )
    op.add_column(
        "mrp_requirements",
        sa.Column(
            "late_in_assembly_quantity",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
    )
    for name in ("first_late_in_assembly_date", "last_late_in_assembly_date"):
        op.add_column("mrp_requirements", sa.Column(name, sa.Date(), nullable=True))
    op.add_column("mrp_suggestions", sa.Column("bom_id", GUID, nullable=True))

    op.create_check_constraint(
        "assembly_non_negative", "mrp_requirements", "assembly_quantity >= 0"
    )
    op.create_check_constraint(
        "late_in_assembly_non_negative",
        "mrp_requirements",
        "late_in_assembly_quantity >= 0",
    )
    op.create_check_constraint(
        "late_in_assembly_arrives_after_required",
        "mrp_requirements",
        "first_late_in_assembly_date IS NULL "
        "OR first_late_in_assembly_date > earliest_required_date",
    )
    op.drop_constraint("first_late_supply_is_the_earlier", "mrp_requirements", type_="check")
    op.create_check_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        "(first_late_incoming_date IS NULL "
        "AND first_late_in_production_date IS NULL "
        "AND first_late_in_assembly_date IS NULL "
        "AND first_late_job_work_date IS NULL) "
        "OR first_late_supply_date = LEAST(first_late_incoming_date, "
        "first_late_in_production_date, first_late_in_assembly_date, "
        "first_late_job_work_date)",
    )
    op.drop_constraint("late_quantity_names_its_date", "mrp_requirements", type_="check")
    op.create_check_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        "late_incoming_quantity + late_in_production_quantity + "
        "late_in_assembly_quantity + late_job_work_quantity = 0 "
        "OR first_late_supply_date IS NOT NULL",
    )
    op.create_check_constraint(
        "late_assembly_interval_ordered",
        "mrp_requirements",
        "last_late_in_assembly_date IS NULL "
        "OR (first_late_in_assembly_date IS NOT NULL "
        "AND last_late_in_assembly_date >= first_late_in_assembly_date)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_assembly_data = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM mrp_requirements WHERE "
            "assembly_quantity > 0 OR late_in_assembly_quantity > 0 "
            "OR first_late_in_assembly_date IS NOT NULL "
            "OR last_late_in_assembly_date IS NOT NULL)"
        )
    ).scalar_one()
    has_bom_snapshots = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM mrp_suggestions WHERE bom_id IS NOT NULL)")
    ).scalar_one()
    if has_assembly_data or has_bom_snapshots:
        raise RuntimeError(
            "Cannot downgrade MRP assembly support while assembly supply or BOM snapshots exist."
        )

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("late_assembly_interval_ordered", "mrp_requirements", type_="check")
    op.drop_constraint("late_quantity_names_its_date", "mrp_requirements", type_="check")
    op.drop_constraint("first_late_supply_is_the_earlier", "mrp_requirements", type_="check")
    op.create_check_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        "(first_late_incoming_date IS NULL "
        "AND first_late_in_production_date IS NULL "
        "AND first_late_job_work_date IS NULL) "
        "OR first_late_supply_date = LEAST(first_late_incoming_date, "
        "first_late_in_production_date, first_late_job_work_date)",
    )
    op.create_check_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        "late_incoming_quantity + late_in_production_quantity + "
        "late_job_work_quantity = 0 OR first_late_supply_date IS NOT NULL",
    )
    op.drop_constraint("late_in_assembly_arrives_after_required", "mrp_requirements", type_="check")
    op.drop_constraint("late_in_assembly_non_negative", "mrp_requirements", type_="check")
    op.drop_constraint("assembly_non_negative", "mrp_requirements", type_="check")
    op.drop_column("mrp_suggestions", "bom_id")
    op.drop_column("mrp_requirements", "last_late_in_assembly_date")
    op.drop_column("mrp_requirements", "first_late_in_assembly_date")
    op.drop_column("mrp_requirements", "late_in_assembly_quantity")
    op.drop_column("mrp_requirements", "assembly_quantity")
