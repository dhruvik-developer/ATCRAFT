"""Domain-owned document policies without reversing document module dependencies.

Read providers are pure SQL builders registered by the owning module at import
time. Document repositories apply them before pagination and every direct
lookup.

Attach policies name the right that filing a document against one entity type
needs on top of ``document.upload``, registered the same way by the module that
owns the entity. Filing a file against a record is not always an internal act:
the dealer catalogue publishes every image filed against a sellable item, so
for that entity type an upload is an edit of the item. The generic upload
checks the policy before it reads a byte (``DocumentService.upload``).
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission

DocumentReadPolicy = Callable[
    [TenantContext, Principal | None, InstrumentedAttribute[Any]], ColumnElement[bool] | None
]
_read_policies: dict[str, DocumentReadPolicy] = {}
_attach_policies: dict[str, Permission] = {}


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


def register_document_attach_policy(entity_type: str, permission: Permission) -> None:
    """Require ``permission`` of anyone filing a document against ``entity_type``."""
    _attach_policies[entity_type] = permission


def document_attach_permission(entity_type: str | None) -> Permission | None:
    """The right filing against ``entity_type`` needs, or ``None`` when it needs none."""
    return None if entity_type is None else _attach_policies.get(entity_type)


def assert_may_attach(principal: Principal, entity_type: str | None) -> None:
    """Refuse to file a document against an entity type the caller may not change.

    403 ``PERMISSION_DENIED`` naming the missing right, like a route gate.
    """
    permission = document_attach_permission(entity_type)
    if permission is not None:
        principal.require(permission)
