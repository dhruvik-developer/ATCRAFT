"""job-work receipt line inspection, one per output item of a returned lot

Revision ID: 948902066228
Revises: 4301062427b5
Created: 2026-10-12 13:01:00.000000+00:00

A job-work lot that came back as several output items raised one inspection
naming the first item for the whole lot's total, which could not be decided: it
asked quality to release more of that item than was held. A lot now raises one
inspection per output item, and each receipt line records the inspection that
holds its output, so ``job_work_receipt_lines`` gains ``qc_inspection_id`` - a
bare UUID with no foreign key, for the same reason the receipt's own column has
none: quality owns ``qc_inspections`` and the line must not depend on it.

Existing lots are backfilled where the receipt's one inspection is for the
line's own output item: those lines are exactly the ones that inspection held.
Lines of another output item on such a lot keep NULL - no inspection was ever
raised for them, which is the defect this revision stops repeating.

Review (master rules section 67):

* Locking impact:  ``ADD COLUMN ... NULL`` is catalogue-only (no rewrite, no
                   scan) but takes a brief ACCESS EXCLUSIVE lock on
                   ``job_work_receipt_lines``. The backfill UPDATE takes row
                   locks on the lines it writes for the length of the statement.
* Data migration:  yes - one set-based UPDATE joining the line to its receipt
                   and the receipt's inspection, no row loop. It only writes
                   lines whose column is still NULL, so a re-run changes
                   nothing. The number of lines linked is logged.
* Rollback:        ``downgrade()`` drops the column. Nothing below this revision
                   reads it; the receipt keeps its own ``qc_inspection_id``, so
                   no link that existed before the upgrade is lost.
* Production size: one row per receipt line - thousands, not a ledger. Seconds.
* Index creation:  none. The line's inspection is read with its receipt, never
                   looked up on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "948902066228"
down_revision: str | None = "4301062427b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

#: A line takes its receipt's inspection when that inspection inspected the
#: line's own output item, in the same company, and the line returned good
#: output to hold. Anything else stays NULL.
BACKFILL = """
    UPDATE job_work_receipt_lines AS line
    SET qc_inspection_id = receipt.qc_inspection_id
    FROM job_work_receipts AS receipt
    JOIN qc_inspections AS inspection
      ON inspection.id = receipt.qc_inspection_id
     AND inspection.company_id = receipt.company_id
    WHERE line.receipt_id = receipt.id
      AND line.company_id = receipt.company_id
      AND line.qc_inspection_id IS NULL
      AND line.output_quantity > 0
      AND inspection.item_id = line.output_item_id
"""


def upgrade() -> None:
    op.add_column(
        "job_work_receipt_lines",
        sa.Column("qc_inspection_id", sa.Uuid(), nullable=True),
    )
    result = op.get_bind().execute(sa.text(BACKFILL))
    log.info("job_work_receipt_lines: %s line(s) linked to their inspection", result.rowcount)


def downgrade() -> None:
    op.drop_column("job_work_receipt_lines", "qc_inspection_id")
