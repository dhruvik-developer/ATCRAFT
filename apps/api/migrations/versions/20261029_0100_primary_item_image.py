"""Select a primary product image without changing document ownership.

Revision ID: 20261029_0100
Revises: 20261028_0100
Create Date: 2026-10-24

The nullable UUID is metadata only. Validation stays in the product service;
removing it on downgrade leaves uploaded image documents and their bytes intact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261029_0100"
down_revision = "20261028_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("items", sa.Column("image_document_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("items", "image_document_id")
