"""Persist the physical units covered by a packing line's FINAL inspection.

Revision ID: 20261030_0100
Revises: 20261029_0100
Create Date: 2026-10-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261030_0100"
down_revision = "20261029_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "packing_order_lines",
        sa.Column(
            "qc_serial_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
    )
    op.add_column("packing_order_lines", sa.Column("qc_batch_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_packing_order_lines_qc_batch_id_stock_batches"),
        "packing_order_lines",
        "stock_batches",
        ["qc_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Preserve known unit identities from an inspection raised by revision 28.
    # Quantity-only history is deliberately not relabelled as named-unit QC.
    op.execute("""
        UPDATE packing_order_lines AS line
        SET qc_serial_ids = inspection.serial_ids, qc_batch_id = inspection.batch_id
        FROM qc_inspections AS inspection
        WHERE line.qc_inspection_id = inspection.id
          AND line.company_id = inspection.company_id
          AND inspection.reference_type = 'packing_order'
          AND inspection.reference_id = line.id
          AND cardinality(inspection.serial_ids) > 0
          AND cardinality(inspection.serial_ids) = line.quantity_to_pack
    """)
    op.create_check_constraint(
        op.f("ck_packing_order_lines_qc_serials_cover_quantity"),
        "packing_order_lines",
        "cardinality(qc_serial_ids) = 0 OR cardinality(qc_serial_ids) = quantity_to_pack",
    )
    op.create_check_constraint(
        op.f("ck_packing_order_lines_qc_batch_has_serials"),
        "packing_order_lines",
        "qc_batch_id IS NULL OR cardinality(qc_serial_ids) > 0",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A downgrade must not silently discard the line's physical-unit selection.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM packing_order_lines WHERE cardinality(qc_serial_ids) > 0) THEN
                RAISE EXCEPTION 'Packing FINAL serial selections exist; downgrade would discard traceability';
            END IF;
        END $$
    """)
    op.drop_constraint(
        op.f("ck_packing_order_lines_qc_batch_has_serials"), "packing_order_lines", type_="check"
    )
    op.drop_constraint(
        op.f("ck_packing_order_lines_qc_serials_cover_quantity"),
        "packing_order_lines",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_packing_order_lines_qc_batch_id_stock_batches"),
        "packing_order_lines",
        type_="foreignkey",
    )
    op.drop_column("packing_order_lines", "qc_batch_id")
    op.drop_column("packing_order_lines", "qc_serial_ids")
