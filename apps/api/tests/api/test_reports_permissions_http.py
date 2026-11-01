"""Report catalogue visibility and route permissions at the HTTP boundary."""

from __future__ import annotations

import pytest

from src.core.deps import Principal
from src.core.permissions import Permission

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _principal_holding(base: Principal, *permissions: Permission) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(permission) for permission in permissions),
        role_codes=base.role_codes,
    )


async def test_catalogue_hides_cost_report_and_valuation_columns(
    api_client, principal: Principal
) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
        principal, Permission.REPORT_VIEW
    )

    response = await api_client.get("/reports/catalogue")

    assert response.status_code == 200, response.text
    descriptors = {entry["key"]: entry for entry in response.json()["data"]}
    assert "PRODUCT_PROFITABILITY" not in descriptors
    for key in ("STOCK_AVAILABILITY", "DEAD_STOCK"):
        assert key in descriptors
        assert "valuation_amount" not in {column["name"] for column in descriptors[key]["columns"]}


async def test_cost_report_route_refuses_a_report_reader_without_cost_permission(
    api_client, principal: Principal
) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
        principal, Permission.REPORT_VIEW
    )

    response = await api_client.get(
        "/reports/product-profitability",
        params={"date_from": "2026-10-01", "date_to": "2026-10-31"},
    )

    assert response.status_code == 403, response.text
