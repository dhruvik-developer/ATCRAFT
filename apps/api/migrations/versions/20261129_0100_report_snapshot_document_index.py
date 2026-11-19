"""Index the file a report job produced: ``report_snapshots.document_id``.

Revision ID: 20261129_0100
Revises: 20261128_0100
Create Date: 2026-11-19

The generic document reads hold a generated report file to the access its job
recorded (``ReportSnapshotRepository.catalogue_permission_visibility`` and the
warehouse-scope policies), so every document a restricted principal lists,
counts or downloads is correlated with ``report_snapshots.document_id`` - and
deleting a document nulls the link through the same foreign key. Nothing
indexed the column, so each of those questions compared a document with every
snapshot of its company.

Measured with the application's ``jit = off`` on 5,000 documents and 10,000
snapshots per company, for a principal assigned two of three stores: one report
file's metadata or download read fell from 4.8-5.0 ms to 0.3 ms, a list page
2,000 rows in from 0.8-1.3 s to 0.27-0.46 s, and deleting 200 linked documents
from 0.9-1.4 s to 0.13-0.16 s. The first page moved only from 59-70 ms to
41-42 ms, and the bounded count not at all (0.55-0.83 s either way): its
catalogue-scope check is planned as a nested loop over a materialised set the
planner puts at 6 snapshots (842 in fact), which no index changes. A
``(company_id, document_id)`` index measured the same; the single column also
serves the foreign key's ``SET NULL`` without a skip scan.

Partial, because an inline result links no file. Locking: ``CREATE INDEX``
holds a SHARE lock on ``report_snapshots`` - reads continue, report jobs wait -
for the build, which the guarded release runs with every writer stopped
(docs/deployment.md); it covers only the rows that link a file. No data is
rewritten, and the downgrade drops the index without losing information.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20261129_0100"
down_revision: str | None = "20261128_0100"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_report_snapshots_document_id",
        "report_snapshots",
        ["document_id"],
        unique=False,
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_report_snapshots_document_id", table_name="report_snapshots")
