"""fifo cost layers

Revision ID: 65bcadcc236b
Revises: ad9f573e5e59
Created: 2026-09-02 06:42:39.725087+00:00

Replaces the moving weighted average with true FIFO valuation.

Two new tables and one new column:

``inventory_cost_layers``        one row per inbound movement - the quantity
                                received and the price paid. Outbound movements
                                consume them oldest first.
``inventory_cost_consumptions``  which layers each outbound movement drew on,
                                and what that cost. Append-only.
``stock_transfer_lines.dispatch_transaction_id``
                                the ``TRANSFER_OUT`` movement a line posted, so
                                the arrival can re-open exactly the layers the
                                dispatch consumed instead of re-costing the
                                stock from an average.

``inventory_balances.average_cost`` is deliberately **kept**. It stops being a
moving average maintained by its own arithmetic and becomes a derived display
figure - ``stock_value / on_hand_quantity``, recomputed from the layers on every
posting - because four things outside the inventory module read it (the finance
valuation query twice, its response schema, and the Excel export). Dropping it
would have been a wider, riskier change for no gain; leaving it as a second,
independently maintained valuation would have been worse than either.

Review checklist (master rules section 67)
------------------------------------------

* Locking impact:      three shapes, and only one is worth planning around.

                       - The two CREATE TABLEs and their indexes take
                         ACCESS EXCLUSIVE on tables this transaction created
                         moments earlier, so nothing else can hold or want that
                         lock. Not built CONCURRENTLY, deliberately: CREATE
                         INDEX CONCURRENTLY cannot run inside a transaction and
                         buys nothing on a zero-row table.

                       - ``ADD COLUMN dispatch_transaction_id ... NULL`` is a
                         catalogue-only change on PostgreSQL 11+. It takes
                         ACCESS EXCLUSIVE on ``stock_transfer_lines`` for the
                         duration of the statement, which is microseconds, but
                         the *acquisition* still queues behind any open
                         transaction holding a conflicting lock on that table.

                       - ``ADD CONSTRAINT ... FOREIGN KEY`` is the one to know
                         about. It needs SHARE ROW EXCLUSIVE on **both**
                         ``stock_transfer_lines`` and ``inventory_transactions``,
                         and the latter is the hottest table in the system:
                         while it is held, every stock posting waits. The
                         validation itself is instant - every value in the new
                         column is NULL - so the window is the lock wait, not
                         the work. On a busy system, split it:
                         ``ADD CONSTRAINT ... NOT VALID`` (a brief lock) and
                         then ``VALIDATE CONSTRAINT`` (SHARE UPDATE EXCLUSIVE,
                         which does not block writes). It is done in one step
                         here because this schema's ``stock_transfer_lines`` is
                         small and the deployment takes a maintenance window.

* Data migration:      yes, and it is the point of the revision. Every
                       ``inventory_balances`` row holding stock gets exactly one
                       opening cost layer, for its whole on-hand quantity, at
                       its own ``average_cost``. Total stock value is therefore
                       identical before and after: the value the balance already
                       claimed is simply written where FIFO can see it. Read in
                       keyset batches of :data:`BACKFILL_BATCH` so the statement
                       size does not grow with the table.

                       ``inventory_balances.stock_value`` and ``average_cost``
                       are **not** rewritten. They already hold
                       ``on_hand x average_cost``, which is what the layers now
                       add up to, and a migration that rewrote a balance-sheet
                       figure would be changing the accounts rather than
                       changing how they are computed. :func:`_verify_backfill`
                       refuses to commit if the layers do not reconcile to the
                       balances they came from.

                       Layer ids are UUIDv7, not ``gen_random_uuid()``. FIFO
                       breaks a ``received_at`` tie on the primary key, and a
                       random v4 id sorts arbitrarily against the v7 ids the
                       application mints - so an opening layer could end up
                       *behind* a later receipt that happened to share its
                       timestamp. Midnight timestamps make that tie real:
                       several callers stamp ``occurred_at`` as a date at
                       00:00:00.

* Rollback:            ``downgrade()`` drops both tables and the column. It
                       loses the layers and the consumption history - there is
                       nowhere else to put them - so the FIFO breakdown of past
                       issues is gone and cannot be reconstructed.

                       What it does **not** lose is the valuation.
                       ``average_cost`` and ``stock_value`` are untouched by
                       this revision and are maintained by the FIFO engine as
                       derived figures, so at the moment of a rollback they hold
                       the position's true cost basis and value. The moving
                       average the older code resumes from is therefore correct
                       on the day, and total stock value is unchanged across the
                       rollback. What changes from then on is the method.

* Production size:     seconds. Two empty CREATE TABLEs, eight indexes on empty
                       tables, one catalogue-only ADD COLUMN, one instantly
                       validated foreign key, and one INSERT ... SELECT-shaped
                       backfill whose cost is one row per stock position - tens
                       of thousands at most, and the read is a plain index scan.

* Index creation:      not CONCURRENTLY, deliberately - see locking impact.

Hand-written additions autogenerate could not produce
-----------------------------------------------------

1. The backfill and its verification.
2. ``trg_inventory_cost_consumptions_immutable``. The consumption table is an
   ``ImmutableLedgerEntity`` and carries financial history - it is the evidence
   for what a past issue cost - so it gets the same append-only trigger the
   initial revision put on ``audit_logs`` and ``inventory_transactions``, using
   the ``atcraft_reject_row_mutation()`` function that revision installed. The
   cost *layers* deliberately get no such trigger: ``remaining_quantity`` is
   updated on every consumption, which is the whole mechanism.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "65bcadcc236b"
down_revision: str | None = "ad9f573e5e59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Balance rows read - and layers written - per round trip during the backfill.
BACKFILL_BATCH = 1_000

#: Marks a layer that has no movement behind it because this revision created
#: it. Mirrors ``src.modules.inventory.models.LAYER_ORIGIN_MIGRATION``; spelled
#: literally here because a migration must not import application code that can
#: change under it.
LAYER_ORIGIN_MIGRATION = "MIGRATION"

#: The nil UUID the balance grain's unique index COALESCEs NULL location and
#: batch to. Repeated here for the same reason as the constant above.
GRAIN_NULL_SENTINEL = "00000000-0000-0000-0000-000000000000"

IMMUTABILITY_TRIGGER = "trg_inventory_cost_consumptions_immutable"


# --- UUIDv7, inlined ---------------------------------------------------------
# A deliberate copy of the shape of ``src.core.ids.uuid7`` rather than an import
# of it: migrations in this project are self-contained, so that a revision keeps
# doing what it did on the day it was written even after the application module
# it would otherwise have imported has moved on.
#
# Monotonicity is the property that matters here, not novelty. The FIFO
# consumption order is ``(received_at, id)``, and the id is what resolves a tie -
# which is not hypothetical, because several callers stamp ``occurred_at`` as a
# calendar date at midnight and two movements on one date share it exactly.

_MAX_COUNTER = 0x0FFF


def _uuid7(state: dict[str, int]) -> uuid.UUID:
    """A time-ordered UUIDv7, monotonic within one call to :func:`_backfill`."""
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


# --- backfill ----------------------------------------------------------------

_SELECT_FIRST_PAGE = sa.text(
    """
    SELECT id, company_id, item_id, warehouse_id, location_id, batch_id,
           on_hand_quantity, average_cost,
           coalesce(last_movement_at, created_at) AS received_at
    FROM inventory_balances
    WHERE on_hand_quantity > 0
    ORDER BY id
    LIMIT :limit
    """
)

_SELECT_NEXT_PAGE = sa.text(
    """
    SELECT id, company_id, item_id, warehouse_id, location_id, batch_id,
           on_hand_quantity, average_cost,
           coalesce(last_movement_at, created_at) AS received_at
    FROM inventory_balances
    WHERE on_hand_quantity > 0 AND id > CAST(:after AS uuid)
    ORDER BY id
    LIMIT :limit
    """
)

_INSERT_LAYER = sa.text(
    """
    INSERT INTO inventory_cost_layers
        (id, company_id, balance_id, item_id, warehouse_id, location_id, batch_id,
         received_at, original_quantity, remaining_quantity, unit_cost,
         source_transaction_id, origin, created_at, updated_at)
    VALUES
        (CAST(:id AS uuid), CAST(:company_id AS uuid), CAST(:balance_id AS uuid),
         CAST(:item_id AS uuid), CAST(:warehouse_id AS uuid),
         CAST(:location_id AS uuid), CAST(:batch_id AS uuid),
         :received_at, :quantity, :quantity, :unit_cost,
         NULL, :origin, now(), now())
    """
)


def _backfill() -> int:
    """One opening layer per position that holds stock, at its own average cost.

    Keyset-paged on the primary key rather than OFFSET, so the cost of the last
    page is the same as the cost of the first. Every layer is written for the
    position's *whole* on-hand quantity: the stock that was there before FIFO
    existed is one undifferentiated lot, and splitting it into invented layers
    would state a purchase history the system does not have.
    """
    bind = op.get_bind()
    state = {"timestamp_ms": 0, "counter": 0}
    after: uuid.UUID | None = None
    written = 0

    while True:
        if after is None:
            rows = bind.execute(_SELECT_FIRST_PAGE, {"limit": BACKFILL_BATCH}).mappings().all()
        else:
            rows = (
                bind.execute(_SELECT_NEXT_PAGE, {"after": str(after), "limit": BACKFILL_BATCH})
                .mappings()
                .all()
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
                "received_at": row["received_at"],
                "quantity": row["on_hand_quantity"],
                "unit_cost": row["average_cost"],
                "origin": LAYER_ORIGIN_MIGRATION,
            }
            for row in rows
        ]
        bind.execute(_INSERT_LAYER, params)
        written += len(params)
        after = rows[-1]["id"]

    return written


def _verify_backfill(written: int) -> None:
    """Refuse to commit a backfill that does not reconcile to the balances.

    Three questions, and all three have to answer yes before this revision is
    allowed to become the schema: did every position that holds stock get a
    layer, does the total quantity in the layers equal the total on hand, and
    does the total value they carry equal the value the balances already
    claimed. A half-finished backfill would leave positions valued at nothing
    and issues costed out of thin air, which is a far more expensive thing to
    discover later than a failed migration is now.
    """
    bind = op.get_bind()
    positions, quantity, value = bind.execute(
        sa.text(
            """
            SELECT count(*),
                   coalesce(sum(on_hand_quantity), 0),
                   coalesce(sum(round(on_hand_quantity * average_cost, 4)), 0)
            FROM inventory_balances
            WHERE on_hand_quantity > 0
            """
        )
    ).one()
    layers, layer_quantity, layer_value = bind.execute(
        sa.text(
            """
            SELECT count(*),
                   coalesce(sum(remaining_quantity), 0),
                   coalesce(sum(round(remaining_quantity * unit_cost, 4)), 0)
            FROM inventory_cost_layers
            WHERE origin = :origin
            """
        ),
        {"origin": LAYER_ORIGIN_MIGRATION},
    ).one()

    problems = []
    if written != positions or layers != positions:
        problems.append(
            f"{positions} positions hold stock but {layers} opening layers exist "
            f"({written} were written)"
        )
    if layer_quantity != quantity:
        problems.append(f"layer quantity {layer_quantity} does not match on-hand {quantity}")
    if layer_value != value:
        problems.append(f"layer value {layer_value} does not match balance value {value}")
    if problems:
        raise RuntimeError(
            "FIFO backfill did not reconcile to inventory_balances, refusing to "
            "commit: " + "; ".join(problems)
        )


def upgrade() -> None:
    op.create_table(
        "inventory_cost_layers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("source_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "original_quantity > 0",
            name=op.f("ck_inventory_cost_layers_original_quantity_positive"),
        ),
        sa.CheckConstraint(
            "remaining_quantity <= original_quantity",
            name=op.f("ck_inventory_cost_layers_remaining_within_original"),
        ),
        sa.CheckConstraint(
            "remaining_quantity >= 0", name=op.f("ck_inventory_cost_layers_remaining_non_negative")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_inventory_cost_layers_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["balance_id"],
            ["inventory_balances.id"],
            name=op.f("fk_inventory_cost_layers_balance_id_inventory_balances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_inventory_cost_layers_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_inventory_cost_layers_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_cost_layers_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_cost_layers_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_inventory_cost_layers_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_transaction_id"],
            ["inventory_transactions.id"],
            name="fk_cost_layers_source_transaction",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_cost_layers_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_cost_layers_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_cost_layers")),
        comment="FIFO cost layers. One row per inbound movement, consumed oldest first.",
    )
    op.create_index(
        op.f("ix_inventory_cost_layers_company_id"),
        "inventory_cost_layers",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inventory_cost_layers_created_at"),
        "inventory_cost_layers",
        ["created_at"],
        unique=False,
    )
    # The grain, spelled exactly as inventory_balances spells it, so a report
    # that reaches the layers by position lands on the same shape of key.
    op.create_index(
        "ix_inventory_cost_layers_grain",
        "inventory_cost_layers",
        [
            "company_id",
            "item_id",
            "warehouse_id",
            sa.literal_column(f"coalesce(location_id, '{GRAIN_NULL_SENTINEL}'::uuid)"),
            sa.literal_column(f"coalesce(batch_id, '{GRAIN_NULL_SENTINEL}'::uuid)"),
        ],
        unique=False,
    )
    # The FIFO pick: partial, because only open layers are ever consumed, and
    # ordered so the scan comes back in consumption order without a sort.
    op.create_index(
        "ix_inventory_cost_layers_open",
        "inventory_cost_layers",
        ["company_id", "balance_id", "received_at", "id"],
        unique=False,
        postgresql_where=sa.text("remaining_quantity > 0"),
    )
    op.create_index(
        "ix_inventory_cost_layers_source",
        "inventory_cost_layers",
        ["company_id", "source_transaction_id"],
        unique=False,
    )

    op.create_table(
        "inventory_cost_consumptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("layer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_inventory_cost_consumptions_quantity_positive")
        ),
        sa.CheckConstraint(
            "total_cost >= 0", name=op.f("ck_inventory_cost_consumptions_total_cost_non_negative")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_inventory_cost_consumptions_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_inventory_cost_consumptions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_cost_consumptions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["layer_id"],
            ["inventory_cost_layers.id"],
            name="fk_cost_consumptions_layer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["inventory_transactions.id"],
            name="fk_cost_consumptions_transaction",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_cost_consumptions")),
        comment="Append-only FIFO consumption detail: which layers an issue drew on.",
    )
    op.create_index(
        op.f("ix_inventory_cost_consumptions_company_id"),
        "inventory_cost_consumptions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_cost_consumptions_layer",
        "inventory_cost_consumptions",
        ["company_id", "layer_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_cost_consumptions_txn",
        "inventory_cost_consumptions",
        ["company_id", "transaction_id"],
        unique=False,
    )

    # Append-only, enforced by the database rather than by convention - the same
    # treatment audit_logs and inventory_transactions got in the initial
    # revision, using the function that revision installed.
    op.execute(
        f"CREATE TRIGGER {IMMUTABILITY_TRIGGER} "
        "BEFORE UPDATE OR DELETE ON inventory_cost_consumptions "
        "FOR EACH ROW EXECUTE FUNCTION atcraft_reject_row_mutation()"
    )

    op.add_column(
        "stock_transfer_lines",
        sa.Column("dispatch_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_transfer_lines_dispatch_txn",
        "stock_transfer_lines",
        "inventory_transactions",
        ["dispatch_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    _verify_backfill(_backfill())


def downgrade() -> None:
    """Remove FIFO layering. See the Rollback note above for what is lost."""
    op.drop_constraint("fk_transfer_lines_dispatch_txn", "stock_transfer_lines", type_="foreignkey")
    op.drop_column("stock_transfer_lines", "dispatch_transaction_id")

    op.execute(f"DROP TRIGGER IF EXISTS {IMMUTABILITY_TRIGGER} ON inventory_cost_consumptions")
    op.drop_index("ix_inventory_cost_consumptions_txn", table_name="inventory_cost_consumptions")
    op.drop_index("ix_inventory_cost_consumptions_layer", table_name="inventory_cost_consumptions")
    op.drop_index(
        op.f("ix_inventory_cost_consumptions_company_id"),
        table_name="inventory_cost_consumptions",
    )
    op.drop_table("inventory_cost_consumptions")

    op.drop_index("ix_inventory_cost_layers_source", table_name="inventory_cost_layers")
    op.drop_index(
        "ix_inventory_cost_layers_open",
        table_name="inventory_cost_layers",
        postgresql_where=sa.text("remaining_quantity > 0"),
    )
    op.drop_index("ix_inventory_cost_layers_grain", table_name="inventory_cost_layers")
    op.drop_index(op.f("ix_inventory_cost_layers_created_at"), table_name="inventory_cost_layers")
    op.drop_index(op.f("ix_inventory_cost_layers_company_id"), table_name="inventory_cost_layers")
    op.drop_table("inventory_cost_layers")
