"""report_snapshots: admit the SALES_INVOICE report key

Revision ID: e7b31c9a04d6
Revises: a2a66f219cb5
Created: 2026-10-11 17:01:00.000000+00:00

The tax-invoice PDF is a document-only job, so it needs a ``report_snapshots``
row keyed ``SALES_INVOICE``. The ``report_key IN (...)`` CHECK enumerates the
keys literally and must admit the new one, or the first invoice PDF fails with
an integrity error. The suite cannot see this: tests build the schema from the
model metadata, which already carries the new member.

The list below is EVERY ``ReportKey`` value at this revision, spelled out. A
migration states the schema as it was; it does not import an enum that keeps
changing after it has run (the precedent is ``b459767d92f5``).

The permission grant lives in the previous revision so a constraint change and
a role change can roll back separately.

Review (master rules section 67):

* Locking impact:  ``DROP CONSTRAINT`` and ``ADD CONSTRAINT ... NOT VALID``
                   each take a brief ACCESS EXCLUSIVE lock without scanning;
                   the separate ``VALIDATE CONSTRAINT`` scans under SHARE UPDATE
                   EXCLUSIVE, which blocks neither reads nor writes. Every
                   existing row already satisfies the wider list.
* Data migration:  none on upgrade. The downgrade deletes ``SALES_INVOICE``
                   job rows before narrowing the constraint: they are derived
                   state, and any artefact one points at is a ``documents`` row
                   with its own retention, left alone.
* Rollback:        restores the previous literal list.
* Production size: one row per generated invoice PDF. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e7b31c9a04d6"
down_revision: str | None = "a2a66f219cb5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose. This is the complete list at the previous revision
# (b459767d92f5 added COST_CENTRE_EXPENSE; 5e2b8c41f7a3 added PACKING_LIST).
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
    "INVENTORY_VALUATION",
    "DISPATCH",
    "COURIER_TRACKING",
    "VENDOR_DISPATCH_HISTORY",
    "COST_CENTRE_EXPENSE",
    "PACKING_LIST",
)
_NEW_KEYS = ("SALES_INVOICE",)


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
