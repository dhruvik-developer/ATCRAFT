"""Filing a document where a portal publishes it needs the right to change its record.

The dealer catalogue shows and serves every JPEG, PNG or WebP filed against a
sellable item to every dealer, by ``documents.entity_type`` alone, so an upload
filed under ``item`` is an edit of the item's public face (docs/assumptions.md
§DOC-1). The products module registers that policy when it is imported, and
``register_module_routers`` is what imports it at startup - so the registry is
read here only after loading the module routers the same way.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import cast

import pytest

from src.common import document_access
from src.common.document_access import assert_may_attach, document_attach_permission
from src.core.deps import Principal
from src.core.errors import PermissionDeniedError
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.dealer_portal.repository import CATALOGUE_ENTITY
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

#: Entity types whose files a portal publishes by ``documents.entity_type`` with
#: no link row naming each file. Every one needs an attach policy, or holding
#: ``document.upload`` alone is enough to publish a file outside the company.
#: The vendor portal is not here: it shares only files a typed link row names.
PUBLISHED_ENTITY_TYPES = (CATALOGUE_ENTITY,)


def unguarded(entity_types: Iterable[str]) -> list[str]:
    """The published entity types that no attach policy covers."""
    return [entity for entity in entity_types if document_attach_permission(entity) is None]


@pytest.fixture(scope="module", autouse=True)
def _started_application() -> None:
    module_routes()


def _caller(*permissions: Permission | str, superuser: bool = False) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="uploader@test.local",
        full_name="Uploader",
        is_superuser=superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


def test_every_published_entity_type_has_an_attach_policy() -> None:
    assert unguarded(PUBLISHED_ENTITY_TYPES) == []


def test_the_guard_reports_a_published_entity_type_with_no_policy(monkeypatch) -> None:
    monkeypatch.delitem(document_access._attach_policies, CATALOGUE_ENTITY)
    assert unguarded(PUBLISHED_ENTITY_TYPES) == [CATALOGUE_ENTITY]


def test_filing_against_an_item_needs_the_right_to_change_items() -> None:
    from src.modules.products.service import ITEM_DOCUMENT_ENTITY

    # The catalogue reads the entity type the products module files images under.
    assert ITEM_DOCUMENT_ENTITY == CATALOGUE_ENTITY
    assert document_attach_permission(CATALOGUE_ENTITY) is Permission.ITEM_UPDATE


@pytest.mark.parametrize(
    "preset", ["STORE_KEEPER", "PURCHASE_EXECUTIVE", "QC_INSPECTOR", "DISPATCH_MANAGER"]
)
def test_an_uploader_without_item_rights_cannot_file_against_an_item(preset: str) -> None:
    granted = cast("frozenset[Permission]", ROLE_PRESETS[preset]["permissions"])
    caller = _caller(*granted)
    # The presets this rule exists for: they may upload, and may not edit items.
    assert caller.has(Permission.DOCUMENT_UPLOAD)
    assert not caller.has(Permission.ITEM_UPDATE)

    with pytest.raises(PermissionDeniedError) as refused:
        assert_may_attach(caller, CATALOGUE_ENTITY)

    assert refused.value.status_code == 403
    assert refused.value.details["required_permission"] == str(Permission.ITEM_UPDATE)


def test_an_item_editor_and_a_superuser_may_file_against_an_item() -> None:
    assert_may_attach(_caller(Permission.DOCUMENT_UPLOAD, Permission.ITEM_UPDATE), CATALOGUE_ENTITY)
    assert_may_attach(_caller(superuser=True), CATALOGUE_ENTITY)


def test_no_entity_and_an_entity_type_with_no_policy_need_nothing_more() -> None:
    caller = _caller(Permission.DOCUMENT_UPLOAD)
    assert document_attach_permission(None) is None
    assert_may_attach(caller, None)
    assert_may_attach(caller, "qc_inspection")


def test_the_registered_right_is_the_one_enforced(monkeypatch) -> None:
    monkeypatch.setitem(document_access._attach_policies, "test_drawing", Permission.BOM_UPDATE)

    with pytest.raises(PermissionDeniedError):
        assert_may_attach(_caller(Permission.ITEM_UPDATE), "test_drawing")
    assert_may_attach(_caller(Permission.BOM_UPDATE), "test_drawing")
