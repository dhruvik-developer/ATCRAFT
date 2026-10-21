"""Batch and serial traceability for WP E8b.

Revision ID: 20261024_0100
Revises: 20261023_0100
Create Date: 2026-10-21

The tracker needs to remember the exact units and lots a document touched, not
only the quantity. Every added column is nullable except the component tracking
snapshot, which is defaulted, so this is safe on a live database: existing rows
read as "taken before tracking was captured" and every posting path writes the
new links from here on.

``serial_ids`` on transfer and adjustment lines is constrained to the line's
quantity - the absolute quantity for an adjustment, because its sign says which
way the units moved. A NULL array still means "this line is not serialised"; an
empty one is meaningless.

The permission grant is part of the migration because role presets are only
refreshed for system roles at startup. A store keeper, inspector, plant manager
or production supervisor is a seeded but non-system role, so without this every
existing deployment would show the tracker's actions and refuse all of them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261024_0100"
down_revision = "20261023_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)
BATCH_MANAGE = "inventory.batch_manage"
GRANTED_ROLES = (
    "SUPER_ADMIN",
    "ADMIN",
    "STORE_KEEPER",
    "QC_INSPECTOR",
    "PLANT_MANAGER",
    "PRODUCTION_SUPERVISOR",
)


def upgrade() -> None:
    op.add_column(
        "stock_transfer_lines",
        sa.Column("serial_ids", postgresql.ARRAY(GUID), nullable=True),
    )
    op.add_column(
        "stock_transfer_lines",
        sa.Column("dispatch_transaction_ids", postgresql.ARRAY(GUID), nullable=True),
    )
    op.create_check_constraint(
        "ck_stock_transfer_lines_serial_count_matches_quantity",
        "stock_transfer_lines",
        "serial_ids IS NULL OR cardinality(serial_ids) = quantity",
    )

    op.add_column(
        "stock_adjustment_lines",
        sa.Column("serial_ids", postgresql.ARRAY(GUID), nullable=True),
    )
    op.create_check_constraint(
        "ck_stock_adjustment_lines_serial_count_matches_quantity",
        "stock_adjustment_lines",
        "serial_ids IS NULL OR cardinality(serial_ids) = abs(quantity)",
    )

    op.add_column(
        "goods_receipt_lines",
        sa.Column("serial_numbers", postgresql.ARRAY(sa.String(64)), nullable=True),
    )
    op.add_column(
        "goods_receipt_lines",
        sa.Column("serial_ids", postgresql.ARRAY(GUID), nullable=True),
    )

    op.add_column(
        "production_entries",
        sa.Column("output_transaction_ids", postgresql.ARRAY(GUID), nullable=True),
    )
    op.add_column("production_entries", sa.Column("output_batch_id", GUID, nullable=True))
    op.add_column(
        "production_entries",
        sa.Column("output_serial_ids", postgresql.ARRAY(GUID), nullable=True),
    )

    op.add_column("assembly_orders", sa.Column("output_batch_id", GUID, nullable=True))
    op.add_column(
        "assembly_orders",
        sa.Column("output_serial_ids", postgresql.ARRAY(GUID), nullable=True),
    )

    op.add_column(
        "assembly_order_components",
        sa.Column(
            "is_batch_tracked_component",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "assembly_order_components",
        sa.Column("issued_batch_id", GUID, nullable=True),
    )
    op.add_column(
        "assembly_order_components",
        sa.Column("consumed_batch_id", GUID, nullable=True),
    )

    op.create_index(
        "ix_stock_serials_batch",
        "stock_serials",
        ["company_id", "batch_id", "status"],
    )

    # One statement, idempotent, and only the roles whose preset includes the
    # new right. A client-defined role is left alone: rewriting its permissions
    # from a preset would silently undo the client's own decision.
    op.execute(
        sa.text(
            """
            UPDATE roles
               SET permissions = array_append(permissions, :permission),
                   updated_at = now()
             WHERE code = ANY(:codes)
               AND NOT (:permission = ANY(permissions))
            """
        ).bindparams(
            sa.bindparam("permission", BATCH_MANAGE, type_=sa.String()),
            sa.bindparam("codes", list(GRANTED_ROLES), type_=postgresql.ARRAY(sa.String())),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE roles
               SET permissions = array_remove(permissions, :permission),
                   updated_at = now()
             WHERE :permission = ANY(permissions)
            """
        ).bindparams(sa.bindparam("permission", BATCH_MANAGE, type_=sa.String()))
    )
    op.drop_index("ix_stock_serials_batch", table_name="stock_serials")
    op.drop_column("assembly_order_components", "consumed_batch_id")
    op.drop_column("assembly_order_components", "issued_batch_id")
    op.drop_column("assembly_order_components", "is_batch_tracked_component")
    op.drop_column("assembly_orders", "output_serial_ids")
    op.drop_column("assembly_orders", "output_batch_id")
    op.drop_column("production_entries", "output_serial_ids")
    op.drop_column("production_entries", "output_batch_id")
    op.drop_column("production_entries", "output_transaction_ids")
    op.drop_column("goods_receipt_lines", "serial_ids")
    op.drop_column("goods_receipt_lines", "serial_numbers")
    op.drop_constraint(
        "ck_stock_adjustment_lines_serial_count_matches_quantity",
        "stock_adjustment_lines",
        type_="check",
    )
    op.drop_column("stock_adjustment_lines", "serial_ids")
    op.drop_constraint(
        "ck_stock_transfer_lines_serial_count_matches_quantity",
        "stock_transfer_lines",
        type_="check",
    )
    op.drop_column("stock_transfer_lines", "dispatch_transaction_ids")
    op.drop_column("stock_transfer_lines", "serial_ids")
