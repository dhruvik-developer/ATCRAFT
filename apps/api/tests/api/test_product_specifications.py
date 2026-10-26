"""Descriptive product routes enforce each caller's exact rights and tenant."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.deps import get_current_principal
from src.core.permissions import Permission
from src.main import app
from src.modules.products.models import ItemAccessory, ItemSpecification
from src.modules.products.specification_schemas import SpecificationAttributeCreate
from src.modules.products.specification_service import SpecificationService
from tests.api.dealer_portal_support import (
    dealer_customer,
    dealer_principal,
    sign_in,
)
from tests.seed_support import seed_category, seed_item

pytestmark = [pytest.mark.api, pytest.mark.integration]


def grant(principal, *permissions: Permission) -> None:
    sign_in(
        replace(
            principal,
            is_superuser=False,
            permissions=frozenset(str(permission) for permission in permissions),
        )
    )


@pytest.fixture
async def world(session, tenant):
    item_id = (await seed_item(session, company_id=tenant.company_id, code="D5-HTTP-ITEM")).id
    child_id = (
        await seed_item(session, company_id=tenant.company_id, code="D5-HTTP-CHILD", can_sell=True)
    ).id
    category_id = (await seed_category(session, company_id=tenant.company_id)).id
    service = SpecificationService(session, tenant)
    attribute = await service.create_attribute(
        SpecificationAttributeCreate(
            code="D5-HTTP-FIRE",
            name="Fire rated",
            value_type="BOOLEAN",
            is_filterable=True,
        )
    )
    return {
        "item_id": item_id,
        "child_id": child_id,
        "category_id": category_id,
        "attribute_id": attribute.id,
    }


@pytest.mark.parametrize("permission", [Permission.ITEM_VIEW, Permission.ITEM_UPDATE])
async def test_item_attribute_lookup_needs_only_the_item_right(
    api_client, principal, world, permission
):
    grant(principal, permission)
    response = await api_client.get(f"/products/items/{world['item_id']}/specification-attributes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True and body["pagination"]["total"] == 1
    assert body["data"][0]["id"] == str(world["attribute_id"])


@pytest.mark.parametrize(
    "permission", [Permission.ITEM_ATTRIBUTE_VIEW, Permission.ITEM_ATTRIBUTE_MANAGE]
)
async def test_template_lookups_do_not_require_the_category_view_right(
    api_client, principal, world, permission
):
    grant(principal, permission)
    categories = await api_client.get("/products/specification-categories")
    assert categories.status_code == 200, categories.text
    assert categories.json()["data"][0]["id"] == str(world["category_id"])
    template = await api_client.get(f"/products/categories/{world['category_id']}/specifications")
    assert template.status_code == 200, template.text
    assert template.json()["data"]["specifications"] == []


@pytest.mark.parametrize("surface", ["attributes", "values", "accessories"])
async def test_reads_require_the_literal_view_permission(api_client, principal, world, surface):
    grant(principal, Permission.ITEM_ATTRIBUTE_MANAGE, Permission.ITEM_UPDATE)
    paths = {
        "attributes": "/products/specification-attributes",
        "values": f"/products/items/{world['item_id']}/specifications",
        "accessories": f"/products/items/{world['item_id']}/accessories",
    }
    response = await api_client.get(paths[surface])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("action", ["master", "template", "values", "accessory"])
async def test_writes_refuse_related_but_different_permissions(
    api_client, principal, world, action
):
    rights = [Permission.ITEM_ATTRIBUTE_VIEW, Permission.ITEM_VIEW]
    rights.append(
        Permission.ITEM_UPDATE
        if action in {"master", "template"}
        else Permission.ITEM_ATTRIBUTE_MANAGE
    )
    grant(principal, *rights)
    if action == "master":
        response = await api_client.post(
            "/products/specification-attributes",
            json={
                "code": "D5-CANNOT-CREATE",
                "name": "No authority",
                "value_type": "TEXT",
            },
        )
    elif action == "template":
        response = await api_client.put(
            f"/products/categories/{world['category_id']}/specifications",
            json={"specifications": []},
        )
    elif action == "values":
        response = await api_client.put(
            f"/products/items/{world['item_id']}/specifications", json={"values": []}
        )
    else:
        response = await api_client.post(
            f"/products/items/{world['item_id']}/accessories",
            json={"accessory_item_id": str(world["child_id"])},
        )
    assert response.status_code == 403, response.text
    assert response.json()["success"] is False


async def test_exact_management_rights_create_patch_retire_and_replace(
    api_client, principal, world
):
    grant(principal, Permission.ITEM_ATTRIBUTE_MANAGE)
    created = await api_client.post(
        "/products/specification-attributes",
        json={
            "code": "D5-HTTP-MATERIAL",
            "name": "Material",
            "value_type": "TEXT",
        },
    )
    assert created.status_code == 201, created.text
    attribute_id = created.json()["data"]["id"]
    assert created.json()["success"] is True and created.json()["message"]
    patched = await api_client.patch(
        f"/products/specification-attributes/{attribute_id}", json={"name": "Body material"}
    )
    assert patched.status_code == 200 and patched.json()["data"]["name"] == "Body material"
    retired = await api_client.delete(
        f"/products/specification-attributes/{attribute_id}",
        params={"reason": "Unneeded test attribute"},
    )
    assert retired.status_code == 200 and retired.json()["data"] is None
    template = await api_client.put(
        f"/products/categories/{world['category_id']}/specifications",
        json={
            "specifications": [
                {"specification_attribute_id": str(world["attribute_id"]), "is_required": True}
            ]
        },
    )
    assert template.status_code == 200, template.text
    grant(principal, Permission.ITEM_UPDATE)
    values = await api_client.put(
        f"/products/items/{world['item_id']}/specifications",
        json={
            "values": [
                {"specification_attribute_id": str(world["attribute_id"]), "value_bool": False}
            ]
        },
    )
    assert values.status_code == 200, values.text
    assert values.json()["data"]["values"][0]["value_bool"] is False


async def test_accessory_create_patch_delete_use_item_update_only(api_client, principal, world):
    grant(principal, Permission.ITEM_UPDATE)
    created = await api_client.post(
        f"/products/items/{world['item_id']}/accessories",
        json={"accessory_item_id": str(world["child_id"]), "quantity": "2"},
    )
    assert created.status_code == 201, created.text
    link_id = created.json()["data"]["id"]
    patched = await api_client.patch(
        f"/products/item-accessories/{link_id}",
        json={"quantity": "3", "notes": "Included in the installation pack"},
    )
    assert patched.status_code == 200, patched.text
    assert Decimal(patched.json()["data"]["quantity"]) == Decimal("3")
    grant(principal, Permission.ITEM_VIEW)
    denied = await api_client.delete(
        f"/products/item-accessories/{link_id}", params={"reason": "Remove installation pack"}
    )
    assert denied.status_code == 403
    grant(principal, Permission.ITEM_UPDATE)
    deleted = await api_client.delete(
        f"/products/item-accessories/{link_id}", params={"reason": "Remove installation pack"}
    )
    assert deleted.status_code == 200 and deleted.json()["data"] is None


@pytest.mark.parametrize("kind", ["master", "accessory"])
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_update_and_delete_guards_run_before_resource_lookup(
    api_client, principal, world, kind, method
):
    grant(
        principal, Permission.ITEM_UPDATE if kind == "master" else Permission.ITEM_ATTRIBUTE_MANAGE
    )
    path = (
        f"/products/specification-attributes/{world['attribute_id']}"
        if kind == "master"
        else f"/products/item-accessories/{uuid.uuid4()}"
    )
    response = await api_client.request(
        method.upper(),
        path,
        **(
            {"json": {"name": "Cannot edit"} if kind == "master" else {"quantity": "2"}}
            if method == "patch"
            else {"params": {"reason": "Cannot delete"}}
        ),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": str(uuid.uuid4())},
        {"quantity": "0"},
        {"quantity": "NaN"},
        {"relation": "BOM"},
    ],
)
async def test_invalid_accessory_body_is_422_without_creating_a_link(
    api_client, session, tenant, world, changes
):
    response = await api_client.post(
        f"/products/items/{world['item_id']}/accessories",
        json={"accessory_item_id": str(world["child_id"]), **changes},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (
        await session.scalar(
            select(func.count(ItemAccessory.id)).where(
                ItemAccessory.company_id == tenant.company_id,
            )
        )
        == 0
    )


@pytest.mark.parametrize("reason", [None, "   "])
async def test_retirement_requires_a_meaningful_reason(api_client, world, reason):
    response = await api_client.delete(
        f"/products/specification-attributes/{world['attribute_id']}",
        params={} if reason is None else {"reason": reason},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"company_id": str(uuid.uuid4()), "values": []},
        {"values": [{"specification_attribute_id": "bad-id", "value_bool": True}]},
        {
            "values": [
                {
                    "specification_attribute_id": str(uuid.uuid4()),
                    "value_type": "BOOLEAN",
                    "value_bool": True,
                }
            ]
        },
        {
            "values": [
                {
                    "specification_attribute_id": str(uuid.uuid4()),
                    "value_bool": True,
                    "value_number": "0",
                }
            ]
        },
        {"values": [{"specification_attribute_id": str(uuid.uuid4()), "value_number": "NaN"}]},
    ],
)
async def test_invalid_values_return_the_standard_422_envelope(api_client, world, bad_payload):
    response = await api_client.put(
        f"/products/items/{world['item_id']}/specifications", json=bad_payload
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False and body["error"]["code"] == "VALIDATION_ERROR"
    assert body["request_id"] and body["error"]["details"]
    assert "Traceback" not in response.text and "INSERT INTO" not in response.text


async def test_foreign_reference_is_404_and_does_not_mutate_the_owned_item(
    api_client, session, tenant, world, other_company
):
    foreign_item_id = (
        await seed_item(session, company_id=other_company.id, code="D5-FOREIGN-HTTP", can_sell=True)
    ).id
    response = await api_client.post(
        f"/products/items/{world['item_id']}/accessories",
        json={"accessory_item_id": str(foreign_item_id)},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert (
        await session.scalar(
            select(func.count(ItemAccessory.id)).where(
                ItemAccessory.company_id == tenant.company_id
            )
        )
        == 0
    )
    response = await api_client.put(
        f"/products/items/{foreign_item_id}/specifications", json={"values": []}
    )
    assert response.status_code == 404, response.text
    assert (
        await session.scalar(
            select(func.count(ItemSpecification.id)).where(
                ItemSpecification.company_id == tenant.company_id
            )
        )
        == 0
    )


async def test_routes_require_authentication(api_client):
    app.dependency_overrides.pop(get_current_principal, None)
    response = await api_client.get("/products/specification-attributes")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_dealer_facets_need_only_browse_and_do_not_grant_staff_access(
    api_client, session, tenant, principal, world
):
    customer = await dealer_customer(session, tenant, "D5 browse-only dealer")
    portal = dealer_principal(principal, customer.id)
    sign_in(replace(portal, permissions=frozenset({str(Permission.DEALER_PORTAL_BROWSE)})))
    response = await api_client.get("/dealer-portal/catalog/specifications", params={"limit": 1})
    assert response.status_code == 200, response.text
    assert response.json()["pagination"]["limit"] == 1
    sign_in(replace(portal, permissions=frozenset({str(Permission.DEALER_PORTAL_ACCOUNT)})))
    denied = await api_client.get("/dealer-portal/catalog/specifications")
    assert denied.status_code == 403, denied.text
