"""Brand and manufacturer masters over HTTP: who may do what, and to whose rows.

The service tests prove the rules; these prove the boundary - that each route
is gated on the right it declares, that a PATCH cannot smuggle a code change
past the schema, that a retirement refused because something still uses the
value comes back as a 422 carrying the reason the screen shows verbatim, and
that another company's brand is answered "not found" both on its own route and
when it is quoted on an item.

Plus the one regression an HTTP test is uniquely placed to catch: the item list
serialises a row carrying ``brand_id``. The list loads an explicit column set
with ``raiseload``, so a summary field the SELECT does not name is a 500 on the
first row - and a list endpoint is only exercised by a test that lists a row it
actually created.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal
from src.core.permissions import Permission
from src.modules.products.models import Brand, Manufacturer
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


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


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def _create(client: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(path, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ------------------------------------------------------------------ rights --


class TestRights:
    async def test_reading_brands_needs_brand_view(self, api_client, principal: Principal) -> None:
        _as(_holding(principal, Permission.ITEM_VIEW))
        assert (await api_client.get("/products/brands")).status_code == 403

        _as(_holding(principal, Permission.BRAND_VIEW))
        assert (await api_client.get("/products/brands")).status_code == 200

    async def test_reading_manufacturers_needs_manufacturer_view(
        self, api_client, principal: Principal
    ) -> None:
        _as(_holding(principal, Permission.BRAND_VIEW))
        assert (await api_client.get("/products/manufacturers")).status_code == 403

        _as(_holding(principal, Permission.MANUFACTURER_VIEW))
        assert (await api_client.get("/products/manufacturers")).status_code == 200

    @pytest.mark.parametrize(
        ("path", "view"),
        [
            ("/products/brands", Permission.BRAND_VIEW),
            ("/products/manufacturers", Permission.MANUFACTURER_VIEW),
        ],
    )
    async def test_the_view_right_alone_cannot_write(
        self, api_client, principal: Principal, path: str, view: Permission
    ) -> None:
        _as(_holding(principal, view))

        response = await api_client.post(path, json={"code": _code("X"), "name": "Nope"})

        assert response.status_code == 403

    async def test_the_manage_right_creates_and_the_list_returns_the_row(
        self, api_client, principal: Principal
    ) -> None:
        _as(
            _holding(
                principal,
                Permission.BRAND_VIEW,
                Permission.BRAND_MANAGE,
                Permission.MANUFACTURER_VIEW,
                Permission.MANUFACTURER_MANAGE,
            )
        )
        maker = await _create(
            api_client,
            "/products/manufacturers",
            {"code": _code("mk").lower(), "name": "Demo Spring Works", "website": ""},
        )
        brand = await _create(
            api_client,
            "/products/brands",
            {"code": _code("BR"), "name": "Demo Partner", "manufacturer_id": maker["id"]},
        )

        assert maker["code"] == maker["code"].upper(), "the code is normalised at the edge"
        assert maker["website"] is None
        listed = await api_client.get("/products/brands", params={"search": brand["code"]})
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()["data"]] == [brand["id"]]
        one = await api_client.get(f"/products/brands/{brand['id']}")
        assert one.json()["data"]["manufacturer_id"] == maker["id"]


# --------------------------------------------------------------- the shape --


class TestShape:
    async def test_a_patch_cannot_move_the_code(self, api_client, principal: Principal) -> None:
        brand = await _create(
            api_client, "/products/brands", {"code": _code("BR"), "name": "Fixed code"}
        )

        response = await api_client.patch(
            f"/products/brands/{brand['id']}", json={"code": "MOVED", "name": "Renamed"}
        )

        assert response.status_code == 422, response.text
        unchanged = await api_client.get(f"/products/brands/{brand['id']}")
        assert unchanged.json()["data"]["name"] == "Fixed code"

    async def test_an_unknown_sort_column_is_a_422_not_a_500(self, api_client) -> None:
        response = await api_client.get("/products/brands", params={"sort_by": "description"})

        assert response.status_code == 422
        assert "sort_order" in response.json()["error"]["details"]["allowed_sort_fields"]

    async def test_every_whitelisted_sort_column_answers(self, api_client) -> None:
        for column in ("code", "name", "sort_order"):
            response = await api_client.get(
                "/products/brands", params={"sort_by": column, "sort_dir": "desc"}
            )
            assert response.status_code == 200, (column, response.text)
        for column in ("code", "name"):
            response = await api_client.get("/products/manufacturers", params={"sort_by": column})
            assert response.status_code == 200, (column, response.text)

    async def test_retiring_needs_a_reason(self, api_client) -> None:
        brand = await _create(api_client, "/products/brands", {"code": _code("BR"), "name": "B"})

        missing = await api_client.delete(f"/products/brands/{brand['id']}")
        retired = await api_client.delete(
            f"/products/brands/{brand['id']}", params={"reason": "Line withdrawn"}
        )

        assert missing.status_code == 422
        assert retired.status_code == 200, retired.text
        assert (await api_client.get(f"/products/brands/{brand['id']}")).status_code == 404


# ------------------------------------------------------ in use, and scope --


class TestInUseAndScope:
    async def test_retiring_a_brand_an_item_carries_is_refused_with_a_reason(
        self, api_client, session: AsyncSession, company
    ) -> None:
        brand = await _create(api_client, "/products/brands", {"code": _code("BR"), "name": "B"})
        unit = await seed_support.seed_uom(session, company_id=company.id)
        item = await api_client.post(
            "/products/items",
            json={
                "code": _code("FG"),
                "name": "Branded handle",
                "item_group": "FINISHED_GOOD",
                "base_uom_id": str(unit.id),
                "production_type": "MAKE",
                "can_manufacture": True,
                "brand_id": brand["id"],
            },
        )
        assert item.status_code == 201, item.text

        refused = await api_client.delete(
            f"/products/brands/{brand['id']}", params={"reason": "Rebranding"}
        )

        assert refused.status_code == 422
        error = refused.json()["error"]
        assert error["code"] == "REFERENCED_RECORD_IN_USE"
        assert brand["code"] in error["message"]

    async def test_another_company_s_brand_is_not_found_anywhere(
        self, api_client, session: AsyncSession, company, other_company
    ) -> None:
        theirs = Brand(company_id=other_company.id, code=_code("XB"), name="Their brand")
        their_maker = Manufacturer(company_id=other_company.id, code=_code("XM"), name="Theirs")
        session.add_all([theirs, their_maker])
        await session.flush()
        unit = await seed_support.seed_uom(session, company_id=company.id)

        assert (await api_client.get(f"/products/brands/{theirs.id}")).status_code == 404
        assert (
            await api_client.get(f"/products/manufacturers/{their_maker.id}")
        ).status_code == 404
        listed = await api_client.get("/products/brands", params={"limit": 200})
        assert str(theirs.id) not in {row["id"] for row in listed.json()["data"]}

        for field, value in (("brand_id", theirs.id), ("manufacturer_id", their_maker.id)):
            response = await api_client.post(
                "/products/items",
                json={
                    "code": _code("X"),
                    "name": "Not theirs",
                    "item_group": "COMPONENT",
                    "base_uom_id": str(unit.id),
                    "can_purchase": True,
                    field: str(value),
                },
            )
            assert response.status_code == 404, (field, response.text)

    async def test_the_item_list_serialises_a_branded_row_and_filters_by_brand(
        self, api_client, session: AsyncSession, company
    ) -> None:
        brand = await _create(api_client, "/products/brands", {"code": _code("BR"), "name": "B"})
        unit = await seed_support.seed_uom(session, company_id=company.id)
        branded = await seed_support.seed_item(
            session,
            company_id=company.id,
            code=_code("FG"),
            base_uom_id=unit.id,
            brand_id=uuid.UUID(brand["id"]),
            can_sell=True,
        )
        await seed_support.seed_item(
            session, company_id=company.id, code=_code("RM"), base_uom_id=unit.id
        )

        everything = await api_client.get("/products/items", params={"limit": 200})
        assert everything.status_code == 200, everything.text
        rows = {row["id"]: row for row in everything.json()["data"]}
        assert rows[str(branded.id)]["brand_id"] == brand["id"]

        filtered = await api_client.get("/products/items", params={"brand_id": brand["id"]})
        assert [row["id"] for row in filtered.json()["data"]] == [str(branded.id)]

        streamed = await api_client.get("/products/items/stream", params={"limit": 200})
        assert streamed.status_code == 200, streamed.text
