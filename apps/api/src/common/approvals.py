"""Segregation of duties on approval: the one predicate the approving modules apply.

BOM, routing, sales, procurement and production each carried a word-for-word copy
of :func:`can_approve`, each claiming to be "one predicate, in one place". It is
now that. The refusal it leads to is :class:`src.core.errors.SelfApprovalError`,
so the same rule answers with the same status, code and sentence everywhere.

ASSUMPTION: the person who raised a document may not approve it, unless they are a
platform superuser or hold ``SUPER_ADMIN``. Approval is evidence of a second
person's judgement (master rules section 42), and a document approved by its own
author is not evidence of anything. Not every approving module grants the same
exemption - see docs/assumptions.md section 2, which lists who is exempt where.
"""

from __future__ import annotations

import uuid

from src.core.deps import Principal

#: The role that may approve its own work where :func:`can_approve` is the rule.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Whether ``principal`` may approve a document raised by ``created_by_id``.

    A document with no recorded author (imported, or raised by the system) has
    no second person to protect, so anyone holding the approval right may.
    """
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


__all__ = ["SUPER_ADMIN_ROLE_CODE", "can_approve"]
