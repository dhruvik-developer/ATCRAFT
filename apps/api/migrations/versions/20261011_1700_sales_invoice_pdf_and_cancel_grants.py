"""Grant sales_invoice.cancel to the system account roles.

``sales_invoice.cancel`` is a new permission string. A role's permission list
is stored, not computed, so adding the enum member reaches no deployed role by
itself. This is the same step the cost-centre grant took, and it is separate
from the report-key CHECK swap: a permission grant and a constraint change
roll back independently.

Only ``is_system`` roles are touched. ACCOUNTS holds the right in the preset
for a new company; on an existing deployment an administrator grants it from
Settings -> Roles.

The string is spelled literally rather than imported from the enum: a migration
is a fixed historical step.

Review (master rules section 67):

* Locking impact:  one UPDATE over ``roles``, tens of rows per company. Row
                   locks only, held for the statement.
* Data migration:  appends one string where absent, so re-running changes nothing.
* Rollback:        removes the string from every system role. Below this
                   revision the cancel route does not exist.
* Production size: proportional to the number of roles. Milliseconds.
* Index creation:  none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2a66f219cb5"
down_revision: str | None = "b28b4dab11a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out, deliberately - see the docstring.
CANCEL = "sales_invoice.cancel"

#: ADMIN and SUPER_ADMIN are the system roles that hold the accounts preset.
#: The vendor system roles do not cancel customer tax invoices.
SYSTEM_ROLES = ("SUPER_ADMIN", "ADMIN")


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE roles
            SET permissions = array_append(permissions, :permission)
            WHERE is_system
              AND code = ANY(:codes)
              AND NOT (:permission = ANY(permissions))
            """
        ).bindparams(permission=CANCEL, codes=list(SYSTEM_ROLES))
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE roles SET permissions = array_remove(permissions, :permission) WHERE is_system"
        ).bindparams(permission=CANCEL)
    )
