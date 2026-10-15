"""grant the shift and shift-roster permissions to the system roles

Revision ID: 0785162d1e47
Revises: 0ac18597894f
Created: 2026-10-12 09:01:00.000000+00:00

``shift.view``, ``shift.manage``, ``shift_roster.view`` and
``shift_roster.manage`` are new permission strings. A role's permission list is
stored, not computed - ``roles.permissions`` is a text array written when the
company was seeded - so adding the enum members reaches no deployed role by
itself, and the Shift roster screen would be invisible to every existing
account, the superuser's role included. This is the same step ``2f5e08c7ba61``
took for the item-attribute permissions, for the same reason; the deploy does
not re-run the seeder that would otherwise refresh the system roles.

Only ``is_system`` roles are touched. The operational presets that hold these
rights for a NEW company - the production roles (view the register, view and
plan the roster), PLANT_MANAGER (which also manages the register) and PLANNER
(both views) - are owned by the client once seeded, and rewriting one from a
preset would silently undo whatever an administrator decided. On an existing
deployment an administrator grants these from Settings -> Roles, exactly as for
any other permission. Until then nothing that already works stops working: the
shift pickers and the operator directory are also readable under rights those
roles already hold (``work_order.view``, ``work_order.manage``,
``production_entry.create``).

The strings are spelled literally rather than imported from the enum: a
migration is a fixed historical step and must do the same thing in a year that
it does today.

Review (master rules section 67):

* Locking impact:  one UPDATE per permission over ``roles``, a table with tens
                   of rows per company. Row locks only, held for the statement.
* Data migration:  yes - appends four strings to an array column, only where
                   absent, so re-running changes nothing.
* Rollback:        ``downgrade()`` removes the four strings from every system
                   role. Correct rather than merely tidy: below this revision the
                   routes they gate do not exist.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0785162d1e47"
down_revision: str | None = "0ac18597894f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
PERMISSIONS = (
    "shift.view",
    "shift.manage",
    "shift_roster.view",
    "shift_roster.manage",
)

#: ADMIN and SUPER_ADMIN are the only system roles with business on the
#: production masters; the vendor roles are system roles too and get none.
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
