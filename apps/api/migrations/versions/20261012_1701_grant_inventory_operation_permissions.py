"""grant the scrap-disposal and internal-issue permissions to the system roles

Revision ID: c52f1a5b3525
Revises: 4ba9df3d2137
Created: 2026-10-12 17:01:00.000000+00:00

``inventory.scrap_dispose``, ``internal_issue.create`` and
``internal_issue.approve`` are new permission strings. A role's permission list
is stored, not computed - ``roles.permissions`` is a text array written when the
company was seeded - so adding the enum members reaches no deployed role by
itself, and the Scrap disposals and Internal issues tabs would refuse every
existing account, the superuser's role included. This is the step
``2f5e08c7ba61`` and ``e1eed3775093`` took for their permissions, for the same
reason: the deploy does not re-run the seeder that would otherwise refresh the
system roles.

Only ``SUPER_ADMIN`` and ``ADMIN`` are touched. The vendor roles are system roles
too and get none of these - a vendor's staff have no business disposing of the
company's scrap or issuing its stores. The operational presets that hold them
for a NEW company (STORE_KEEPER: dispose and create; PLANT_MANAGER: approve)
are client-owned once seeded; on an existing deployment an administrator grants
them from Settings -> Roles, exactly as for any other permission.

The strings are spelled literally rather than imported from the enum: a
migration is a fixed historical step and must do the same thing in a year that
it does today.

Review (master rules section 67):

* Locking impact:  one UPDATE per permission over ``roles``, a table with tens
                   of rows per company. Row locks only, held for the statement.
* Data migration:  yes - appends three strings to an array column, only where
                   absent, so re-running changes nothing.
* Rollback:        ``downgrade()`` removes the three strings from every system
                   role. Correct rather than merely tidy: below this revision
                   the routes they gate do not exist.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c52f1a5b3525"
down_revision: str | None = "4ba9df3d2137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
PERMISSIONS = (
    "inventory.scrap_dispose",
    "internal_issue.create",
    "internal_issue.approve",
)

#: The two system roles with business in the stores; never a vendor role.
SYSTEM_ROLES = ("SUPER_ADMIN", "ADMIN")


def upgrade() -> None:
    for permission in PERMISSIONS:
        op.execute(
            sa.text(
                """
                UPDATE roles
                SET permissions = array_append(permissions, :permission)
                WHERE is_system
                  AND code = ANY(:codes)
                  AND NOT (:permission = ANY(permissions))
                """
            ).bindparams(permission=permission, codes=list(SYSTEM_ROLES))
        )


def downgrade() -> None:
    for permission in PERMISSIONS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :permission) "
                "WHERE is_system"
            ).bindparams(permission=permission)
        )
