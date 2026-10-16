"""report_snapshots: admit the JOB_WORK_COST and JOB_WORK_CHALLAN keys

Revision ID: 20261016_1200
Revises: 20261016_1100

WP E7b adds two report keys. ``JOB_WORK_COST`` is a catalogue report - vendor,
process, order and line with input cost, charges, debits and cost per unit -
and ``JOB_WORK_CHALLAN`` is a document-only key for the GST Rule 55 delivery
challan PDF, queued and downloaded through the same job row as the packing
list. The ``report_key IN (...)`` CHECK enumerates every key literally and
must admit both.

Locking impact: ``DROP CONSTRAINT`` and ``ADD CONSTRAINT ... NOT VALID`` each
take a brief ACCESS EXCLUSIVE lock without scanning the table; the separate
``VALIDATE CONSTRAINT`` scans under SHARE UPDATE EXCLUSIVE, which does not
block reads or writes. The table is small (one row per generated report) and
every existing row already satisfies the wider constraint.

Data migration: none on upgrade. The downgrade deletes any ``JOB_WORK_COST``
or ``JOB_WORK_CHALLAN`` job rows before narrowing the constraint back; they
are derived state - the snapshot table is not a source of truth and is never
soft-deleted - and the PDF each challan job points at is a ``documents`` row
with its own retention, which is left alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261016_1200"
down_revision: str | None = "20261016_1100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose: a migration must state the schema as it was at this
# revision, not import an enum that will keep changing after it has run.
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
)
_NEW_KEYS = ("JOB_WORK_COST", "JOB_WORK_CHALLAN")


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
    _swap_constraint((*_PREVIOUS_KEYS, *_NEW_KEYS))


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE report_key IN ({_in_list(_NEW_KEYS)})")
    _swap_constraint(_PREVIOUS_KEYS)
