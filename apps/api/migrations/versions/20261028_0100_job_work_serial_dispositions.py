"""Record the named units issued to and dispositioned by job-work vendors.

Revision ID: 20261028_0100
Revises: 20261027_0100
Create Date: 2026-10-24

Constant defaults preserve existing untracked documents without a row rewrite.
Constraint validation scans the three document tables; release with writers
drained and a bounded lock wait. Downgrade refuses to discard serial evidence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261028_0100"
down_revision = "20261027_0100"
branch_labels = None
depends_on = None

_ARRAYS = {
    "job_work_order_lines": ("serial_ids",),
    "job_work_challan_lines": ("serial_ids",),
    "job_work_receipt_lines": (
        "output_serial_ids",
        "rejected_serial_ids",
        "returned_unprocessed_serial_ids",
        "scrapped_serial_ids",
    ),
    "qc_inspections": ("serial_ids",),
}
_JSON = {
    "job_work_receipt_lines": "converted_serial_ids",
    "qc_inspections": "serial_dispositions",
    "qc_inspection_decisions": "serial_dispositions",
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "job_work_order_lines",
        sa.Column("is_serial_tracked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for table, columns in _ARRAYS.items():
        for column in columns:
            op.add_column(
                table,
                sa.Column(
                    column,
                    postgresql.ARRAY(sa.UUID()),
                    nullable=False,
                    server_default=sa.text("'{}'::uuid[]"),
                ),
            )
    for table, column in _JSON.items():
        op.add_column(
            table,
            sa.Column(
                column, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )
    op.create_table(
        "stock_serial_conversions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("source_serial_id", sa.UUID(), nullable=False),
        sa.Column("output_serial_id", sa.UUID(), nullable=False),
        sa.Column("job_work_receipt_line_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_serial_conversions")),
        sa.UniqueConstraint(
            "company_id", "source_serial_id", name="uq_stock_serial_conversions_source"
        ),
        sa.UniqueConstraint(
            "company_id", "output_serial_id", name="uq_stock_serial_conversions_output"
        ),
        sa.CheckConstraint(
            "source_serial_id <> output_serial_id",
            name=op.f("ck_stock_serial_conversions_conversion_changes_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="RESTRICT",
            name=op.f("fk_stock_serial_conversions_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["source_serial_id"],
            ["stock_serials.id"],
            ondelete="RESTRICT",
            name=op.f("fk_stock_serial_conversions_source_serial_id_stock_serials"),
        ),
        sa.ForeignKeyConstraint(
            ["output_serial_id"],
            ["stock_serials.id"],
            ondelete="RESTRICT",
            name=op.f("fk_stock_serial_conversions_output_serial_id_stock_serials"),
        ),
        sa.ForeignKeyConstraint(
            ["job_work_receipt_line_id"],
            ["job_work_receipt_lines.id"],
            ondelete="RESTRICT",
            name=op.f("fk_serial_conversion_receipt_line"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
            name=op.f("fk_stock_serial_conversions_created_by_id_users"),
        ),
    )
    op.create_index(
        "ix_stock_serial_conversions_company_id", "stock_serial_conversions", ["company_id"]
    )
    op.create_index(
        "ix_stock_serial_conversions_receipt",
        "stock_serial_conversions",
        ["company_id", "job_work_receipt_line_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_stock_serial_conversions_immutable "
        "BEFORE DELETE OR UPDATE ON stock_serial_conversions "
        "FOR EACH ROW EXECUTE FUNCTION public.atcraft_reject_row_mutation()"
    )
    op.create_check_constraint(
        op.f("ck_qc_inspections_inspection_serials_match_quantity"),
        "qc_inspections",
        "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = inspected_quantity",
    )
    op.create_check_constraint(
        op.f("ck_job_work_order_lines_issued_serials_match_quantity"),
        "job_work_order_lines",
        "(is_serial_tracked AND cardinality(serial_ids) = issued_quantity) OR "
        "(NOT is_serial_tracked AND cardinality(serial_ids) = 0)",
    )
    op.create_check_constraint(
        op.f("ck_job_work_challan_lines_challan_serials_match_quantity"),
        "job_work_challan_lines",
        "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = quantity",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM job_work_order_lines WHERE cardinality(serial_ids) > 0) "
            "OR EXISTS (SELECT 1 FROM qc_inspections WHERE cardinality(serial_ids) > 0) "
            "OR EXISTS (SELECT 1 FROM stock_serial_conversions)"
        )
    ):
        raise RuntimeError(
            "Serial job-work history exists; downgrade would discard its traceability."
        )
    op.drop_constraint(
        op.f("ck_qc_inspections_inspection_serials_match_quantity"), "qc_inspections", type_="check"
    )
    op.drop_table("stock_serial_conversions")
    for table, column in _JSON.items():
        op.drop_column(table, column)
    op.drop_constraint(
        op.f("ck_job_work_challan_lines_challan_serials_match_quantity"),
        "job_work_challan_lines",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_job_work_order_lines_issued_serials_match_quantity"),
        "job_work_order_lines",
        type_="check",
    )
    for table, columns in reversed(tuple(_ARRAYS.items())):
        for column in reversed(columns):
            op.drop_column(table, column)
    op.drop_column("job_work_order_lines", "is_serial_tracked")
