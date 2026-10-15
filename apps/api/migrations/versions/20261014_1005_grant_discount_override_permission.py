"""grant the sales discount-override permission to the system roles

Revision ID: d4e10a02bb02
Revises: d4e10a01aa01
Created: 2026-10-14 10:05:00.000000+00:00

``sales_order.discount_override`` is a new permission string. A role's
permission list is stored, not computed - ``roles.permissions`` is a text
array written when the company was seeded - so adding the enum member reaches
no deployed role by itself, and an above-threshold discount would be refused
for every existing account, the superuser's role included. This is the step
``c52f1a5b3525`` took for the stores permissions, for the same reason: the
deploy does not re-run the seeder that would otherwise refresh the system
roles.

Only ``SUPER_ADMIN`` and ``ADMIN`` are touched. The sales desk
(``SALES_EXECUTIVE``) deliberately does not get it: anyone who can raise an
order granting their own large discounts is the defect this package closes,
and granting the override to the same role would reopen it. On an existing
deployment an administrator grants it to whoever holds discount authority from
Settings -> Roles, exactly as for any other permission. The vendor and dealer
portal roles are system roles too and get none of this - a portal caller never
prices through this path with an explicit discount.

The string is spelled literally rather than imported from the enum: a
migration is a fixed historical step and must do the same thing in a year that
it does today.

Review (master rules section 67):

* Locking impact:  one UPDATE over ``roles``, a table with tens of rows per
                    company. Row locks only, held for the statement.
* Data migration:  yes - appends one string to an array column, only where
                    absent, so re-running changes nothing.
* Rollback:        ``downgrade()`` removes the string from every system role.
                    Correct rather than merely tidy: below this revision the
                    threshold rule it authorises does not exist.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e10a02bb02"
down_revision: str | None = "d4e10a01aa01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
PERMISSIONS = ("sales_order.discount_override",)

#: The two system roles with business signing off a discount; never a sales,
#: vendor or dealer role.
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
