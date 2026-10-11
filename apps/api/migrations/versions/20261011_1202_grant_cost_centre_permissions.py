"""grant the cost-centre permissions to the system roles

Revision ID: e1eed3775093
Revises: faad7a416b38
Created: 2026-10-11 12:02:00.000000+00:00

``cost_centre.view`` and ``cost_centre.manage`` are new permission strings. A
role's permission list is stored, not computed - ``roles.permissions`` is a
text array written when the company was seeded - so adding the enum members
reaches no deployed role by itself, and the Cost centres tab would be invisible
to every existing account, the superuser's role included. This is the same
step ``2f5e08c7ba61`` took for the item-attribute permissions, for the same
reason; the deploy does not re-run the seeder that would otherwise refresh the
system roles.

Only ``is_system`` roles are touched. The operational presets that hold these
permissions for a NEW company - ACCOUNTS (view and manage), the purchase and
production roles and PLANNER (view) - are owned by the client once seeded, and
rewriting one from a preset would silently undo whatever an administrator
decided. On an existing deployment an administrator grants these from
Settings -> Roles, exactly as for any other permission.

The strings are spelled literally rather than imported from the enum: a
migration is a fixed historical step and must do the same thing in a year that
it does today.

Review (master rules section 67):

* Locking impact:  one UPDATE per permission over ``roles``, a table with tens
                   of rows per company. Row locks only, held for the statement.
* Data migration:  yes - appends two strings to an array column, only where
                   absent, so re-running changes nothing.
* Rollback:        ``downgrade()`` removes both strings from every system role.
                   Correct rather than merely tidy: below this revision the
                   routes they gate do not exist.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1eed3775093"
down_revision: str | None = "faad7a416b38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
VIEW = "cost_centre.view"
MANAGE = "cost_centre.manage"

#: ADMIN and SUPER_ADMIN are the only system roles with business on the
#: organisation masters; the vendor roles are system roles too and get neither.
SYSTEM_ROLES = ("SUPER_ADMIN", "ADMIN")


def _grant(codes: tuple[str, ...], permission: str) -> None:
    op.execute(
        sa.text(
            """
            UPDATE roles
            SET permissions = array_append(permissions, :permission)
            WHERE is_system
              AND code = ANY(:codes)
              AND NOT (:permission = ANY(permissions))
            """
        ).bindparams(permission=permission, codes=list(codes))
    )


def upgrade() -> None:
    _grant(SYSTEM_ROLES, VIEW)
    _grant(SYSTEM_ROLES, MANAGE)


def downgrade() -> None:
    for permission in (VIEW, MANAGE):
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :permission) "
                "WHERE is_system"
            ).bindparams(permission=permission)
        )
