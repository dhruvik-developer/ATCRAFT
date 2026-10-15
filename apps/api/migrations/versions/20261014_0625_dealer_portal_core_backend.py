"""Dealer portal core backend (WP C3a).

Revision ID: 4f29c8bead9e
Revises: c1bpush2026

Review: users.customer_id and sales_orders.source are new NULL/DEFAULT
columns on large tables - no rewrite, brief ACCESS EXCLUSIVE metadata locks
only; the customer FK and both CHECKs are added NOT VALID then VALIDATEd so
existing rows are scanned without blocking writes. New tables are empty; their
FKs and indexes are built at creation. The role seed inserts one row per
company per code only when absent. Rollback drops the new tables and columns,
removes the dealer rights from the administrator roles, and deletes the seeded
dealer roles (membership rows go with them through ON DELETE CASCADE, so a
downgrade on a database with dealer accounts loses those assignments).
Expected size proportional to the users/sales_orders validation scans;
schedule at low load.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f29c8bead9e"
down_revision: str | None = "c1bpush2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEALER_ROLES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "DEALER_OWNER",
        "Dealer Owner",
        "Portal access scoped to the account dealer.",
        (
            "dealer_portal.account",
            "dealer_portal.browse",
            "dealer_portal.order",
            "dealer_portal.return",
            "notification.view",
        ),
    ),
    (
        "DEALER_BUYER",
        "Dealer Buyer",
        "Portal access scoped to the account dealer.",
        (
            "dealer_portal.browse",
            "dealer_portal.order",
            "dealer_portal.return",
            "notification.view",
        ),
    ),
    (
        "DEALER_ACCOUNTS",
        "Dealer Accounts",
        "Portal access scoped to the account dealer.",
        (
            "dealer_portal.account",
            "dealer_portal.browse",
            "notification.view",
        ),
    ),
)

DEALER_RIGHTS: tuple[str, ...] = (
    "dealer_portal.browse",
    "dealer_portal.order",
    "dealer_portal.account",
    "dealer_portal.return",
)


def _seed_dealer_roles() -> None:
    for code, name, description, permissions in DEALER_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO roles "
                "(id, company_id, code, name, description, permissions, "
                "is_system, is_vendor_role, is_dealer_role, is_active) "
                "SELECT gen_random_uuid(), companies.id, :code, :name, "
                ":description, CAST(:permissions AS varchar[]), "
                "true, false, true, true "
                "FROM companies WHERE NOT EXISTS ("
                "SELECT 1 FROM roles "
                "WHERE roles.company_id = companies.id AND roles.code = :code)"
            ).bindparams(
                code=code, name=name, description=description, permissions=list(permissions)
            )
        )
    for right in DEALER_RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_append(permissions, :right) "
                "WHERE is_system AND code IN ('SUPER_ADMIN', 'ADMIN') "
                "AND NOT (:right = ANY(permissions))"
            ).bindparams(right=right)
        )


def _unseed_dealer_roles() -> None:
    for right in DEALER_RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :right) "
                "WHERE is_system AND code IN ('SUPER_ADMIN', 'ADMIN')"
            ).bindparams(right=right)
        )
    op.execute(
        sa.text(
            "DELETE FROM roles WHERE code IN ('DEALER_OWNER', 'DEALER_BUYER', 'DEALER_ACCOUNTS')"
        )
    )


def upgrade() -> None:
    op.create_table(
        "dealer_offers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("brand_id", sa.Uuid(), nullable=True),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("audience_price_type", sa.String(length=16), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column(
            "min_quantity", sa.Numeric(precision=18, scale=4), server_default="1", nullable=False
        ),
        sa.Column("discount_percentage", sa.Numeric(precision=9, scale=4), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(scope_kind = 'ALL' AND category_id IS NULL AND brand_id IS NULL "
            "AND item_id IS NULL) OR (scope_kind = 'CATEGORY' AND category_id IS NOT NULL "
            "AND brand_id IS NULL AND item_id IS NULL) OR (scope_kind = 'BRAND' "
            "AND brand_id IS NOT NULL AND category_id IS NULL AND item_id IS NULL) "
            "OR (scope_kind = 'ITEM' AND item_id IS NOT NULL AND category_id IS NULL "
            "AND brand_id IS NULL)",
            name=op.f("ck_dealer_offers_scope_targets_match"),
        ),
        sa.CheckConstraint(
            "audience_price_type IS NULL OR audience_price_type IN "
            "('SALE', 'DEALER', 'DISTRIBUTOR')",
            name=op.f("ck_dealer_offers_audience_known"),
        ),
        sa.CheckConstraint(
            "scope_kind IN ('ALL', 'CATEGORY', 'BRAND', 'ITEM')",
            name=op.f("ck_dealer_offers_scope_kind_known"),
        ),
        sa.CheckConstraint(
            "discount_percentage > 0 AND discount_percentage <= 100",
            name=op.f("ck_dealer_offers_discount_open_interval"),
        ),
        sa.CheckConstraint("min_quantity > 0", name=op.f("ck_dealer_offers_min_quantity_positive")),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name=op.f("ck_dealer_offers_dates_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
            name=op.f("fk_dealer_offers_brand_id_brands"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["item_categories.id"],
            name=op.f("fk_dealer_offers_category_id_item_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dealer_offers_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_offers_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_dealer_offers_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_dealer_offers_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_offers_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dealer_offers")),
        sa.UniqueConstraint("company_id", "code", name="uq_dealer_offers_company_id_code"),
    )
    op.create_index(
        "ix_dealer_offers_active",
        "dealer_offers",
        ["company_id", "is_active", "sort_order"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_offers_company_id"), "dealer_offers", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_dealer_offers_created_at"), "dealer_offers", ["created_at"], unique=False
    )
    op.create_index(
        "ix_dealer_offers_customer",
        "dealer_offers",
        ["company_id", "customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_offers_deleted_at"), "dealer_offers", ["deleted_at"], unique=False
    )
    op.create_table(
        "dealer_carts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dealer_carts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_carts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_dealer_carts_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_carts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_dealer_carts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dealer_carts")),
        sa.UniqueConstraint("company_id", "user_id", name="uq_dealer_carts_company_user"),
    )
    op.create_index(
        op.f("ix_dealer_carts_company_id"), "dealer_carts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_dealer_carts_created_at"), "dealer_carts", ["created_at"], unique=False
    )
    op.create_index(
        "ix_dealer_carts_customer", "dealer_carts", ["company_id", "customer_id"], unique=False
    )
    op.create_table(
        "dealer_cart_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("item_variant_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_dealer_cart_lines_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["cart_id"],
            ["dealer_carts.id"],
            name=op.f("fk_dealer_cart_lines_cart_id_dealer_carts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dealer_cart_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_cart_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_dealer_cart_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_variant_id"],
            ["item_variants.id"],
            name=op.f("fk_dealer_cart_lines_item_variant_id_item_variants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_cart_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dealer_cart_lines")),
    )
    op.create_index(
        "ix_dealer_cart_lines_cart",
        "dealer_cart_lines",
        ["company_id", "cart_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_cart_lines_company_id"),
        "dealer_cart_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_cart_lines_created_at"),
        "dealer_cart_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "uq_dealer_cart_lines_grain",
        "dealer_cart_lines",
        [
            "cart_id",
            "item_id",
            sa.literal_column(
                "coalesce(item_variant_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
        ],
        unique=True,
    )
    op.create_table(
        "dealer_wishlist_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("item_variant_id", sa.Uuid(), nullable=True),
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dealer_wishlist_items_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_wishlist_items_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_dealer_wishlist_items_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_dealer_wishlist_items_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_variant_id"],
            ["item_variants.id"],
            name=op.f("fk_dealer_wishlist_items_item_variant_id_item_variants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dealer_wishlist_items_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_dealer_wishlist_items_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dealer_wishlist_items")),
    )
    op.create_index(
        "ix_dealer_wishlist_customer",
        "dealer_wishlist_items",
        ["company_id", "customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_wishlist_items_company_id"),
        "dealer_wishlist_items",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dealer_wishlist_items_created_at"),
        "dealer_wishlist_items",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "uq_dealer_wishlist_grain",
        "dealer_wishlist_items",
        [
            "company_id",
            "user_id",
            "item_id",
            sa.literal_column(
                "coalesce(item_variant_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
        ],
        unique=True,
    )

    # --- existing tables --------------------------------------------------
    op.add_column("roles", sa.Column("is_dealer_role", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE roles SET is_dealer_role = false WHERE is_dealer_role IS NULL"))
    op.alter_column("roles", "is_dealer_role", nullable=False)

    op.add_column(
        "sales_orders",
        sa.Column("source", sa.String(length=16), server_default="ERP", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_sales_orders_source_known"),
        "sales_orders",
        "source IN ('ERP', 'DEALER_PORTAL')",
        postgresql_not_valid=True,
    )
    op.execute(sa.text("ALTER TABLE sales_orders VALIDATE CONSTRAINT ck_sales_orders_source_known"))

    op.add_column("sales_order_lines", sa.Column("offer_id", sa.Uuid(), nullable=True))

    op.add_column("users", sa.Column("customer_id", sa.Uuid(), nullable=True))
    op.create_index("ix_users_customer", "users", ["company_id", "customer_id"], unique=False)
    op.create_index(op.f("ix_users_customer_id"), "users", ["customer_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_users_customer_id_customers"),
        "users",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="CASCADE",
        postgresql_not_valid=True,
    )
    op.execute(sa.text("ALTER TABLE users VALIDATE CONSTRAINT fk_users_customer_id_customers"))
    op.create_check_constraint(
        op.f("ck_users_portal_binding_exclusive"),
        "users",
        "vendor_id IS NULL OR customer_id IS NULL",
        postgresql_not_valid=True,
    )
    op.execute(sa.text("ALTER TABLE users VALIDATE CONSTRAINT ck_users_portal_binding_exclusive"))

    _seed_dealer_roles()


def downgrade() -> None:
    _unseed_dealer_roles()
    op.drop_constraint(op.f("ck_users_portal_binding_exclusive"), "users", type_="check")
    op.drop_constraint(op.f("fk_users_customer_id_customers"), "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_customer_id"), table_name="users")
    op.drop_index("ix_users_customer", table_name="users")
    op.drop_column("users", "customer_id")
    op.drop_column("sales_order_lines", "offer_id")
    op.drop_constraint(op.f("ck_sales_orders_source_known"), "sales_orders", type_="check")
    op.drop_column("sales_orders", "source")
    op.drop_column("roles", "is_dealer_role")
    op.drop_index("uq_dealer_wishlist_grain", table_name="dealer_wishlist_items")
    op.drop_index(op.f("ix_dealer_wishlist_items_created_at"), table_name="dealer_wishlist_items")
    op.drop_index(op.f("ix_dealer_wishlist_items_company_id"), table_name="dealer_wishlist_items")
    op.drop_index("ix_dealer_wishlist_customer", table_name="dealer_wishlist_items")
    op.drop_table("dealer_wishlist_items")
    op.drop_index("uq_dealer_cart_lines_grain", table_name="dealer_cart_lines")
    op.drop_index(op.f("ix_dealer_cart_lines_created_at"), table_name="dealer_cart_lines")
    op.drop_index(op.f("ix_dealer_cart_lines_company_id"), table_name="dealer_cart_lines")
    op.drop_index("ix_dealer_cart_lines_cart", table_name="dealer_cart_lines")
    op.drop_table("dealer_cart_lines")
    op.drop_index("ix_dealer_carts_customer", table_name="dealer_carts")
    op.drop_index(op.f("ix_dealer_carts_created_at"), table_name="dealer_carts")
    op.drop_index(op.f("ix_dealer_carts_company_id"), table_name="dealer_carts")
    op.drop_table("dealer_carts")
    op.drop_index("ix_dealer_offers_customer", table_name="dealer_offers")
    op.drop_index(op.f("ix_dealer_offers_deleted_at"), table_name="dealer_offers")
    op.drop_index(op.f("ix_dealer_offers_created_at"), table_name="dealer_offers")
    op.drop_index(op.f("ix_dealer_offers_company_id"), table_name="dealer_offers")
    op.drop_index("ix_dealer_offers_active", table_name="dealer_offers")
    op.drop_table("dealer_offers")
