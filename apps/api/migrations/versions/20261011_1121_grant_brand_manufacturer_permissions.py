"""grant the brand and manufacturer permissions to the system roles

Revision ID: a683a56652a1
Revises: 22a566492b2c
Created: 2026-10-11 11:21:00.000000+00:00

`roles.permissions` is a stored text array, so a member added to the
`Permission` enum reaches no deployed role by itself - which is exactly how the
item-attribute tab came to be invisible to everyone, the superuser included,
until `2f5e08c7ba61` granted it. A release does not run the seeder, so this
does the same thing for the four permissions the brand and manufacturer masters
introduce.

Only `is_system` roles are touched, for the reason that precedent gives: the
operational presets (DESIGN_ENGINEER, SALES_EXECUTIVE, PURCHASE_*, PLANNER and
the rest) are seeded from presets and then owned by the client, and rewriting
one would silently undo what an administrator decided. On an existing
deployment a design engineer therefore does NOT gain `brand.manage`
automatically; an administrator grants it from Settings -> Roles. A newly
seeded company gets it from the preset.

The strings are spelled literally rather than imported from `ROLE_PRESETS`, so
this revision does the same thing in a year that it does today.

Review (master rules section 67):

* Locking impact:  one UPDATE per permission over `roles`, tens of rows per
                   company. Row locks only, held for the statement.
* Data migration:  yes - it appends four strings to an array column, only where
                   each is absent, so re-running changes nothing.
* Rollback:        `downgrade()` removes the four strings again from system
                   roles. Correct rather than merely tidy: below `22a566492b2c`
                   the endpoints they gate do not exist, and a role carrying
                   them would name permissions nothing checks.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a683a56652a1"
down_revision: str | None = "22a566492b2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
PERMISSIONS = (
    "brand.view",
    "brand.manage",
    "manufacturer.view",
    "manufacturer.manage",
)

#: The vendor roles are `is_system` too, but have no business with the item
#: master, so only these two receive the grant.
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
    for permission in PERMISSIONS:
        _grant(SYSTEM_ROLES, permission)


def downgrade() -> None:
    for permission in PERMISSIONS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :permission) "
                "WHERE is_system"
            ).bindparams(permission=permission)
        )
