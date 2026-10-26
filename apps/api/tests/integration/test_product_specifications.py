"""Specifications are tenant-owned descriptions; accessories never move stock."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.constants import MAX_SPECIFICATION_BATCH
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, DuplicateError, NotFoundError, ValidationError
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom.models import Bom, BomLine
from src.modules.inventory.models import InventoryBalance, InventoryTransaction
from src.modules.organization.models import Company
from src.modules.products.models import (
    AccessoryRelation,
    CategorySpecification,
    Item,
    ItemAccessory,
    ItemCategory,
    ItemSpecification,
    SpecificationAttribute,
    SpecificationValueType,
    UnitOfMeasure,
)
from src.modules.products.repository import ItemRepository
from src.modules.products.schemas import ItemFilters
from src.modules.products.specification_schemas import (
    CategorySpecificationIn,
    CategorySpecificationsReplace,
    ItemAccessoryCreate,
    ItemAccessoryUpdate,
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    SpecificationAttributeCreate,
    SpecificationAttributeUpdate,
)
from src.modules.products.specification_service import (
    SpecificationService,
    accessories_for_items,
    specifications_for_items,
)
from src.modules.users.models import User
from tests.seed_support import seed_category, seed_company, seed_item, seed_uom, seed_user

pytestmark = pytest.mark.integration


@pytest.fixture
def service(session: AsyncSession, tenant: TenantContext) -> SpecificationService:
    return SpecificationService(session, tenant)


async def make_attribute(service: SpecificationService, **changes: object):
    return await service.create_attribute(
        SpecificationAttributeCreate.model_validate(
            {
                "code": f"SP-{uuid.uuid4().hex[:8].upper()}",
                "name": "Fire rated",
                "value_type": "BOOLEAN",
                **changes,
            }
        )
    )


async def make_item(session: AsyncSession, tenant: TenantContext, **changes: Any) -> Item:
    return await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"SP-ITEM-{uuid.uuid4().hex[:8].upper()}",
        **changes,
    )


def values(*entries: tuple[uuid.UUID, dict[str, Any]]) -> ItemSpecificationsReplace:
    return ItemSpecificationsReplace(
        values=[
            ItemSpecificationValueIn(specification_attribute_id=attribute_id, **payload)
            for attribute_id, payload in entries
        ]
    )


def templates(*entries: tuple[uuid.UUID, bool, int]) -> CategorySpecificationsReplace:
    return CategorySpecificationsReplace(
        specifications=[
            CategorySpecificationIn(
                specification_attribute_id=attribute_id, is_required=required, sort_order=sort_order
            )
            for attribute_id, required, sort_order in entries
        ]
    )


@contextmanager
def statements(engine: Any) -> Iterator[list[str]]:
    recorded: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        recorded.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield recorded
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_master_crud_search_sort_pagination_and_audit(service, session, tenant):
    first = await make_attribute(
        service,
        code=" D5-MATERIAL ",
        name="Material",
        value_type="OPTION",
        options=["Brass", "Steel"],
    )
    await make_attribute(service, code="D5-FIRE", name="Fire rated")
    page, pagination = await service.list_attributes(
        PageParams(limit=1), SortParams(sort_by="code")
    )
    assert len(page) == 1
    assert page[0].code == "D5-FIRE"
    assert pagination.total == 2 and pagination.has_next
    found, _ = await service.list_attributes(PageParams(), search="MATERIAL")
    assert [row.id for row in found] == [first.id]
    changed = await service.update_attribute(
        first.id, SpecificationAttributeUpdate(name="Body material")
    )
    assert changed.name == "Body material"
    assert (await service.get_attribute(first.id)).name == "Body material"
    audits = list(
        await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.company_id == tenant.company_id,
                AuditLog.entity_id == first.id,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )
    assert [row.action for row in audits] == [AuditAction.CREATE, AuditAction.UPDATE]
    assert audits[-1].old_values["name"] == "Material"
    assert audits[-1].new_values["name"] == "Body material"


async def test_master_duplicate_and_retired_codes_are_never_reused(service):
    row = await make_attribute(service, code="D5-UNIQUE")
    await service.retire_attribute(row.id, reason="Unused duplicate prevention test")
    with pytest.raises(NotFoundError):
        await service.get_attribute(row.id)
    with pytest.raises(DuplicateError):
        await make_attribute(service, code="d5-unique")
    assert (await service.list_attributes(PageParams()))[0] == []


async def test_category_ancestors_merge_required_flags_and_nearest_order(service, session, tenant):
    root = await seed_category(session, company_id=tenant.company_id)
    child = await seed_category(session, company_id=tenant.company_id, parent=root)
    leaf = await seed_category(session, company_id=tenant.company_id, parent=child)
    sibling = await seed_category(session, company_id=tenant.company_id)
    root_id, child_id, sibling_id = root.id, child.id, sibling.id
    item = await make_item(session, tenant, category_id=leaf.id)
    item_id = item.id
    fire = await make_attribute(service)
    warranty = await make_attribute(service, name="Warranty", value_type="NUMBER", unit="years")
    unrelated = await make_attribute(service, name="Unrelated")
    await service.replace_category_specifications(root_id, templates((fire.id, True, 8)))
    await service.replace_category_specifications(
        child_id, templates((fire.id, False, 1), (warranty.id, True, 2))
    )
    await service.replace_category_specifications(sibling_id, templates((unrelated.id, True, 0)))
    read = await service.get_item_specifications(item_id)
    assert read.missing_required_count == 2
    assert [row.attribute.id for row in read.template] == [fire.id, warranty.id]
    assert read.template[0].is_required is True
    assert read.template[0].source_category_ids == [root_id, child_id]
    assert read.template[0].sort_order == 1
    await service.replace_item_specifications(
        item_id, values((fire.id, {"value_bool": False}), (warranty.id, {"value_number": "0"}))
    )
    complete = await service.get_item_specifications(item_id)
    assert complete.missing_required_count == 0
    assert not any(row.missing_required for row in complete.template)


async def test_item_without_a_category_can_store_values_without_templates(service, session, tenant):
    item = await make_item(session, tenant)
    item_id = item.id
    attribute = await make_attribute(service)
    read = await service.replace_item_specifications(
        item_id, values((attribute.id, {"value_bool": False}))
    )
    assert read.template == [] and read.missing_required_count == 0
    assert read.values[0].value_bool is False


async def test_inactive_items_and_categories_remain_descriptively_editable(
    service, session, tenant
):
    """Deactivation blocks operational use, while staff can still correct descriptions."""
    category_id = (
        await seed_category(
            session,
            company_id=tenant.company_id,
            is_active=False,
        )
    ).id
    item_id = (
        await make_item(
            session,
            tenant,
            category_id=category_id,
            is_active=False,
        )
    ).id
    attribute_id = (await make_attribute(service)).id
    template = await service.replace_category_specifications(
        category_id,
        templates((attribute_id, True, 0)),
    )
    assert template.specifications[0].is_required is True
    saved = await service.replace_item_specifications(
        item_id,
        values((attribute_id, {"value_bool": False})),
    )
    assert saved.values[0].value_bool is False and saved.missing_required_count == 0
    cleared = await service.replace_item_specifications(item_id, values())
    assert cleared.values == [] and cleared.missing_required_count == 1


async def test_value_replacement_clears_removed_values_and_records_both_snapshots(
    service, session, tenant
):
    item = await make_item(session, tenant)
    item_id = item.id
    fire = await make_attribute(service)
    width = await make_attribute(service, value_type="NUMBER", name="Backset", unit="mm")
    await service.replace_item_specifications(
        item_id, values((fire.id, {"value_bool": True}), (width.id, {"value_number": "60.1250"}))
    )
    replaced = await service.replace_item_specifications(
        item_id, values((width.id, {"value_number": "0"}))
    )
    assert len(replaced.values) == 1 and replaced.values[0].value_number == Decimal("0")
    updates = list(
        await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.company_id == tenant.company_id,
                AuditLog.entity_type == "item_specifications",
                AuditLog.entity_id == item_id,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )
    assert len(updates) == 2
    assert len(updates[-1].old_values["values"]) == 2
    assert len(updates[-1].new_values["values"]) == 1
    assert (await service.replace_item_specifications(item_id, values())).values == []


async def test_invalid_replacement_preserves_old_values_and_writes_no_audit(
    service, session, tenant
):
    item_id = (await make_item(session, tenant)).id
    fire = await make_attribute(service)
    material = await make_attribute(service, value_type="OPTION", options=["Brass", "Steel"])
    await service.replace_item_specifications(item_id, values((fire.id, {"value_bool": True})))
    before = await session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.company_id == tenant.company_id)
    )
    with pytest.raises(ValidationError):
        await service.replace_item_specifications(
            item_id,
            values((fire.id, {"value_bool": False}), (material.id, {"value_option": "Wood"})),
        )
    kept = await service.get_item_specifications(item_id)
    assert len(kept.values) == 1 and kept.values[0].value_bool is True
    assert (
        await session.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.company_id == tenant.company_id)
        )
        == before
    )


@pytest.mark.parametrize("target", ["value", "template"])
async def test_inactive_and_retired_attributes_cannot_be_assigned(service, session, tenant, target):
    attribute = await make_attribute(service)
    inactive_id = attribute.id
    await service.update_attribute(inactive_id, SpecificationAttributeUpdate(is_active=False))
    retired_id = (await make_attribute(service)).id
    await service.retire_attribute(retired_id, reason="No longer offered")
    item_id = (await make_item(session, tenant)).id
    category_id = (await seed_category(session, company_id=tenant.company_id)).id
    for attribute_id, error in [(inactive_id, ValidationError), (retired_id, NotFoundError)]:
        with pytest.raises(error):
            if target == "value":
                await service.replace_item_specifications(
                    item_id, values((attribute_id, {"value_bool": True}))
                )
            else:
                await service.replace_category_specifications(
                    category_id, templates((attribute_id, True, 0))
                )


@pytest.mark.parametrize("target", ["value", "template"])
async def test_referenced_attributes_cannot_be_deactivated_or_retired(
    service, session, tenant, target
):
    attribute_id = (await make_attribute(service)).id
    if target == "value":
        item_id = (await make_item(session, tenant)).id
        await service.replace_item_specifications(
            item_id, values((attribute_id, {"value_bool": False}))
        )
    else:
        category_id = (await seed_category(session, company_id=tenant.company_id)).id
        await service.replace_category_specifications(
            category_id, templates((attribute_id, False, 0))
        )
    with pytest.raises(BusinessRuleError):
        await service.retire_attribute(attribute_id, reason="Must retain linked vocabulary")
    with pytest.raises(BusinessRuleError):
        await service.update_attribute(attribute_id, SpecificationAttributeUpdate(is_active=False))
    assert (await service.get_attribute(attribute_id)).is_active


async def test_used_options_and_types_are_protected_but_unused_choices_can_change(
    service, session, tenant
):
    item_id = (await make_item(session, tenant)).id
    attribute_id = (
        await make_attribute(service, value_type="OPTION", options=["Brass", "Steel"])
    ).id
    await service.replace_item_specifications(
        item_id, values((attribute_id, {"value_option": "Brass"}))
    )
    with pytest.raises(BusinessRuleError):
        await service.update_attribute(
            attribute_id, SpecificationAttributeUpdate(options=["Steel"])
        )
    with pytest.raises(BusinessRuleError):
        await service.update_attribute(
            attribute_id,
            SpecificationAttributeUpdate(value_type=SpecificationValueType.TEXT, options=[]),
        )
    changed = await service.update_attribute(
        attribute_id, SpecificationAttributeUpdate(options=["Brass", "Zinc"])
    )
    assert changed.options == ["Brass", "Zinc"]


async def test_foreign_tenant_references_are_indistinguishable_from_missing(
    service, session, tenant, other_company
):
    foreign_id = other_company.id
    foreign_user_id = (await seed_user(session, company_id=foreign_id)).id
    other = SpecificationService(
        session, TenantContext(company_id=foreign_id, user_id=foreign_user_id)
    )
    attribute_id = (await make_attribute(other)).id
    other_item_id = (await seed_item(session, company_id=foreign_id, code="D5-FOREIGN")).id
    other_category_id = (await seed_category(session, company_id=foreign_id)).id
    item_id = (await make_item(session, tenant)).id
    category_id = (await seed_category(session, company_id=tenant.company_id)).id
    for operation in [
        lambda: service.get_attribute(attribute_id),
        lambda: service.get_item_specifications(other_item_id),
        lambda: service.get_category_specifications(other_category_id),
        lambda: service.replace_item_specifications(
            item_id, values((attribute_id, {"value_bool": True}))
        ),
        lambda: service.replace_category_specifications(
            category_id, templates((attribute_id, True, 0))
        ),
        lambda: service.create_accessory(
            item_id, ItemAccessoryCreate(accessory_item_id=other_item_id)
        ),
    ]:
        with pytest.raises(NotFoundError):
            await operation()
    assert (await service.list_attributes(PageParams()))[0] == []


async def test_category_replacement_removes_templates_and_audits_old_state(
    service, session, tenant
):
    category_id = (await seed_category(session, company_id=tenant.company_id)).id
    first = await make_attribute(service)
    second = await make_attribute(service)
    await service.replace_category_specifications(category_id, templates((first.id, True, 0)))
    result = await service.replace_category_specifications(
        category_id, templates((second.id, False, 1))
    )
    assert [row.attribute.id for row in result.specifications] == [second.id]
    assert (
        await session.scalar(
            select(func.count(CategorySpecification.id)).where(
                CategorySpecification.category_id == category_id
            )
        )
        == 1
    )
    assert (
        await service.replace_category_specifications(category_id, templates())
    ).specifications == []


async def test_accessories_allow_cycles_and_included_non_sellable_items_without_stock_side_effects(
    service, session, tenant
):
    parent_id = (await make_item(session, tenant, can_sell=True)).id
    kit_id = (await make_item(session, tenant, can_sell=False)).id
    included = await service.create_accessory(
        parent_id,
        ItemAccessoryCreate(
            accessory_item_id=kit_id, relation=AccessoryRelation.INCLUDED, quantity="2"
        ),
    )
    await service.create_accessory(kit_id, ItemAccessoryCreate(accessory_item_id=parent_id))
    assert included.relation is AccessoryRelation.INCLUDED
    assert included.accessory_can_sell is False
    for model in (InventoryTransaction, InventoryBalance, Bom, BomLine):
        assert (
            await session.scalar(
                select(func.count()).select_from(model).where(model.company_id == tenant.company_id)
            )
            == 0
        )


async def test_accessory_self_non_sellable_optional_and_duplicate_links_are_refused(
    service, session, tenant
):
    parent_id = (await make_item(session, tenant, can_sell=True)).id
    child_id = (await make_item(session, tenant, can_sell=False)).id
    with pytest.raises(ValidationError):
        await service.create_accessory(parent_id, ItemAccessoryCreate(accessory_item_id=parent_id))
    with pytest.raises(ValidationError):
        await service.create_accessory(parent_id, ItemAccessoryCreate(accessory_item_id=child_id))
    await service.create_accessory(
        parent_id,
        ItemAccessoryCreate(accessory_item_id=child_id, relation=AccessoryRelation.INCLUDED),
    )
    with pytest.raises(DuplicateError):
        await service.create_accessory(
            parent_id,
            ItemAccessoryCreate(accessory_item_id=child_id, relation=AccessoryRelation.INCLUDED),
        )


async def test_accessory_patch_reorders_and_clear_notes_then_delete_is_audited(
    service, session, tenant
):
    parent_id = (await make_item(session, tenant)).id
    child_id = (await make_item(session, tenant, can_sell=True)).id
    created = await service.create_accessory(
        parent_id, ItemAccessoryCreate(accessory_item_id=child_id, notes="Original note")
    )
    changed = await service.update_accessory(
        created.id, ItemAccessoryUpdate(quantity="2.5", sort_order=4, notes=None)
    )
    assert changed.quantity == Decimal("2.5") and changed.sort_order == 4
    assert changed.notes is None
    listed, pagination = await service.list_accessories(parent_id, PageParams(limit=1))
    assert [row.id for row in listed] == [created.id] and pagination.total == 1
    await service.delete_accessory(created.id, reason="Association no longer supplied")
    assert (await service.list_accessories(parent_id, PageParams()))[0] == []
    actions = list(
        await session.scalars(
            select(AuditLog.action)
            .where(AuditLog.company_id == tenant.company_id, AuditLog.entity_id == created.id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )
    assert actions == [AuditAction.CREATE, AuditAction.UPDATE, AuditAction.DELETE]


async def test_included_accessory_cannot_be_reclassified_optional_if_not_sellable(
    service, session, tenant
):
    parent_id = (await make_item(session, tenant)).id
    child_id = (await make_item(session, tenant, can_sell=False)).id
    created = await service.create_accessory(
        parent_id,
        ItemAccessoryCreate(accessory_item_id=child_id, relation=AccessoryRelation.INCLUDED),
    )
    with pytest.raises(ValidationError):
        await service.update_accessory(
            created.id, ItemAccessoryUpdate(relation=AccessoryRelation.OPTIONAL)
        )
    assert (await service.list_accessories(parent_id, PageParams()))[0][
        0
    ].relation is AccessoryRelation.INCLUDED


async def test_accessory_patch_validates_refreshed_relation_after_locking(service, session, tenant):
    parent_id = (await make_item(session, tenant)).id
    child_id = (await make_item(session, tenant, can_sell=False)).id
    created = await service.create_accessory(
        parent_id,
        ItemAccessoryCreate(accessory_item_id=child_id, relation=AccessoryRelation.INCLUDED),
    )
    cached = await service.repo.accessory(created.id)
    # Reproduce an association reclassification committed elsewhere while its
    # old ORM identity remains cached. Existing optional targets can later
    # become non-sellable; a subsequent edit must revalidate their live state.
    await session.execute(
        update(ItemAccessory)
        .where(ItemAccessory.id == created.id)
        .values(relation=AccessoryRelation.OPTIONAL)
        .execution_options(synchronize_session=False)
    )
    assert cached.relation == AccessoryRelation.INCLUDED
    before = await session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.company_id == tenant.company_id)
    )
    with pytest.raises(ValidationError, match="OPTIONAL accessory must be sellable"):
        await service.update_accessory(created.id, ItemAccessoryUpdate(quantity="2"))
    read = (await service.list_accessories(parent_id, PageParams()))[0][0]
    assert read.relation is AccessoryRelation.OPTIONAL and read.quantity == 1
    assert (
        await session.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.company_id == tenant.company_id)
        )
        == before
    )


async def test_bulk_helpers_stay_one_query_with_templates_values_and_foreign_ids(
    service, session, tenant, other_company, engine
):
    category_id = (await seed_category(session, company_id=tenant.company_id)).id
    base_uom_id = (await seed_uom(session, company_id=tenant.company_id)).id
    attribute_id = (await make_attribute(service)).id
    await service.replace_category_specifications(category_id, templates((attribute_id, True, 0)))
    item_ids = [
        (await make_item(session, tenant, category_id=category_id, base_uom_id=base_uom_id)).id
        for _ in range(30)
    ]
    accessory_id = (await make_item(session, tenant, can_sell=True)).id
    for item_id in item_ids:
        await service.replace_item_specifications(
            item_id, values((attribute_id, {"value_bool": False}))
        )
        await service.create_accessory(item_id, ItemAccessoryCreate(accessory_item_id=accessory_id))
    foreign_id = (await seed_item(session, company_id=other_company.id, code="D5-BULK-FOREIGN")).id
    await session.flush()
    with statements(engine) as specification_statements:
        result = await specifications_for_items(
            session, tenant, [*item_ids, item_ids[0], foreign_id, uuid.uuid4()]
        )
    assert len(specification_statements) == 1
    assert set(result) == set(item_ids)
    assert all(len(row.values) == 1 and not row.missing_required_count for row in result.values())
    with statements(engine) as accessory_statements:
        accessories = await accessories_for_items(session, tenant, [*item_ids, foreign_id])
    assert len(accessory_statements) == 1
    assert set(accessories) == set(item_ids)
    assert all(
        len(rows) == 1 and rows[0].accessory_item_id == accessory_id
        for rows in accessories.values()
    )
    with statements(engine) as empty_statements:
        assert await specifications_for_items(session, tenant, []) == {}
        assert await accessories_for_items(session, tenant, []) == {}
    assert empty_statements == []


async def test_batch_helpers_refuse_oversized_requests_before_querying(session, tenant, engine):
    item_ids = [uuid.uuid4() for _ in range(MAX_SPECIFICATION_BATCH + 1)]
    with statements(engine) as attempted:
        for helper in (specifications_for_items, accessories_for_items):
            with pytest.raises(ValidationError, match="At most"):
                await helper(session, tenant, item_ids)
    assert attempted == []


async def test_accessory_mutations_and_batch_reads_cannot_reach_another_tenant(
    service, session, tenant, other_company
):
    foreign_company_id = other_company.id
    foreign_user_id = (await seed_user(session, company_id=foreign_company_id)).id
    foreign_tenant = TenantContext(company_id=foreign_company_id, user_id=foreign_user_id)
    foreign = SpecificationService(session, foreign_tenant)
    foreign_parent_id = (await make_item(session, foreign_tenant)).id
    foreign_child_id = (await make_item(session, foreign_tenant, can_sell=True)).id
    link = await foreign.create_accessory(
        foreign_parent_id, ItemAccessoryCreate(accessory_item_id=foreign_child_id)
    )
    with pytest.raises(NotFoundError):
        await service.list_accessories(foreign_parent_id, PageParams())
    with pytest.raises(NotFoundError):
        await service.update_accessory(link.id, ItemAccessoryUpdate(quantity="3"))
    with pytest.raises(NotFoundError):
        await service.delete_accessory(link.id, reason="Cannot delete another tenant's link")
    assert await accessories_for_items(session, tenant, [foreign_parent_id]) == {}
    assert (await foreign.list_accessories(foreign_parent_id, PageParams()))[0][0].quantity == 1


async def test_inactive_accessory_is_reported_and_can_be_removed(service, session, tenant):
    parent_id = (await make_item(session, tenant)).id
    child = await make_item(session, tenant, can_sell=True)
    child_id = child.id
    link = await service.create_accessory(
        parent_id, ItemAccessoryCreate(accessory_item_id=child_id)
    )
    child.is_active = False
    await session.flush()
    read = await accessories_for_items(session, tenant, [parent_id])
    assert read[parent_id][0].accessory_is_active is False
    await service.delete_accessory(link.id, reason="Inactive accessory no longer supplied")
    with pytest.raises(ValidationError, match="inactive"):
        await service.create_accessory(parent_id, ItemAccessoryCreate(accessory_item_id=child_id))


async def test_item_filters_intersect_filterable_values_and_treat_empty_as_no_matches(
    service, session, tenant
):
    material_id = (
        await make_attribute(
            service,
            code="D5-MATERIAL",
            name="Material",
            value_type="OPTION",
            options=["Brass", "Steel"],
            is_filterable=True,
        )
    ).id
    fire_id = (await make_attribute(service, code="D5-FIRE", is_filterable=True)).id
    private_id = (
        await make_attribute(
            service,
            code="D5-PRIVATE",
            value_type="OPTION",
            options=["Brass"],
            is_filterable=False,
        )
    ).id
    backset_id = (
        await make_attribute(
            service,
            code="D5-BACKSET",
            value_type="NUMBER",
            is_filterable=True,
        )
    ).id
    first_id = (await make_item(session, tenant)).id
    second_id = (await make_item(session, tenant)).id
    steel_id = (await make_item(session, tenant)).id
    empty_id = (await make_item(session, tenant)).id
    await service.replace_item_specifications(
        first_id,
        values(
            (material_id, {"value_option": "Brass"}),
            (fire_id, {"value_bool": False}),
            (private_id, {"value_option": "Brass"}),
            (backset_id, {"value_number": "60"}),
        ),
    )
    await service.replace_item_specifications(
        second_id,
        values(
            (material_id, {"value_option": "Brass"}),
            (fire_id, {"value_bool": True}),
        ),
    )
    await service.replace_item_specifications(
        steel_id,
        values(
            (material_id, {"value_option": "Steel"}),
        ),
    )
    await service.create_accessory(
        first_id,
        ItemAccessoryCreate(
            accessory_item_id=empty_id,
            relation=AccessoryRelation.INCLUDED,
        ),
    )
    repo = ItemRepository(session, tenant)
    cases = [
        (["D5-MATERIAL=Brass"], None, {first_id, second_id}),
        (["D5-FIRE=false"], None, {first_id}),
        (["D5-FIRE=true"], None, {second_id}),
        (["D5-MATERIAL=Brass", "D5-FIRE=false"], None, {first_id}),
        (["D5-MATERIAL="], None, set()),
        (["D5-UNKNOWN=Brass"], None, set()),
        (["D5-PRIVATE=Brass"], None, set()),
        (["D5-BACKSET=60"], None, set()),
        ([], True, {first_id}),
        ([], False, {second_id, steel_id, empty_id}),
        (["D5-MATERIAL=Brass"], False, {second_id}),
    ]
    for spec, has_accessories, expected in cases:
        rows, pagination = await repo.search_page(
            ItemFilters(spec=spec, has_accessories=has_accessories),
            PageParams(),
            include_cost=False,
        )
        assert {row.id for row in rows} == expected, (spec, has_accessories)
        assert pagination.total == len(expected)


@pytest.mark.concurrency
async def test_waiting_assignment_rechecks_attribute_after_deactivation_commits(engine):
    """The original active read must not survive a concurrent availability change."""
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as setup:
        company_id = (await seed_company(setup, prefix="D5RACE")).id
        user_id = (await seed_user(setup, company_id=company_id)).id
        tenant = TenantContext(company_id=company_id, user_id=user_id)
        item_id = (await make_item(setup, tenant)).id
        attribute_id = (await make_attribute(SpecificationService(setup, tenant))).id
        await setup.commit()
    assignment: asyncio.Task[Any] | None = None
    try:
        async with maker() as editor, maker() as writer:
            await editor.execute(
                select(SpecificationAttribute)
                .where(SpecificationAttribute.id == attribute_id)
                .with_for_update()
            )
            assignment = asyncio.create_task(
                SpecificationService(writer, tenant).replace_item_specifications(
                    item_id, values((attribute_id, {"value_bool": False}))
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(assignment), timeout=0.2)
            # The actor uses the real service while already holding the row lock.
            await SpecificationService(editor, tenant).update_attribute(
                attribute_id, SpecificationAttributeUpdate(is_active=False)
            )
            await editor.commit()
            with pytest.raises(ValidationError, match="inactive"):
                await asyncio.wait_for(assignment, timeout=10)
        async with maker() as check:
            assert (
                await check.scalar(
                    select(func.count(ItemSpecification.id)).where(
                        ItemSpecification.company_id == company_id
                    )
                )
                == 0
            )
    finally:
        if assignment is not None and not assignment.done():
            assignment.cancel()
            await asyncio.gather(assignment, return_exceptions=True)
        # Every predicate is this test's newly committed tenant; source data and
        # parallel fixtures cannot share its UUID.
        async with maker() as cleanup:
            for model in (
                ItemSpecification,
                CategorySpecification,
                ItemAccessory,
                SpecificationAttribute,
                AuditLog,
                Item,
                ItemCategory,
                UnitOfMeasure,
                User,
            ):
                await cleanup.execute(delete(model).where(model.company_id == company_id))
            await cleanup.execute(delete(Company).where(Company.id == company_id))
            await cleanup.commit()
