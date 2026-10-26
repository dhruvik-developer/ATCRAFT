"""Every new portal and promotion action authorizes before reading business data."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from src.core.deps import get_current_principal
from src.main import app

pytestmark = [pytest.mark.api, pytest.mark.integration]

PORTAL_ROUTES = [
    ("GET", "/invoices"),
    ("GET", "/invoices/{id}"),
    ("POST", "/invoices/{id}/pdf"),
    ("GET", "/credit-notes"),
    ("GET", "/credit-notes/{id}"),
    ("POST", "/credit-notes/{id}/pdf"),
    ("GET", "/returns"),
    ("GET", "/returns/{id}"),
    ("GET", "/returns/eligible-dispatches"),
    ("POST", "/returns"),
    ("GET", "/jobs/{id}"),
    ("GET", "/documents/{id}/download"),
    ("GET", "/banners"),
    ("GET", "/offers"),
    ("GET", "/images/{id}"),
]


@pytest.mark.parametrize("method,path", PORTAL_ROUTES)
async def test_staff_cannot_impersonate_a_dealer_even_with_all_permissions(
    api_client, method, path
):
    response = await api_client.request(
        method, "/dealer-portal" + path.replace("{id}", str(uuid.uuid4())), json={}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SCOPE_DENIED"


@pytest.mark.parametrize("method,path", PORTAL_ROUTES)
async def test_dealer_without_the_endpoint_permission_is_refused(
    api_client, principal, method, path
):
    denied = replace(
        principal, is_superuser=False, permissions=frozenset(), customer_id=uuid.uuid4()
    )
    app.dependency_overrides[get_current_principal] = lambda: denied
    response = await api_client.request(
        method, "/dealer-portal" + path.replace("{id}", str(uuid.uuid4())), json={}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("resource", ["banners", "offers"])
@pytest.mark.parametrize("action", ["activate", "deactivate", "retire"])
@pytest.mark.parametrize("as_dealer", [False, True])
async def test_reasoned_promotion_actions_require_manage_and_exclude_dealers(
    api_client, principal, resource, action, as_dealer
):
    context = (
        replace(principal, customer_id=uuid.uuid4())
        if as_dealer
        else replace(principal, is_superuser=False, permissions=frozenset())
    )
    app.dependency_overrides[get_current_principal] = lambda: context
    response = await api_client.post(
        f"/promotions/{resource}/{uuid.uuid4()}/{action}", json={"reason": "Campaign review"}
    )
    assert response.status_code == 403, response.text
