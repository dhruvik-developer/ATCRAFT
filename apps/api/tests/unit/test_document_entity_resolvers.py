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

import re
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.common import document_access
from src.common.document_access import assert_entity_held, document_entity_resolver
from src.core.deps import Principal, TenantContext
from src.core.errors import NotFoundError
from src.modules.documents.router import upload_document
from src.modules.finance.credit_note_service import CREDIT_NOTE_DOCUMENT_ENTITY
from src.modules.finance.sales_invoice_service import SALES_INVOICE_DOCUMENT_ENTITY
from src.modules.finance.service import VALUATION_DOCUMENT_ENTITY
from src.modules.job_work.service import CHALLAN_DOCUMENT_ENTITY
from src.modules.packing.service import PACKING_ORDER_DOCUMENT_ENTITY
from src.modules.procurement.inbound_repository import ORDER_DOCUMENT_ENTITY
from src.modules.procurement.service import (
    PURCHASE_RETURN_DOCUMENT_ENTITY,
    RECEIPT_DOCUMENT_ENTITY,
)
from src.modules.products.service import ITEM_DOCUMENT_ENTITY
from src.modules.quality.service import INSPECTION_DOCUMENT_ENTITY
from src.modules.reports.service import REPORT_DOCUMENT_ENTITY
from src.modules.sales.service import SALES_ORDER_DOCUMENT_ENTITY
from src.modules.vendors.service import VENDOR_DOCUMENT_ENTITY
from src.modules.warehouses.service import WAREHOUSE_DOCUMENT_ENTITY
from src.workers.tasks import reports as report_tasks
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

#: The Documents page's model, whose ``KNOWN_ENTITY_TYPES`` are the record types
#: its free-form "Attach to record type" field suggests.
DOCUMENT_MODEL = (
    Path(__file__).resolve().parents[3]
    / "web"
    / "src"
    / "features"
    / "documents"
    / "document-model.ts"
)

#: Entity types a person files documents under through ``POST /documents``: the
#: item image panel, an inspection's attachments and reading photos, the record
#: types the Documents page suggests, the records people attach their own
#: paperwork to - a purchase order's signed copy, a carrier's challan on a goods
#: receipt, a vendor's registration, a customer's order on a sales order, a stock
#: extract on a warehouse, which the demo loader files as a person would - and
#: every type the report worker files a generated document under. The Documents
#: page lists those types and takes the type as free text, so a person can file
#: a customer's signed invoice, an acknowledged credit note, debit note or
#: challan, or an auditor's note on a valuation under the same type. Every one
#: needs a resolver, or a mistyped or foreign id files the document against
#: nothing.
FILED_THROUGH_UPLOAD = (
    ITEM_DOCUMENT_ENTITY,
    INSPECTION_DOCUMENT_ENTITY,
    PACKING_ORDER_DOCUMENT_ENTITY,
    REPORT_DOCUMENT_ENTITY,
    ORDER_DOCUMENT_ENTITY,
    RECEIPT_DOCUMENT_ENTITY,
    PURCHASE_RETURN_DOCUMENT_ENTITY,
    VENDOR_DOCUMENT_ENTITY,
    SALES_ORDER_DOCUMENT_ENTITY,
    SALES_INVOICE_DOCUMENT_ENTITY,
    CREDIT_NOTE_DOCUMENT_ENTITY,
    WAREHOUSE_DOCUMENT_ENTITY,
    VALUATION_DOCUMENT_ENTITY,
    CHALLAN_DOCUMENT_ENTITY,
)

#: Entity types whose records are report jobs. ``GET /reports/jobs/{id}``
#: answers 404 for a job its caller may not open, so filing against one needs
#: that same access, not only a job this company holds - or a 201 where the job
#: route says 404 tells the filer which ids are jobs.
HELD_TO_THE_FILERS_ACCESS = (REPORT_DOCUMENT_ENTITY, VALUATION_DOCUMENT_ENTITY)

#: A type no module knows, which ``POST /documents`` stores as given.
UNKNOWN_ENTITY_TYPE = "drawing_register"

_SUGGESTIONS = re.compile(r"KNOWN_ENTITY_TYPES\s*=\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"'([a-z][a-z0-9_]*)'")
_RESOLVED_IN_DESCRIPTION = re.compile(r"its module resolves \(([^)]*)\)")


def unresolved(entity_types: tuple[str, ...]) -> list[str]:
    """The entity types no registered resolver covers."""
    return [entity for entity in entity_types if document_entity_resolver(entity) is None]


def worker_filed(module: object) -> tuple[str, ...]:
    """The entity types a worker module files documents under: its ``*_ENTITY`` names."""
    return tuple(
        sorted(
            {
                value
                for name, value in vars(module).items()
                if name.endswith("_ENTITY") and isinstance(value, str)
            }
        )
    )


def suggested(source: str) -> list[str]:
    """The record types a Documents page model suggests, in its order."""
    found = _SUGGESTIONS.search(source)
    assert found is not None, "KNOWN_ENTITY_TYPES is no longer a literal list"
    return _QUOTED.findall(found.group(1))


@pytest.fixture(scope="module", autouse=True)
def _started_application() -> None:
    module_routes()


def _session() -> AsyncSession:
    """A stand-in session: the resolvers under test here never touch it."""
    return cast("AsyncSession", object())


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _filer() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="filer@atcraft.test",
        full_name="Document Filer",
        is_superuser=False,
        permissions=frozenset(),
    )


def test_every_entity_type_a_person_files_through_the_upload_resolves() -> None:
    assert unresolved(FILED_THROUGH_UPLOAD) == []


def test_the_guard_reports_a_filed_entity_type_with_no_resolver(monkeypatch) -> None:
    monkeypatch.delitem(document_access._entity_resolvers, INSPECTION_DOCUMENT_ENTITY)
    assert unresolved(FILED_THROUGH_UPLOAD) == [INSPECTION_DOCUMENT_ENTITY]


def test_the_registry_resolves_exactly_the_declared_entity_types() -> None:
    # Which types have a resolver is stated above; adding or removing one adds
    # an entity type to the list or takes it off.
    assert sorted(document_access._entity_resolvers) == sorted(FILED_THROUGH_UPLOAD)
    assert unresolved((UNKNOWN_ENTITY_TYPE,)) == [UNKNOWN_ENTITY_TYPE]


def test_the_guard_reports_a_resolver_nobody_declared(monkeypatch) -> None:
    async def anything(session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID) -> None:
        return None

    monkeypatch.setitem(document_access._entity_resolvers, UNKNOWN_ENTITY_TYPE, anything)
    assert sorted(document_access._entity_resolvers) != sorted(FILED_THROUGH_UPLOAD)
    assert unresolved((UNKNOWN_ENTITY_TYPE,)) == []


def test_every_type_the_report_worker_files_under_resolves() -> None:
    # The Documents page lists a generated invoice, credit note, debit note,
    # valuation workbook or challan under its type, and a person typing that
    # type beside another company's id, or an id naming none, filed against
    # nothing - those five were stored as given.
    filed = worker_filed(report_tasks)

    assert report_tasks.SALES_INVOICE_ENTITY in filed
    assert unresolved(filed) == []


def test_the_worker_guard_reports_a_type_with_no_resolver(monkeypatch) -> None:
    monkeypatch.delitem(document_access._entity_resolvers, CREDIT_NOTE_DOCUMENT_ENTITY)
    monkeypatch.setattr(report_tasks, "SURVEY_SHEET_ENTITY", UNKNOWN_ENTITY_TYPE, raising=False)

    assert unresolved(worker_filed(report_tasks)) == [
        CREDIT_NOTE_DOCUMENT_ENTITY,
        UNKNOWN_ENTITY_TYPE,
    ]


def test_every_record_type_the_documents_page_suggests_resolves() -> None:
    # The page invites a person to type these; one without a resolver files a
    # mistyped or foreign id against nothing.
    offered = suggested(DOCUMENT_MODEL.read_text(encoding="utf-8"))

    assert offered, "the Documents page suggests no record type"
    assert unresolved(tuple(offered)) == []


def test_the_suggestion_reader_sees_each_quoted_type() -> None:
    source = (
        f"export const KNOWN_ENTITY_TYPES = ['qc_inspection', '{UNKNOWN_ENTITY_TYPE}'] as const"
    )
    assert suggested(source) == ["qc_inspection", UNKNOWN_ENTITY_TYPE]
    assert unresolved(tuple(suggested(source))) == [UNKNOWN_ENTITY_TYPE]


def test_the_upload_route_names_exactly_the_resolved_types() -> None:
    # The contract says which types are held to a record; it is written by hand,
    # so it is read back against the registry.
    (route,) = (
        mounted.route for mounted in module_routes() if mounted.route.endpoint is upload_document
    )
    named = _RESOLVED_IN_DESCRIPTION.search(route.description or "")

    assert named is not None, route.description
    assert sorted(re.findall(r"`([a-z_]+)`", named.group(1))) == sorted(
        document_access._entity_resolvers
    )


def test_the_worker_files_under_the_types_people_file_under() -> None:
    # Every generated document is found under the same type as a file a person
    # attached to that record, named once by the record's owner.
    assert report_tasks.PACKING_LIST_ENTITY == PACKING_ORDER_DOCUMENT_ENTITY
    assert report_tasks.GENERATED_REPORT_ENTITY == REPORT_DOCUMENT_ENTITY
    assert report_tasks.PURCHASE_ORDER_ENTITY == ORDER_DOCUMENT_ENTITY
    assert report_tasks.SALES_INVOICE_ENTITY == SALES_INVOICE_DOCUMENT_ENTITY
    assert report_tasks.CREDIT_NOTE_ENTITY == CREDIT_NOTE_DOCUMENT_ENTITY
    assert report_tasks.PURCHASE_RETURN_ENTITY == PURCHASE_RETURN_DOCUMENT_ENTITY
    assert report_tasks.VALUATION_ENTITY == VALUATION_DOCUMENT_ENTITY
    assert report_tasks.JOB_WORK_CHALLAN_ENTITY == CHALLAN_DOCUMENT_ENTITY


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


def test_a_report_job_is_held_to_what_the_filer_may_open() -> None:
    assert sorted(document_access._entity_access_checks) == sorted(HELD_TO_THE_FILERS_ACCESS)
    # A module filing against its own job passes no principal and is resolved
    # as before, so each of these types keeps its resolver too.
    assert unresolved(HELD_TO_THE_FILERS_ACCESS) == []


async def test_a_filer_is_held_to_the_access_check_and_a_module_to_the_resolver(
    monkeypatch,
) -> None:
    seen: list[tuple[str, uuid.UUID]] = []

    async def held(session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID) -> object:
        seen.append(("resolver", entity_id))
        return object()

    async def out_of_reach(
        session: AsyncSession, tenant: TenantContext, principal: Principal, entity_id: uuid.UUID
    ) -> object:
        seen.append(("access", entity_id))
        raise NotFoundError("Test drawing", entity_id)

    monkeypatch.setitem(document_access._entity_resolvers, "test_drawing", held)
    monkeypatch.setitem(document_access._entity_access_checks, "test_drawing", out_of_reach)
    record = uuid.uuid4()

    with pytest.raises(NotFoundError) as refused:
        await assert_entity_held(_session(), _tenant(), "test_drawing", record, principal=_filer())
    # The check alone decides for a filer: it refuses an id naming nothing too,
    # because a resolver's differently worded 404 beside it would be an oracle.
    assert refused.value.message == f"Test drawing not found: {record}."
    assert seen == [("access", record)]

    # A module filing against its own record passes no principal: resolved.
    await assert_entity_held(_session(), _tenant(), "test_drawing", record)
    # A type with a resolver and no access check resolves a filer as before.
    monkeypatch.delitem(document_access._entity_access_checks, "test_drawing")
    await assert_entity_held(_session(), _tenant(), "test_drawing", record, principal=_filer())

    assert seen == [("access", record), ("resolver", record), ("resolver", record)]
