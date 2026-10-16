"""Production cost report and its period-query index.

Revision ID: 20261016_1100
Revises: 20261016_1000
"""

from __future__ import annotations

from alembic import op

revision = "20261016_1100"
down_revision = "20261016_1000"
branch_labels = None
depends_on = None

_TABLE = "report_snapshots"
_CONSTRAINT = "ck_report_snapshots_report_key_is_known"
_INDEX = "ix_production_orders_company_status_updated_at"
_PREVIOUS_KEYS = (
    "SALES_ORDER_STATUS",
    "STOCK_AVAILABILITY",
    "MRP_SHORTAGE",
    "PURCHASE_PENDING",
    "PRODUCTION_PENDING",
    "JOB_WORK_PENDING",
    "QC_REJECTION",
    "VENDOR_PERFORMANCE",
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
    "PURCHASE_ORDER",
    "PACKING_LIST",
    "SALES_INVOICE",
    "REWORK_RATE",
)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "production_orders",
        ["company_id", "status", "updated_at"],
    )
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        "report_key IN ('SALES_ORDER_STATUS', 'STOCK_AVAILABILITY', 'MRP_SHORTAGE', "
        "'PURCHASE_PENDING', 'PRODUCTION_PENDING', 'JOB_WORK_PENDING', 'QC_REJECTION', "
        "'VENDOR_PERFORMANCE', 'PRODUCT_PROFITABILITY', 'FAST_MOVING', 'SLOW_MOVING', "
        "'DEAD_STOCK', 'ORDER_DELAY', 'PRODUCTION_EFFICIENCY', 'MACHINE_UTILISATION', "
        "'SHIFT_PRODUCTION', 'INVENTORY_VALUATION', 'DISPATCH', 'COURIER_TRACKING', "
        "'VENDOR_DISPATCH_HISTORY', 'COST_CENTRE_EXPENSE', 'SCRAP_REGISTER', "
        "'CONSUMPTION_BY_DEPARTMENT', 'PURCHASE_ORDER', 'PACKING_LIST', 'SALES_INVOICE', "
        "'REWORK_RATE', 'PRODUCTION_COST')",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE report_snapshots VALIDATE CONSTRAINT ck_report_snapshots_report_key_is_known"
    )


def downgrade() -> None:
    op.execute("DELETE FROM report_snapshots WHERE report_key = 'PRODUCTION_COST'")
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        f"report_key IN ({_in_list(_PREVIOUS_KEYS)})",
        postgresql_not_valid=True,
    )
    op.execute(
        "ALTER TABLE report_snapshots VALIDATE CONSTRAINT ck_report_snapshots_report_key_is_known"
    )
    op.drop_index(_INDEX, table_name="production_orders")
