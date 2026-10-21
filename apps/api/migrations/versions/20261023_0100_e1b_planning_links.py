"""Planning links for WP E1b: sales settings, pegged supply and transfer provenance.

Revision ID: 20261023_0100
Revises: 20261022_0100
Create Date: 2026-10-19

The auto-plan-on-confirm step raises draft documents for a confirmed order's
shortfall and then keeps them true as the order changes, so four tables gain
the links that makes possible:

* ``sales_settings`` - one sparse row per company; ``auto_plan_on_confirm``
  nullable, because NULL must read as the code default rather than as "off";
* ``production_orders.source_line_id`` - the sales order *line* a job was
  raised for, which is what lets a replan net open supply against exactly the
  promise it covers (NULL for every other source);
* ``purchase_requisition_lines.source_demand_refs`` - the JSONB
  ``[{demand_type, demand_id, demand_line_id}]`` provenance of WP D1 section 1,
  written by the planner and carried onto purchase orders at conversion;
* ``stock_transfers.source_type`` / ``source_id`` and
  ``stock_transfer_lines.source_line_id`` - the bare pair naming the document a
  transfer answers (a short sales order line, later a reallocation request).

Every added column is nullable or defaulted, so the revision is safe on a live
database: existing rows read as "not planned from a sales order", which is
exactly what they are, and the application writes the new values from here on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261023_0100"
down_revision = "20261022_0100"
branch_labels = None
depends_on = None

GUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "sales_settings",
        sa.Column("id", GUID, nullable=False),
        sa.Column("company_id", GUID, nullable=False),
        sa.Column("auto_plan_on_confirm", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", GUID, nullable=True),
        sa.Column("updated_by_id", GUID, nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_sales_settings_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_sales_settings_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_sales_settings_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sales_settings"),
        sa.UniqueConstraint("company_id", name="uq_sales_settings_company"),
    )
    op.create_index("ix_sales_settings_company_id", "sales_settings", ["company_id"])
    op.create_index("ix_sales_settings_created_at", "sales_settings", ["created_at"])

    op.add_column("production_orders", sa.Column("source_line_id", GUID, nullable=True))

    op.add_column(
        "purchase_requisition_lines",
        sa.Column("source_demand_refs", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_purchase_requisition_lines_demand_refs",
        "purchase_requisition_lines",
        ["source_demand_refs"],
        postgresql_using="gin",
    )

    op.add_column(
        "stock_transfers",
        sa.Column(
            "source_type",
            sa.String(length=32),
            server_default="MANUAL",
            nullable=False,
        ),
    )
    op.add_column("stock_transfers", sa.Column("source_id", GUID, nullable=True))
    op.create_check_constraint(
        "source_type_known",
        "stock_transfers",
        "source_type IN ('MANUAL', 'SALES_ORDER', 'REALLOCATION')",
    )
    op.create_index(
        "ix_stock_transfers_source", "stock_transfers", ["company_id", "source_type", "source_id"]
    )

    op.add_column("stock_transfer_lines", sa.Column("source_line_id", GUID, nullable=True))


def downgrade() -> None:
    op.drop_column("stock_transfer_lines", "source_line_id")

    op.drop_index("ix_stock_transfers_source", table_name="stock_transfers")
    op.drop_constraint("source_type_known", "stock_transfers", type_="check")
    op.drop_column("stock_transfers", "source_id")
    op.drop_column("stock_transfers", "source_type")

    op.drop_index(
        "ix_purchase_requisition_lines_demand_refs", table_name="purchase_requisition_lines"
    )
    op.drop_column("purchase_requisition_lines", "source_demand_refs")

    op.drop_column("production_orders", "source_line_id")

    op.drop_index("ix_sales_settings_created_at", table_name="sales_settings")
    op.drop_index("ix_sales_settings_company_id", table_name="sales_settings")
    op.drop_table("sales_settings")
