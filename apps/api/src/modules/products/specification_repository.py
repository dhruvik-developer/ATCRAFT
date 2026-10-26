"""Tenant-scoped specification persistence and single-query catalogue seams."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    Boolean,
    Integer,
    Select,
    cast,
    delete,
    exists,
    false,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.pagination import PageParams, SortParams, paginate
from src.core.responses import OffsetPagination
from src.core.types import GUID
from src.modules.products.models import (
    AccessoryRelation,
    CategorySpecification,
    Item,
    ItemAccessory,
    ItemCategory,
    ItemSpecification,
    SpecificationAttribute,
    SpecificationValueType,
)

ATTRIBUTE_FIELDS = (
    "id",
    "code",
    "name",
    "value_type",
    "unit",
    "options",
    "is_filterable",
    "sort_order",
    "is_active",
    "created_at",
    "updated_at",
)
VALUE_FIELDS = (
    "id",
    "specification_attribute_id",
    "value_type",
    "value_text",
    "value_number",
    "value_bool",
    "value_option",
)


def specification_predicates(
    company_id: uuid.UUID,
    item_column: Any,
    spec: Sequence[str],
    has_accessories: bool | None,
    *,
    dealer_visible_only: bool = False,
) -> list[Any]:
    """Repeated filters intersect; an explicitly empty value cannot remove a restriction."""
    clauses: list[Any] = []
    for expression in spec:
        code, _, value = expression.partition("=")
        if not value:
            clauses.append(false())
            continue
        match = (SpecificationAttribute.value_type == SpecificationValueType.OPTION) & (
            ItemSpecification.value_option == value
        )
        if value.lower() in {"true", "false"}:
            match |= (
                SpecificationAttribute.value_type == SpecificationValueType.BOOLEAN
            ) & ItemSpecification.value_bool.is_(value.lower() == "true")
        clauses.append(
            exists().where(
                ItemSpecification.company_id == company_id,
                ItemSpecification.item_id == item_column,
                SpecificationAttribute.id == ItemSpecification.specification_attribute_id,
                SpecificationAttribute.company_id == company_id,
                SpecificationAttribute.code == code,
                SpecificationAttribute.is_active.is_(True),
                SpecificationAttribute.deleted_at.is_(None),
                SpecificationAttribute.is_filterable.is_(True),
                match,
            )
        )
    if has_accessories is not None:
        accessory = aliased(Item)
        match_accessory = exists().where(
            ItemAccessory.company_id == company_id,
            ItemAccessory.item_id == item_column,
            accessory.id == ItemAccessory.accessory_item_id,
            accessory.company_id == company_id,
        )
        if dealer_visible_only:
            # INCLUDED remains descriptive even when its internal SKU retires.
            # OPTIONAL must be a current sellable SKU, exactly as in the facade.
            match_accessory = match_accessory.where(
                (ItemAccessory.relation == AccessoryRelation.INCLUDED)
                | (
                    accessory.deleted_at.is_(None)
                    & accessory.is_active.is_(True)
                    & accessory.can_sell.is_(True)
                )
            )
        clauses.append(match_accessory if has_accessories else ~match_accessory)
    return clauses


class SpecificationAttributeRepository(BaseRepository[SpecificationAttribute]):
    model = SpecificationAttribute
    entity_label = "Specification attribute"
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012 - repository configuration
        "code": SpecificationAttribute.code,
        "name": SpecificationAttribute.name,
        "sort_order": SpecificationAttribute.sort_order,
    }

    async def lock(self, attribute_id: uuid.UUID) -> SpecificationAttribute:
        row = (
            await self.session.execute(
                self._scoped()
                .where(self.model.id == attribute_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            from src.core.errors import NotFoundError

            raise NotFoundError(self.entity_label, attribute_id)
        return row

    async def for_assignment(
        self, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SpecificationAttribute]:
        if not ids:
            return {}
        rows = (
            (
                await self.session.execute(
                    self._scoped()
                    .where(self.model.id.in_(ids))
                    .order_by(self.model.id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return {row.id: row for row in rows}

    async def list_attributes(
        self,
        params: PageParams,
        sort: SortParams | None = None,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        filterable_only: bool = False,
    ) -> tuple[list[SpecificationAttribute], OffsetPagination]:
        stmt = self._apply_search(self._scoped(), search)
        if is_active is not None:
            stmt = stmt.where(self.model.is_active.is_(is_active))
        if filterable_only:
            stmt = stmt.where(
                self.model.is_filterable.is_(True),
                self.model.value_type.in_(
                    [SpecificationValueType.OPTION, SpecificationValueType.BOOLEAN]
                ),
            )
        chosen = (sort or SortParams()).resolve(self.sortable_fields, None)
        stmt = stmt.order_by(
            chosen if chosen is not None else self.model.sort_order, self.model.name, self.model.id
        )
        return await paginate(self.session, stmt, params)

    async def usage(self, attribute_id: uuid.UUID) -> tuple[bool, bool]:
        row = (
            await self.session.execute(
                select(
                    exists()
                    .where(
                        ItemSpecification.company_id == self.tenant.company_id,
                        ItemSpecification.specification_attribute_id == attribute_id,
                    )
                    .label("values"),
                    exists()
                    .where(
                        CategorySpecification.company_id == self.tenant.company_id,
                        CategorySpecification.specification_attribute_id == attribute_id,
                    )
                    .label("templates"),
                )
            )
        ).one()
        return bool(row[0]), bool(row[1])

    async def used_options(self, attribute_id: uuid.UUID) -> set[str]:
        rows = await self.session.scalars(
            select(ItemSpecification.value_option)
            .where(
                ItemSpecification.company_id == self.tenant.company_id,
                ItemSpecification.specification_attribute_id == attribute_id,
                ItemSpecification.value_option.is_not(None),
            )
            .distinct()
        )
        return {value for value in rows if value is not None}

    async def code_exists(self, code: str) -> bool:
        return bool(
            await self.session.scalar(
                select(
                    exists().where(
                        self.model.company_id == self.tenant.company_id, self.model.code == code
                    )
                )
            )
        )


class SpecificationRepository:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant

    async def item(self, item_id: uuid.UUID) -> Item | None:
        return (
            await self.session.execute(
                select(Item).where(
                    Item.company_id == self.tenant.company_id,
                    Item.id == item_id,
                    Item.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def lock_items(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Item]:
        rows = (
            (
                await self.session.execute(
                    select(Item)
                    .where(
                        Item.company_id == self.tenant.company_id,
                        Item.id.in_(ids),
                        Item.deleted_at.is_(None),
                    )
                    .order_by(Item.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return {row.id: row for row in rows}

    async def category(self, category_id: uuid.UUID, *, lock: bool = False) -> ItemCategory | None:
        stmt = select(ItemCategory).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.id == category_id,
            ItemCategory.deleted_at.is_(None),
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def categories(
        self, params: PageParams, search: str | None = None
    ) -> tuple[list[ItemCategory], OffsetPagination]:
        stmt = select(ItemCategory).where(
            ItemCategory.company_id == self.tenant.company_id, ItemCategory.deleted_at.is_(None)
        )
        if search:
            stmt = stmt.where(
                ItemCategory.name.ilike(f"%{search}%") | ItemCategory.code.ilike(f"%{search}%")
            )
        return await paginate(
            self.session, stmt.order_by(ItemCategory.path, ItemCategory.id), params
        )

    async def category_specifications(self, category_id: uuid.UUID) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    select(CategorySpecification, SpecificationAttribute)
                    .join(
                        SpecificationAttribute,
                        SpecificationAttribute.id
                        == CategorySpecification.specification_attribute_id,
                    )
                    .where(
                        CategorySpecification.company_id == self.tenant.company_id,
                        CategorySpecification.category_id == category_id,
                        SpecificationAttribute.company_id == self.tenant.company_id,
                    )
                    .order_by(
                        CategorySpecification.sort_order,
                        SpecificationAttribute.name,
                        CategorySpecification.id,
                    )
                )
            ).all()
        )

    async def replace_templates(
        self, category_id: uuid.UUID, rows: Sequence[CategorySpecification]
    ) -> None:
        await self.session.execute(
            delete(CategorySpecification).where(
                CategorySpecification.company_id == self.tenant.company_id,
                CategorySpecification.category_id == category_id,
            )
        )
        self.session.add_all(rows)
        await self.session.flush()

    async def replace_values(self, item_id: uuid.UUID, rows: Sequence[ItemSpecification]) -> None:
        await self.session.execute(
            delete(ItemSpecification).where(
                ItemSpecification.company_id == self.tenant.company_id,
                ItemSpecification.item_id == item_id,
            )
        )
        self.session.add_all(rows)
        await self.session.flush()

    async def specification_rows(self, item_ids: Sequence[uuid.UUID]) -> list[Any]:
        """BASE + VALUE + inherited TEMPLATE records in one round trip."""
        if not item_ids:
            return []
        attribute_columns = [
            getattr(SpecificationAttribute, name).label(f"attribute_{name}")
            for name in ATTRIBUTE_FIELDS
        ]
        value_columns = [
            getattr(ItemSpecification, name).label(f"value_{name}") for name in VALUE_FIELDS
        ]
        null_attributes = [
            cast(null(), getattr(SpecificationAttribute, name).type).label(f"attribute_{name}")
            for name in ATTRIBUTE_FIELDS
        ]
        null_values = [
            cast(null(), getattr(ItemSpecification, name).type).label(f"value_{name}")
            for name in VALUE_FIELDS
        ]
        scoped = (
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(item_ids),
        )
        empty_template = [
            cast(null(), GUID).label("source_category_id"),
            cast(null(), Integer).label("source_depth"),
            cast(null(), Integer).label("template_sort_order"),
            cast(null(), Boolean).label("is_required"),
        ]
        base = select(
            Item.id.label("item_id"),
            literal("BASE").label("kind"),
            *null_attributes,
            *null_values,
            *empty_template,
        ).where(*scoped)
        values = (
            select(
                Item.id.label("item_id"),
                literal("VALUE").label("kind"),
                *attribute_columns,
                *value_columns,
                *empty_template,
            )
            .join(ItemSpecification, ItemSpecification.item_id == Item.id)
            .join(
                SpecificationAttribute,
                SpecificationAttribute.id == ItemSpecification.specification_attribute_id,
            )
            .where(
                *scoped,
                ItemSpecification.company_id == self.tenant.company_id,
                SpecificationAttribute.company_id == self.tenant.company_id,
            )
        )
        current = aliased(ItemCategory)
        ancestor = aliased(ItemCategory)
        templates = (
            select(
                Item.id.label("item_id"),
                literal("TEMPLATE").label("kind"),
                *attribute_columns,
                *null_values,
                ancestor.id.label("source_category_id"),
                ancestor.depth.label("source_depth"),
                CategorySpecification.sort_order.label("template_sort_order"),
                CategorySpecification.is_required.label("is_required"),
            )
            .join(current, current.id == Item.category_id)
            .join(ancestor, current.path.like(ancestor.path + "%"))
            .join(CategorySpecification, CategorySpecification.category_id == ancestor.id)
            .join(
                SpecificationAttribute,
                SpecificationAttribute.id == CategorySpecification.specification_attribute_id,
            )
            .where(
                *scoped,
                current.company_id == self.tenant.company_id,
                ancestor.company_id == self.tenant.company_id,
                current.deleted_at.is_(None),
                ancestor.deleted_at.is_(None),
                CategorySpecification.company_id == self.tenant.company_id,
                SpecificationAttribute.company_id == self.tenant.company_id,
                SpecificationAttribute.deleted_at.is_(None),
                SpecificationAttribute.is_active.is_(True),
            )
        )
        return list(
            (await self.session.execute(union_all(base, values, templates))).mappings().all()
        )

    def accessories_stmt(self, item_ids: Sequence[uuid.UUID]) -> Select[Any]:
        accessory = aliased(Item)
        return (
            select(
                ItemAccessory,
                accessory.code,
                accessory.name,
                (accessory.is_active & accessory.deleted_at.is_(None)).label("accessory_is_active"),
                accessory.can_sell,
            )
            .join(Item, Item.id == ItemAccessory.item_id)
            .join(accessory, accessory.id == ItemAccessory.accessory_item_id)
            .where(
                ItemAccessory.company_id == self.tenant.company_id,
                ItemAccessory.item_id.in_(item_ids),
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                accessory.company_id == self.tenant.company_id,
            )
            .order_by(ItemAccessory.sort_order, accessory.name, ItemAccessory.id)
        )

    async def accessory_rows(self, item_ids: Sequence[uuid.UUID]) -> list[Any]:
        if not item_ids:
            return []
        return list((await self.session.execute(self.accessories_stmt(item_ids))).all())

    async def accessory_detail(self, item_id: uuid.UUID, accessory_id: uuid.UUID) -> Any:
        return (
            await self.session.execute(
                self.accessories_stmt([item_id]).where(ItemAccessory.id == accessory_id)
            )
        ).one_or_none()

    async def accessory_page(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[list[Any], OffsetPagination]:
        stmt = (
            select(ItemAccessory)
            .where(
                ItemAccessory.company_id == self.tenant.company_id, ItemAccessory.item_id == item_id
            )
            .order_by(ItemAccessory.sort_order, ItemAccessory.id)
        )
        rows, pagination = await paginate(self.session, stmt, params)
        if not rows:
            return [], pagination
        labelled = (
            await self.session.execute(
                self.accessories_stmt([item_id]).where(
                    ItemAccessory.id.in_([row.id for row in rows])
                )
            )
        ).all()
        by_id = {row[0].id: row for row in labelled}
        return [by_id[row.id] for row in rows], pagination

    async def remove_accessory(self, accessory_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ItemAccessory).where(
                ItemAccessory.company_id == self.tenant.company_id, ItemAccessory.id == accessory_id
            )
        )

    async def accessory(
        self, accessory_id: uuid.UUID, *, lock: bool = False
    ) -> ItemAccessory | None:
        stmt = select(ItemAccessory).where(
            ItemAccessory.company_id == self.tenant.company_id, ItemAccessory.id == accessory_id
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def accessory_count(self, item_id: uuid.UUID) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(ItemAccessory)
                .where(
                    ItemAccessory.company_id == self.tenant.company_id,
                    ItemAccessory.item_id == item_id,
                )
            )
            or 0
        )

    async def accessory_exists(self, item_id: uuid.UUID, accessory_item_id: uuid.UUID) -> bool:
        return bool(
            await self.session.scalar(
                select(
                    exists().where(
                        ItemAccessory.company_id == self.tenant.company_id,
                        ItemAccessory.item_id == item_id,
                        ItemAccessory.accessory_item_id == accessory_item_id,
                    )
                )
            )
        )

    async def dealer_facets(
        self, params: PageParams
    ) -> tuple[list[SpecificationAttribute], OffsetPagination]:
        stmt = select(SpecificationAttribute).where(
            SpecificationAttribute.company_id == self.tenant.company_id,
            SpecificationAttribute.deleted_at.is_(None),
            SpecificationAttribute.is_active.is_(True),
            SpecificationAttribute.is_filterable.is_(True),
            SpecificationAttribute.value_type.in_(
                [SpecificationValueType.OPTION, SpecificationValueType.BOOLEAN]
            ),
            exists().where(
                ItemSpecification.company_id == self.tenant.company_id,
                ItemSpecification.specification_attribute_id == SpecificationAttribute.id,
                Item.id == ItemSpecification.item_id,
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                Item.is_active.is_(True),
                Item.can_sell.is_(True),
            ),
        )
        return await paginate(
            self.session,
            stmt.order_by(
                SpecificationAttribute.sort_order,
                SpecificationAttribute.name,
                SpecificationAttribute.id,
            ),
            params,
        )
