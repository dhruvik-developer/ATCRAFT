"""Grant sales return and credit note permissions.

Revision ID: 20261016_1410
Revises: 20261016_1400
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_1410"
down_revision: str | None = "20261016_1400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_RIGHTS = {
    "SALES_EXECUTIVE": (
        "sales_return.view",
        "sales_return.create",
        "credit_note.view",
    ),
    "DISPATCH_MANAGER": ("sales_return.view", "sales_return.create"),
    "STORE_MANAGER": ("sales_return.view", "sales_return.create"),
    "QC_INSPECTOR": ("sales_return.view",),
    "ACCOUNTS": ("credit_note.view", "credit_note.create", "credit_note.cancel"),
}


def upgrade() -> None:
    for role, rights in ROLE_RIGHTS.items():
        for right in rights:
            op.execute(
                sa.text(
                    "UPDATE roles SET permissions = array_append(permissions, :right) "
                    "WHERE code = :role AND NOT (:right = ANY(permissions))"
                ).bindparams(role=role, right=right)
            )


def downgrade() -> None:
    for role, rights in ROLE_RIGHTS.items():
        for right in rights:
            op.execute(
                sa.text(
                    "UPDATE roles SET permissions = array_remove(permissions, :right) "
                    "WHERE code = :role"
                ).bindparams(role=role, right=right)
            )
