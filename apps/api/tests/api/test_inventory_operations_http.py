"""Bin moves, scrap disposals and internal issues over HTTP: rights, cost and scope.

Decided at the boundary, so tested there:

* each write is refused to a caller without the exact right its route declares
  - viewing stock is not disposing of it, raising an issue is not approving it;
* what a document cost is served only to a caller who may see costs, in the
  lists as well as the detail - and a stock viewer still gets the quantities;
* a principal restricted to one warehouse neither lists nor reads another
  warehouse's documents;
* every list answers 200 *with a row in it* - the trap where a list endpoint
  served an empty table cleanly and 500'd on its first real row.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from src.core.clock import today
from src.core.deps import Principal, get_current_principal
from src.core.permissions import Permission
from src.main import app
from tests.integration.test_inventory_operations import _bin, _department, _scrap, _stock

pytestmark = [pytest.mark.api, pytest.mark.integration]

COST_FIELDS = ("unit_cost", "total_cost")


def _holding(principal: Principal, *permissions: Permission, **overrides: Any) -> Principal:
    return replace(
        principal,
        is_superuser=False,
        permissions=frozenset(str(value) for value in permissions),
        **overrides,
    )


def _act_as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


async def _bin_move(api_client, item, warehouse, rack) -> dict[str, Any]:
    response = await api_client.post(
        "/inventory/bin-moves",
        json={
            "warehouse_id": str(warehouse.id),
            "reason": "Put received goods on the rack",
            "lines": [{"item_id": str(item.id), "quantity": "2", "to_location_id": str(rack.id)}],
        },
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


class TestTheRightsEachWriteDeclares:
    async def test_viewing_stock_does_not_dispose_of_scrap(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_VIEW, Permission.INVENTORY_TRANSFER))
        response = await api_client.post(
            "/inventory/scrap-disposals",
            json={
                "warehouse_id": "019a0000-0000-7000-8000-000000000001",
                "reason": "Dispose of damaged metal",
                "disposal_kind": "WRITE_OFF",
                "lines": [{"item_id": "019a0000-0000-7000-8000-000000000002", "quantity": "1"}],
            },
        )
        assert response.status_code == 403

    async def test_moving_bins_needs_the_transfer_right(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_VIEW, Permission.INVENTORY_ADJUST))
        response = await api_client.post(
            "/inventory/bin-moves",
            json={
                "warehouse_id": "019a0000-0000-7000-8000-000000000001",
                "reason": "Putaway",
                "lines": [
                    {
                        "item_id": "019a0000-0000-7000-8000-000000000002",
                        "quantity": "1",
                        "to_location_id": "019a0000-0000-7000-8000-000000000003",
                    }
                ],
            },
        )
        assert response.status_code == 403

    async def test_raising_an_issue_is_not_approving_one(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_VIEW, Permission.INTERNAL_ISSUE_CREATE))
        for verb in ("approve", "reject"):
            response = await api_client.post(
                f"/inventory/internal-issues/019a0000-0000-7000-8000-000000000001/{verb}",
                json={"reason": "Not needed this month"},
            )
            assert response.status_code == 403, verb

    async def test_approving_issues_is_not_raising_them(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_VIEW, Permission.INTERNAL_ISSUE_APPROVE))
        response = await api_client.post(
            "/inventory/internal-issues",
            json={
                "warehouse_id": "019a0000-0000-7000-8000-000000000001",
                "reason": "Maintenance consumables",
                "department_id": "019a0000-0000-7000-8000-000000000004",
                "lines": [{"item_id": "019a0000-0000-7000-8000-000000000002", "quantity": "1"}],
            },
        )
        assert response.status_code == 403

    async def test_stating_a_write_up_cost_needs_a_cost_right(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_ADJUST))
        response = await api_client.post(
            "/inventory/adjustments",
            json={
                "warehouse_id": "019a0000-0000-7000-8000-000000000001",
                "reason_code": "CORRECTION",
                "reason": "Correct counted stock quantity",
                "lines": [
                    {
                        "item_id": "019a0000-0000-7000-8000-000000000002",
                        "quantity": "1",
                        "unit_cost": "500",
                    }
                ],
            },
        )
        assert response.status_code == 403, response.text

    async def test_a_rejection_must_say_why(self, api_client, principal) -> None:
        _act_as(_holding(principal, Permission.INVENTORY_ADJUST_APPROVE))
        response = await api_client.post(
            "/inventory/adjustments/019a0000-0000-7000-8000-000000000001/reject", json={}
        )
        assert response.status_code == 422


class TestCostIsServedOnlyToWhoMaySeeIt:
    async def test_a_stock_viewer_reads_quantities_and_no_cost(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        created = await _bin_move(api_client, item, warehouse, rack)
        assert Decimal(created["total_value"]) == Decimal("24")
        assert Decimal(created["lines"][0]["unit_cost"]) == Decimal("12")

        _act_as(_holding(principal, Permission.INVENTORY_VIEW))
        detail = await api_client.get(f"/inventory/bin-moves/{created['id']}")
        assert detail.status_code == 200, detail.text
        row = detail.json()["data"]
        assert "total_value" not in row
        assert Decimal(row["lines"][0]["quantity"]) == Decimal("2")
        assert row["lines"][0]["item_code"] == item.code
        for field in COST_FIELDS:
            assert field not in row["lines"][0]

        listed = await api_client.get("/inventory/bin-moves")
        assert listed.status_code == 200, listed.text
        [mine] = [entry for entry in listed.json()["data"] if entry["id"] == created["id"]]
        assert "total_value" not in mine

        _act_as(_holding(principal, Permission.INVENTORY_VIEW, Permission.INVENTORY_VALUATION_VIEW))
        valued = await api_client.get(f"/inventory/bin-moves/{created['id']}")
        assert Decimal(valued.json()["data"]["total_value"]) == Decimal("24")


class TestEveryListListsWhatWasRaised:
    async def test_the_three_documents_round_trip(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        moved = await _bin_move(api_client, item, warehouse, rack)

        await _scrap(session, tenant, item, warehouse, "3")
        disposal = await api_client.post(
            "/inventory/scrap-disposals",
            json={
                "warehouse_id": str(warehouse.id),
                "reason": "Sell the offcuts",
                "disposal_kind": "SALE",
                "buyer_reference": "Shree Metals",
                "realised_amount": "90.00",
                "lines": [{"item_id": str(item.id), "quantity": "3"}],
            },
        )
        assert disposal.status_code == 201, disposal.text
        assert Decimal(disposal.json()["data"]["total_value"]) == Decimal("36")

        department = await _department(session, tenant, unique_code)
        issue = await api_client.post(
            "/inventory/internal-issues",
            json={
                "warehouse_id": str(warehouse.id),
                "reason": "Workshop consumables",
                "department_id": str(department.id),
                "lines": [{"item_id": str(item.id), "quantity": "1"}],
            },
        )
        assert issue.status_code == 201, issue.text
        issue_id = issue.json()["data"]["id"]
        posted = await api_client.post(f"/inventory/internal-issues/{issue_id}/post")
        assert posted.status_code == 200, posted.text
        assert posted.json()["data"]["status"] == "POSTED"

        for path, created_id in (
            ("/inventory/bin-moves", moved["id"]),
            ("/inventory/scrap-disposals", disposal.json()["data"]["id"]),
            ("/inventory/internal-issues", issue_id),
        ):
            listed = await api_client.get(path, params={"sort_by": "created_at"})
            assert listed.status_code == 200, f"{path}: {listed.text}"
            assert created_id in {row["id"] for row in listed.json()["data"]}, path

        for report in ("scrap-register", "consumption-by-department"):
            answer = await api_client.get(
                f"/reports/{report}",
                params={"date_from": str(today()), "date_to": str(today())},
            )
            assert answer.status_code == 200, f"{report}: {answer.text}"
            assert answer.json()["data"], report


class TestWarehouseScope:
    async def test_a_restricted_principal_sees_only_its_own_warehouses_documents(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        created = await _bin_move(api_client, item, warehouse, rack)
        _, elsewhere = await _stock(session, tenant, unique_code)

        _act_as(
            _holding(
                principal,
                Permission.INVENTORY_VIEW,
                allowed_warehouse_ids=(elsewhere.id,),
            )
        )
        listed = await api_client.get("/inventory/bin-moves")
        assert listed.status_code == 200
        assert created["id"] not in {row["id"] for row in listed.json()["data"]}
        detail = await api_client.get(f"/inventory/bin-moves/{created['id']}")
        assert detail.status_code == 404


async def test_the_disposal_kind_is_validated_at_the_boundary(api_client) -> None:
    response = await api_client.post(
        "/inventory/scrap-disposals",
        json={
            "warehouse_id": "019a0000-0000-7000-8000-000000000001",
            "reason": "Write off with money",
            "disposal_kind": "WRITE_OFF",
            "realised_amount": "10",
            "lines": [{"item_id": "019a0000-0000-7000-8000-000000000002", "quantity": "1"}],
        },
    )
    assert response.status_code == 422
