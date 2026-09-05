"""purchase return line stock state

Revision ID: 3c8f2a7d5b91
Revises: d91ec6b47a20
Created: 2026-09-05 00:00:00+00:00

Adds ``purchase_return_lines.stock_state``: the pool a returned quantity leaves.
A return always posted out of ``AVAILABLE``, so goods an incoming inspection
had rejected into ``DAMAGED`` could not be returned at all for a batch-tracked
item (the AVAILABLE grain for that lot is empty) and, for an untracked item with
other good stock, deducted the good stock and left the damaged units in place.
The service now records the pool per line and posts against it.

Locking impact: one ``ADD COLUMN`` with a constant default and one CHECK
constraint. On PostgreSQL 11+ a constant ``server_default`` on a NOT NULL column
is a catalogue-only change with no table rewrite; the CHECK scans the table
once, which is small. Both need a brief ACCESS EXCLUSIVE lock, so run outside
long transactions.

Data migration: none. Every existing line was posted out of AVAILABLE, which is
exactly what the default records for it. Rollback drops the column and the
constraint; nothing else reads them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c8f2a7d5b91"
down_revision: str | None = "d91ec6b47a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "purchase_return_lines",
        sa.Column(
            "stock_state",
            sa.String(length=24),
            nullable=False,
            server_default="AVAILABLE",
        ),
    )
    op.create_check_constraint(
        op.f("ck_purchase_return_lines_stock_state_returnable"),
        "purchase_return_lines",
        "stock_state IN ('AVAILABLE', 'DAMAGED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_purchase_return_lines_stock_state_returnable"),
        "purchase_return_lines",
        type_="check",
    )
    op.drop_column("purchase_return_lines", "stock_state")
