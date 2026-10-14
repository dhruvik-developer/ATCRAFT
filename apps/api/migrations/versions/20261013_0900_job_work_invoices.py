"""Typed job-work vendor invoices, billing tracking and order tax facts.

Revision ID: 3b7a1c9e4f2a
Revises: 48e4df04913e
Created: 2026-10-13 09:00:00.000000+00:00

A job-work invoice is a vendor invoice of a new type. It shares payments,
allocations, approval, disputes and the payables ledger with a purchase bill,
and it matches a job-work order and its receipt lines instead of a purchase
order and a goods receipt (docs/assumptions.md §B3-1):

* ``vendor_invoices.invoice_type`` - ``PURCHASE`` or ``JOB_WORK``, NOT NULL,
  backfilled to ``PURCHASE`` for every existing row by the column default.
* ``vendor_invoices.job_work_order_id`` - RESTRICT, mandatory exactly when the
  type is ``JOB_WORK`` (``ck_vendor_invoices_job_work_invoice_names_its_order``).
* ``vendor_invoices.recovered_amount`` - the excess-loss recovery posted with
  the invoice (Dr AP / Cr job-work expense). The vendor's net payable is
  ``total_amount - recovered_amount`` (docs/assumptions.md §B3-2).
* ``vendor_invoice_lines.job_work_receipt_line_id`` - RESTRICT, mutually
  exclusive with ``purchase_order_line_id``. The foreign key carries an
  explicit name: the generated one is 70 bytes and PostgreSQL would truncate
  it (see ``tests/unit/test_schema_identifier_length.py``).
* ``vendor_invoice_lines.recovery_amount`` - this line's share of the recovery
  the approval posted, so cancelling the invoice removes exactly what it added
  even when a later invoice posted a further recovery on the same receipt line.
* ``job_work_receipt_lines.invoiced_amount`` / ``debit_posted_amount`` - how
  much of the charges approved invoices have billed, and how much of the
  vendor debit has been recovered. Written only at invoice approval or
  cancellation, under row locks taken in id order.
* ``job_work_orders.tax_rate`` / ``sac_code`` - the GST rate and the SAC for
  the processing service (docs/assumptions.md §B3-3).

Review (master rules section 67):

* Locking impact:  every ``ADD COLUMN`` is catalogue-only in PostgreSQL 11+
  (NULL or a constant default - no rewrite, no scan) but takes a brief ACCESS
  EXCLUSIVE lock that queues behind a long transaction on the table. Both
  foreign keys and every new CHECK are added ``NOT VALID`` and validated
  separately: ``VALIDATE CONSTRAINT`` takes only SHARE UPDATE EXCLUSIVE and
  blocks neither reads nor writes, where a validating ``ADD CONSTRAINT``
  would hold SHARE ROW EXCLUSIVE on both tables for a full scan. The three
  index builds read their table once; ``CREATE INDEX CONCURRENTLY`` cannot
  run inside the migration transaction.
* Data migration:  none beyond the ``invoice_type`` default. Every existing
  invoice is a purchase bill, every existing receipt line is unbilled and
  unrecovered, and both new CHECK pairs hold trivially on zeros and NULLs.
* Rollback:        ``downgrade()`` drops the constraints, indexes and columns
  in the reverse order. Rows written while this revision was applied lose
  their job-work links, their tracking amounts and the order tax facts; the
  journals the approvals posted survive, as ledger rows always do.
* Production size: the four tables are document-sized (thousands of rows, not
  millions). Each validation scans its table once.
* Index creation:  ``ix_vendor_invoices_type``,
  ``ix_vendor_invoices_job_work_order``,
  ``ix_vendor_invoices_job_work_order_id``,
  ``ix_vendor_invoice_lines_job_work_receipt`` and
  ``ix_vendor_invoice_lines_job_work_receipt_line_id`` on the existing
  tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b7a1c9e4f2a"
down_revision: str | None = "48e4df04913e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The explicit foreign-key name declared on
#: ``VendorInvoiceLine.job_work_receipt_line_id`` - see the module docstring.
JW_RECEIPT_FK = "fk_vendor_invoice_lines_jw_receipt"

JW_ORDER_FK = "fk_vendor_invoices_job_work_order_id_job_work_orders"

_CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "vendor_invoices",
        "ck_vendor_invoices_invoice_type_known",
        "invoice_type IN ('PURCHASE', 'JOB_WORK')",
    ),
    (
        "vendor_invoices",
        "ck_vendor_invoices_job_work_invoice_names_its_order",
        "(invoice_type = 'JOB_WORK') = (job_work_order_id IS NOT NULL)",
    ),
    ("vendor_invoices", "ck_vendor_invoices_recovered_non_negative", "recovered_amount >= 0"),
    (
        "vendor_invoice_lines",
        "ck_vendor_invoice_lines_one_source_document",
        "purchase_order_line_id IS NULL OR job_work_receipt_line_id IS NULL",
    ),
    (
        "vendor_invoice_lines",
        "ck_vendor_invoice_lines_recovery_non_negative",
        "recovery_amount >= 0",
    ),
    (
        "job_work_orders",
        "ck_job_work_orders_tax_rate_bounded",
        "tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 100)",
    ),
    (
        "job_work_orders",
        "ck_job_work_orders_sac_code_not_blank",
        "sac_code IS NULL OR length(btrim(sac_code)) > 0",
    ),
    (
        "job_work_receipt_lines",
        "ck_job_work_receipt_lines_invoiced_non_negative",
        "invoiced_amount >= 0",
    ),
    (
        "job_work_receipt_lines",
        "ck_job_work_receipt_lines_invoiced_within_charges",
        "invoiced_amount <= charges",
    ),
    (
        "job_work_receipt_lines",
        "ck_job_work_receipt_lines_debit_posted_non_negative",
        "debit_posted_amount >= 0",
    ),
    (
        "job_work_receipt_lines",
        "ck_job_work_receipt_lines_debit_posted_within_vendor_debit",
        "debit_posted_amount <= vendor_debit_amount",
    ),
)


def upgrade() -> None:
    op.add_column(
        "vendor_invoices",
        sa.Column("invoice_type", sa.String(length=16), nullable=False, server_default="PURCHASE"),
    )
    op.add_column("vendor_invoices", sa.Column("job_work_order_id", sa.Uuid(), nullable=True))
    op.add_column(
        "vendor_invoices",
        sa.Column(
            "recovered_amount",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "vendor_invoice_lines", sa.Column("job_work_receipt_line_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "vendor_invoice_lines",
        sa.Column(
            "recovery_amount",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("job_work_orders", sa.Column("tax_rate", sa.Numeric(precision=9, scale=4)))
    op.add_column(
        "job_work_orders",
        sa.Column("sac_code", sa.String(length=16), nullable=True, server_default="9988"),
    )
    op.add_column(
        "job_work_receipt_lines",
        sa.Column(
            "invoiced_amount",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "job_work_receipt_lines",
        sa.Column(
            "debit_posted_amount",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
    )

    # NOT VALID, then validated below: see the locking note above.
    op.execute(
        f"ALTER TABLE vendor_invoices ADD CONSTRAINT {JW_ORDER_FK} "
        "FOREIGN KEY (job_work_order_id) REFERENCES job_work_orders (id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        f"ALTER TABLE vendor_invoice_lines ADD CONSTRAINT {JW_RECEIPT_FK} "
        "FOREIGN KEY (job_work_receipt_line_id) REFERENCES job_work_receipt_lines (id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    for table, name, expression in _CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")

    op.create_index(
        "ix_vendor_invoices_type", "vendor_invoices", ["company_id", "invoice_type", "invoice_date"]
    )
    op.create_index(
        "ix_vendor_invoices_job_work_order",
        "vendor_invoices",
        ["company_id", "job_work_order_id"],
    )
    # The column's own ``index=True``: the composite above serves
    # tenant-scoped reads, and this one serves the unconstrained join the
    # migration validation and the ORM relationship walk use.
    op.create_index(
        "ix_vendor_invoices_job_work_order_id", "vendor_invoices", ["job_work_order_id"]
    )
    op.create_index(
        "ix_vendor_invoice_lines_job_work_receipt",
        "vendor_invoice_lines",
        ["company_id", "job_work_receipt_line_id"],
    )
    op.create_index(
        "ix_vendor_invoice_lines_job_work_receipt_line_id",
        "vendor_invoice_lines",
        ["job_work_receipt_line_id"],
    )

    op.execute(f"ALTER TABLE vendor_invoices VALIDATE CONSTRAINT {JW_ORDER_FK}")
    op.execute(f"ALTER TABLE vendor_invoice_lines VALIDATE CONSTRAINT {JW_RECEIPT_FK}")
    for table, name, _expression in _CHECKS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    op.drop_index(
        "ix_vendor_invoice_lines_job_work_receipt_line_id", table_name="vendor_invoice_lines"
    )
    op.drop_index("ix_vendor_invoice_lines_job_work_receipt", table_name="vendor_invoice_lines")
    op.drop_index("ix_vendor_invoices_job_work_order_id", table_name="vendor_invoices")
    op.drop_index("ix_vendor_invoices_job_work_order", table_name="vendor_invoices")
    op.drop_index("ix_vendor_invoices_type", table_name="vendor_invoices")

    for table, name, _expression in reversed(_CHECKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE vendor_invoice_lines DROP CONSTRAINT IF EXISTS {JW_RECEIPT_FK}")
    op.execute(f"ALTER TABLE vendor_invoices DROP CONSTRAINT IF EXISTS {JW_ORDER_FK}")

    op.drop_column("job_work_receipt_lines", "debit_posted_amount")
    op.drop_column("job_work_receipt_lines", "invoiced_amount")
    op.drop_column("job_work_orders", "sac_code")
    op.drop_column("job_work_orders", "tax_rate")
    op.drop_column("vendor_invoice_lines", "recovery_amount")
    op.drop_column("vendor_invoice_lines", "job_work_receipt_line_id")
    op.drop_column("vendor_invoices", "recovered_amount")
    op.drop_column("vendor_invoices", "job_work_order_id")
    op.drop_column("vendor_invoices", "invoice_type")
