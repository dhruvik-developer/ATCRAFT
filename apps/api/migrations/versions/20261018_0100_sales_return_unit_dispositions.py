"""Persist serial-level customer-return QC decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261018_0100"
down_revision = "20261017_0120"
branch_labels = None
depends_on = None


_EMPTY_UUID_ARRAY = sa.text("'{}'::uuid[]")


def upgrade() -> None:
    for column in ("accepted_serial_ids", "damaged_serial_ids", "scrapped_serial_ids"):
        op.add_column(
            "sales_return_lines",
            sa.Column(
                column,
                postgresql.ARRAY(sa.UUID()),
                server_default=_EMPTY_UUID_ARRAY,
                nullable=False,
            ),
        )
    # ASSUMPTION: prior no-QC receipts accepted units; see docs/assumptions.md A3-1.
    op.execute(
        """
        UPDATE sales_return_lines AS line
        SET accepted_serial_ids = line.serial_ids
        FROM sales_returns AS header
        WHERE header.id = line.sales_return_id
          AND header.status IN ('RECEIVED', 'CLOSED')
          AND line.requires_qc = false
          AND cardinality(line.serial_ids) > 0
        """
    )
    op.create_check_constraint(
        "serial_dispositions_valid",
        "sales_return_lines",
        "accepted_serial_ids <@ serial_ids "
        "AND damaged_serial_ids <@ serial_ids "
        "AND scrapped_serial_ids <@ serial_ids "
        "AND NOT (accepted_serial_ids && damaged_serial_ids) "
        "AND NOT (accepted_serial_ids && scrapped_serial_ids) "
        "AND NOT (damaged_serial_ids && scrapped_serial_ids) "
        "AND cardinality(accepted_serial_ids) + cardinality(damaged_serial_ids) "
        "+ cardinality(scrapped_serial_ids) <= cardinality(serial_ids)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "serial_dispositions_valid",
        "sales_return_lines",
        type_="check",
    )
    for column in ("scrapped_serial_ids", "damaged_serial_ids", "accepted_serial_ids"):
        op.drop_column("sales_return_lines", column)
