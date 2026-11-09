"""Filing a document against a record needs the record to be this company's.

``documents.entity_id`` is polymorphic and carries no foreign key, so another
company's record, or an id naming none, satisfies every constraint: the file was
stored with 201 and listed under nothing anyone can open. The module owning an
entity type registers a tenant-scoped reader for it, and the generic upload
resolves the id before it reads a byte (docs/assumptions.md §DOC-2). The
registry is filled at import, which ``register_module_routers`` does at startup,
so it is read here only after loading the module routers the same way.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.common import document_access
from src.common.document_access import assert_entity_held, document_entity_resolver
from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.modules.procurement.inbound_repository import ORDER_DOCUMENT_ENTITY
from src.modules.products.service import ITEM_DOCUMENT_ENTITY
from src.modules.quality.service import INSPECTION_DOCUMENT_ENTITY
from src.workers.tasks import reports as report_tasks
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

#: Entity types the application's own screens file through ``POST /documents``:
#: the item image panel, and an inspection's attachments and reading photos.
#: Every one needs a resolver, or a mistyped or foreign id files the document
#: against nothing.
FILED_THROUGH_UPLOAD = (ITEM_DOCUMENT_ENTITY, INSPECTION_DOCUMENT_ENTITY)

#: Entity types the code base files documents under that no module resolves.
#: Each is written by the module or worker owning the record, against a record
#: it has just read or created - generated PDFs and exports, a purchase order's
#: shared files, the demo loader's attachments - and no screen names one through
#: ``POST /documents``, which stores such a type as given. A screen that starts
#: filing under one moves it to ``FILED_THROUGH_UPLOAD``, and its owner
#: registers a resolver.
UNRESOLVED_ENTITY_TYPES = (
    report_tasks.PACKING_LIST_ENTITY,
    report_tasks.SALES_INVOICE_ENTITY,
    report_tasks.CREDIT_NOTE_ENTITY,
    report_tasks.PURCHASE_RETURN_ENTITY,
    report_tasks.PURCHASE_ORDER_ENTITY,
    report_tasks.VALUATION_ENTITY,
    report_tasks.JOB_WORK_CHALLAN_ENTITY,
    report_tasks.GENERATED_REPORT_ENTITY,
    ORDER_DOCUMENT_ENTITY,
    "goods_receipt",
    "vendor",
    "sales_order",
    "warehouse",
)


def unresolved(entity_types: tuple[str, ...]) -> list[str]:
    """The entity types no registered resolver covers."""
    return [entity for entity in entity_types if document_entity_resolver(entity) is None]


@pytest.fixture(scope="module", autouse=True)
def _started_application() -> None:
    module_routes()


def _session() -> AsyncSession:
    """A stand-in session: the resolvers under test here never touch it."""
    return cast("AsyncSession", object())


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def test_every_entity_type_a_screen_files_through_the_upload_resolves() -> None:
    assert unresolved(FILED_THROUGH_UPLOAD) == []


def test_the_guard_reports_a_filed_entity_type_with_no_resolver(monkeypatch) -> None:
    monkeypatch.delitem(document_access._entity_resolvers, INSPECTION_DOCUMENT_ENTITY)
    assert unresolved(FILED_THROUGH_UPLOAD) == [INSPECTION_DOCUMENT_ENTITY]


def test_the_registry_resolves_exactly_the_declared_entity_types() -> None:
    # Which types have a resolver and which have none is stated above; adding or
    # removing one moves an entity type from one list to the other.
    assert sorted(document_access._entity_resolvers) == sorted(FILED_THROUGH_UPLOAD)
    assert unresolved(UNRESOLVED_ENTITY_TYPES) == list(UNRESOLVED_ENTITY_TYPES)


def test_the_guard_reports_a_resolver_nobody_declared(monkeypatch) -> None:
    async def anything(session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID) -> None:
        return None

    monkeypatch.setitem(document_access._entity_resolvers, "vendor", anything)
    assert sorted(document_access._entity_resolvers) != sorted(FILED_THROUGH_UPLOAD)
    assert "vendor" not in unresolved(UNRESOLVED_ENTITY_TYPES)


async def test_a_resolved_entity_type_refuses_an_id_its_reader_cannot_find(monkeypatch) -> None:
    seen: list[tuple[Any, TenantContext, uuid.UUID]] = []

    async def none_held(session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID) -> None:
        seen.append((session, tenant, entity_id))
        raise NotFoundError("Test drawing", entity_id)

    monkeypatch.setitem(document_access._entity_resolvers, "test_drawing", none_held)
    session, tenant, missing = _session(), _tenant(), uuid.uuid4()

    with pytest.raises(NotFoundError) as refused:
        await assert_entity_held(session, tenant, "test_drawing", missing)

    assert refused.value.status_code == 404
    assert refused.value.message == f"Test drawing not found: {missing}."
    # The owning module's reader is handed the caller's tenant: it decides.
    assert seen == [(session, tenant, missing)]


async def test_a_held_record_and_an_unresolved_entity_type_are_accepted(monkeypatch) -> None:
    held: list[uuid.UUID] = []

    async def found(session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID) -> object:
        held.append(entity_id)
        return object()

    monkeypatch.setitem(document_access._entity_resolvers, "test_drawing", found)
    record = uuid.uuid4()

    await assert_entity_held(_session(), _tenant(), "test_drawing", record)
    # No resolver, or no entity at all: stored as given, and no reader runs.
    await assert_entity_held(_session(), _tenant(), "drawing_register", uuid.uuid4())
    await assert_entity_held(_session(), _tenant(), None, None)

    assert held == [record]
