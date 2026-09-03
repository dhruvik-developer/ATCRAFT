"""fifo opening layer correction

Revision ID: 7a1e4c9b2d55
Revises: 66b1c0e2d1f4
Created: 2026-09-02 14:20:00.000000+00:00

Corrects, forward, three defects in revision ``65bcadcc236b`` (FIFO cost
layers). That revision is **already applied** to the application database, so it
is left exactly as it shipped: editing an applied migration in place changes
what a database that has already run it is believed to contain, and every other
environment would then be reconciled against a file that never ran there. A data
defect that has already reached a schema is corrected by a new revision, in the
open, or it is not corrected at all.

It chains off ``66b1c0e2d1f4`` rather than off ``65bcadcc236b`` itself, which is
what the defect report asked for, only because another revision landed on
``65bcadcc236b`` first. Chaining there too would leave the tree with two heads
and ``alembic upgrade head`` would refuse to run. The correction is unrelated to
what ``66b1c0e2d1f4`` does (MRP late-supply dates), so the ordering is
immaterial; a single head is not.

What was wrong
--------------

1. **The opening layers were dated at the newest event at the position.**
   ``65bcadcc236b`` dated each backfilled layer at
   ``coalesce(last_movement_at, created_at)`` - the *last* time anything
   happened there. An opening layer stands for the oldest stock at the position:
   everything that predates FIFO valuation, as one undifferentiated lot. Under
   FIFO the date decides the consumption order, so dating it at the newest event
   put it at the **back** of the queue. A layer for genuinely older stock,
   opened after the migration by a receipt keyed with a backdated
   ``occurred_at``, would then be consumed *before* the opening stock - the
   opposite of the method. This revision re-dates every backfilled layer to a
   microsecond before the earliest thing known at its position, which is what
   ``MovementEngine._reconciliation_timestamp`` already does for the same reason
   (the FIFO key breaks a tie on the UUIDv7 primary key, and a migration layer's
   id is not the oldest, so an *equal* timestamp would sort it last again).

2. **The backfill and its verification were anchored on the wrong column.**
   Both used ``on_hand_quantity * average_cost``. ``average_cost`` is a derived
   six-place per-unit figure; ``stock_value`` is the column the finance
   valuation report reads and the one that reaches a balance sheet. Where the
   two disagree - and they disagree by the rounding of a six-place unit price
   multiplied back up by the quantity - the layers were built to match the
   figure nobody reports. This revision re-prices the untouched opening layers
   from ``stock_value`` and verifies against ``stock_value``.

3. **The locking-impact review named the wrong tables.** It said the two
   ``CREATE TABLE`` statements "take ACCESS EXCLUSIVE on tables this transaction
   created moments earlier, so nothing else can hold or want that lock", and
   named only ``stock_transfer_lines`` and ``inventory_transactions`` as tables
   outside the revision that it locks. That is not what those statements do. A
   ``CREATE TABLE`` with foreign keys also takes a lock on every **referenced**
   table, and the backfill reads the hottest table in the system from end to
   end. The corrected review is below, for the record and for whoever writes the
   next one.

   ``inventory_cost_layers`` references ``inventory_balances``, ``items``,
   ``warehouses``, ``storage_locations``, ``stock_batches``,
   ``inventory_transactions``, ``companies`` and ``users`` - eight pre-existing
   tables, each taking SHARE ROW EXCLUSIVE for the duration of the CREATE.
   ``inventory_cost_consumptions`` adds ``inventory_cost_layers``,
   ``inventory_transactions``, ``companies`` and ``users``. SHARE ROW EXCLUSIVE
   conflicts with itself and with ROW EXCLUSIVE, which is the lock every
   ordinary INSERT/UPDATE/DELETE takes - so while it is held, **writes to
   ``inventory_balances`` and ``inventory_transactions`` block**, not just DDL.
   That is the real window, and it is far wider than "tables this transaction
   created moments earlier". Then ``_backfill`` reads every
   ``inventory_balances`` row holding stock, in keyset pages, inside the same
   transaction - so those locks are held for the duration of the backfill, not
   for the microseconds the DDL takes. On a busy system the revision needs a
   maintenance window; it was described as though it did not.

Review checklist (master rules section 67)
------------------------------------------

* Locking impact:      no DDL at all. Three UPDATE statements, each taking
                       ROW EXCLUSIVE on its table and a row lock per row
                       touched:

                       - ``inventory_cost_layers`` - every row with
                         ``origin = 'MIGRATION'``, one per stock position that
                         existed when FIFO landed. It conflicts with a
                         concurrent posting at the *same* position only, and the
                         posting takes the balance row lock first, so the two
                         serialise the way any two postings do.
                       - ``inventory_balances`` - only the positions whose
                         opening layer is still untouched and whose stored value
                         disagrees with it at four places. Usually none.

                       Both are held to the end of the transaction, so the
                       revision should run when the warehouse is quiet, but it
                       blocks nothing it does not touch and needs no window on a
                       system that is idle.

* Data migration:      yes, and it is the whole revision - see above. Total
                       stock value is unchanged except for a rounding residual
                       under 0.0001 a position, which is moved *onto*
                       ``stock_value`` so the balance and its layers reconcile
                       exactly rather than being left a fraction apart.

* Rollback:            ``downgrade()`` is a deliberate no-op and says so at the
                       point it does nothing. This revision adds no schema, so
                       there is nothing to drop; and the values it replaced were
                       wrong, so restoring them is not a rollback, it is
                       re-breaking the FIFO consumption order on purpose. The
                       old dates and prices are not recoverable in any case -
                       ``last_movement_at`` has moved on since. Downgrading past
                       this revision leaves the corrected data in place, which
                       is both the honest outcome and the safe one: the code at
                       ``66b1c0e2d1f4`` reads these columns exactly the same way.

* Production size:     one indexed UPDATE per stock position that existed at the
                       FIFO cutover - tens of thousands at most - plus a
                       verification pass over the same rows. Seconds.

* Index creation:      none.

Hand-written additions autogenerate could not produce
-----------------------------------------------------

All of it. Autogenerate compares schemas; this revision changes no schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1e4c9b2d55"
down_revision: str | None = "66b1c0e2d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Marks the layers ``65bcadcc236b`` created. Mirrors
#: ``src.modules.inventory.models.LAYER_ORIGIN_MIGRATION``; spelled literally
#: here for the same reason that revision spelled it literally - a migration
#: must not import application code that can change under it.
LAYER_ORIGIN_MIGRATION = "MIGRATION"

#: How far apart a position's stored value and its layers may be before this
#: revision refuses to commit. ``stock_value`` is ``NUMERIC(18, 4)`` and a
#: layer's ``unit_cost`` is ``NUMERIC(18, 6)``, so re-pricing from the value
#: cannot always land on it exactly; the residual is under half of 0.0001 a
#: position before the snap below, and zero after it. Anything larger is a
#: backfill that did not reconcile, which is what the check exists to catch.
RECONCILIATION_TOLERANCE = "0.01"


# --- the correction ----------------------------------------------------------

#: An opening layer is the oldest stock at its position, so it must sort before
#: every other layer there. A microsecond earlier than the earliest of them -
#: not equal to it, because the FIFO key ``(received_at, id)`` breaks a tie on
#: the primary key and these ids are not the oldest in the table.
_REDATE_OPENING_LAYERS = sa.text(
    """
    UPDATE inventory_cost_layers AS l
    SET received_at = anchor.stamp - interval '1 microsecond',
        updated_at = now()
    FROM inventory_balances AS b
    CROSS JOIN LATERAL (
        SELECT least(
            b.created_at,
            (SELECT min(o.received_at)
             FROM inventory_cost_layers AS o
             WHERE o.balance_id = b.id
               AND o.origin IS DISTINCT FROM :origin)
        ) AS stamp
    ) AS anchor
    WHERE l.balance_id = b.id
      AND l.company_id = b.company_id
      AND l.origin = :origin
      AND l.received_at >= anchor.stamp
    """
)

#: Re-price an opening layer from ``stock_value`` - the column finance reads -
#: rather than from ``on_hand_quantity * average_cost``.
#:
#: Deliberately narrow. Only a layer that is still exactly as the backfill wrote
#: it (``remaining = original``), is the position's only open layer, and still
#: accounts for the position's whole on-hand is re-priced. Anything else has had
#: real movements posted against it since, its ``stock_value`` reflects those
#: movements as well, and dividing the current value by the surviving quantity
#: would invent a price the position never paid.
_OPENING_LAYER_PREDICATES = """
      l.balance_id = b.id
      AND l.company_id = b.company_id
      AND l.origin = :origin
      AND l.remaining_quantity > 0
      AND l.remaining_quantity = l.original_quantity
      AND b.on_hand_quantity = l.remaining_quantity
      AND NOT EXISTS (
          SELECT 1 FROM inventory_cost_layers AS o
          WHERE o.balance_id = b.id
            AND o.remaining_quantity > 0
            AND o.id <> l.id
      )
"""

_REPRICE_OPENING_LAYERS = sa.text(
    f"""
    UPDATE inventory_cost_layers AS l
    SET unit_cost = round(b.stock_value / l.remaining_quantity, 6),
        updated_at = now()
    FROM inventory_balances AS b
    WHERE {_OPENING_LAYER_PREDICATES}
      AND round(l.remaining_quantity * l.unit_cost, 4) <> round(b.stock_value, 4)
    """
)

#: The residual the six-place re-pricing above cannot land on exactly, moved
#: onto the balance so the position and its layers reconcile to the paisa.
#: Under FIFO the layers are the definition of a position's value and
#: ``stock_value`` is the engine's record of them, so snapping the record to the
#: definition is making the two agree, not restating the accounts: the whole
#: movement is under 0.0001 a position, in the direction the layers already say.
#: ``average_cost`` is ``stock_value / on_hand``, which for a single-layer
#: position is the layer's own price, so it is written from the same number.
_SNAP_BALANCE_TO_LAYER = sa.text(
    f"""
    UPDATE inventory_balances AS b
    SET stock_value = round(l.remaining_quantity * l.unit_cost, 4),
        average_cost = l.unit_cost,
        updated_at = now()
    FROM inventory_cost_layers AS l
    WHERE {_OPENING_LAYER_PREDICATES}
      AND b.stock_value <> round(l.remaining_quantity * l.unit_cost, 4)
    """
)


def _verify(bind: sa.engine.Connection) -> None:
    """Refuse to commit a correction that did not correct.

    Two questions, and this revision exists because the answer to the second one
    was never asked of the right column.

    1. Does every backfilled layer now sort before everything else at its
       position? If one does not, the FIFO consumption order is still wrong
       there and the re-dating missed it.
    2. Do the untouched opening positions reconcile to ``stock_value`` - the
       column the finance valuation report reads and a balance sheet is drawn
       from? The original revision asked this of ``on_hand * average_cost``,
       which is a derived figure, so it could pass while the reported value was
       something else.
    """
    misordered = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM inventory_cost_layers AS l
            WHERE l.origin = :origin
              AND EXISTS (
                  SELECT 1 FROM inventory_cost_layers AS o
                  WHERE o.balance_id = l.balance_id
                    AND o.origin IS DISTINCT FROM :origin
                    AND o.received_at <= l.received_at
              )
            """
        ),
        {"origin": LAYER_ORIGIN_MIGRATION},
    ).scalar_one()

    drifted = bind.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM inventory_cost_layers AS l
            JOIN inventory_balances AS b ON {_OPENING_LAYER_PREDICATES}
            WHERE abs(b.stock_value - l.remaining_quantity * l.unit_cost)
                  > CAST(:tolerance AS numeric)
            """
        ),
        {"origin": LAYER_ORIGIN_MIGRATION, "tolerance": RECONCILIATION_TOLERANCE},
    ).scalar_one()

    problems = []
    if misordered:
        problems.append(
            f"{misordered} opening cost layers still sort at or after a later "
            "layer at the same position"
        )
    if drifted:
        problems.append(
            f"{drifted} opening positions do not reconcile to their own stock_value "
            f"within {RECONCILIATION_TOLERANCE}"
        )
    if problems:
        raise RuntimeError(
            "FIFO opening-layer correction did not reconcile, refusing to commit: "
            + "; ".join(problems)
        )


def upgrade() -> None:
    bind = op.get_bind()
    # ``origin`` is bound rather than spelled into the statement: it is the one
    # value these statements select on, and a migration that interpolates the
    # value it filters by is a migration whose SQL changes shape if the constant
    # ever grows a quote.
    marker = {"origin": LAYER_ORIGIN_MIGRATION}
    bind.execute(_REDATE_OPENING_LAYERS, marker)
    bind.execute(_REPRICE_OPENING_LAYERS, marker)
    bind.execute(_SNAP_BALANCE_TO_LAYER, marker)
    _verify(bind)


def downgrade() -> None:
    """Deliberately does nothing. See the Rollback note in the module docstring.

    This revision adds no schema, so there is nothing to drop. It replaced two
    wrong values with two right ones, and the wrong ones are not recoverable -
    ``last_movement_at``, which the wrong date was copied from, has moved on
    since. Putting the FIFO consumption order back the way it was would not be a
    rollback; it would be a decision to consume the newest stock first again.

    Downgrading past this revision therefore leaves the corrected data in place,
    which is safe: nothing before it reads ``received_at`` or ``unit_cost``
    differently.
    """
