"""add procurement report keys and bounded-report indexes

Revision ID: 20261123_0100
Revises: 20261122_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_0100"
down_revision: str | None = "20261122_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPORT_SNAPSHOT_KEY_CONSTRAINT = "ck_report_snapshots_report_key_is_known"
REPORT_SNAPSHOT_KEYS = (
    "SALES_ORDER_STATUS",
    "STOCK_AVAILABILITY",
    "MRP_SHORTAGE",
    "PURCHASE_PENDING",
    "PURCHASE_ORDER_REGISTER",
    "PURCHASE_ORDER_SUMMARY",
    "DELAYED_PURCHASE_ORDERS",
    "DUPLICATE_PO_OVERRIDES",
    "VENDOR_ACCEPTANCE",
    "INBOUND_SHIPMENTS",
    "INBOUND_FREIGHT",
    "VENDOR_INBOUND_HISTORY",
    "PRODUCTION_PENDING",
    "JOB_WORK_PENDING",
    "QC_REJECTION",
    "VENDOR_PERFORMANCE",
    "DEALER_PERFORMANCE",
    "PRODUCT_PROFITABILITY",
    "FAST_MOVING",
    "SLOW_MOVING",
    "DEAD_STOCK",
    "ORDER_DELAY",
    "PRODUCTION_EFFICIENCY",
    "MACHINE_UTILISATION",
    "SHIFT_PRODUCTION",
    "INVENTORY_VALUATION",
    "DISPATCH",
    "COURIER_TRACKING",
    "VENDOR_DISPATCH_HISTORY",
    "COST_CENTRE_EXPENSE",
    "SCRAP_REGISTER",
    "CONSUMPTION_BY_DEPARTMENT",
    "SALES_REGISTER",
    "PURCHASE_REGISTER",
    "GSTR1",
    "TRIAL_BALANCE",
    "PURCHASE_ORDER",
    "PACKING_LIST",
    "SALES_INVOICE",
    "CREDIT_NOTE",
    "PURCHASE_RETURN_DEBIT_NOTE",
    "REWORK_RATE",
    "PRODUCTION_COST",
    "JOB_WORK_COST",
    "JOB_WORK_CHALLAN",
)
PREVIOUS_REPORT_SNAPSHOT_KEYS = (
    "SALES_ORDER_STATUS",
    "STOCK_AVAILABILITY",
    "MRP_SHORTAGE",
    "PURCHASE_PENDING",
    "PRODUCTION_PENDING",
    "JOB_WORK_PENDING",
    "QC_REJECTION",
    "VENDOR_PERFORMANCE",
    "DEALER_PERFORMANCE",
    "PRODUCT_PROFITABILITY",
    "FAST_MOVING",
    "SLOW_MOVING",
    "DEAD_STOCK",
    "ORDER_DELAY",
    "PRODUCTION_EFFICIENCY",
    "MACHINE_UTILISATION",
    "SHIFT_PRODUCTION",
    "INVENTORY_VALUATION",
    "DISPATCH",
    "COURIER_TRACKING",
    "VENDOR_DISPATCH_HISTORY",
    "COST_CENTRE_EXPENSE",
    "SCRAP_REGISTER",
    "CONSUMPTION_BY_DEPARTMENT",
    "SALES_REGISTER",
    "PURCHASE_REGISTER",
    "GSTR1",
    "TRIAL_BALANCE",
    "PURCHASE_ORDER",
    "PACKING_LIST",
    "SALES_INVOICE",
    "CREDIT_NOTE",
    "PURCHASE_RETURN_DEBIT_NOTE",
    "REWORK_RATE",
    "PRODUCTION_COST",
    "JOB_WORK_COST",
    "JOB_WORK_CHALLAN",
)


def _swap_report_snapshot_keys(keys: tuple[str, ...]) -> None:
    spelled = ", ".join(f"'{key}'" for key in keys)
    op.drop_constraint(op.f(REPORT_SNAPSHOT_KEY_CONSTRAINT), "report_snapshots", type_="check")
    op.execute(
        sa.text(
            f"ALTER TABLE report_snapshots ADD CONSTRAINT "
            f"{REPORT_SNAPSHOT_KEY_CONSTRAINT} CHECK (report_key IN ({spelled})) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE report_snapshots VALIDATE CONSTRAINT {REPORT_SNAPSHOT_KEY_CONSTRAINT}"
        )
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_purchase_orders_company_po_date",
            "purchase_orders",
            ["company_id", "po_date"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_purchase_orders_company_sent_at",
            "purchase_orders",
            ["company_id", "sent_at"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_purchase_orders_duplicate_override_created",
            "purchase_orders",
            ["company_id", "created_at"],
            postgresql_where=sa.text("duplicate_acknowledged"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_po_shipments_company_shipped_at",
            "purchase_order_shipments",
            ["company_id", "shipped_at"],
            postgresql_concurrently=True,
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap_report_snapshot_keys(REPORT_SNAPSHOT_KEYS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap_report_snapshot_keys(PREVIOUS_REPORT_SNAPSHOT_KEYS)
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_po_shipments_company_shipped_at",
            table_name="purchase_order_shipments",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_purchase_orders_duplicate_override_created",
            table_name="purchase_orders",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_purchase_orders_company_sent_at",
            table_name="purchase_orders",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_purchase_orders_company_po_date",
            table_name="purchase_orders",
            postgresql_concurrently=True,
        )
