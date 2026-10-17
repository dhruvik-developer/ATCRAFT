"""Admit the credit-note PDF report key."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261017_0120"
down_revision: str | None = "20261017_0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

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
    "PRODUCTION_COST",
    "JOB_WORK_COST",
    "JOB_WORK_CHALLAN",
)
NEW_KEY = "CREDIT_NOTE"


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _swap_constraint(values: Sequence[str]) -> None:
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        TABLE,
        f"report_key IN ({_in_list(values)})",
        postgresql_not_valid=True,
    )
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CONSTRAINT}")


def upgrade() -> None:
    _swap_constraint((*_PREVIOUS_KEYS, NEW_KEY))


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE report_key = '{NEW_KEY}'")
    _swap_constraint(_PREVIOUS_KEYS)
