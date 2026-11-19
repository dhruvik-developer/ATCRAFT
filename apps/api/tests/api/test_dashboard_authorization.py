"""Dashboard values obey current permissions and verified warehouse assignments."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.core.clock import utcnow
from src.core.deps import Principal, get_current_principal
from src.core.permissions import Permission
from src.main import app
from src.modules.reports import service
from src.modules.reports.models import InventorySummary
from src.modules.reports.schemas import DashboardRead
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _read_as(principal: Principal, *permissions: Permission, **scope: Any) -> None:
    reader = replace(
        principal,
        permissions=frozenset(str(permission) for permission in permissions),
        **scope,
    )
    app.dependency_overrides[get_current_principal] = lambda: reader


@pytest.fixture
def cached(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Exercise the real cache keys without depending on an external Redis."""
    values: dict[str, Any] = {}

    async def get(key: str) -> Any:
        return deepcopy(values.get(key))

    async def put(key: str, value: Any, *, ttl: int) -> None:
        values[key] = deepcopy(value)

    monkeypatch.setattr(service, "cache_get", get)
    monkeypatch.setattr(service, "cache_set", put)
    return values


@pytest.fixture
async def positions(session, company, other_company, unique_code):
    now = utcnow()
    rows = []
    for owner, amount, refreshed in (
        (company, "100", now - timedelta(hours=1)),
        (company, "300", now),
        (other_company, "9000", now + timedelta(hours=1)),
    ):
        warehouse = await seed_support.seed_warehouse(
            session, company_id=owner.id, code=unique_code("DASH-WH")
        )
        item = await seed_support.seed_item(
            session, company_id=owner.id, code=unique_code("DASH-ITEM")
        )
        row = InventorySummary(
            company_id=owner.id,
            warehouse_id=warehouse.id,
            item_id=item.id,
            valuation_amount=Decimal(amount),
            refreshed_at=refreshed,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def test_dashboard_access_alone_does_not_grant_any_business_tile(
    api_client, principal, positions
) -> None:
    _read_as(principal, Permission.DASHBOARD_VIEW)
    response = await api_client.get("/reports/dashboard")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # Every figure the schema declares, not a list written here: a tile added to
    # the select and to DashboardRead without a permission entry reaches this
    # reader, and must fail this test as the original nine would have.
    protected = set(DashboardRead.model_fields) - {"generated_at"}
    shown = {field: data[field] for field in protected if data[field] is not None}
    assert protected
    assert shown == {}, shown
    assert data["generated_at"]


async def test_inventory_read_does_not_grant_valuation_and_revocation_changes_cache(
    api_client, principal, positions, cached
) -> None:
    _read_as(principal, Permission.DASHBOARD_VIEW, Permission.INVENTORY_VALUATION_VIEW)
    privileged = await api_client.get("/reports/dashboard")
    assert privileged.status_code == 200, privileged.text
    assert Decimal(privileged.json()["data"]["stock_value"]) == Decimal("400")

    _read_as(principal, Permission.DASHBOARD_VIEW, Permission.INVENTORY_VIEW)
    restricted = await api_client.get("/reports/dashboard")
    assert restricted.status_code == 200, restricted.text
    assert restricted.json()["data"]["stock_value"] is None
    assert restricted.json()["data"]["stock_refreshed_at"] is None
    assert len(cached) == 2


async def test_stock_value_and_refresh_time_cache_are_isolated_by_warehouse_and_tenant(
    api_client, principal, positions, cached
) -> None:
    first, second, foreign = positions
    rights = (Permission.DASHBOARD_VIEW, Permission.INVENTORY_VALUATION_VIEW)
    for row in (first, second, first, foreign):
        _read_as(
            principal,
            *rights,
            company_id=row.company_id,
            allowed_warehouse_ids=(row.warehouse_id,),
        )
        response = await api_client.get("/reports/dashboard")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert Decimal(data["stock_value"]) == row.valuation_amount
        assert data["stock_refreshed_at"] == row.refreshed_at.isoformat().replace("+00:00", "Z")

    _read_as(principal, *rights)
    unrestricted = await api_client.get("/reports/dashboard")
    assert Decimal(unrestricted.json()["data"]["stock_value"]) == Decimal("400")
    assert len(cached) == 4


async def test_empty_allowed_stock_position_is_zero_and_superuser_retains_company_access(
    api_client, principal, positions, cached, other_company
) -> None:
    rights = (Permission.DASHBOARD_VIEW, Permission.INVENTORY_VALUATION_VIEW)
    _read_as(principal, *rights, allowed_warehouse_ids=(positions[2].warehouse_id,))
    empty = await api_client.get("/reports/dashboard")
    assert empty.status_code == 200, empty.text
    assert Decimal(empty.json()["data"]["stock_value"]) == 0
    assert empty.json()["data"]["stock_refreshed_at"] is None

    _read_as(principal, is_superuser=True, allowed_warehouse_ids=(positions[0].warehouse_id,))
    superuser = await api_client.get("/reports/dashboard")
    assert superuser.status_code == 200, superuser.text
    assert Decimal(superuser.json()["data"]["stock_value"]) == Decimal("400")


async def test_dashboard_route_still_requires_dashboard_permission(api_client, principal) -> None:
    _read_as(principal, Permission.INVENTORY_VALUATION_VIEW)
    response = await api_client.get("/reports/dashboard")
    assert response.status_code == 403, response.text
    assert response.json()["success"] is False
