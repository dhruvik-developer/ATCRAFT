"""grant the item attribute permissions to the roles that should hold them

Revision ID: 2f5e08c7ba61
Revises: 9b6f2ac41d38
Created: 2026-09-06 09:02:00.000000+00:00

A role's permission list is stored, not computed. `roles.permissions` is a text
array written when the company was seeded, and `_seed_roles` skipped any role
that already existed - so `item_attribute.view` and `item_attribute.manage`
reached the enum, the presets, the API and the frontend catalogue, and never
reached a single deployed role. The Attributes tab was invisible to everyone,
including the superuser, whose preset is literally `frozenset(Permission)`.

This was found by opening the screen rather than by any test: the permission
catalogue test compares the backend enum with the frontend constant and both
were correct, the API returned the rows, and the tab simply was not there.

`_seed_roles` now refreshes a SYSTEM role's permissions from its preset on every
run, which closes this for every future release. This migration does the same
thing once for deployments that will not re-run the seeder.

Only `is_system` roles are touched. A role the client built is theirs, and
rewriting it from a preset would silently undo what they decided.

The two permission strings are spelled literally rather than imported from
`ROLE_PRESETS`. A migration that reads current code does not do the same thing
in a year that it does today, and a migration is supposed to be a fixed
historical step.

Review (master rules section 67):

* Locking impact:  one UPDATE over `roles`, a table with tens of rows per
                   company. Row locks only, held for the statement.
* Data migration:  yes - it appends two strings to an array column, and only
                   where they are absent, so re-running changes nothing.
* Rollback:        `downgrade()` removes the two permissions again. That is
                   correct rather than merely tidy: the columns and endpoints
                   they gate do not exist below `4c1d70b8ea92`, so a role
                   carrying them after a downgrade would name a permission
                   nothing checks.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f5e08c7ba61"
down_revision: str | None = "9b6f2ac41d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
VIEW = "item_attribute.view"
MANAGE = "item_attribute.manage"

#: Only ADMIN and SUPER_ADMIN, because only they - and the vendor roles, which
#: have no business with the item master - are `is_system`. The operational
#: presets (DESIGN_ENGINEER, PLANT_MANAGER, STORE_KEEPER and the rest) are
#: seeded from presets and then OWNED BY THE CLIENT: `is_system` is false on
#: them precisely so an administrator can tune what a planner or a storekeeper
#: may do. Rewriting one from a preset would silently undo that decision, so
#: this migration does not, and neither does `_seed_roles`.
#:
#: The consequence is worth stating rather than hiding: on an existing
#: deployment a design engineer does NOT gain this permission automatically. An
#: administrator grants it from Settings -> Roles, which is the same way they
#: would grant any other. A newly seeded company gets it from the preset, where
#: DESIGN_ENGINEER holds both.
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
