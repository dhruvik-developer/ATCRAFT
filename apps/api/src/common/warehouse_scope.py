"""Warehouse assignment policy shared by master, document and movement queries.

Company isolation remains the repository's responsibility. An empty assignment
means unrestricted access within that company; platform administrators are also
unrestricted, matching the warehouse master policy.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import InstrumentedAttribute

from src.core.deps import TenantContext
from src.core.errors import AuthorizationError, ErrorCode


def warehouse_scope(tenant: TenantContext, *columns: InstrumentedAttribute[Any]) -> tuple[Any, ...]:
    """AND every warehouse column with the assignment, retaining tenant filters."""
    if tenant.is_superuser or not tenant.allowed_warehouse_ids:
        return ()
    return tuple(column.in_(list(tenant.allowed_warehouse_ids)) for column in columns)


def assert_warehouse_scope(tenant: TenantContext, *warehouse_ids: uuid.UUID) -> None:
    """Refuse a user-driven operation touching any unassigned warehouse."""
    if tenant.is_superuser or not tenant.allowed_warehouse_ids:
        return
    outside = [
        value for value in dict.fromkeys(warehouse_ids) if value not in tenant.allowed_warehouse_ids
    ]
    if outside:
        raise AuthorizationError(
            "This document involves a warehouse you are not assigned to.",
            code=ErrorCode.SCOPE_DENIED,
            details={"warehouse_ids": [str(value) for value in outside]},
        )
