"""report_snapshots: admit every round-2 report key in one CHECK

Revision ID: 7f3070625160
Revises: 4f29c8bead9e
Created: 2026-10-14 09:00:00.000000+00:00

The five round-2 lanes each widened the ``report_key IN (...)`` CHECK for the
keys they added, on five parallel branches of the revision graph:

* supply (``c20ce1d543b5``): SCRAP_REGISTER, CONSUMPTION_BY_DEPARTMENT;
* operations (``48e4df04913e``): SHIFT_PRODUCTION;
* commercial (``e7b31c9a04d6``): SALES_INVOICE;
* portals (``4480835a545e``): PURCHASE_ORDER.

Re-chaining the branches in the merge order supply, quality, operations,
commercial, portals leaves the portals swap last, so without this migration
the CHECK would admit only the 22 keys the portals lane knew and refuse the
other four. The list below is EVERY ``ReportKey`` value on main after the
round-2 integration, spelled out - a migration states the schema as it was,
it does not import an enum that keeps changing after it has run (the precedent
is ``5e2b8c41f7a3``).

Review (master rules section 67):

* Locking impact:  ``DROP CONSTRAINT`` and ``ADD CONSTRAINT ... NOT VALID``
                    each take a brief ACCESS EXCLUSIVE lock without scanning;
                    the separate ``VALIDATE CONSTRAINT`` scans under SHARE UPDATE
                    EXCLUSIVE, which blocks neither reads nor writes. Every
                    existing row already satisfies the wider list.
* Data migration:  none on upgrade. The downgrade deletes the four keys' job
                    rows before narrowing the constraint: they are derived state,
                    and any artefact one points at is a ``documents`` row with its
                    own retention, left alone.
* Rollback:        restores the previous literal list.
* Production size: one row per generated report. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7f3070625160"
down_revision: str | None = "4f29c8bead9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose - see the docstring. This is the complete list the
# portals swap (4480835a545e) left behind.
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
    "PURCHASE_ORDER",
    "PACKING_LIST",
)
_NEW_KEYS = (
    "SHIFT_PRODUCTION",
    "SCRAP_REGISTER",
    "CONSUMPTION_BY_DEPARTMENT",
    "SALES_INVOICE",
)


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
