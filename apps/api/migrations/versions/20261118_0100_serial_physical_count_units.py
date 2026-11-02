"""reconcile physical counts by serial identity

Revision ID: 20261118_0100
Revises: 20261117_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import src.core.types

revision: str = "20261118_0100"
down_revision: str | None = "20261117_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SERIALS_PER_PHYSICAL_COUNT_LINE = 500


def upgrade() -> None:
    op.add_column(
        "physical_count_lines",
        sa.Column(
            "counted_serial_ids",
            postgresql.ARRAY(src.core.types.GUID()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_physical_count_lines_counted_serials_bounded"),
        "physical_count_lines",
        "counted_serial_ids IS NULL OR "
        f"cardinality(counted_serial_ids) <= {MAX_SERIALS_PER_PHYSICAL_COUNT_LINE}",
    )
    op.create_check_constraint(
        op.f("ck_physical_count_lines_counted_serials_match_quantity"),
        "physical_count_lines",
        "counted_serial_ids IS NULL OR "
        "(counted_quantity IS NOT NULL AND "
        "cardinality(counted_serial_ids) = counted_quantity)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_physical_count_lines_counted_serials_match_quantity"),
        "physical_count_lines",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_physical_count_lines_counted_serials_bounded"),
        "physical_count_lines",
        type_="check",
    )
    op.drop_column("physical_count_lines", "counted_serial_ids")
