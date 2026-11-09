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

Entity resolvers prove an ``entity_id`` is a record of that type this company
holds, through the owning module's tenant-scoped repository. ``entity_id`` has
no foreign key - it cannot have one, the type is polymorphic - so without one a
file was filed against another company's record or an id naming none, with
201, and listed nowhere. The generic upload resolves before it reads a byte; an
entity type no module resolves is stored as given (docs/assumptions.md §DOC-2).
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission

DocumentReadPolicy = Callable[
    [TenantContext, Principal | None, InstrumentedAttribute[Any]], ColumnElement[bool] | None
]
_read_policies: dict[str, DocumentReadPolicy] = {}
_attach_policies: dict[str, Permission] = {}

#: Reads one record of an entity type through its owner's tenant-scoped
#: repository, raising ``NotFoundError`` for a missing or another company's id -
#: in practice that repository's ``get_or_404``. Awaited for that refusal alone.
DocumentEntityResolver = Callable[[AsyncSession, TenantContext, uuid.UUID], Awaitable[object]]
_entity_resolvers: dict[str, DocumentEntityResolver] = {}


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


def register_document_entity_resolver(entity_type: str, resolver: DocumentEntityResolver) -> None:
    """Hold an id filed under ``entity_type`` to a record of it this company holds."""
    _entity_resolvers[entity_type] = resolver


def document_entity_resolver(entity_type: str | None) -> DocumentEntityResolver | None:
    """The reader that resolves ``entity_type``'s ids, or ``None`` when none is registered."""
    return None if entity_type is None else _entity_resolvers.get(entity_type)


async def assert_entity_held(
    session: AsyncSession,
    tenant: TenantContext,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
) -> None:
    """Refuse to file a document against a record this company does not hold.

    404 ``NOT_FOUND`` naming the record, the same for an unknown id and another
    company's, as every tenant-scoped read answers. An entity type no module
    resolves - and no entity at all - is accepted as given.
    """
    resolver = document_entity_resolver(entity_type)
    if resolver is not None and entity_id is not None:
        await resolver(session, tenant, entity_id)
