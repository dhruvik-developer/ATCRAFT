"""Capture serials returned against dispatched serialized goods."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261017_0110"
down_revision = "20261017_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales_return_lines",
        sa.Column(
            "serial_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "sales_return_line_serial_count_bounded",
        "sales_return_lines",
        "cardinality(serial_ids) <= quantity",
    )


def downgrade() -> None:
    op.drop_constraint(
        "sales_return_line_serial_count_bounded",
        "sales_return_lines",
        type_="check",
    )
    op.drop_column("sales_return_lines", "serial_ids")
