"""Link received customer-return lines to quality inspections."""

import sqlalchemy as sa
from alembic import op

revision = "20261017_0100"
down_revision = "20261016_1410"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales_return_lines",
        sa.Column("qc_inspection_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_sales_return_lines_qc_inspection",
        "sales_return_lines",
        "qc_inspections",
        ["qc_inspection_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_sales_return_lines_qc_inspection",
        "sales_return_lines",
        type_="foreignkey",
    )
    op.drop_column("sales_return_lines", "qc_inspection_id")
