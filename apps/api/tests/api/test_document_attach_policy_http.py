"""``POST /documents`` against an item needs the right to change the item.

The dealer catalogue shows and serves every JPEG, PNG or WebP filed against a
sellable item to every dealer (docs/assumptions.md §DOC-1). Before the attach
policy, ``document.upload`` alone - which the store keeper, purchase executive,
QC inspector and dispatch manager presets hold without ``item.update`` - was
enough to put a defect photo or an internal drawing in front of every dealer
from the Documents screen. These drive the route end to end: the refusal
stores nothing, an item editor's image reaches the catalogue, and an image filed
under any other entity type stays inside the company.

Fixtures hand back ids, never ORM rows: an expected failure rolls its savepoint
back and expires what was loaded before it (CLAUDE.md).
"""

from __future__ import annotations

import uuid
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

    # Filing anywhere else is unchanged: the same keeper's inspection photo of
    # that very item is stored, and stays inside the company.
    photo = data_of(await upload(api_client, "qc_inspection", catalogue.item_id, "dent.png"), 201)
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
