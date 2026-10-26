"""The one-time D5 demo is complete, atomic and preserves operator changes."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.core.database import transaction
from src.core.demo_data import DemoContext
from src.core.demo_data import specifications as demo
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.bom.models import Bom
from src.modules.inventory.models import InventoryBalance, InventoryTransaction, StockSerial
from src.modules.products.models import (
    AccessoryRelation,
    CategorySpecification,
    Item,
    ItemAccessory,
    ItemSpecification,
    SpecificationAttribute,
    SpecificationValueType,
)
from src.modules.products.specification_schemas import (
    CategorySpecificationsReplace,
    ItemAccessoryUpdate,
    ItemSpecificationsReplace,
    SpecificationAttributeCreate,
)
from src.modules.products.specification_service import SpecificationService
from tests.seed_support import seed_category, seed_item, seed_uom

pytestmark = pytest.mark.integration


def _context(session, tenant, principal) -> DemoContext:
    return DemoContext(
        session,
        replace(tenant, is_superuser=True),
        replace(principal, is_superuser=True),
        tenant.company_id,
        tenant.user_id,
    )


@pytest.fixture
async def demo_kit(session, tenant, principal):
    ctx = _context(session, tenant, principal)
    async with transaction(session):
        uom = await seed_uom(session, company_id=tenant.company_id, code="NOS")
        handles = await seed_category(session, company_id=tenant.company_id, code="HANDLE-LEVER")
        locks = await seed_category(session, company_id=tenant.company_id, code="LOCK-MORT")
        brass = await seed_item(
            session,
            company_id=tenant.company_id,
            code="DM-FG-LEVER-AB",
            base_uom_id=uom.id,
            category_id=handles.id,
            can_sell=True,
        )
        stainless = await seed_item(
            session,
            company_id=tenant.company_id,
            code="DM-FG-LEVER-SS",
            base_uom_id=uom.id,
            category_id=handles.id,
            can_sell=True,
        )
        mortise = await seed_item(
            session,
            company_id=tenant.company_id,
            code="DM-FG-MORTISE",
            base_uom_id=uom.id,
            category_id=locks.id,
            can_sell=True,
        )
        digital = await seed_item(
            session,
            company_id=tenant.company_id,
            code="DM-FG-DIGILOCK",
            base_uom_id=uom.id,
            category_id=locks.id,
            can_sell=True,
        )
    return ctx, [brass.id, stainless.id, mortise.id, digital.id], [handles.id, locks.id]


async def _snapshot(ctx):
    models = [
        SpecificationAttribute,
        CategorySpecification,
        ItemSpecification,
        ItemAccessory,
        Item,
        AuditLog,
        InventoryBalance,
        InventoryTransaction,
        StockSerial,
        Bom,
    ]
    fields = ", ".join(
        f"'{model.__tablename__}', "
        "(SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id),'[]'::jsonb) "
        f"FROM {model.__tablename__} r WHERE r.company_id=:company)"
        for model in models
    )
    return await ctx.session.scalar(
        text(f"SELECT jsonb_build_object({fields})"), {"company": ctx.company_id}
    )


async def test_fresh_specification_demo_has_all_types_both_relations_and_identical_rerun(demo_kit):
    ctx, item_ids, _ = demo_kit
    await demo.seed(ctx)
    service = SpecificationService(ctx.session, ctx.tenant)
    attributes, page = await service.list_attributes(PageParams(limit=100))
    assert not page.has_next
    assert {row.name for row in attributes} == {
        "Material",
        "Lock Type",
        "Backset",
        "Keys Included",
        "Warranty",
        "Fire Rated",
        "Mounting",
    }
    assert {row.value_type for row in attributes} == set(SpecificationValueType)
    specifications = await service.get_item_specifications(item_ids[0])
    by_code = {row.attribute.code: row for row in specifications.values}
    assert by_code["DM-SPEC-KEYS"].value_number == Decimal("0")
    assert by_code["DM-SPEC-FIRE-RATED"].value_bool is False
    assert by_code["DM-SPEC-MATERIAL"].value_option == "Brass"
    assert by_code["DM-SPEC-MOUNTING"].value_text
    assert specifications.missing_required_count == 0
    accessories, _ = await service.list_accessories(item_ids[0], PageParams())
    assert {row.relation for row in accessories} == set(AccessoryRelation)
    included = next(row for row in accessories if row.relation is AccessoryRelation.INCLUDED)
    optional = next(row for row in accessories if row.relation is AccessoryRelation.OPTIONAL)
    assert included.accessory_can_sell is False
    assert optional.accessory_can_sell is True
    assert ctx.id_of("item", "DM-ACC-SCREW-KIT") == included.accessory_item_id
    assert ctx.id_of("item", "DM-ACC-ROSE-SS") == optional.accessory_item_id
    before = await _snapshot(ctx)
    assert before["inventory_transactions"] == []
    assert before["boms"] == []
    rerun = _context(ctx.session, ctx.tenant, ctx.principal)
    await demo.seed(rerun)
    assert await _snapshot(ctx) == before
    assert rerun.refs == ctx.refs
    assert rerun.counts[demo.STAGE]["created"] == 0


async def test_rerun_keeps_operator_clear_retirement_accessory_edits_and_removals(demo_kit):
    ctx, item_ids, category_ids = demo_kit
    await demo.seed(ctx)
    service = SpecificationService(ctx.session, ctx.tenant)
    for item_id in item_ids:
        await service.replace_item_specifications(item_id, ItemSpecificationsReplace(values=[]))
    for category_id in category_ids:
        await service.replace_category_specifications(
            category_id, CategorySpecificationsReplace(specifications=[])
        )
    retired_id = ctx.id_of("specification_attribute", "DM-SPEC-KEYS")
    await service.retire_attribute(retired_id, reason="Operator removed the keys specification")
    first, _ = await service.list_accessories(item_ids[0], PageParams())
    retained = first[0].id
    await service.update_accessory(
        retained,
        ItemAccessoryUpdate(quantity="3", sort_order=77, notes="Operator-edited association"),
    )
    await service.delete_accessory(first[1].id, reason="Operator removed the optional association")
    second, _ = await service.list_accessories(item_ids[1], PageParams())
    for relation in second:
        await service.delete_accessory(relation.id, reason="Operator removed the association")
    before = await _snapshot(ctx)
    await demo.seed(_context(ctx.session, ctx.tenant, ctx.principal))
    assert await _snapshot(ctx) == before
    assert (await service.get_item_specifications(item_ids[0])).values == []
    assert (await service.get_category_specifications(category_ids[0])).specifications == []
    remaining, _ = await service.list_accessories(item_ids[0], PageParams())
    assert [(row.id, row.quantity, row.sort_order) for row in remaining] == [
        (retained, Decimal("3"), 77)
    ]


async def test_initial_seed_preserves_existing_changed_types_options_and_retired_master(demo_kit):
    ctx, item_ids, _ = demo_kit
    service = SpecificationService(ctx.session, ctx.tenant)
    material = await service.create_attribute(
        SpecificationAttributeCreate(
            code="DM-SPEC-MATERIAL",
            name="Operator material",
            value_type=SpecificationValueType.OPTION,
            options=["Titanium"],
            sort_order=99,
        )
    )
    keys = await service.create_attribute(
        SpecificationAttributeCreate(
            code="DM-SPEC-KEYS", name="Operator key notes", value_type=SpecificationValueType.TEXT
        )
    )
    fire = await service.create_attribute(
        SpecificationAttributeCreate(
            code="DM-SPEC-FIRE-RATED",
            name="Retired fire rating",
            value_type=SpecificationValueType.BOOLEAN,
        )
    )
    await service.retire_attribute(
        fire.id, reason="Operator retired this before demonstration setup"
    )
    await demo.seed(ctx)
    assert await service.get_attribute(material.id) == material
    assert await service.get_attribute(keys.id) == keys
    values = await service.get_item_specifications(item_ids[0])
    assert {material.id, keys.id, fire.id}.isdisjoint(
        value.specification_attribute_id for value in values.values
    )
    assert values.missing_required_count == 1
    assert ctx.id_of("specification_attribute", "DM-SPEC-FIRE-RATED") == fire.id
    before = await _snapshot(ctx)
    await demo.seed(_context(ctx.session, ctx.tenant, ctx.principal))
    assert await _snapshot(ctx) == before


async def test_seed_failure_rolls_back_every_new_description_and_marker(demo_kit, monkeypatch):
    ctx, _, _ = demo_kit
    before = await _snapshot(ctx)

    async def fail_late(*_args, **_kwargs):
        raise RuntimeError("Simulated accessory failure after earlier specification writes")

    with monkeypatch.context() as scoped:
        scoped.setattr(SpecificationService, "create_accessory", fail_late)
        with pytest.raises(RuntimeError, match="Simulated accessory failure"):
            await demo.seed(ctx)
    assert await _snapshot(ctx) == before
    await demo.seed(_context(ctx.session, ctx.tenant, ctx.principal))
    assert (await _snapshot(ctx))["item_specifications"]
