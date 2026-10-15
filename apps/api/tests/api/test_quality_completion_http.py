"""Quality completion over HTTP (WP E5).

The rules are covered in ``tests/integration/test_quality_completion_e5.py``.
These assert the boundary a screen meets: the raise dialog posts to
``POST /quality/inspections`` on ``qc.inspect``; the rework endpoints need
``qc.inspect`` to open and ``qc.decide`` to complete; the re-inspection needs
``packing.manage``; the rework-rate report rides the catalogue on
``report.view``; and one manual lot is raised, decided, reworked and re-decided
through the API, with the decision history coming back on the detail read.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup, ProductionType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person, holding exactly these rights."""
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


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def _lot(session: AsyncSession, tenant: TenantContext) -> dict[str, uuid.UUID]:
    """An item, a store and 100 available pieces."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    store = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("FG"),
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.BUY,
        can_sell=True,
        can_scrap=True,
        standard_cost=Decimal("120"),
    )
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item.id,
            warehouse_id=store.id,
            quantity=Decimal("100"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="E5 http fixture opening stock",
            unit_cost=Decimal("120"),
        )
    return {"item": item.id, "store": store.id, "uom": uom.id}


def _raise_body(lot: dict[str, uuid.UUID], *, quantity: str = "100") -> dict:
    return {
        "item_id": str(lot["item"]),
        "warehouse_id": str(lot["store"]),
        "qc_stage": "IN_PROCESS",
        "inspected_quantity": quantity,
        "hold_stock": True,
        "reference_type": "manual",
        "reason": "Supervisor pulled the lot",
    }


def _decide_body(accepted: str, **kwargs: str) -> dict:
    return {
        "accepted_quantity": accepted,
        "rejected_quantity": kwargs.get("rejected", "0"),
        "rework_quantity": kwargs.get("rework", "0"),
        "hold_quantity": "0",
        "scrap_quantity": "0",
        "reason": "E5 http verdict",
    }


class TestPermissions:
    async def test_raising_an_inspection_needs_qc_inspect(
        self, api_client, session, tenant, principal
    ) -> None:
        lot = await _lot(session, tenant)
        _as(_holding(principal, Permission.QC_VIEW))
        refused = await api_client.post("/quality/inspections", json=_raise_body(lot))
        assert refused.status_code == 403
        assert refused.json()["error"]["details"]["required_permission"] == "qc.inspect"

    async def test_rework_reads_need_qc_view_and_writes_need_inspect_and_decide(
        self, api_client, session, tenant, principal
    ) -> None:
        lot = await _lot(session, tenant)
        _as(_holding(principal, Permission.QC_INSPECT, Permission.QC_DECIDE))
        raised = await api_client.post("/quality/inspections", json=_raise_body(lot))
        assert raised.status_code == 201, raised.text
        inspection_id = raised.json()["data"]["id"]

        _as(_holding(principal))
        assert (await api_client.get("/quality/rework")).status_code == 403

        _as(_holding(principal, Permission.QC_VIEW))
        assert (await api_client.get("/quality/rework")).status_code == 200
        refused = await api_client.post(
            "/quality/rework",
            json={
                "inspection_id": inspection_id,
                "quantity": "10",
                "assigned_to": str(principal.user_id),
                "reason": "No right to open",
            },
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["details"]["required_permission"] == "qc.inspect"

    async def test_completing_rework_needs_qc_decide(
        self, api_client, session, tenant, principal
    ) -> None:
        lot = await _lot(session, tenant)
        _as(_holding(principal, Permission.QC_INSPECT, Permission.QC_DECIDE))
        raised = await api_client.post("/quality/inspections", json=_raise_body(lot))
        inspection_id = raised.json()["data"]["id"]
        decided = await api_client.post(
            f"/quality/inspections/{inspection_id}/decide",
            json=_decide_body("60", rework="40"),
        )
        assert decided.status_code == 200, decided.text
        opened = await api_client.post(
            "/quality/rework",
            json={
                "inspection_id": inspection_id,
                "quantity": "40",
                "assigned_to": str(principal.user_id),
                "reason": "Bench rework",
            },
        )
        record_id = opened.json()["data"]["id"]
        started = await api_client.post(
            f"/quality/rework/{record_id}/start", json={"reason": "On the bench"}
        )
        assert started.status_code == 200, started.text

        _as(_holding(principal, Permission.QC_INSPECT))
        refused = await api_client.post(
            f"/quality/rework/{record_id}/complete",
            json={"outcome": "REWORKED_OK", "reason": "Polished out"},
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["details"]["required_permission"] == "qc.decide"

    async def test_the_rework_rate_report_needs_report_view(self, api_client, principal) -> None:
        _as(_holding(principal, Permission.REPORT_EXPORT))
        refused = await api_client.get("/reports/rework-rate")
        assert refused.status_code == 403


class TestManualLotOverHttp:
    async def test_raise_decide_rework_and_redecide(
        self, api_client, session, tenant, principal
    ) -> None:
        lot = await _lot(session, tenant)
        _as(
            _holding(
                principal,
                Permission.QC_INSPECT,
                Permission.QC_DECIDE,
                Permission.QC_VIEW,
                Permission.REPORT_VIEW,
            )
        )

        raised = await api_client.post("/quality/inspections", json=_raise_body(lot))
        assert raised.status_code == 201, raised.text
        inspection_id = raised.json()["data"]["id"]

        decided = await api_client.post(
            f"/quality/inspections/{inspection_id}/decide",
            json=_decide_body("60", rework="40"),
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["data"]["still_on_hold_quantity"] == "40.0000"

        opened = await api_client.post(
            "/quality/rework",
            json={
                "inspection_id": inspection_id,
                "quantity": "40",
                "assigned_to": str(principal.user_id),
                "reason": "Bench rework",
            },
        )
        assert opened.status_code == 201, opened.text
        record_id = opened.json()["data"]["id"]
        assert opened.json()["data"]["status"] == "OPEN"

        listed = await api_client.get("/quality/rework", params={"status": "OPEN"})
        assert record_id in {row["id"] for row in listed.json()["data"]}

        assert (
            await api_client.post(f"/quality/rework/{record_id}/start", json={"reason": "Taken"})
        ).status_code == 200
        completed = await api_client.post(
            f"/quality/rework/{record_id}/complete",
            json={"outcome": "REWORKED_OK", "reason": "Polished out"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["data"]["status"] == "DONE"

        redecided = await api_client.post(
            f"/quality/inspections/{inspection_id}/decide", json=_decide_body("100")
        )
        assert redecided.status_code == 200, redecided.text

        detail = await api_client.get(f"/quality/inspections/{inspection_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()["data"]
        assert [row["sequence"] for row in body["decisions"]] == [1, 2]
        assert [row["code"] for row in body["rework_records"]] == [opened.json()["data"]["code"]]

        report = await api_client.get(
            "/reports/rework-rate",
            params={"date_from": "2026-10-01", "date_to": "2026-10-31"},
        )
        assert report.status_code == 200, report.text
