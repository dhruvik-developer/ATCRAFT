"""The company-wide production surfaces over HTTP: who may read them.

The rules are covered in ``tests/integration/test_production_service.py``.
These assert the boundary:

* ``GET /production/entries`` answers the whole company's bookings with
  ``production_order.view`` and 403s without it - and the literal ``entries``
  segment is not swallowed by ``/{order_id}``, which would 422 on the UUID
  parse instead of answering 200;
* ``GET /production/capacity`` answers the board with the same right and
  refuses an unbounded window at the door.
"""

from __future__ import annotations

import pytest

from src.core.deps import Principal
from src.core.permissions import Permission

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(base: Principal, *permissions: Permission) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


async def test_company_wide_entries_need_the_order_view(api_client, principal) -> None:
    _as(_holding(principal, Permission.WORK_ORDER_VIEW))
    assert (await api_client.get("/production/entries")).status_code == 403

    _as(_holding(principal, Permission.PRODUCTION_ORDER_VIEW))
    response = await api_client.get("/production/entries")
    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_capacity_needs_the_order_view_and_a_bounded_window(api_client, principal) -> None:
    _as(_holding(principal, Permission.WORK_ORDER_VIEW))
    assert (await api_client.get("/production/capacity")).status_code == 403

    _as(_holding(principal, Permission.PRODUCTION_ORDER_VIEW))
    board = await api_client.get("/production/capacity")
    assert board.status_code == 200
    assert board.json()["pagination"]["page"] == 1

    inverted = await api_client.get(
        "/production/capacity",
        params={"date_from": "2026-10-16", "date_to": "2026-10-01"},
    )
    assert inverted.status_code == 422
