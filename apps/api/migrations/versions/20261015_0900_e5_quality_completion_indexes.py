"""Quality completion (WP E5) follow-up: the three model indexes the base revision
did not create.

Revision ID: e5b240120002
Revises: e5a140120001
Created: 2026-10-15 09:00:00.000000+00:00

* ``ix_qc_rework_records_inspection_id`` - the single-column index behind the
  ``index=True`` on ``QcReworkRecord.inspection_id``. The base revision created
  the composite ``ix_qc_rework_records_inspection`` but not this one; every
  other ``*_inspection_id`` FK column in the module carries both.
* ``ix_qc_rework_records_open`` - the partial bench work-list index
  (``company_id, status`` where the cycle is unfinished), which the rework
  list's status filter reads.
* ``ix_qc_inspection_decisions_inspection_id`` - the single-column index behind
  the ``index=True`` on ``QcInspectionDecision.inspection_id``.

Review (master rules section 67):

* Locking impact:  three plain ``CREATE INDEX`` builds, each taking a brief
                    SHARE lock on its own (near-empty) table. ``CONCURRENTLY``
                    cannot run inside the migration transaction.
* Data migration:  none.
* Rollback:        drops the three indexes; the base revision's downgrade
                    drops the tables themselves.
* Production size: both tables start empty and grow one row per rework cycle
                    or verdict.
* Index creation:  the three indexes below, nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5b240120002"
down_revision: str | None = "e5a140120001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_qc_rework_records_inspection_id"),
        "qc_rework_records",
        ["inspection_id"],
        unique=False,
    )
    op.create_index(
        "ix_qc_rework_records_open",
        "qc_rework_records",
        ["company_id", "status"],
        unique=False,
        postgresql_where="status <> 'DONE'",
    )
    op.create_index(
        op.f("ix_qc_inspection_decisions_inspection_id"),
        "qc_inspection_decisions",
        ["inspection_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_qc_inspection_decisions_inspection_id"),
        table_name="qc_inspection_decisions",
    )
    op.drop_index("ix_qc_rework_records_open", table_name="qc_rework_records")
    op.drop_index(op.f("ix_qc_rework_records_inspection_id"), table_name="qc_rework_records")
