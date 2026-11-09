"""``POST /documents`` against an item needs the right to change the item, and
against a record of a type its module resolves, one this company holds.

The dealer catalogue shows and serves every JPEG, PNG or WebP filed against a
sellable item to every dealer (docs/assumptions.md §DOC-1). Before the attach
policy, ``document.upload`` alone - which the store keeper, purchase executive,
QC inspector and dispatch manager presets hold without ``item.update`` - was
enough to put a defect photo or an internal drawing in front of every dealer
from the Documents screen. These drive the route end to end: the refusal
stores nothing, an item editor's image reaches the catalogue, and an image filed
under any other entity type stays inside the company. A file filed against an
item, an inspection, a vendor or a warehouse that is another company's, or no
record at all, is refused before a byte is stored, and so is a warehouse
outside the caller's own assignment (docs/assumptions.md §DOC-2).

Fixtures hand back ids, never ORM rows: an expected failure rolls its savepoint
back and expires what was loaded before it (CLAUDE.md).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.documents.models import Document
from src.modules.quality import service as quality
from src.modules.quality.models import QcStage
from src.modules.quality.schemas import InspectionCreate
from tests import seed_support
from tests.api.dealer_portal_support import (
    dealer_customer,
    dealer_principal,
    portal_user,
    sellable_item,
    sign_in,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(48)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_path", root)
    return root


@pytest.fixture
async def catalogue(
    session: AsyncSession, tenant: TenantContext, company, principal: Principal
) -> SimpleNamespace:
    """A sellable item and a dealer buyer who can see it in the catalogue."""
    company.state, company.state_code = "Gujarat", "24"
    await session.flush()
    dealer = await dealer_customer(session, tenant, "Our Hardware Mart")
    buyer = await portal_user(session, tenant, dealer.id)
    item = await sellable_item(session, tenant)
    return SimpleNamespace(
        item_id=item.id, dealer=dealer_principal(principal, dealer.id, user_id=buyer.id)
    )


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's rows, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


async def raised_inspection(
    session: AsyncSession, owner: TenantContext, item_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A hand-raised inspection of ``owner``'s, holding no stock; of ``item_id`` if named."""
    if item_id is None:
        item_id = (
            await seed_support.seed_item(
                session, company_id=owner.company_id, code=f"QC-{uuid.uuid4().hex[:8].upper()}"
            )
        ).id
    store = await seed_support.seed_warehouse(
        session, company_id=owner.company_id, code=f"QW-{uuid.uuid4().hex[:8].upper()}"
    )
    raised = await quality.create_inspection(
        session,
        owner,
        InspectionCreate(
            item_id=item_id,
            warehouse_id=store.id,
            qc_stage=QcStage.IN_PROCESS,
            inspected_quantity=Decimal("4"),
            reason="Supervisor pulled the lot",
        ),
    )
    return raised.id


async def seeded(session: AsyncSession, company_id: uuid.UUID, entity_type: str) -> uuid.UUID:
    """A vendor or a warehouse of ``company_id``'s, by the document entity type naming it."""
    code = f"DOC-{uuid.uuid4().hex[:8].upper()}"
    if entity_type == "vendor":
        return (await seed_support.seed_vendor(session, company_id=company_id, code=code)).id
    return (await seed_support.seed_warehouse(session, company_id=company_id, code=code)).id


#: What a 404 calls each record type, as its repository's ``get_or_404`` words it.
RECORD_LABELS = {"vendor": "Vendor", "warehouse": "Warehouse"}


def preset(base: Principal, name: str) -> Principal:
    """The same person holding exactly one role preset."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS[name]["permissions"])
    return holding(base, *granted)


def holding(base: Principal, *permissions: Permission) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


async def upload(
    api_client: AsyncClient, entity_type: str, entity_id: uuid.UUID, name: str = "front.png"
) -> Response:
    return await api_client.post(
        "/documents",
        data={"category": "ATTACHMENT", "entity_type": entity_type, "entity_id": str(entity_id)},
        files={"file": (name, PNG, "image/png")},
    )


def data_of(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def error_of(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


async def filed_against(session: AsyncSession, entity_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Document).where(Document.entity_id == entity_id)
    return int((await session.execute(stmt)).scalar_one())


def stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


async def test_a_store_keeper_cannot_publish_an_image_to_the_dealer_catalogue(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    catalogue: SimpleNamespace,
    storage: Path,
) -> None:
    sign_in(preset(principal, "STORE_KEEPER"))

    refused = await upload(api_client, "item", catalogue.item_id)

    error = error_of(refused, 403, "PERMISSION_DENIED")
    assert error["details"]["required_permission"] == str(Permission.ITEM_UPDATE)
    # Refused before a byte was read: no row, and nothing left on disk.
    assert await filed_against(session, catalogue.item_id) == 0
    assert stored_files(storage) == []

    # Filing anywhere else is unchanged: the same keeper's photo for an
    # inspection of that very item is stored, and stays inside the company.
    inspection_id = await raised_inspection(session, tenant, catalogue.item_id)
    photo = data_of(await upload(api_client, "qc_inspection", inspection_id, "dent.png"), 201)
    assert photo["entity_type"] == "qc_inspection"
    sign_in(catalogue.dealer)
    detail = data_of(await api_client.get(f"/dealer-portal/catalog/items/{catalogue.item_id}"))
    assert detail["image_ids"] == []
    error_of(await api_client.get(f"/dealer-portal/images/{photo['id']}"), 404, "NOT_FOUND")


async def test_an_item_editor_files_an_image_the_dealer_catalogue_shows(
    api_client: AsyncClient,
    principal: Principal,
    catalogue: SimpleNamespace,
    storage: Path,
) -> None:
    sign_in(holding(principal, Permission.DOCUMENT_UPLOAD, Permission.ITEM_UPDATE))

    filed = data_of(await upload(api_client, "item", catalogue.item_id), 201)

    assert (filed["entity_type"], filed["entity_id"]) == ("item", str(catalogue.item_id))
    assert len(stored_files(storage)) == 1
    sign_in(catalogue.dealer)
    detail = data_of(await api_client.get(f"/dealer-portal/catalog/items/{catalogue.item_id}"))
    assert detail["image_ids"] == [filed["id"]]
    served = await api_client.get(f"/dealer-portal/images/{filed['id']}")
    assert served.status_code == 200, served.text
    assert served.content == PNG


@pytest.mark.parametrize("whose", ["another-company", "no-such-item"])
async def test_filing_against_an_item_this_company_does_not_hold_is_not_found(
    api_client: AsyncClient,
    session: AsyncSession,
    principal: Principal,
    foreign: TenantContext,
    storage: Path,
    whose: str,
) -> None:
    named = uuid.uuid4()
    if whose == "another-company":
        theirs = await seed_support.seed_item(
            session, company_id=foreign.company_id, code=f"FG-{uuid.uuid4().hex[:8].upper()}"
        )
        named = theirs.id
    sign_in(holding(principal, Permission.DOCUMENT_UPLOAD, Permission.ITEM_UPDATE))

    error = error_of(await upload(api_client, "item", named), 404, "NOT_FOUND")

    assert error["message"] == f"Item not found: {named}."
    # Refused before a byte was read: no row, and nothing left on disk.
    assert await filed_against(session, named) == 0
    assert stored_files(storage) == []


@pytest.mark.parametrize("whose", ["another-company", "no-such-inspection", "an-item"])
async def test_filing_against_an_inspection_this_company_does_not_hold_is_not_found(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    foreign: TenantContext,
    storage: Path,
    whose: str,
) -> None:
    if whose == "another-company":
        named = await raised_inspection(session, foreign)
    elif whose == "an-item":
        # This company holds the record, but an item's id is not an inspection's.
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=f"FG-{uuid.uuid4().hex[:8].upper()}"
        )
        named = item.id
    else:
        named = uuid.uuid4()
    sign_in(preset(principal, "QC_INSPECTOR"))

    error = error_of(await upload(api_client, "qc_inspection", named, "dent.png"), 404, "NOT_FOUND")

    assert error["message"] == f"QC inspection not found: {named}."
    assert await filed_against(session, named) == 0
    assert stored_files(storage) == []


async def test_an_inspector_files_a_photo_against_this_companys_inspection(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    storage: Path,
) -> None:
    inspection_id = await raised_inspection(session, tenant)
    sign_in(preset(principal, "QC_INSPECTOR"))

    filed = data_of(await upload(api_client, "qc_inspection", inspection_id, "dent.png"), 201)

    assert (filed["entity_type"], filed["entity_id"]) == ("qc_inspection", str(inspection_id))
    assert await filed_against(session, inspection_id) == 1
    assert len(stored_files(storage)) == 1


async def test_an_entity_type_no_module_resolves_is_filed_as_given(
    api_client: AsyncClient,
    session: AsyncSession,
    principal: Principal,
    storage: Path,
) -> None:
    named = uuid.uuid4()
    sign_in(holding(principal, Permission.DOCUMENT_UPLOAD))

    filed = data_of(await upload(api_client, "drawing_register", named, "plan.png"), 201)

    assert (filed["entity_type"], filed["entity_id"]) == ("drawing_register", str(named))
    assert await filed_against(session, named) == 1


@pytest.mark.parametrize("entity_type", sorted(RECORD_LABELS))
@pytest.mark.parametrize("whose", ["another-company", "no-such-record"])
async def test_filing_against_a_vendor_or_a_warehouse_this_company_does_not_hold_is_not_found(
    api_client: AsyncClient,
    session: AsyncSession,
    principal: Principal,
    foreign: TenantContext,
    storage: Path,
    entity_type: str,
    whose: str,
) -> None:
    # Types only the demo loader filed under until now - a person typing one
    # into the Documents page's "Attach to" fields filed against nothing.
    named = (
        await seeded(session, foreign.company_id, entity_type)
        if whose == "another-company"
        else uuid.uuid4()
    )
    sign_in(holding(principal, Permission.DOCUMENT_UPLOAD))

    error = error_of(await upload(api_client, entity_type, named, "licence.png"), 404, "NOT_FOUND")

    assert error["message"] == f"{RECORD_LABELS[entity_type]} not found: {named}."
    assert await filed_against(session, named) == 0
    assert stored_files(storage) == []


@pytest.mark.parametrize("entity_type", sorted(RECORD_LABELS))
async def test_a_file_is_filed_against_this_companys_vendor_or_warehouse(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    storage: Path,
    entity_type: str,
) -> None:
    named = await seeded(session, tenant.company_id, entity_type)
    sign_in(holding(principal, Permission.DOCUMENT_UPLOAD))

    filed = data_of(await upload(api_client, entity_type, named, "licence.png"), 201)

    assert (filed["entity_type"], filed["entity_id"]) == (entity_type, str(named))
    assert await filed_against(session, named) == 1
    assert len(stored_files(storage)) == 1


async def test_a_warehouse_outside_the_callers_assignment_is_not_found(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    storage: Path,
) -> None:
    # This company's store, but one the caller is not assigned to: the resolver
    # reads through the warehouse repository, which every read of a warehouse
    # is held to, so it is as absent here as on the warehouse list.
    own = await seeded(session, tenant.company_id, "warehouse")
    other = await seeded(session, tenant.company_id, "warehouse")
    sign_in(replace(holding(principal, Permission.DOCUMENT_UPLOAD), allowed_warehouse_ids=(own,)))

    error = error_of(await upload(api_client, "warehouse", other, "licence.png"), 404, "NOT_FOUND")
    filed = data_of(await upload(api_client, "warehouse", own, "licence.png"), 201)

    assert error["message"] == f"Warehouse not found: {other}."
    assert await filed_against(session, other) == 0
    assert filed["entity_id"] == str(own)
