"""Cost centres over HTTP: who may do what, and what another tenant cannot reach.

The service rules are covered in ``tests/integration/test_cost_centres_service``.
These assert the boundary: the routes are gated on ``cost_centre.view`` and
``cost_centre.manage`` exactly as declared, a retirement needs its reason in the
query string, another company's centre answers 404 - never 403, which would
confirm it exists - and the two other surfaces this release adds (the
cost-centre statement and the company's bank details) validate at the door.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.organization.models import CostCentreType
from src.modules.organization.schemas import CostCentreCreate
from src.modules.organization.service import CostCentreService

pytestmark = [pytest.mark.api, pytest.mark.integration]

BASE = "/organization/cost-centres"


def _holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person, a superuser no longer, holding exactly these rights."""
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


def _code() -> str:
    return f"CC-{uuid.uuid4().hex[:6].upper()}"


def _body(**overrides: object) -> dict[str, object]:
    return {
        "code": _code(),
        "name": "Buffing and Polishing",
        "cost_centre_type": "PRODUCTION",
        **overrides,
    }


class TestPermissions:
    async def test_viewing_needs_view_and_creating_needs_manage(
        self, api_client, principal: Principal
    ) -> None:
        _as(_holding(principal, Permission.COMPANY_VIEW))
        assert (await api_client.get(BASE)).status_code == 403

        _as(_holding(principal, Permission.COST_CENTRE_VIEW))
        assert (await api_client.get(BASE)).status_code == 200
        refused = await api_client.post(BASE, json=_body())
        assert refused.status_code == 403
        assert refused.json()["error"]["details"]["required_permission"] == "cost_centre.manage"

        _as(_holding(principal, Permission.COST_CENTRE_VIEW, Permission.COST_CENTRE_MANAGE))
        created = await api_client.post(BASE, json=_body())
        assert created.status_code == 201
        assert created.json()["message"] == "Cost centre created"


class TestLifecycleOverHttp:
    async def test_create_list_patch_and_retire(self, api_client) -> None:
        code = _code()
        created = await api_client.post(BASE, json=_body(code=code.lower(), name="  Packing "))
        assert created.status_code == 201
        centre = created.json()["data"]
        assert centre["code"] == code
        assert centre["name"] == "Packing"
        assert centre["cost_centre_type"] == "PRODUCTION"

        listed = await api_client.get(BASE, params={"search": code, "is_active": "true"})
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["data"]] == [centre["id"]]

        patched = await api_client.patch(
            f"{BASE}/{centre['id']}", json={"cost_centre_type": "SERVICE", "is_active": False}
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["is_active"] is False

        no_reason = await api_client.delete(f"{BASE}/{centre['id']}")
        assert no_reason.status_code == 422

        retired = await api_client.delete(
            f"{BASE}/{centre['id']}", params={"reason": "Merged into packing line"}
        )
        assert retired.status_code == 200
        assert (await api_client.get(f"{BASE}/{centre['id']}")).status_code == 404

    async def test_a_code_cannot_be_patched(self, api_client) -> None:
        created = (await api_client.post(BASE, json=_body())).json()["data"]
        refused = await api_client.patch(f"{BASE}/{created['id']}", json={"code": "NEW"})
        assert refused.status_code == 422

    async def test_an_unknown_sort_column_is_refused_not_ignored(self, api_client) -> None:
        response = await api_client.get(BASE, params={"sort_by": "description"})
        assert response.status_code == 422


class TestTenantScope:
    async def test_another_companys_centre_is_not_found_anywhere(
        self, api_client, session: AsyncSession, other_company, user
    ) -> None:
        foreign = await CostCentreService(
            session, TenantContext(company_id=other_company.id, user_id=user.id)
        ).create_cost_centre(
            CostCentreCreate(code=_code(), name="Not ours", cost_centre_type=CostCentreType.SELLING)
        )

        assert (await api_client.get(f"{BASE}/{foreign.id}")).status_code == 404
        patched = await api_client.patch(f"{BASE}/{foreign.id}", json={"name": "Taken"})
        assert patched.status_code == 404
        listed = await api_client.get(BASE, params={"search": foreign.code})
        assert listed.json()["data"] == []

        # Nor can it be named as a parent, or as a department's default.
        as_parent = await api_client.post(BASE, json=_body(parent_id=str(foreign.id)))
        assert as_parent.status_code == 404
        department = await api_client.post(
            "/organization/departments",
            json={"code": _code(), "name": "Sales", "cost_centre_id": str(foreign.id)},
        )
        assert department.status_code == 404


class TestStatementRoute:
    async def test_it_needs_a_period_and_answers_with_the_catalogue_shape(
        self, api_client, principal: Principal
    ) -> None:
        unbounded = await api_client.get("/reports/cost-centre-expense")
        assert unbounded.status_code == 422

        bounded = await api_client.get(
            "/reports/cost-centre-expense",
            params={"date_from": date(2026, 4, 1).isoformat(), "date_to": "2026-06-30"},
        )
        assert bounded.status_code == 200
        assert bounded.json()["data"] == []
        assert bounded.json()["message"].startswith("Cost centre statement")

        _as(_holding(principal, Permission.COST_CENTRE_VIEW))
        gated = await api_client.get(
            "/reports/cost-centre-expense",
            params={"date_from": "2026-04-01", "date_to": "2026-06-30"},
        )
        assert gated.status_code == 403

    async def test_it_is_offered_in_the_catalogue(self, api_client) -> None:
        catalogue = await api_client.get("/reports/catalogue")
        keys = {entry["key"] for entry in catalogue.json()["data"]}
        assert "COST_CENTRE_EXPENSE" in keys


class TestCompanyBankDetails:
    async def test_a_malformed_ifsc_is_a_field_error(self, api_client) -> None:
        response = await api_client.patch(
            "/organization/company", json={"bank_ifsc": "HDFC1001204"}
        )
        assert response.status_code == 422
        fields = response.json()["error"]["details"]["fields"]
        assert any("bank_ifsc" in key for key in fields)

    async def test_bank_details_are_saved_and_read_back(self, api_client) -> None:
        response = await api_client.patch(
            "/organization/company",
            json={
                "bank_name": "State Bank of India",
                "bank_account_name": "Atcraft Hardware Pvt Ltd",
                "bank_account_number": "3021 4455 6677",
                "bank_ifsc": "sbin 0003472",
                "bank_branch": "Rajkot Main",
                "invoice_terms": "Payment within 30 days.\nInterest at 18% thereafter.",
            },
        )
        assert response.status_code == 200
        company = response.json()["data"]
        assert company["bank_ifsc"] == "SBIN0003472"
        assert company["bank_account_number"] == "302144556677"

        read = (await api_client.get("/organization/company")).json()["data"]
        assert read["bank_branch"] == "Rajkot Main"
        assert read["invoice_terms"].endswith("18% thereafter.")

    async def test_editing_them_needs_company_manage(
        self, api_client, principal: Principal
    ) -> None:
        _as(_holding(principal, Permission.COMPANY_VIEW, Permission.COST_CENTRE_MANAGE))
        response = await api_client.patch(
            "/organization/company", json={"bank_name": "Somebody else's bank"}
        )
        assert response.status_code == 403
