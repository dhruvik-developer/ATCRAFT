"""enforce one pending request per document action

Revision ID: 20261120_0100
Revises: 20261119_0100
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261120_0100"
down_revision: str | None = "20261119_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "uq_approval_requests_pending_entity_action"


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT company_id, entity_type, entity_id, requested_action, count(*) "
                "FROM approval_requests WHERE status = 'PENDING' "
                "GROUP BY company_id, entity_type, entity_id, requested_action "
                "HAVING count(*) > 1 ORDER BY company_id, entity_type, entity_id, requested_action "
                "LIMIT 10"
            )
        )
        .all()
    )
    if duplicates:
        raise RuntimeError(
            "Cannot add the pending-approval uniqueness index: duplicate open requests exist. "
            "Resolve each listed company/entity/action through its owning workflow, then retry "
            f"the migration. Duplicate groups: {duplicates!r}"
        )

    op.create_index(
        INDEX_NAME,
        "approval_requests",
        ["company_id", "entity_type", "entity_id", "requested_action"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="approval_requests")
