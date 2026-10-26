"""Populate D5 once, preserving later edits, removals, retirements and ordering."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from src.core.database import advisory_xact_lock, transaction
from src.core.demo_data import DemoContext, find_by_code
from src.core.pagination import PageParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.audit.service import record_audit
from src.modules.products.models import (
    AccessoryRelation,
    Item,
    ItemCategory,
    ItemGroup,
    SpecificationAttribute,
    SpecificationValueType,
    UnitOfMeasure,
)
from src.modules.products.schemas import ItemCreate
from src.modules.products.service import create_item
from src.modules.products.specification_schemas import (
    CategorySpecificationIn,
    CategorySpecificationsReplace,
    ItemAccessoryCreate,
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    SpecificationAttributeCreate,
)
from src.modules.products.specification_service import SpecificationService

STAGE = "specifications"
MARKER = "demo_specification_seed"


def attribute_definitions(ctx: DemoContext) -> list[SpecificationAttributeCreate]:
    return [
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "MATERIAL"),
            name="Material",
            value_type=SpecificationValueType.OPTION,
            options=["Brass", "Stainless Steel 304", "Zinc Alloy", "Aluminium"],
            is_filterable=True,
            sort_order=10,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "LOCK-TYPE"),
            name="Lock Type",
            value_type=SpecificationValueType.OPTION,
            options=["Mortise", "Cylindrical", "Rim"],
            is_filterable=True,
            sort_order=20,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "BACKSET"),
            name="Backset",
            value_type=SpecificationValueType.NUMBER,
            unit="mm",
            sort_order=30,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "KEYS"),
            name="Keys Included",
            value_type=SpecificationValueType.NUMBER,
            sort_order=40,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "WARRANTY"),
            name="Warranty",
            value_type=SpecificationValueType.NUMBER,
            unit="years",
            sort_order=50,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "FIRE-RATED"),
            name="Fire Rated",
            value_type=SpecificationValueType.BOOLEAN,
            is_filterable=True,
            sort_order=60,
        ),
        SpecificationAttributeCreate(
            code=ctx.code("SPEC", "MOUNTING"),
            name="Mounting",
            value_type=SpecificationValueType.TEXT,
            sort_order=70,
        ),
    ]


async def _has_history(ctx: DemoContext, entity_type: str, entity_id: uuid.UUID) -> bool:
    return (
        await ctx.session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.company_id == ctx.company_id,
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == entity_id,
            )
            .limit(1)
        )
        is not None
    )


async def _accessory_item(ctx: DemoContext, *, included: bool, uom_id: uuid.UUID) -> Item:
    code = ctx.code("ACC", "SCREW-KIT" if included else "ROSE-SS")
    existing = await find_by_code(ctx.session, Item, ctx.company_id, code)
    if existing is not None:
        ctx.remember("item", code, existing.id)
        return existing
    row = await create_item(
        ctx.session,
        ctx.tenant,
        ItemCreate(
            code=code,
            name="Included handle fixing screw kit"
            if included
            else "Matching stainless steel escutcheon rose",
            base_uom_id=uom_id,
            item_group=ItemGroup.SPARE,
            can_purchase=True,
            can_manufacture=False,
            can_sell=not included,
            sale_price=Decimal("0") if included else Decimal("125"),
            dealer_price=Decimal("0") if included else Decimal("100"),
            description="Informational accessory association; "
            "it does not reserve or consume inventory.",
        ),
    )
    ctx.note(STAGE, created=1)
    ctx.remember("item", code, row.id)
    return row


async def seed(ctx: DemoContext) -> None:
    definitions = attribute_definitions(ctx)
    async with transaction(ctx.session):
        await advisory_xact_lock(ctx.session, MARKER, ctx.company_id)
        found = list(
            (
                await ctx.session.scalars(
                    select(SpecificationAttribute).where(
                        SpecificationAttribute.company_id == ctx.company_id,
                        SpecificationAttribute.code.in_([row.code for row in definitions]),
                    )
                )
            ).all()
        )
        by_code = {row.code: row for row in found}
        for row in found:
            ctx.remember("specification_attribute", row.code, row.id)
        if await _has_history(ctx, MARKER, ctx.company_id):
            accessories = (
                await ctx.session.scalars(
                    select(Item).where(
                        Item.company_id == ctx.company_id,
                        Item.code.in_([ctx.code("ACC", "SCREW-KIT"), ctx.code("ACC", "ROSE-SS")]),
                    )
                )
            ).all()
            for item in accessories:
                ctx.remember("item", item.code, item.id)
            ctx.note(STAGE, existing=len(found))
            return
        service = SpecificationService(ctx.session, ctx.tenant)
        # Service writes are deliberately sequential seed operations. Interactive
        # item/dealer reads use the shared one-query batch helpers instead.
        for definition in definitions:
            if definition.code in by_code:
                ctx.note(STAGE, existing=1)
                continue
            read = await service.create_attribute(definition)
            by_code[definition.code] = await service.attributes.get_or_404(read.id)
            ctx.remember("specification_attribute", read.code, read.id)
            ctx.note(STAGE, created=1)
        expected_types = {definition.code: definition.value_type for definition in definitions}
        usable = {
            code.removeprefix(ctx.code("SPEC") + "-"): row
            for code, row in by_code.items()
            if row.is_active and row.deleted_at is None and row.value_type == expected_types[code]
        }
        categories = list(
            (
                await ctx.session.scalars(
                    select(ItemCategory).where(
                        ItemCategory.company_id == ctx.company_id,
                        ItemCategory.code.in_(["HANDLE-LEVER", "LOCK-MORT"]),
                        ItemCategory.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        for category in categories:
            if await _has_history(ctx, "category_specifications", category.id):
                continue
            current = await service.get_category_specifications(category.id)
            if current.specifications:
                continue
            names = ["MATERIAL", "KEYS", "WARRANTY", "FIRE-RATED", "MOUNTING"]
            if category.code == "LOCK-MORT":
                names += ["LOCK-TYPE", "BACKSET"]
            await service.replace_category_specifications(
                category.id,
                CategorySpecificationsReplace(
                    specifications=[
                        CategorySpecificationIn(
                            specification_attribute_id=usable[name].id,
                            is_required=name in {"MATERIAL", "LOCK-TYPE", "BACKSET"},
                            sort_order=usable[name].sort_order,
                        )
                        for name in names
                        if name in usable
                    ]
                ),
            )
            ctx.note(STAGE, created=1)
        codes = [
            ctx.code("FG", suffix) for suffix in ("LEVER-AB", "LEVER-SS", "MORTISE", "DIGILOCK")
        ]
        items = list(
            (
                await ctx.session.scalars(
                    select(Item).where(
                        Item.company_id == ctx.company_id,
                        Item.code.in_(codes),
                        Item.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        for item in items:
            if await _has_history(ctx, "item_specifications", item.id):
                continue
            if (await service.get_item_specifications(item.id)).values:
                continue
            handle = "LEVER" in item.code
            material = (
                "Brass"
                if item.code.endswith("LEVER-AB")
                else "Stainless Steel 304"
                if item.code.endswith("LEVER-SS")
                else "Aluminium"
                if item.code.endswith("DIGILOCK")
                else "Zinc Alloy"
            )
            raw: dict[str, dict[str, object]] = {
                "MATERIAL": {"value_option": material},
                "KEYS": {"value_number": Decimal("0") if handle else Decimal("3")},
                "WARRANTY": {"value_number": Decimal("5")},
                "FIRE-RATED": {"value_bool": False},
                "MOUNTING": {
                    "value_text": "Concealed fixing with supplied screw kit"
                    if handle
                    else "Reversible mortise lock installation"
                },
            }
            if not handle:
                raw.update(
                    {
                        "LOCK-TYPE": {"value_option": "Mortise"},
                        "BACKSET": {"value_number": Decimal("65")},
                    }
                )
            values = [
                ItemSpecificationValueIn.model_validate(
                    {"specification_attribute_id": usable[name].id, **value}
                )
                for name, value in raw.items()
                if name in usable
                and ("value_option" not in value or value["value_option"] in usable[name].options)
            ]
            if values:
                await service.replace_item_specifications(
                    item.id, ItemSpecificationsReplace(values=values)
                )
                ctx.note(STAGE, created=1)
        handles = [item for item in items if "LEVER" in item.code and item.is_active]
        uom = await find_by_code(ctx.session, UnitOfMeasure, ctx.company_id, "NOS")
        if handles and uom is not None and uom.deleted_at is None and uom.is_active:
            kit = await _accessory_item(ctx, included=True, uom_id=uom.id)
            rose = await _accessory_item(ctx, included=False, uom_id=uom.id)
            for item in handles:
                existing = (await service.list_accessories(item.id, PageParams(limit=200)))[0]
                # A pre-existing association list belongs to its operator. Do
                # not add defaults beside edits made before this seed ran.
                prior_change = await ctx.session.scalar(
                    select(AuditLog.id)
                    .where(
                        AuditLog.company_id == ctx.company_id,
                        AuditLog.entity_type == "item_accessory",
                        (AuditLog.old_values["item_id"].astext == str(item.id))
                        | (AuditLog.new_values["item_id"].astext == str(item.id)),
                    )
                    .limit(1)
                )
                if existing or prior_change is not None:
                    continue
                for accessory, relation, sort_order in [
                    (kit, AccessoryRelation.INCLUDED, 10),
                    (rose, AccessoryRelation.OPTIONAL, 20),
                ]:
                    if (
                        accessory.deleted_at is not None
                        or not accessory.is_active
                        or (relation is AccessoryRelation.OPTIONAL and not accessory.can_sell)
                    ):
                        continue
                    await service.create_accessory(
                        item.id,
                        ItemAccessoryCreate(
                            accessory_item_id=accessory.id,
                            relation=relation,
                            sort_order=sort_order,
                            notes="Demonstration association; no BOM or stock effect",
                        ),
                    )
                    ctx.note(STAGE, created=1)
        await record_audit(
            ctx.session,
            company_id=ctx.company_id,
            user_id=ctx.user_id,
            action=AuditAction.CREATE,
            entity_type=MARKER,
            entity_id=ctx.company_id,
            entity_code=ctx.code("SPECIFICATIONS", "D5"),
            summary="Completed initial specification and accessory demonstration setup; "
            "reruns preserve operator changes",
        )
