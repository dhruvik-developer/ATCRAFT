"""Replace vendor roles with portal-only rights and grant administrator rights.

Locking: row locks on small roles table. Data migration: literal permission arrays.
Rollback: restores previous vendor presets and removes administrator portal rights.
Size: tens of roles per company. Indexes: none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a1100scope"
down_revision: str | None = "159ffb5b9cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
OLD = {
    "VENDOR_OWNER": [
        "dispatch.create",
        "dispatch.update",
        "dispatch.view",
        "document.upload",
        "document.view",
        "job_work.view",
        "notification.view",
        "payment.view",
        "purchase_order.update",
        "purchase_order.view",
        "qc.view",
        "user.view",
        "vendor_invoice.create",
        "vendor_invoice.view",
    ],
    "VENDOR_MANAGER": [
        "dispatch.view",
        "document.upload",
        "document.view",
        "job_work.view",
        "notification.view",
        "purchase_order.update",
        "purchase_order.view",
    ],
    "VENDOR_DISPATCH_MANAGER": [
        "dispatch.create",
        "dispatch.update",
        "dispatch.view",
        "document.upload",
        "document.view",
        "notification.view",
        "purchase_order.view",
    ],
    "VENDOR_QC_CONTACT": [
        "dispatch.view",
        "document.upload",
        "document.view",
        "notification.view",
        "purchase_order.view",
        "qc.view",
    ],
    "VENDOR_ACCOUNT_MANAGER": [
        "dispatch.view",
        "document.upload",
        "document.view",
        "ledger.view",
        "notification.view",
        "payment.view",
        "purchase_order.view",
        "vendor_invoice.create",
        "vendor_invoice.view",
    ],
}
NEW = {
    "VENDOR_OWNER": [
        "notification.view",
        "vendor_portal.access",
        "vendor_portal.respond",
        "vendor_portal.ship",
        "vendor_portal.invoice",
        "vendor_portal.quality",
    ],
    "VENDOR_MANAGER": [
        "notification.view",
        "vendor_portal.access",
        "vendor_portal.respond",
        "vendor_portal.ship",
    ],
    "VENDOR_DISPATCH_MANAGER": ["notification.view", "vendor_portal.access", "vendor_portal.ship"],
    "VENDOR_QC_CONTACT": ["notification.view", "vendor_portal.access", "vendor_portal.quality"],
    "VENDOR_ACCOUNT_MANAGER": [
        "notification.view",
        "vendor_portal.access",
        "vendor_portal.invoice",
    ],
}
RIGHTS = [
    "vendor_portal.access",
    "vendor_portal.respond",
    "vendor_portal.ship",
    "vendor_portal.invoice",
    "vendor_portal.quality",
]


def _replace(mapping: dict[str, list[str]]) -> None:
    for code, permissions in mapping.items():
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = :permissions WHERE is_system AND code = :code"
            ).bindparams(permissions=permissions, code=code)
        )


def upgrade() -> None:
    _replace(NEW)
    for right in RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_append(permissions, :right) WHERE is_system AND code IN ('SUPER_ADMIN','ADMIN') AND NOT (:right = ANY(permissions))"
            ).bindparams(right=right)
        )


def downgrade() -> None:
    _replace(OLD)
    for right in RIGHTS:
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = array_remove(permissions, :right) WHERE is_system AND code IN ('SUPER_ADMIN','ADMIN')"
            ).bindparams(right=right)
        )
