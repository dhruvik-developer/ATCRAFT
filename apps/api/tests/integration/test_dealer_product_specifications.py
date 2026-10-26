"""Dealer descriptions stay tenant-safe, browseable, and priced for their channel."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest

from src.core.clock import today
from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.core.pagination import PageParams
from src.modules.customers.models import CustomerType
from src.modules.dealer_portal.service import DealerPortalService
from src.modules.products.models import AccessoryRelation, ItemPrice, PriceType
from src.modules.products.repository import ItemRepository
from src.modules.products.schemas import ItemFilters
from src.modules.products.specification_schemas import (
    ItemAccessoryCreate,
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    SpecificationAttributeCreate,
)
from src.modules.products.specification_service import SpecificationService
from tests.seed_support import seed_customer, seed_item, seed_user

pytestmark = pytest.mark.integration


@pytest.fixture
async def portal(session, tenant):
    customer = await seed_customer(
        session,
        company_id=tenant.company_id,
        code="D5-DEALER",
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.DEALER,
    )
    return DealerPortalService(session, replace(tenant, customer_id=customer.id))


@pytest.fixture
def descriptions(session, tenant):
    return SpecificationService(session, tenant)


async def item(session, tenant, **changes):
    return await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"D5-{uuid.uuid4().hex[:8].upper()}",
        can_sell=True,
        dealer_price=Decimal("100"),
        **changes,
    )


async def attribute(service, **changes):
    return await service.create_attribute(
        SpecificationAttributeCreate.model_validate(
            {
                "code": f"D5-SP-{uuid.uuid4().hex[:8].upper()}",
                "name": "Fire rated",
                "value_type": "BOOLEAN",
                "is_filterable": True,
                **changes,
            }
        )
    )


async def set_value(service, item_id, attribute_id, **value):
    return await service.replace_item_specifications(
        item_id,
        ItemSpecificationsReplace(
            values=[
                ItemSpecificationValueIn(specification_attribute_id=attribute_id, **value),
            ]
        ),
    )


async def catalog(portal, **filters):
    return await portal.catalog_items(
        PageParams(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
        **filters,
    )


async def test_dealer_catalog_filters_are_server_side_and_intersect(
    session, tenant, portal, descriptions
):
    fire = await attribute(descriptions, code="D5-FIRE")
    first_id = (await item(session, tenant)).id
    second_id = (await item(session, tenant)).id
    accessory_id = (await item(session, tenant)).id
    await set_value(descriptions, first_id, fire.id, value_bool=False)
    await set_value(descriptions, second_id, fire.id, value_bool=True)
    await descriptions.create_accessory(
        first_id, ItemAccessoryCreate(accessory_item_id=accessory_id)
    )
    cases = [
        ({"spec": ["D5-FIRE=false"]}, {first_id}),
        ({"spec": ["D5-FIRE=true"]}, {second_id}),
        ({"spec": ["D5-FIRE="]}, set()),
        ({"has_accessories": True}, {first_id}),
        ({"has_accessories": False}, {second_id, accessory_id}),
        ({"spec": ["D5-FIRE=true"], "has_accessories": True}, set()),
    ]
    for filters, expected in cases:
        rows, page = await catalog(portal, **filters)
        assert {row.id for row in rows} == expected, filters
        assert page.total == len(expected)


async def test_dealer_facets_are_paginated_and_only_describe_sellable_live_items(
    session, tenant, portal, descriptions
):
    fire = await attribute(descriptions, name="Fire rated", sort_order=0)
    material = await attribute(
        descriptions, name="Material", value_type="OPTION", options=["Brass", "Steel"], sort_order=1
    )
    private = await attribute(descriptions, name="Internal", is_filterable=False)
    numeric = await attribute(descriptions, name="Width", value_type="NUMBER")
    unsold = await attribute(descriptions, name="For non-sellable item")
    inactive = await attribute(descriptions, name="For inactive item")
    unused = await attribute(descriptions, name="Never used")
    live_id = (await item(session, tenant)).id
    nonsellable_id = (
        await seed_item(
            session, company_id=tenant.company_id, code="D5-NONSELLABLE", can_sell=False
        )
    ).id
    inactive_item = await item(session, tenant)
    inactive_id = inactive_item.id
    await descriptions.replace_item_specifications(
        live_id,
        ItemSpecificationsReplace(
            values=[
                ItemSpecificationValueIn(specification_attribute_id=fire.id, value_bool=False),
                ItemSpecificationValueIn(
                    specification_attribute_id=material.id, value_option="Brass"
                ),
                ItemSpecificationValueIn(specification_attribute_id=private.id, value_bool=True),
                ItemSpecificationValueIn(specification_attribute_id=numeric.id, value_number="0"),
            ]
        ),
    )
    await set_value(descriptions, nonsellable_id, unsold.id, value_bool=True)
    await set_value(descriptions, inactive_id, inactive.id, value_bool=True)
    inactive_item.is_active = False
    await session.flush()
    first, page = await portal.catalogue_specification_filters(PageParams(limit=1))
    assert [row.id for row in first] == [fire.id]
    assert page.total == 2 and page.has_next
    second, _ = await portal.catalogue_specification_filters(PageParams(page=2, limit=1))
    assert [row.id for row in second] == [material.id]
    assert second[0].options == ["Brass", "Steel"]
    assert unused.id not in {row.id for row in first + second}


async def test_dealer_detail_renders_typed_values_without_internal_master_fields(
    session, tenant, portal, descriptions
):
    item_id = (await item(session, tenant, standard_cost=Decimal("999"))).id
    fire = await attribute(descriptions, name="Fire rated")
    backset = await attribute(descriptions, name="Backset", value_type="NUMBER", unit="mm")
    material = await attribute(
        descriptions, name="Material", value_type="OPTION", options=["Brass"]
    )
    care = await attribute(descriptions, name="Care", value_type="TEXT")
    await descriptions.replace_item_specifications(
        item_id,
        ItemSpecificationsReplace(
            values=[
                ItemSpecificationValueIn(specification_attribute_id=fire.id, value_bool=False),
                ItemSpecificationValueIn(specification_attribute_id=backset.id, value_number="0"),
                ItemSpecificationValueIn(
                    specification_attribute_id=material.id, value_option="Brass"
                ),
                ItemSpecificationValueIn(
                    specification_attribute_id=care.id, value_text="Clean with cloth"
                ),
            ]
        ),
    )
    detail = await portal.catalog_item(item_id)
    assert {row.name: (row.value, row.unit) for row in detail.specifications} == {
        "Fire rated": ("No", None),
        "Backset": ("0", "mm"),
        "Material": ("Brass", None),
        "Care": ("Clean with cloth", None),
    }
    wire = detail.model_dump(mode="json")
    assert "standard_cost" not in wire
    assert all(set(row) == {"name", "value", "unit"} for row in wire["specifications"])


async def test_included_non_sellable_accessory_has_no_price_or_order_request(
    session, tenant, portal, descriptions
):
    parent_id = (await item(session, tenant)).id
    kit_id = (
        await seed_item(
            session,
            company_id=tenant.company_id,
            code="D5-SCREW-KIT",
            can_sell=False,
            standard_cost=Decimal("2"),
        )
    ).id
    await descriptions.create_accessory(
        parent_id,
        ItemAccessoryCreate(
            accessory_item_id=kit_id,
            relation=AccessoryRelation.INCLUDED,
            quantity="4",
            notes="Staff-only sourcing evidence",
        ),
    )
    relation = (await portal.catalog_item(parent_id)).accessories[0]
    assert relation.in_the_box and relation.relation == "INCLUDED"
    assert relation.price is None and relation.price_on_request is False
    assert relation.quantity == 4
    assert "notes" not in relation.model_dump() and "standard_cost" not in relation.model_dump()


async def test_optional_accessory_uses_its_own_dealer_quantity_slab(
    session, tenant, portal, descriptions
):
    parent_id = (await item(session, tenant)).id
    accessory_id = (await item(session, tenant)).id
    await descriptions.create_accessory(
        parent_id,
        ItemAccessoryCreate(
            accessory_item_id=accessory_id,
            quantity="10",
        ),
    )
    for quantity, amount in [("0", "90"), ("10", "70")]:
        session.add(
            ItemPrice(
                company_id=tenant.company_id,
                item_id=accessory_id,
                price_type=PriceType.DEALER,
                min_quantity=Decimal(quantity),
                price=Decimal(amount),
                valid_from=today(),
                is_active=True,
            )
        )
    await session.flush()
    relation = (await portal.catalog_item(parent_id)).accessories[0]
    assert relation.price == Decimal("70")
    assert not relation.in_the_box and not relation.price_on_request


async def test_optional_distributor_accessory_without_channel_price_is_on_request(
    session, tenant, descriptions
):
    customer = await seed_customer(
        session,
        company_id=tenant.company_id,
        code="D5-DISTRIBUTOR",
        customer_type=CustomerType.DISTRIBUTOR,
        price_list_type=PriceType.DISTRIBUTOR,
    )
    portal = DealerPortalService(session, replace(tenant, customer_id=customer.id))
    parent_id = (await item(session, tenant)).id
    accessory_id = (await item(session, tenant)).id
    await descriptions.create_accessory(
        parent_id, ItemAccessoryCreate(accessory_item_id=accessory_id)
    )
    relation = (await portal.catalog_item(parent_id)).accessories[0]
    assert relation.price is None and relation.price_on_request is True
    assert relation.in_the_box is False


async def test_has_accessories_matches_the_associations_visible_to_the_dealer(
    session, tenant, portal, descriptions
):
    inactive_optional_parent = (await item(session, tenant)).id
    unsellable_optional_parent = (await item(session, tenant)).id
    included_parent = (await item(session, tenant)).id
    inactive_optional = await item(session, tenant)
    unsellable_optional = await item(session, tenant)
    included = await item(session, tenant)
    await descriptions.create_accessory(
        inactive_optional_parent,
        ItemAccessoryCreate(
            accessory_item_id=inactive_optional.id,
        ),
    )
    await descriptions.create_accessory(
        unsellable_optional_parent,
        ItemAccessoryCreate(
            accessory_item_id=unsellable_optional.id,
        ),
    )
    await descriptions.create_accessory(
        included_parent,
        ItemAccessoryCreate(
            accessory_item_id=included.id,
            relation=AccessoryRelation.INCLUDED,
        ),
    )
    inactive_optional.is_active = False
    unsellable_optional.can_sell = False
    included.is_active = False
    included.can_sell = False
    await session.flush()
    assert (await portal.catalog_item(inactive_optional_parent)).accessories == []
    assert (await portal.catalog_item(unsellable_optional_parent)).accessories == []
    assert (await portal.catalog_item(included_parent)).accessories[0].in_the_box is True
    visible, _ = await catalog(portal, has_accessories=True)
    assert {row.id for row in visible} == {included_parent}
    hidden, _ = await catalog(portal, has_accessories=False)
    assert {row.id for row in hidden} == {inactive_optional_parent, unsellable_optional_parent}
    # Staff can still find and maintain an association whose OPTIONAL target has
    # stopped being orderable; that operational search has a different purpose.
    staff, _ = await ItemRepository(session, tenant).search_page(
        ItemFilters(has_accessories=True),
        PageParams(),
        include_cost=False,
    )
    assert {row.id for row in staff} == {
        inactive_optional_parent,
        unsellable_optional_parent,
        included_parent,
    }


async def test_foreign_items_and_facets_never_enter_a_dealer_catalog(
    session, tenant, portal, descriptions, other_company
):
    foreign_company_id = other_company.id
    foreign_user_id = (await seed_user(session, company_id=foreign_company_id)).id
    foreign_id = (
        await seed_item(
            session, company_id=foreign_company_id, code="D5-FOREIGN-DEALER", can_sell=True
        )
    ).id
    foreign = SpecificationService(
        session,
        TenantContext(
            company_id=foreign_company_id,
            user_id=foreign_user_id,
        ),
    )
    foreign_attribute_id = (await attribute(foreign, code="D5-FOREIGN-FACET")).id
    await set_value(foreign, foreign_id, foreign_attribute_id, value_bool=True)
    with pytest.raises(NotFoundError):
        await portal.catalog_item(foreign_id)
    rows, page = await catalog(portal)
    assert rows == [] and page.total == 0
    facets, page = await portal.catalogue_specification_filters(PageParams())
    assert facets == [] and page.total == 0
