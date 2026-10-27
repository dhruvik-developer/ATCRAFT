"""Retain PO source allocations and amendment/approval evidence.

Revision ID: 20261103_0100
Revises: 20261102_0100

The backfill preserves counters and every scalar link. Inconsistent legacy
counters fail before DDL rather than silently rewriting operational history.
Locks are bounded and all changes are transactional; schedule a maintenance
window for a large procurement history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from src.core.ids import new_id

revision = "20261103_0100"
down_revision = "20261102_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE purchase_orders, purchase_order_lines, purchase_requisition_lines "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    connection = op.get_bind()
    mismatches = (
        connection.execute(
            sa.text("""
        SELECT pr.id
        FROM purchase_requisition_lines pr
        LEFT JOIN (
            SELECT pol.requisition_line_id, SUM(pol.quantity) AS quantity
            FROM purchase_order_lines pol
            JOIN purchase_orders po ON po.id = pol.purchase_order_id
            WHERE pol.requisition_line_id IS NOT NULL
              AND po.status NOT IN ('CANCELLED', 'REJECTED')
            GROUP BY pol.requisition_line_id
        ) linked ON linked.requisition_line_id = pr.id
        WHERE pr.ordered_quantity <> COALESCE(linked.quantity, 0)
        LIMIT 10
    """)
        )
        .scalars()
        .all()
    )
    foreign_links = (
        connection.execute(
            sa.text("""
        SELECT pol.id FROM purchase_order_lines pol
        JOIN purchase_requisition_lines pr ON pr.id = pol.requisition_line_id
        JOIN purchase_orders po ON po.id = pol.purchase_order_id
        WHERE pol.company_id <> pr.company_id OR pol.company_id <> po.company_id
        LIMIT 10
    """)
        )
        .scalars()
        .all()
    )
    if mismatches or foreign_links:
        raise RuntimeError(
            "PO allocation migration refused: reconcile legacy ordered counters/tenant links "
            f"before retrying (requisition lines: {mismatches}; PO lines: {foreign_links}). "
            "No counters or document history were changed."
        )
    op.create_unique_constraint(
        "uq_purchase_order_lines_company_id_id", "purchase_order_lines", ["company_id", "id"]
    )
    op.create_unique_constraint(
        "uq_purchase_requisition_lines_company_id_id",
        "purchase_requisition_lines",
        ["company_id", "id"],
    )
    for name, type_, nullable, default in (
        ("amendment_number", sa.Integer(), False, "0"),
        ("last_amended_at", sa.DateTime(timezone=True), True, None),
        ("last_amended_by_id", postgresql.UUID(as_uuid=True), True, None),
        ("duplicate_confirmed_by_id", postgresql.UUID(as_uuid=True), True, None),
        ("duplicate_confirmed_at", sa.DateTime(timezone=True), True, None),
        ("duplicate_confirmation_reason", sa.Text(), True, None),
    ):
        op.add_column(
            "purchase_orders", sa.Column(name, type_, nullable=nullable, server_default=default)
        )
    for name in ("last_amended_by_id", "duplicate_confirmed_by_id"):
        op.create_foreign_key(
            op.f(f"fk_purchase_orders_{name}_users"),
            "purchase_orders",
            "users",
            [name],
            ["id"],
            ondelete="SET NULL",
        )
    for name, condition in (
        ("amendment_number_non_negative", "amendment_number >= 0"),
        ("amendment_evidence_complete", "(amendment_number = 0) = (last_amended_at IS NULL)"),
        (
            "duplicate_confirmation_evidence_complete",
            "(duplicate_confirmed_at IS NULL) = (duplicate_confirmation_reason IS NULL)",
        ),
    ):
        op.create_check_constraint(op.f(f"ck_purchase_orders_{name}"), "purchase_orders", condition)
    allocation = op.create_table(
        "purchase_order_source_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_order_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requisition_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_order_source_allocations")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["company_id", "purchase_order_line_id"],
            ["purchase_order_lines.company_id", "purchase_order_lines.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "requisition_line_id"],
            ["purchase_requisition_lines.company_id", "purchase_requisition_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "company_id",
            "purchase_order_line_id",
            "requisition_line_id",
            name="uq_purchase_order_source_allocations_line_source",
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_purchase_order_source_allocations_quantity_positive")
        ),
    )
    for name, columns in (
        ("company_id", ["company_id"]),
        ("created_at", ["created_at"]),
        ("source", ["company_id", "requisition_line_id", "is_active"]),
        ("line", ["company_id", "purchase_order_line_id"]),
    ):
        op.create_index(f"ix_purchase_order_source_allocations_{name}", allocation.name, columns)
    rows = connection.execute(
        sa.text("""
        SELECT pol.company_id, pol.id AS purchase_order_line_id, pol.requisition_line_id,
               pol.quantity, po.status NOT IN ('CANCELLED', 'REJECTED') AS is_active,
               pol.created_at, pol.updated_at, pol.created_by_id, pol.updated_by_id
        FROM purchase_order_lines pol JOIN purchase_orders po ON po.id = pol.purchase_order_id
        WHERE pol.requisition_line_id IS NOT NULL
    """)
    )
    while batch := rows.mappings().fetchmany(500):
        connection.execute(allocation.insert(), [{"id": new_id(), **dict(row)} for row in batch])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Keep the upgrade's table order and freeze allocation writes too. These
    # locks must precede the refusal checks: otherwise an amendment can commit
    # new evidence after preflight and have it removed by the following DDL.
    op.execute(
        "LOCK TABLE purchase_orders, purchase_order_lines, purchase_requisition_lines, "
        "purchase_order_source_allocations IN SHARE ROW EXCLUSIVE MODE"
    )
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM purchase_orders WHERE amendment_number > 0 OR duplicate_confirmed_at IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Downgrade refused: purchase-order revision/confirmation history exists."
        )
    if connection.scalar(
        sa.text("""
        SELECT EXISTS (
          SELECT 1 FROM purchase_order_source_allocations a
          JOIN purchase_order_lines l ON l.id = a.purchase_order_line_id
          WHERE l.requisition_line_id IS DISTINCT FROM a.requisition_line_id
             OR l.quantity <> a.quantity
        )
    """)
    ):
        raise RuntimeError(
            "Downgrade refused: pooled allocations cannot be represented by scalar links."
        )
    op.drop_table("purchase_order_source_allocations")
    for name in (
        "amendment_number_non_negative",
        "amendment_evidence_complete",
        "duplicate_confirmation_evidence_complete",
    ):
        op.drop_constraint(op.f(f"ck_purchase_orders_{name}"), "purchase_orders", type_="check")
    for name in ("last_amended_by_id", "duplicate_confirmed_by_id"):
        op.drop_constraint(
            op.f(f"fk_purchase_orders_{name}_users"), "purchase_orders", type_="foreignkey"
        )
    for name in (
        "amendment_number",
        "last_amended_at",
        "last_amended_by_id",
        "duplicate_confirmed_by_id",
        "duplicate_confirmed_at",
        "duplicate_confirmation_reason",
    ):
        op.drop_column("purchase_orders", name)
    op.drop_constraint(
        "uq_purchase_order_lines_company_id_id", "purchase_order_lines", type_="unique"
    )
    op.drop_constraint(
        "uq_purchase_requisition_lines_company_id_id", "purchase_requisition_lines", type_="unique"
    )
