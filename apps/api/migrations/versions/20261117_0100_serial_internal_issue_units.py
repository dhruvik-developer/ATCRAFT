"""name serial-tracked units on internal issues

Revision ID: 20261117_0100
Revises: 20261116_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import src.core.types

revision: str = "20261117_0100"
down_revision: str | None = "20261116_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SERIALS_PER_INTERNAL_ISSUE_LINE = 500


def upgrade() -> None:
    op.add_column(
        "internal_issue_lines",
        sa.Column(
            "serial_ids",
            postgresql.ARRAY(src.core.types.GUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_internal_issue_lines_serial_count_bounded"),
        "internal_issue_lines",
        f"cardinality(serial_ids) <= {MAX_SERIALS_PER_INTERNAL_ISSUE_LINE}",
    )
    op.create_check_constraint(
        op.f("ck_internal_issue_lines_serial_count_matches_quantity"),
        "internal_issue_lines",
        "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = quantity",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_internal_issue_lines_serial_count_matches_quantity"),
        "internal_issue_lines",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_internal_issue_lines_serial_count_bounded"),
        "internal_issue_lines",
        type_="check",
    )
    op.drop_column("internal_issue_lines", "serial_ids")
