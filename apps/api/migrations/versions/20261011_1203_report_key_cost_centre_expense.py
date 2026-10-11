"""report_snapshots: admit the COST_CENTRE_EXPENSE report key

Revision ID: b459767d92f5
Revises: e1eed3775093
Created: 2026-10-11 12:03:00.000000+00:00

The cost-centre statement is a catalogue report like any other, so a request
past the inline row limit becomes a background job - a ``report_snapshots`` row
keyed ``COST_CENTRE_EXPENSE``. The ``report_key IN (...)`` CHECK enumerates the
keys literally and must admit the new one, or the first large statement fails
with an integrity error. The suite cannot see this: tests build the schema from
the model metadata, which already carries the new member.

The list below is EVERY ``ReportKey`` value at this revision, spelled out - a
migration states the schema as it was, it does not import an enum that keeps
changing after it has run (the precedent is ``5e2b8c41f7a3``).

Review (master rules section 67):

* Locking impact:  ``DROP CONSTRAINT`` and ``ADD CONSTRAINT ... NOT VALID``
                   each take a brief ACCESS EXCLUSIVE lock without scanning;
                   the separate ``VALIDATE CONSTRAINT`` scans under SHARE UPDATE
                   EXCLUSIVE, which blocks neither reads nor writes. Every
                   existing row already satisfies the wider list.
* Data migration:  none on upgrade. The downgrade deletes ``COST_CENTRE_EXPENSE``
                   job rows before narrowing the constraint: they are derived
                   state (the snapshot table is not a source of truth and is
                   never soft-deleted), and any artefact one points at is a
                   ``documents`` row with its own retention, left alone.
* Rollback:        restores the previous literal list.
* Production size: one row per generated report. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b459767d92f5"
down_revision: str | None = "e1eed3775093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose - see the docstring. This is the complete list at the
# previous revision (5e2b8c41f7a3 added PACKING_LIST).
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
    "PACKING_LIST",
)
_NEW_KEYS = ("COST_CENTRE_EXPENSE",)


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
