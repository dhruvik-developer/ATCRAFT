"""name serial-tracked units on bin moves

Revision ID: 20261116_0100
Revises: 20261115_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import src.core.types

revision: str = "20261116_0100"
down_revision: str | None = "20261115_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SERIALS_PER_BIN_MOVE_LINE = 500


def upgrade() -> None:
    op.add_column(
        "bin_move_lines",
        sa.Column(
            "serial_ids",
            postgresql.ARRAY(src.core.types.GUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_bin_move_lines_serial_count_bounded"),
        "bin_move_lines",
        f"cardinality(serial_ids) <= {MAX_SERIALS_PER_BIN_MOVE_LINE}",
    )
    op.create_check_constraint(
        op.f("ck_bin_move_lines_serial_count_matches_quantity"),
        "bin_move_lines",
        "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = quantity",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_bin_move_lines_serial_count_matches_quantity"),
        "bin_move_lines",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_bin_move_lines_serial_count_bounded"),
        "bin_move_lines",
        type_="check",
    )
    op.drop_column("bin_move_lines", "serial_ids")
