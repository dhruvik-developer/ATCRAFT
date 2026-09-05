"""secure TOTP MFA state

Revision ID: a4f7c29d8e11
Revises: 7e8eb4bdd0ed
Created: 2026-09-04 00:00:00+00:00

MFA seeds are now stored as Fernet ciphertext, which is longer than the legacy
Base32 value. The last accepted TOTP counter is persisted to make a code
single-use even when concurrent requests reach different API workers.

Existing plaintext seeds are left intact for a rolling deployment: the new
application can read the legacy Base32 shape and encrypts it on the next
successful MFA verification. Removing that compatibility in the migration
would require putting the application's encryption key into schema tooling and
would make rollback unsafe.

Locking impact: two brief catalogue changes on ``users``; widening VARCHAR is
metadata-only on supported PostgreSQL versions. Adding a nullable column has no
table rewrite. Run outside long transactions because ACCESS EXCLUSIVE is still
required briefly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4f7c29d8e11"
down_revision: str | None = "7e8eb4bdd0ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.add_column("users", sa.Column("mfa_last_used_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # Encrypted values do not fit the old VARCHAR(64). Clearing them is safer
    # than truncating credentials into undecryptable garbage; a downgraded user
    # must enrol MFA again.
    op.execute("UPDATE users SET mfa_enabled = false, mfa_secret = NULL")
    op.drop_column("users", "mfa_last_used_step")
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
