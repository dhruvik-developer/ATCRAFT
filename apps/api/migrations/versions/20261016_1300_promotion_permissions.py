"""Grant staff promotion administration rights.

Revision ID: 20261016_1300
Revises: 20261016_1200
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_1300"
down_revision: str | None = "20261016_1200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS = ("promotion.view", "promotion.manage")


def upgrade() -> None:
    for right in RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_append(permissions, :right) "
                "WHERE (is_system AND code IN ('SUPER_ADMIN', 'ADMIN')) "
                "OR code = 'SALES_EXECUTIVE' "
                "AND NOT (:right = ANY(permissions))"
            ).bindparams(right=right)
        )


def downgrade() -> None:
    for right in RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :right) "
                "WHERE (is_system AND code IN ('SUPER_ADMIN', 'ADMIN')) "
                "OR code = 'SALES_EXECUTIVE'"
            ).bindparams(right=right)
        )
