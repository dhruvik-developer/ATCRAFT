"""Attribute every dispatch movement to the exact document line that posted it.

Revision ID: 20261111_0100
Revises: 20261110_0100

A dispatch line can post more than one ``DISPATCH`` movement: one per reservation
position, or one per serial. ``dispatch_lines.inventory_transaction_id`` anchors
only the first, so profitability cannot recover the full line cost from it. The
new ``source_line_id`` carries the exact link on every movement.

The backfill first trusts the existing anchor, then falls back to a one-to-one
match on dispatch, item, batch and serial. Rows that could belong to more than
one line stay NULL: the report then shows their cost as pending instead of
inventing an attribution. The append-only trigger is disabled only around the
backfill and re-enabled before the transaction commits.

Writers should be drained for the bounded-lock schema/backfill operation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261111_0100"
down_revision = "20261110_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("inventory_transactions", sa.Column("source_line_id", sa.UUID(), nullable=True))

    # The ledger is append-only at the trigger level. A backfill is the one
    # legitimate in-place write: it records provenance that was always implied
    # by the row, and it never changes quantity, value or balance.
    op.execute(
        "ALTER TABLE inventory_transactions DISABLE TRIGGER trg_inventory_transactions_immutable"
    )
    # If either statement below fails, the migration transaction rolls back and
    # the DISABLE above rolls back with it, so the trigger is never left off.
    op.execute(
        """
        WITH anchored AS (
            SELECT line.id AS line_id,
                   line.inventory_transaction_id AS transaction_id,
                   count(*) OVER (
                       PARTITION BY line.inventory_transaction_id
                   ) AS matches
            FROM dispatch_lines AS line
            WHERE line.inventory_transaction_id IS NOT NULL
        )
        UPDATE inventory_transactions AS movement
           SET source_line_id = anchored.line_id
          FROM anchored
         WHERE movement.id = anchored.transaction_id
           AND movement.source_line_id IS NULL
           AND anchored.matches = 1
        """
    )
    op.execute(
        """
        WITH candidates AS (
            SELECT movement.id AS transaction_id,
                   (array_agg(line.id))[1] AS line_id,
                   count(*) AS matches
            FROM inventory_transactions AS movement
            JOIN dispatch_lines AS line
              ON line.company_id = movement.company_id
             AND line.dispatch_id = movement.reference_id
             AND line.item_id = movement.item_id
             AND line.batch_id IS NOT DISTINCT FROM movement.batch_id
             AND (
                 movement.serial_id IS NULL
                 OR movement.serial_id = ANY(line.serial_ids)
             )
            WHERE movement.transaction_type = 'DISPATCH'
              AND movement.reference_type = 'dispatch'
              AND movement.source_line_id IS NULL
            GROUP BY movement.id
        )
        UPDATE inventory_transactions AS movement
           SET source_line_id = candidates.line_id
          FROM candidates
         WHERE movement.id = candidates.transaction_id
           AND candidates.matches = 1
        """
    )
    op.execute(
        "ALTER TABLE inventory_transactions ENABLE TRIGGER trg_inventory_transactions_immutable"
    )

    op.create_index(
        "ix_inventory_transactions_source_line",
        "inventory_transactions",
        ["company_id", "source_line_id"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Attribution can be exact only while this column exists: two lines may
    # share an item and lot but not a serial, and the ledger row alone cannot
    # say which one posted it. Refuse rather than silently lose that link.
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM inventory_transactions WHERE source_line_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("Dispatch line attribution exists; downgrade would discard it.")
    op.drop_index("ix_inventory_transactions_source_line", table_name="inventory_transactions")
    op.drop_column("inventory_transactions", "source_line_id")
