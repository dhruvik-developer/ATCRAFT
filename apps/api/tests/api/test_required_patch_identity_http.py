"""Explicit null identity fields return field-level 422 without entering a service."""

import uuid

import pytest

from src.modules.customers.service import CustomerService
from src.modules.users.service import RoleService, UserService

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.mark.parametrize(
    "route,field,service,method",
    [
        ("/customers/{id}", "name", CustomerService, "update_customer"),
        ("/customers/{id}/contacts/{contact}", "name", CustomerService, "update_contact"),
        ("/roles/{id}", "name", RoleService, "update_role"),
        ("/users/{id}", "full_name", UserService, "update_user"),
        ("/users/{id}", "email", UserService, "update_user"),
    ],
)
async def test_null_identity_is_422_before_any_write(
    api_client, monkeypatch, route, field, service, method
):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid identity must be refused before the business service runs")

    monkeypatch.setattr(service, method, forbidden)
    response = await api_client.patch(
        route.format(id=uuid.uuid4(), contact=uuid.uuid4()), json={field: None}
    )
    assert response.status_code == 422
    fields = response.json()["error"]["details"]["fields"]
    assert field in fields
    assert "cannot be null" in str(fields[field])
