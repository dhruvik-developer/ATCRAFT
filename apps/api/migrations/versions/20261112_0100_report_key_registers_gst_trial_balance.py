"""Admit the tax-register, GSTR-1 and trial-balance report keys.

Revision ID: 20261112_0100
Revises: 20261111_0100

``report_snapshots.report_key`` is guarded by a CHECK that spells every member
of the closed catalogue literally, because a migration states the schema as it
was and must not import an enum that keeps growing. The four keys below are new
in WP E10b:

* ``SALES_REGISTER`` and ``PURCHASE_REGISTER`` are catalogue reports, so their
  background exports need the same snapshot row as every other tabular report.
* ``GSTR1`` and ``TRIAL_BALANCE`` are rendered documents produced by dedicated
  tasks, but they still travel through the report job row, and the column
  refuses a value it does not list.

The constraint is recreated ``NOT VALID`` and then validated: that keeps the
lock on a busy ``report_snapshots`` table short, and validation still checks
every existing row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261112_0100"
down_revision = "20261111_0100"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_report_snapshots_report_key_is_known"

#: Every ``ReportKey`` at the time of this revision, spelled out. The unit test
#: ``test_report_key_check_migration.py`` reads this tuple through
#: ``_condition(_ALL_KEYS)`` and compares it with the enum.
_ALL_KEYS = (
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

#: The list this revision replaces, for a downgrade that leaves the schema as
#: the previous revision stated it.
_PREVIOUS_KEYS = tuple(
    key
    for key in _ALL_KEYS
    if key not in {"SALES_REGISTER", "PURCHASE_REGISTER", "GSTR1", "TRIAL_BALANCE"}
)


def _condition(keys: tuple[str, ...]) -> str:
    """The CHECK body for a literal key list."""
    spelled = ", ".join(f"'{key}'" for key in keys)
    return f"report_key IN ({spelled})"


def _swap(keys: tuple[str, ...]) -> None:
    # ``op.f`` marks the name as already fully qualified. Without it the naming
    # convention would prefix a second ``ck_report_snapshots_`` and the drop
    # would look for a constraint that does not exist.
    op.drop_constraint(op.f(_CONSTRAINT), "report_snapshots", type_="check")
    op.execute(
        sa.text(
            f"ALTER TABLE report_snapshots ADD CONSTRAINT {_CONSTRAINT} "
            f"CHECK ({_condition(keys)}) NOT VALID"
        )
    )
    op.execute(sa.text(f"ALTER TABLE report_snapshots VALIDATE CONSTRAINT {_CONSTRAINT}"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap(_ALL_KEYS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _swap(_PREVIOUS_KEYS)
