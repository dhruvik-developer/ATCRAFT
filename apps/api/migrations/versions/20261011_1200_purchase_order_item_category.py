"""purchase order procurement category, backfilled from the line items

Revision ID: 159ffb5b9cb4
Revises: b459767d92f5
Created: 2026-10-11 12:00:00.000000+00:00

A requisition now splits into one purchase order per (vendor, procurement
category, warehouse), and a category-restricted vendor manager sees only the
orders in their categories. Both need the order to say which category it was
raised for, so ``purchase_orders`` gains ``item_category_id``.

The column is derived, never typed: on create, update and conversion the
service sets it to the one procurement category the lines share, or NULL when
they span several (docs/assumptions.md §50). Existing orders are backfilled
here with the conservative half of that rule - the item's *own* category, where
every line of the order is an item in one and the same category. An order with
mixed categories, or with any line whose item has no category, is left NULL,
which is exactly what the service would write for it. What the backfill does
not attempt is the ancestor step (the nearest ancestor the vendor is qualified
for), because that reads ``vendor_categories`` as it stands today rather than
as it stood when the order was raised; an order the buyer edits is re-derived
by the service with the full rule.

Review (master rules section 67):

* Locking impact:  ``ADD COLUMN ... NULL`` is catalogue-only in PostgreSQL 11+
                   (no rewrite, no scan) but takes a brief ACCESS EXCLUSIVE
                   lock and queues behind a long transaction on
                   ``purchase_orders``. The foreign key is added ``NOT VALID``
                   and validated after the backfill: ``VALIDATE CONSTRAINT``
                   takes SHARE UPDATE EXCLUSIVE, which blocks neither reads nor
                   writes. The backfill UPDATE takes row locks on the orders it
                   touches for the length of the statement.
* Data migration:  yes - one set-based UPDATE, no row loop. It only writes
                   rows whose ``item_category_id`` is still NULL, so a re-run
                   changes nothing. The number of orders categorised is logged.
* Rollback:        ``downgrade()`` drops the index, the constraint and the
                   column. The values are derived data and nothing else reads
                   them below this revision, so nothing that existed before the
                   upgrade is lost; a later re-upgrade backfills again from the
                   items, which reproduces item-own categories but not one a
                   conversion resolved to an ancestor - those orders read NULL
                   until a buyer next edits them.
* Production size: proportional to ``purchase_orders`` joined to its lines -
                   thousands to low hundreds of thousands of rows, never a
                   ledger. Seconds.
* Index creation:  one composite index, ``ix_purchase_orders_vendor_category``,
                   built inside the migration's transaction (not CONCURRENTLY)
                   on a table of that size; it blocks writes to purchase orders
                   for the seconds the build takes, which is acceptable in the
                   deploy window and is how every index in this chain is built.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "159ffb5b9cb4"
down_revision: str | None = "b459767d92f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

FK_NAME = "fk_purchase_orders_item_category_id_item_categories"
INDEX_NAME = "ix_purchase_orders_vendor_category"

#: Every line's item sits in one and the same category of the order's own
#: company. ``count(*) = count(category.id)`` refuses an order with any line
#: whose item has no category (or one belonging to another tenant, which the
#: join on company drops), and ``count(DISTINCT ...) = 1`` refuses a mixed one.
BACKFILL = """
    UPDATE purchase_orders AS po
    SET item_category_id = single.category_id
    FROM (
        SELECT line.purchase_order_id,
               (array_agg(category.id))[1] AS category_id
        FROM purchase_order_lines AS line
        JOIN items AS item
          ON item.id = line.item_id
        LEFT JOIN item_categories AS category
          ON category.id = item.category_id
         AND category.company_id = line.company_id
        GROUP BY line.purchase_order_id
        HAVING count(DISTINCT category.id) = 1
           AND count(*) = count(category.id)
    ) AS single
    WHERE po.id = single.purchase_order_id
      AND po.item_category_id IS NULL
"""


def upgrade() -> None:
    op.add_column("purchase_orders", sa.Column("item_category_id", sa.Uuid(), nullable=True))
    # NOT VALID, then validated after the backfill: see the locking note above.
    op.execute(
        f"ALTER TABLE purchase_orders ADD CONSTRAINT {FK_NAME} "
        "FOREIGN KEY (item_category_id) REFERENCES item_categories (id) "
        "ON DELETE RESTRICT NOT VALID"
    )

    result = op.get_bind().execute(sa.text(BACKFILL))
    log.info("purchase_orders: %s order(s) given an item category", result.rowcount)

    op.execute(f"ALTER TABLE purchase_orders VALIDATE CONSTRAINT {FK_NAME}")
    op.create_index(
        INDEX_NAME,
        "purchase_orders",
        ["company_id", "vendor_id", "item_category_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="purchase_orders")
    op.drop_constraint(FK_NAME, "purchase_orders", type_="foreignkey")
    op.drop_column("purchase_orders", "item_category_id")
