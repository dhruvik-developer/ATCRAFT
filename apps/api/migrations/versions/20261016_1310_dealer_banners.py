"""Add dealer promotion banners.

Revision ID: 20261016_1310
Revises: 20261016_1300
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_1310"
down_revision: str | None = "20261016_1300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dealer_banners",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column("image_document_id", sa.UUID(), nullable=False),
        sa.Column("link_kind", sa.String(length=16), nullable=False),
        sa.Column("link_id", sa.UUID(), nullable=True),
        sa.Column("audience_price_type", sa.String(length=16), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="INACTIVE", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "code", name="uq_dealer_banners_company_id_code"),
        sa.CheckConstraint(
            "link_kind IN ('NONE','ITEM','CATEGORY','BRAND','OFFER')",
            name="ck_dealer_banners_link_kind_known",
        ),
        sa.CheckConstraint(
            "(link_kind = 'NONE' AND link_id IS NULL) OR "
            "(link_kind <> 'NONE' AND link_id IS NOT NULL)",
            name="ck_dealer_banners_link_target_matches",
        ),
        sa.CheckConstraint(
            "audience_price_type IS NULL OR audience_price_type IN ('SALE','DEALER','DISTRIBUTOR')",
            name="ck_dealer_banners_audience_known",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','INACTIVE','ARCHIVED')",
            name="ck_dealer_banners_status_known",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name="ck_dealer_banners_dates_ordered",
        ),
    )
    op.create_index(
        "ix_dealer_banners_window",
        "dealer_banners",
        ["company_id", "status", "sort_order"],
    )
    op.create_index("ix_dealer_banners_company_id", "dealer_banners", ["company_id"])
    op.create_index("ix_dealer_banners_deleted_at", "dealer_banners", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_dealer_banners_deleted_at", table_name="dealer_banners")
    op.drop_index("ix_dealer_banners_company_id", table_name="dealer_banners")
    op.drop_index("ix_dealer_banners_window", table_name="dealer_banners")
    op.drop_table("dealer_banners")
