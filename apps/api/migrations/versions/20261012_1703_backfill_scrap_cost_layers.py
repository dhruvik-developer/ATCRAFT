"""open a scrap cost layer for the stock already standing in the scrap pool

Revision ID: a91d8a14b2c7
Revises: c20ce1d543b5
Created: 2026-10-12 17:03:00.000000+00:00

From the previous revision on, stock moving into scrap opens a layer in the
position's SCRAP queue at exactly what its ledger row charged, so a scrap
disposal can be valued from what the scrap cost (docs/assumptions.md §E8-2).
Scrap that was already standing in ``scrap_quantity`` before that has no layer.
This revision opens one per such position:

* **quantity** - the position's whole ``scrap_quantity``;
* **unit cost** - the blended cost of the ledger rows that put stock into scrap
  at that grain, ``sum(|total_cost|) / sum(|quantity|)``: what the scrap was
  charged at when it was scrapped. Nothing could take stock *out* of scrap
  before the scrap disposal existed, so those rows are the pool's whole
  history. A position whose history carries no cost falls back to the item's
  standard cost, then to zero - the floor the engine's own reconciliation
  applies to an uncovered quantity;
* **received_at** - the earliest of those rows, so a later scrap layer sorts
  behind it and the legacy scrap is disposed of first;
* ``origin = 'MIGRATION'``, no source transaction - the same marking the FIFO
  revision ``65bcadcc236b`` gave the opening layers it wrote.

One layer per position, not one per historical movement: the order the legacy
scrap would be consumed in among itself was never recorded, and inventing it
would state a history the system does not have. Its total is what matters, and
the verification below refuses to commit unless the queue covers every scrap
quantity exactly.

A row "put stock into scrap" when the engine resolved its landing pool to
SCRAP: a target-qualified type stored with ``stock_state = 'SCRAP'``, or a
``SCRAP`` row stored with the default ``'AVAILABLE'``. The type list is spelled
out - it is the engine's transition table at this revision.

Review (master rules section 67):

* Locking impact:  INSERTs into ``inventory_cost_layers`` only; reads of
                   ``inventory_balances`` and ``inventory_transactions`` take
                   no lock a posting waits on. Run in the deployment window, as
                   the FIFO backfill was: a posting that lands between this
                   revision and the new code rolling out is covered by the
                   engine's own scrap-queue reconciliation.
* Data migration:  yes, and it is the point of the revision. Keyset batches of
                   :data:`BATCH`; layer ids are UUIDv7 for the reason
                   ``65bcadcc236b`` gives. :func:`_verify` refuses to commit a
                   queue that does not add up to the scrap it stands for.
* Rollback:        ``downgrade()`` deletes the layers this revision opened
                   that nothing has consumed. A consumed one means a scrap
                   disposal is on the ledger, and the previous revision's
                   downgrade refuses that case outright.
* Production size: one row per position holding scrap - a handful to a few
                   hundred. Seconds at most.
* Index creation:  none.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "a91d8a14b2c7"
down_revision: str | None = "c20ce1d543b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 500
ORIGIN = "MIGRATION"

#: Types whose stored ``stock_state`` names the pool they landed in - the
#: engine's target-qualified transitions at this revision.
_TARGET_QUALIFIED = (
    "PURCHASE_RECEIPT",
    "PRODUCTION_RECEIPT",
    "JOB_WORK_RECEIPT",
    "MATERIAL_RETURN",
    "ASSEMBLY_RECEIPT",
    "CUSTOMER_RETURN",
    "TRANSFER_IN",
    "ADJUSTMENT_INCREASE",
    "OPENING_BALANCE",
    "QC_RELEASE",
    "JOB_WORK_ISSUE",
    "SCRAP",
    "QC_HOLD",
)
_TARGET_LIST = ", ".join(f"'{value}'" for value in _TARGET_QUALIFIED)

_SELECT = f"""
    SELECT b.id, b.company_id, b.item_id, b.warehouse_id, b.location_id, b.batch_id,
           b.scrap_quantity, b.created_at, i.standard_cost,
           h.charged, h.quantity AS charged_quantity, h.first_at
    FROM inventory_balances b
    JOIN items i ON i.id = b.item_id
    LEFT JOIN LATERAL (
        SELECT sum(abs(t.total_cost)) AS charged,
               sum(abs(t.quantity)) AS quantity,
               min(t.occurred_at) AS first_at
        FROM inventory_transactions t
        WHERE t.company_id = b.company_id
          AND t.item_id = b.item_id
          AND t.warehouse_id = b.warehouse_id
          AND t.location_id IS NOT DISTINCT FROM b.location_id
          AND t.batch_id IS NOT DISTINCT FROM b.batch_id
          AND (
                (t.transaction_type IN ({_TARGET_LIST}) AND t.stock_state = 'SCRAP')
             OR (t.transaction_type = 'SCRAP' AND t.stock_state = 'AVAILABLE')
          )
    ) h ON true
    WHERE b.scrap_quantity > 0
      AND NOT EXISTS (
          SELECT 1 FROM inventory_cost_layers l
          WHERE l.balance_id = b.id AND l.pool = 'SCRAP' AND l.remaining_quantity > 0
      )
      AND b.id > CAST(:after AS uuid)
    ORDER BY b.id
    LIMIT :limit
"""

_INSERT = sa.text(
    """
    INSERT INTO inventory_cost_layers
        (id, company_id, balance_id, item_id, warehouse_id, location_id, batch_id,
         received_at, original_quantity, remaining_quantity, unit_cost, pool,
         source_transaction_id, origin, created_at, updated_at)
    VALUES
        (CAST(:id AS uuid), CAST(:company_id AS uuid), CAST(:balance_id AS uuid),
         CAST(:item_id AS uuid), CAST(:warehouse_id AS uuid),
         CAST(:location_id AS uuid), CAST(:batch_id AS uuid),
         :received_at, :quantity, :quantity, :unit_cost, 'SCRAP',
         NULL, :origin, now(), now())
    """
)

_UNIT_PRICE = Decimal("0.000001")
_MAX_COUNTER = 0x0FFF


def _uuid7(state: dict[str, int]) -> uuid.UUID:
    """A time-ordered UUIDv7, monotonic within one run - see ``65bcadcc236b``.

    Inlined rather than imported from ``src.core.ids``: a migration keeps doing
    what it did on the day it was written, whatever the application becomes.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    if timestamp_ms <= state["timestamp_ms"]:
        timestamp_ms = state["timestamp_ms"]
        state["counter"] += 1
        if state["counter"] > _MAX_COUNTER:
            timestamp_ms += 1
            state["counter"] = 0
    else:
        state["counter"] = int.from_bytes(os.urandom(2), "big") & 0x03FF
    state["timestamp_ms"] = timestamp_ms

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | (state["counter"] & _MAX_COUNTER) << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


def _unit_cost(row: Any) -> Decimal:
    charged = row["charged"]
    quantity = row["charged_quantity"]
    if charged is not None and quantity:
        cost = Decimal(charged) / Decimal(quantity)
        if cost > 0:
            return cost.quantize(_UNIT_PRICE)
    standard = Decimal(row["standard_cost"] or 0)
    return standard.quantize(_UNIT_PRICE) if standard > 0 else Decimal("0.000000")


def _backfill() -> int:
    bind = op.get_bind()
    state = {"timestamp_ms": 0, "counter": 0}
    after = uuid.UUID(int=0)
    written = 0
    while True:
        rows = (
            bind.execute(sa.text(_SELECT), {"after": str(after), "limit": BATCH}).mappings().all()
        )
        if not rows:
            break
        params: list[dict[str, Any]] = [
            {
                "id": str(_uuid7(state)),
                "company_id": str(row["company_id"]),
                "balance_id": str(row["id"]),
                "item_id": str(row["item_id"]),
                "warehouse_id": str(row["warehouse_id"]),
                "location_id": str(row["location_id"]) if row["location_id"] else None,
                "batch_id": str(row["batch_id"]) if row["batch_id"] else None,
                "received_at": row["first_at"] or row["created_at"],
                "quantity": row["scrap_quantity"],
                "unit_cost": _unit_cost(row),
                "origin": ORIGIN,
            }
            for row in rows
        ]
        bind.execute(_INSERT, params)
        written += len(params)
        after = rows[-1]["id"]
    return written


def _verify() -> None:
    """Refuse to commit unless every scrap quantity is exactly covered by its queue."""
    bind = op.get_bind()
    gaps = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM inventory_balances b
            LEFT JOIN LATERAL (
                SELECT coalesce(sum(l.remaining_quantity), 0) AS covered
                FROM inventory_cost_layers l
                WHERE l.balance_id = b.id AND l.pool = 'SCRAP'
            ) q ON true
            WHERE b.scrap_quantity > 0 AND q.covered <> b.scrap_quantity
            """
        )
    ).scalar_one()
    if gaps:
        raise RuntimeError(
            f"Scrap cost backfill does not reconcile: {gaps} position(s) hold scrap their "
            "scrap cost layers do not exactly cover. Nothing was committed."
        )


def upgrade() -> None:
    _backfill()
    _verify()


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM inventory_cost_layers l
        WHERE l.pool = 'SCRAP'
          AND l.origin = '{ORIGIN}'
          AND l.source_transaction_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM inventory_cost_consumptions c WHERE c.layer_id = l.id
          )
        """
    )
