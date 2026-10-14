"""assembly order priority, carried onto its component reservations

Revision ID: 4301062427b5
Revises: 159ffb5b9cb4
Created: 2026-10-12 13:00:00.000000+00:00

An assembly order's release reserves every component HARD, and those
reservations went in at allocation's default priority with no required date -
so a reallocation weighing them against a sales order could not tell an urgent
lock-set build from a speculative one. ``assembly_orders`` gains ``priority``
(lower is more urgent, the production order's scale and default), which release
now passes onto each reservation together with the planned start as its
required date.

Review (master rules section 67):

* Locking impact:  ``ADD COLUMN ... NOT NULL DEFAULT 100`` with a constant
                   default is catalogue-only in PostgreSQL 11+ - no rewrite, no
                   scan - but takes a brief ACCESS EXCLUSIVE lock and queues
                   behind a long transaction on ``assembly_orders``. The CHECK
                   is added ``NOT VALID`` and validated separately: VALIDATE
                   takes SHARE UPDATE EXCLUSIVE, which blocks neither reads nor
                   writes.
* Data migration:  none. Every existing order takes the default, 100, which is
                   exactly the priority its reservations were already taken at.
* Rollback:        ``downgrade()`` drops the constraint and the column. The
                   value is a planning preference with nothing else reading it
                   below this revision; reservations already taken keep the
                   priority they were taken at.
* Production size: one row per assembly order - thousands, not a ledger.
* Index creation:  none. The board and planning read priority off rows they
                   reach through the existing status indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4301062427b5"
down_revision: str | None = "159ffb5b9cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_NAME = "ck_assembly_orders_priority_non_negative"


def upgrade() -> None:
    op.add_column(
        "assembly_orders",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
    )
    # NOT VALID, then validated: see the locking note above.
    op.execute(
        f"ALTER TABLE assembly_orders ADD CONSTRAINT {CHECK_NAME} CHECK (priority >= 0) NOT VALID"
    )
    op.execute(f"ALTER TABLE assembly_orders VALIDATE CONSTRAINT {CHECK_NAME}")


def downgrade() -> None:
    # op.f(): the name is already the full ``ck_<table>_<name>`` form, and must
    # not have the metadata naming convention applied to it a second time.
    op.drop_constraint(op.f(CHECK_NAME), "assembly_orders", type_="check")
    op.drop_column("assembly_orders", "priority")
