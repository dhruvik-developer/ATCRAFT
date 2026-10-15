"""Quality completion (WP E5): production and final QC, rework, decision history.

Revision ID: e5a140120001
Revises: 7f3070625160
Created: 2026-10-14 12:00:00.000000+00:00

* ``items.requires_production_qc`` / ``items.requires_final_qc`` - the two item
  flags production QC and final QC key off (docs/assumptions.md §E5-1, §E5-8).
* ``production_orders.requires_qc`` - whether this order's output is booked
  into QC_HOLD with an in-process inspection. Existing rows read false: they
  were received straight to AVAILABLE and must keep completing as before.
* ``production_entries.qc_inspection_id`` / ``qc_inspection_code`` /
  ``qc_hold_open`` - the inspection one booking raised, and whether its output
  is still held. Bare UUIDs, not a foreign key: quality owns the row.
* ``packing_order_lines.qc_inspection_id`` - the FINAL inspection starting the
  order raised for the line. Bare UUID for the same reason.
* ``qc_rework_records`` - one rework cycle per row (OPEN -> IN_PROGRESS ->
  DONE), with assignee or work centre, outcome, optional cost and notes.
* ``qc_inspection_decisions`` - append-only verdict history: every decision
  writes its cumulative disposition, so re-deciding a reworked lot does not
  erase how it was decided before.
* ``report_key_is_known`` widened with the literal ``REWORK_RATE`` key, in the
  same drop / re-add ``NOT VALID`` / ``VALIDATE`` shape as ``7f3070625160``.

Review (master rules section 67):

* Locking impact:  every ``ADD COLUMN`` is catalogue-only (NULL or a constant
                    default - no rewrite, no scan) but takes a brief ACCESS
                    EXCLUSIVE lock. Both new foreign keys and every new CHECK
                    are added ``NOT VALID`` and validated separately. The new
                    tables are empty, so their validations scan nothing. Index
                    builds are plain (``CONCURRENTLY`` cannot run inside the
                    migration transaction).
* Data migration:  none. New columns default to "no QC owed", which matches
                    every existing row: no past receipt was held for these
                    inspections.
* Rollback:        restores the previous ``report_key`` list after deleting
                    any ``REWORK_RATE`` job rows, then drops the new tables,
                    constraints and columns in reverse order. Verdicts decided
                    while this revision was applied keep their current
                    disposition on ``qc_inspections``; only the per-cycle
                    history rows are lost.
* Production size: document tables (thousands of rows). Each validation scans
                    its table once.
* Index creation:  ``ix_qc_rework_records_inspection``,
                    ``ix_qc_rework_records_open``,
                    ``ix_qc_inspection_decisions_inspection``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e5a140120001"
down_revision: str | None = "7f3070625160"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_report_snapshots_report_key_is_known"
TABLE = "report_snapshots"

# Literal on purpose: a migration states the schema as it was, it does not
# import an enum that keeps changing after it has run. This is the complete
# list ``7f3070625160`` left behind.
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
)
_NEW_KEYS = ("REWORK_RATE",)


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _actor_columns() -> list[sa.Column[Any]]:
    return [
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column(
            "requires_production_qc",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "items",
        sa.Column(
            "requires_final_qc",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "production_orders",
        sa.Column("requires_qc", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("production_entries", sa.Column("qc_inspection_id", sa.Uuid(), nullable=True))
    op.add_column(
        "production_entries", sa.Column("qc_inspection_code", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "production_entries",
        sa.Column("qc_hold_open", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("packing_order_lines", sa.Column("qc_inspection_id", sa.Uuid(), nullable=True))

    op.create_table(
        "qc_rework_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("inspection_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("work_center_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("cost", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_actor_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_rework_records")),
        sa.UniqueConstraint(
            "company_id", "code", name=op.f("uq_qc_rework_records_company_id_code")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_rework_records_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_rework_records_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_qc_rework_records_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'DONE')",
            name=op.f("ck_qc_rework_records_status_known"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('REWORKED_OK', 'SCRAPPED', 'RETURNED')",
            name=op.f("ck_qc_rework_records_outcome_known"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_qc_rework_records_quantity_positive")),
        sa.CheckConstraint(
            "cost IS NULL OR cost >= 0", name=op.f("ck_qc_rework_records_cost_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'DONE') = (outcome IS NOT NULL)",
            name=op.f("ck_qc_rework_records_outcome_matches_completion"),
        ),
        sa.CheckConstraint(
            "(status = 'DONE') = (completed_at IS NOT NULL)",
            name=op.f("ck_qc_rework_records_completion_is_timed"),
        ),
    )
    op.execute(
        "ALTER TABLE qc_rework_records ADD CONSTRAINT "
        "fk_qc_rework_records_inspection_id_qc_inspections "
        "FOREIGN KEY (inspection_id) REFERENCES qc_inspections (id) "
        "ON DELETE CASCADE NOT VALID"
    )
    op.execute(
        "ALTER TABLE qc_rework_records ADD CONSTRAINT "
        "fk_qc_rework_records_assigned_to_users "
        "FOREIGN KEY (assigned_to) REFERENCES users (id) "
        "ON DELETE SET NULL NOT VALID"
    )
    op.execute(
        "ALTER TABLE qc_rework_records ADD CONSTRAINT "
        "fk_qc_rework_records_work_center_id_work_centers "
        "FOREIGN KEY (work_center_id) REFERENCES work_centers (id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    op.create_index(
        op.f("ix_qc_rework_records_inspection"),
        "qc_rework_records",
        ["company_id", "inspection_id", "created_at"],
    )
    op.create_index(op.f("ix_qc_rework_records_company_id"), "qc_rework_records", ["company_id"])
    op.create_index(op.f("ix_qc_rework_records_created_at"), "qc_rework_records", ["created_at"])
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT ck_qc_rework_records_status_known"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT ck_qc_rework_records_outcome_known"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT ck_qc_rework_records_quantity_positive"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT ck_qc_rework_records_cost_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT "
        "ck_qc_rework_records_outcome_matches_completion"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT ck_qc_rework_records_completion_is_timed"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT "
        "fk_qc_rework_records_inspection_id_qc_inspections"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT fk_qc_rework_records_assigned_to_users"
    )
    op.execute(
        "ALTER TABLE qc_rework_records VALIDATE CONSTRAINT "
        "fk_qc_rework_records_work_center_id_work_centers"
    )

    op.create_table(
        "qc_inspection_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("inspection_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("accepted_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("rejected_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("rework_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("hold_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("scrap_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_inspection_decisions")),
        sa.UniqueConstraint(
            "inspection_id", "sequence", name=op.f("uq_qc_inspection_decisions_inspection_sequence")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_inspection_decisions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_inspection_decisions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "result IN ('PENDING', 'ACCEPTED', 'REWORK', 'REJECTED', 'HOLD', 'SCRAP')",
            name=op.f("ck_qc_inspection_decisions_result_known"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_qc_inspection_decisions_sequence_positive")
        ),
        sa.CheckConstraint(
            "accepted_quantity >= 0",
            name=op.f("ck_qc_inspection_decisions_accepted_non_negative"),
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0",
            name=op.f("ck_qc_inspection_decisions_rejected_non_negative"),
        ),
        sa.CheckConstraint(
            "rework_quantity >= 0",
            name=op.f("ck_qc_inspection_decisions_rework_non_negative"),
        ),
        sa.CheckConstraint(
            "hold_quantity >= 0", name=op.f("ck_qc_inspection_decisions_hold_non_negative")
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_qc_inspection_decisions_scrap_non_negative")
        ),
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions ADD CONSTRAINT "
        "fk_qc_inspection_decisions_inspection_id_qc_inspections "
        "FOREIGN KEY (inspection_id) REFERENCES qc_inspections (id) "
        "ON DELETE CASCADE NOT VALID"
    )
    op.create_index(
        op.f("ix_qc_inspection_decisions_inspection"),
        "qc_inspection_decisions",
        ["company_id", "inspection_id", "sequence"],
    )
    op.create_index(
        op.f("ix_qc_inspection_decisions_company_id"), "qc_inspection_decisions", ["company_id"]
    )
    op.create_index(
        op.f("ix_qc_inspection_decisions_created_at"), "qc_inspection_decisions", ["created_at"]
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_result_known"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_sequence_positive"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_accepted_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_rejected_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_rework_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_hold_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "ck_qc_inspection_decisions_scrap_non_negative"
    )
    op.execute(
        "ALTER TABLE qc_inspection_decisions VALIDATE CONSTRAINT "
        "fk_qc_inspection_decisions_inspection_id_qc_inspections"
    )

    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    # One literal string, not built by ``_in_list``: the history test
    # (``tests/unit/test_report_key_check_migration.py``) reads the keys a
    # CHECK swap states by finding exactly one ``report_key IN (...)`` literal
    # in ``upgrade()``. A swap migration that only swaps can build the list
    # from tuples; this one also creates tables, whose index column lists
    # would read as sixteen more key lists. Same twenty-seven keys either way.
    op.create_check_constraint(
        op.f(CONSTRAINT),
        TABLE,
        "report_key IN ('SALES_ORDER_STATUS', 'STOCK_AVAILABILITY', 'MRP_SHORTAGE', 'PURCHASE_PENDING', 'PRODUCTION_PENDING', 'JOB_WORK_PENDING', 'QC_REJECTION', 'VENDOR_PERFORMANCE', 'PRODUCT_PROFITABILITY', 'FAST_MOVING', 'SLOW_MOVING', 'DEAD_STOCK', 'ORDER_DELAY', 'PRODUCTION_EFFICIENCY', 'MACHINE_UTILISATION', 'SHIFT_PRODUCTION', 'INVENTORY_VALUATION', 'DISPATCH', 'COURIER_TRACKING', 'VENDOR_DISPATCH_HISTORY', 'COST_CENTRE_EXPENSE', 'SCRAP_REGISTER', 'CONSUMPTION_BY_DEPARTMENT', 'PURCHASE_ORDER', 'PACKING_LIST', 'SALES_INVOICE', 'REWORK_RATE')",
        postgresql_not_valid=True,
    )
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CONSTRAINT}")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE report_key IN ({_in_list(_NEW_KEYS)})")
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        TABLE,
        f"report_key IN ({_in_list(_PREVIOUS_KEYS)})",
        postgresql_not_valid=True,
    )
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CONSTRAINT}")

    op.drop_index(
        op.f("ix_qc_inspection_decisions_created_at"), table_name="qc_inspection_decisions"
    )
    op.drop_index(
        op.f("ix_qc_inspection_decisions_company_id"), table_name="qc_inspection_decisions"
    )
    op.drop_index(
        op.f("ix_qc_inspection_decisions_inspection"), table_name="qc_inspection_decisions"
    )
    op.drop_table("qc_inspection_decisions")

    op.drop_index(op.f("ix_qc_rework_records_created_at"), table_name="qc_rework_records")
    op.drop_index(op.f("ix_qc_rework_records_company_id"), table_name="qc_rework_records")
    op.drop_index(op.f("ix_qc_rework_records_inspection"), table_name="qc_rework_records")
    op.drop_table("qc_rework_records")

    op.drop_column("packing_order_lines", "qc_inspection_id")
    op.drop_column("production_entries", "qc_hold_open")
    op.drop_column("production_entries", "qc_inspection_code")
    op.drop_column("production_entries", "qc_inspection_id")
    op.drop_column("production_orders", "requires_qc")
    op.drop_column("items", "requires_final_qc")
    op.drop_column("items", "requires_production_qc")
