"""Add item packing recipes and packing-order MRP demand.

Revision ID: 20261113_0100
Revises: 20261112_0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261113_0100"
down_revision = "20261112_0100"
branch_labels = None
depends_on = None

_DEMAND_SOURCE_CHECK = "ck_mrp_demands_demand_source_known"


def _replace_demand_source_check(values: str) -> None:
    op.drop_constraint(op.f(_DEMAND_SOURCE_CHECK), "mrp_demands", type_="check")
    op.execute(
        "ALTER TABLE mrp_demands ADD CONSTRAINT "
        f"{op.f(_DEMAND_SOURCE_CHECK)} CHECK (demand_source IN ({values})) NOT VALID"
    )
    op.execute(f'ALTER TABLE mrp_demands VALIDATE CONSTRAINT "{op.f(_DEMAND_SOURCE_CHECK)}"')


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "item_packing_specs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("packing_material_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_per_unit", sa.Numeric(18, 4), nullable=True),
        sa.Column("units_per_carton", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            "(quantity_per_unit IS NOT NULL AND quantity_per_unit > 0 "
            "AND units_per_carton IS NULL) OR "
            "(quantity_per_unit IS NULL AND units_per_carton IS NOT NULL "
            "AND units_per_carton > 0)",
            name=op.f("ck_item_packing_specs_packing_spec_has_one_positive_ratio"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_packing_specs_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_item_packing_specs_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_item_packing_specs_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["packing_material_item_id"],
            ["items.id"],
            name=op.f("fk_item_packing_specs_packing_material_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_item_packing_specs_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_packing_specs")),
    )
    op.create_index(
        op.f("ix_item_packing_specs_company_id"),
        "item_packing_specs",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_item_packing_specs_created_at"),
        "item_packing_specs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_item_packing_specs_item_id"),
        "item_packing_specs",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_item_packing_specs_packing_material_item_id"),
        "item_packing_specs",
        ["packing_material_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_item_packing_specs_item_default",
        "item_packing_specs",
        ["company_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_item_packing_specs_default_material",
        "item_packing_specs",
        ["company_id", "item_id", "packing_material_item_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    _replace_demand_source_check("'SALES_ORDER', 'MANUAL', 'REORDER', 'PACKING_ORDER'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    has_packing_demands = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM mrp_demands WHERE demand_source = 'PACKING_ORDER')"
            )
        )
        .scalar_one()
    )
    if has_packing_demands:
        raise RuntimeError(
            "Cannot remove packing-order demand support while MRP snapshots contain it."
        )
    has_specs = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM item_packing_specs)"))
        .scalar_one()
    )
    if has_specs:
        raise RuntimeError("Cannot remove item packing specifications while rows exist.")
    _replace_demand_source_check("'SALES_ORDER', 'MANUAL', 'REORDER'")
    op.drop_index("uq_item_packing_specs_default_material", table_name="item_packing_specs")
    op.drop_index("ix_item_packing_specs_item_default", table_name="item_packing_specs")
    op.drop_table("item_packing_specs")
