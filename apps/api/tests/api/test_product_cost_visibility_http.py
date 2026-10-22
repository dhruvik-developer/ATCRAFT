"""Documenting both item schemas must preserve their permission-dependent wire shape."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, get_current_principal
from src.core.permissions import Permission
from src.main import app
from tests.seed_support import seed_item

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.mark.parametrize("surface", ["detail", "offset", "cursor"])
@pytest.mark.parametrize("include_cost", [False, True])
async def test_item_read_cost_fields_follow_the_permission(
    api_client: AsyncClient,
    session: AsyncSession,
    principal: Principal,
    surface: str,
    include_cost: bool,
) -> None:
    item = await seed_item(
        session,
        company_id=principal.company_id,
        code=f"COST-{uuid.uuid4().hex[:8].upper()}",
        standard_cost=Decimal("123.4500"),
    )
    permissions = {str(Permission.ITEM_VIEW)}
    if include_cost:
        permissions.add(str(Permission.ITEM_COST_VIEW))
    reader = replace(principal, is_superuser=False, permissions=frozenset(permissions))
    app.dependency_overrides[get_current_principal] = lambda: reader

    paths = {
        "detail": f"/products/items/{item.id}",
        "offset": "/products/items",
        "cursor": "/products/items/stream",
    }
    response = await api_client.get(paths[surface])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert isinstance(body["message"], str)
    if surface == "detail":
        row = body["data"]
    else:
        row = next(row for row in body["data"] if row["id"] == str(item.id))
        assert "pagination" in body

    assert row["code"] == item.code
    assert ("standard_cost" in row) is include_cost
    if include_cost:
        assert Decimal(row["standard_cost"]) == Decimal("123.4500")
