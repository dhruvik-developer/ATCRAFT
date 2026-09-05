"""Stock figures and money figures are separate permissions, over HTTP.

``inventory.view`` answers "how much is there". ``item.cost_view`` and
``inventory.valuation_view`` answer "what is it worth", and they exist as
separate rights because a store keeper, a QC inspector and a dispatch clerk all
need the first and are deliberately not given the second.

A security review found the balance endpoints returning ``average_cost``,
``stock_value`` and ``total_value`` to every caller holding ``inventory.view``
alone - so a company's entire stock valuation, and the unit cost of every part,
were readable by roles the permission model was written to keep them from.

Tested at the HTTP boundary, because the boundary is where the decision is made
and where it went wrong: the service is *supposed* to return the widest model,
and the route narrows it. A service-level test would have passed throughout.

The related trap is worth stating: these routes declare ``response_model=None``.
FastAPI serialises a handler's return value against the declared response model,
so a fixed one re-widens the payload to the union and puts the withheld field
straight back. ``products/router.py`` documents the same convention for
``standard_cost``, and following it was the difference between this working and
silently not.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal
from src.core.permissions import Permission

pytestmark = [pytest.mark.api, pytest.mark.integration]

COST_FIELDS = ("average_cost", "stock_value")


def _principal_holding(base: Principal, *permissions: Permission) -> Principal:
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


async def _balance_row(client) -> dict:
    response = await client.get("/inventory/balances", params={"limit": 1})
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    return rows[0] if rows else {}


class TestBalanceCostVisibility:
    async def test_a_caller_with_only_inventory_view_gets_no_cost_at_all(
        self, api_client, session: AsyncSession, principal: Principal, seeded_balance
    ) -> None:
        from src.core.deps import get_current_principal
        from src.main import app

        app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
            principal, Permission.INVENTORY_VIEW
        )
        row = await _balance_row(api_client)

        assert row, "the fixture produced no balance to read"
        for field in COST_FIELDS:
            assert field not in row, (
                f"{field} reached a caller holding inventory.view alone - "
                "the whole point of item.cost_view is that they do not see it"
            )
        # The quantities are what they came for and must survive the narrowing.
        assert "on_hand_quantity" in row
        assert "available_quantity" in row

    async def test_a_caller_holding_item_cost_view_still_gets_it(
        self, api_client, session: AsyncSession, principal: Principal, seeded_balance
    ) -> None:
        """The guard must not withhold the figure from whoever is entitled to it.

        Half of this defect class is over-exposure; the other half is a fix that
        blanks the field for everybody and is noticed weeks later by the one
        person who needed it.
        """
        from src.core.deps import get_current_principal
        from src.main import app

        app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
            principal, Permission.INVENTORY_VIEW, Permission.ITEM_COST_VIEW
        )
        row = await _balance_row(api_client)

        assert row
        for field in COST_FIELDS:
            assert field in row, f"{field} was withheld from a holder of item.cost_view"

    async def test_the_item_summary_gates_its_total_on_the_valuation_right(
        self, api_client, session: AsyncSession, principal: Principal, seeded_balance
    ) -> None:
        """A whole-item total is what ``inventory.valuation_view`` exists for.

        The per-item cost right is not enough here and is not consulted: a
        company-wide valuation is a different disclosure from one part's unit
        cost, which is why the two permissions are separate.
        """
        from src.core.deps import get_current_principal
        from src.main import app

        item_id = seeded_balance

        app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
            principal, Permission.INVENTORY_VIEW
        )
        response = await api_client.get(f"/inventory/items/{item_id}/summary")
        assert response.status_code == 200, response.text
        without = response.json()["data"][0]
        assert "total_value" not in without
        assert "total_on_hand" in without
        assert all("stock_value" not in w for w in without.get("warehouses", []))

        app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
            principal, Permission.INVENTORY_VIEW, Permission.INVENTORY_VALUATION_VIEW
        )
        response = await api_client.get(f"/inventory/items/{item_id}/summary")
        assert response.status_code == 200, response.text
        with_value = response.json()["data"][0]
        assert "total_value" in with_value


@pytest.fixture
async def seeded_balance(session: AsyncSession, principal: Principal) -> uuid.UUID:
    """One balance row with a real cost on it, built through the ORM.

    Through the models rather than a hand-written INSERT: many NOT NULL columns
    on these tables default in Python and not in the database, and a column list
    written by hand goes stale the moment a module adds one - which CLAUDE.md
    records as having broken 43 tests twice.
    """
    from src.modules.inventory.models import InventoryBalance
    from src.modules.products.models import Item, ItemGroup, UnitOfMeasure, UomCategory
    from src.modules.warehouses.models import Warehouse, WarehouseType

    uom = UnitOfMeasure(
        company_id=principal.company_id,
        code=f"NOS-{uuid.uuid4().hex[:4]}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
        decimal_places=0,
        is_base=True,
    )
    session.add(uom)
    await session.flush()

    item = Item(
        company_id=principal.company_id,
        code=f"COST-{uuid.uuid4().hex[:6].upper()}",
        name="Costed part",
        item_group=ItemGroup.COMPONENT,
        base_uom_id=uom.id,
    )
    warehouse = Warehouse(
        company_id=principal.company_id,
        code=f"WH-{uuid.uuid4().hex[:4].upper()}",
        name="Store",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    session.add_all([item, warehouse])
    await session.flush()

    session.add(
        InventoryBalance(
            company_id=principal.company_id,
            item_id=item.id,
            warehouse_id=warehouse.id,
            on_hand_quantity=Decimal("200.0000"),
            # `available_quantity` is a GENERATED column - Postgres computes it
            # from the pools, and inserting a value is refused outright. That is
            # the right design: availability can never drift from its parts.
            average_cost=Decimal("380.000000"),
            stock_value=Decimal("76000.0000"),
        )
    )
    await session.flush()
    return item.id
