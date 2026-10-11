"""companies: the bank details and terms printed on outgoing invoices

Revision ID: faad7a416b38
Revises: 3746fadd0b86
Created: 2026-10-11 12:01:00.000000+00:00

Six nullable columns on ``companies``: ``bank_name``, ``bank_account_name``,
``bank_account_number``, ``bank_ifsc``, ``bank_branch`` and ``invoice_terms``.
They are what a sales invoice prints under its totals - where the customer pays,
and on what terms. All optional: a company that has not entered them still
invoices, and the document simply leaves the block out.

The IFSC shape (``^[A-Z]{4}0[A-Z0-9]{6}$``) is enforced by the API schema, the
same validator the vendor master uses (``src/common/banking.py``), not by a
CHECK: the vendor columns carry none either, and one rule in two places is how
the two would start to disagree.

Review (master rules section 67):

* Locking impact:  six ``ADD COLUMN ... NULL`` on ``companies``, each
                   catalogue-only with a brief ACCESS EXCLUSIVE lock. The table
                   holds one row per tenant.
* Data migration:  none; every existing company starts with no bank details.
* Rollback:        ``downgrade()`` drops the six columns. LOSSY for anything
                   entered after the upgrade.
* Production size: one row per tenant. Milliseconds.
* Index creation:  none - nothing filters or sorts on these columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "faad7a416b38"
down_revision: str | None = "3746fadd0b86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: tuple[tuple[str, sa.types.TypeEngine[str]], ...] = (
    ("bank_name", sa.String(length=120)),
    ("bank_account_name", sa.String(length=255)),
    # 34 characters covers an IBAN as well as an Indian account number.
    ("bank_account_number", sa.String(length=34)),
    ("bank_ifsc", sa.String(length=11)),
    ("bank_branch", sa.String(length=120)),
    ("invoice_terms", sa.Text()),
)


def upgrade() -> None:
    for name, column_type in COLUMNS:
        op.add_column("companies", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("companies", name)
