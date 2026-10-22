"""Net issued job-work returns as dated MRP supply.

Revision ID: 20261026_0100
Revises: 20261025_0100
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261026_0100"
down_revision = "20261025_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mrp_requirements",
        sa.Column(
            "job_work_quantity",
            sa.Numeric(18, 4),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "mrp_requirements",
        sa.Column(
            "late_job_work_quantity",
            sa.Numeric(18, 4),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "mrp_requirements", sa.Column("first_late_job_work_date", sa.Date(), nullable=True)
    )
    op.add_column(
        "mrp_requirements", sa.Column("last_late_job_work_date", sa.Date(), nullable=True)
    )

    op.drop_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        type_="check",
    )
    op.drop_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        type_="check",
    )
    op.create_check_constraint(
        "job_work_non_negative",
        "mrp_requirements",
        "job_work_quantity >= 0",
    )
    op.create_check_constraint(
        "late_job_work_non_negative",
        "mrp_requirements",
        "late_job_work_quantity >= 0",
    )
    op.create_check_constraint(
        "late_job_work_arrives_after_required",
        "mrp_requirements",
        "first_late_job_work_date IS NULL OR first_late_job_work_date > earliest_required_date",
    )
    op.create_check_constraint(
        "late_job_work_interval_ordered",
        "mrp_requirements",
        "last_late_job_work_date IS NULL "
        "OR (first_late_job_work_date IS NOT NULL "
        "AND last_late_job_work_date >= first_late_job_work_date)",
    )
    op.create_check_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        "(first_late_incoming_date IS NULL "
        "AND first_late_in_production_date IS NULL "
        "AND first_late_job_work_date IS NULL) "
        "OR first_late_supply_date = LEAST("
        "first_late_incoming_date, first_late_in_production_date, "
        "first_late_job_work_date)",
    )
    op.create_check_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        "late_incoming_quantity + late_in_production_quantity + late_job_work_quantity = 0 "
        "OR first_late_supply_date IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE mrp_requirements "
        "SET first_late_supply_date = CASE "
        "WHEN first_late_incoming_date IS NULL "
        "AND first_late_in_production_date IS NULL THEN NULL "
        "ELSE LEAST(first_late_incoming_date, first_late_in_production_date) END"
    )
    op.drop_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        type_="check",
    )
    op.drop_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        type_="check",
    )
    for constraint in (
        "late_job_work_interval_ordered",
        "late_job_work_arrives_after_required",
        "late_job_work_non_negative",
        "job_work_non_negative",
    ):
        op.drop_constraint(constraint, "mrp_requirements", type_="check")
    op.create_check_constraint(
        "first_late_supply_is_the_earlier",
        "mrp_requirements",
        "(first_late_incoming_date IS NULL AND first_late_in_production_date IS NULL) "
        "OR first_late_supply_date = LEAST("
        "first_late_incoming_date, first_late_in_production_date)",
    )
    op.create_check_constraint(
        "late_quantity_names_its_date",
        "mrp_requirements",
        "late_incoming_quantity + late_in_production_quantity = 0 "
        "OR first_late_supply_date IS NOT NULL",
    )
    op.drop_column("mrp_requirements", "last_late_job_work_date")
    op.drop_column("mrp_requirements", "first_late_job_work_date")
    op.drop_column("mrp_requirements", "late_job_work_quantity")
    op.drop_column("mrp_requirements", "job_work_quantity")
