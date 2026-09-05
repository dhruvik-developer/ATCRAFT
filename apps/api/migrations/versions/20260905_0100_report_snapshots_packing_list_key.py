"""report_snapshots: admit the PACKING_LIST job key

Revision ID: 5e2b8c41f7a3
Revises: 3c8f2a7d5b91
Created: 2026-09-05 01:00:00+00:00

The packing-list PDF and the full inventory-valuation workbook used to answer
``202`` with a bare Celery task id and a ``status_url`` no route served. Both now
queue a ``report_snapshots`` row - the same job row every background report
uses, read by ``GET /reports/jobs/{id}`` - so the client can poll it and, once
it completes, follow ``download_url``. The valuation reuses the existing
``INVENTORY_VALUATION`` key; the packing list is a rendered document rather
than a tabular report and needs a key of its own, ``PACKING_LIST``, which the
``report_key IN (...)`` CHECK enumerates literally and must therefore admit.

Locking impact: ``DROP CONSTRAINT`` and ``ADD CONSTRAINT ... NOT VALID`` each
take a brief ACCESS EXCLUSIVE lock without scanning the table; the separate
``VALIDATE CONSTRAINT`` scans under SHARE UPDATE EXCLUSIVE, which does not
block reads or writes. The table is small (one row per generated report) and
every existing row already satisfies the wider constraint.

Data migration: none on upgrade. The downgrade deletes any ``PACKING_LIST``
job rows before narrowing the constraint back; they are derived state - the
snapshot table is not a source of truth and is never soft-deleted - and the
PDF each points at is a ``documents`` row with its own retention, which is
left alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "5e2b8c41f7a3"
down_revision: str | None = "3c8f2a7d5b91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose: a migration must state the schema as it was at this
# revision, not import an enum that will keep changing after it has run.
_CATALOGUE_KEYS = (
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
    "INVENTORY_VALUATION",
    "DISPATCH",
    "COURIER_TRACKING",
    "VENDOR_DISPATCH_HISTORY",
)
_NEW_KEYS = ("PACKING_LIST",)


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
    _swap_constraint((*_CATALOGUE_KEYS, *_NEW_KEYS))


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE report_key IN ({_in_list(_NEW_KEYS)})")
    _swap_constraint(_CATALOGUE_KEYS)
