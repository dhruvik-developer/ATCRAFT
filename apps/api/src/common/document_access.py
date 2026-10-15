"""Domain-owned read predicates without reversing document module dependencies.

Providers are pure SQL builders registered by the owning module at import time.
Document repositories apply them before pagination and every direct lookup.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute

from src.core.deps import Principal, TenantContext

DocumentReadPolicy = Callable[
    [TenantContext, Principal | None, InstrumentedAttribute[Any]], ColumnElement[bool] | None
]
_read_policies: dict[str, DocumentReadPolicy] = {}


def register_document_read_policy(name: str, policy: DocumentReadPolicy) -> None:
    _read_policies[name] = policy


def document_read_predicates(
    tenant: TenantContext,
    document_id: InstrumentedAttribute[Any],
    principal: Principal | None = None,
) -> tuple[ColumnElement[bool], ...]:
    return tuple(
        predicate
        for policy in _read_policies.values()
        if (predicate := policy(tenant, principal, document_id)) is not None
    )
