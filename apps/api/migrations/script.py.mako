"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Review checklist (master rules section 67) - delete the lines that do not apply:

* Locking impact:      does any statement here take an ACCESS EXCLUSIVE lock on a
                       large table? Prefer CREATE INDEX CONCURRENTLY and
                       ADD COLUMN ... NULL followed by a backfill.
* Data migration:      are existing rows given correct values, in batches?
* Rollback:            does downgrade() actually restore the previous state, or
                       does it lose data? Say so explicitly if it does.
* Production size:     how long will this run against the real row counts?
* Index creation:      built CONCURRENTLY where the table is already large?
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
