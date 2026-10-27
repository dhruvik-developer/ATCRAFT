"""Give allocation tenant foreign keys names within PostgreSQL's identifier bound.

Revision ID: 20261104_0100
Revises: 20261103_0100

Only constraint names change. Existing validated foreign keys, rows and
allocation identities remain intact. The table lock has a five-second bound.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261104_0100"
down_revision = "20261103_0100"
branch_labels = None
depends_on = None

NAMES = {
    ("company_id", "purchase_order_line_id"): (
        "fk_purchase_order_source_allocations_company_id_purchas_507d",
        "fk_po_source_allocations_line_tenant",
    ),
    ("company_id", "requisition_line_id"): (
        "fk_purchase_order_source_allocations_company_id_requisi_7438",
        "fk_po_source_allocations_requisition_tenant",
    ),
}


def _rename(*, backwards: bool) -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    connection = op.get_bind()
    quote = connection.dialect.identifier_preparer.quote
    for foreign_key in sa.inspect(connection).get_foreign_keys("purchase_order_source_allocations"):
        names = NAMES.get(tuple(foreign_key["constrained_columns"]))
        if names is None:
            continue
        target = names[0 if backwards else 1]
        current = foreign_key["name"]
        if current != target:
            op.execute(
                sa.text(
                    "ALTER TABLE purchase_order_source_allocations RENAME CONSTRAINT "
                    f"{quote(current)} TO {quote(target)}"
                )
            )


def upgrade() -> None:
    _rename(backwards=False)


def downgrade() -> None:
    _rename(backwards=True)
