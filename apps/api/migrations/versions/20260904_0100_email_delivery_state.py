"""email delivery state

Revision ID: d91ec6b47a20
Revises: a4f7c29d8e11
Created: 2026-09-04 01:00:00+00:00

Adds independent SMTP delivery timestamps and an encrypted reset-token outbox.
``notifications.delivered_at``
already means that at least one channel reached the user, so it cannot also be
an e-mail idempotency marker for rows visible in-app.

Locking impact: three brief ``ADD COLUMN`` catalogue changes. All columns are
nullable with no default, so PostgreSQL does not rewrite either table. Run
outside long transactions because each statement still needs a short ACCESS
EXCLUSIVE lock.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d91ec6b47a20"
down_revision: str | None = "a4f7c29d8e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("email_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "password_reset_tokens",
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "password_reset_tokens",
        sa.Column("email_token_ciphertext", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("password_reset_tokens", "email_token_ciphertext")
    op.drop_column("password_reset_tokens", "email_sent_at")
    op.drop_column("notifications", "email_delivered_at")
