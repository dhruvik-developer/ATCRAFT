"""Governed specifications and informational accessories, independent of stock."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.actors import actor_id
from src.core.constants import MAX_ITEM_ACCESSORIES, MAX_SPECIFICATION_BATCH
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, DuplicateError, NotFoundError, ValidationError
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.organization.service import (
    MasterLifecycleState,
    master_lifecycle,
    referenced_in_use,
)
from src.modules.products.models import (
    AccessoryRelation,
    CategorySpecification,
    Item,
    ItemAccessory,
    ItemSpecification,
    SpecificationAttribute,
    SpecificationValueType,
)
from src.modules.products.specification_repository import (
    ATTRIBUTE_FIELDS,
    VALUE_FIELDS,
    SpecificationAttributeRepository,
    SpecificationRepository,
)
from src.modules.products.specification_schemas import (
    CategorySpecificationRead,
    CategorySpecificationsRead,
    CategorySpecificationsReplace,
    DealerSpecificationFilterRead,
    ItemAccessoryCreate,
    ItemAccessoryRead,
    ItemAccessoryUpdate,
    ItemSpecificationsRead,
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    ItemSpecificationValueRead,
    ResolvedSpecificationRead,
    SpecificationAttributeCreate,
    SpecificationAttributeRead,
    SpecificationAttributeUpdate,
    SpecificationCategoryOption,
    validate_options,
)

SPECIFICATION_LIFECYCLE = master_lifecycle(
    "Specification attribute", Permission.ITEM_ATTRIBUTE_MANAGE
)
VALUE_FIELD_BY_TYPE = {
    SpecificationValueType.TEXT: "value_text",
    SpecificationValueType.NUMBER: "value_number",
    SpecificationValueType.BOOLEAN: "value_bool",
    SpecificationValueType.OPTION: "value_option",
}


def validate_specification_value(
    attribute: SpecificationAttribute, value: ItemSpecificationValueIn
) -> None:
    expected = VALUE_FIELD_BY_TYPE[SpecificationValueType(attribute.value_type)]
    if getattr(value, expected) is None:
        raise ValidationError(
            f"Specification '{attribute.name}' requires a {attribute.value_type.lower()} value.",
            details={
                "specification_attribute_id": str(attribute.id),
                "value_type": str(attribute.value_type),
            },
        )
    if value.value_option is not None and value.value_option not in attribute.options:
        raise ValidationError(
            f"Choose a configured option for specification '{attribute.name}'.",
            details={"specification_attribute_id": str(attribute.id), "options": attribute.options},
        )


def _batch_ids(item_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    if len(item_ids) > MAX_SPECIFICATION_BATCH:
        raise ValidationError(f"At most {MAX_SPECIFICATION_BATCH} items may be read together.")
    return list(dict.fromkeys(item_ids))


async def specifications_for_items(
    session: AsyncSession, tenant: TenantContext, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ItemSpecificationsRead]:
    """Read values, inherited templates and warnings in exactly one query."""
    rows = await SpecificationRepository(session, tenant).specification_rows(_batch_ids(item_ids))
    result: dict[uuid.UUID, ItemSpecificationsRead] = {}
    templates: dict[tuple[uuid.UUID, uuid.UUID], list[Any]] = {}
    for row in rows:
        item_id = row["item_id"]
        current = result.setdefault(item_id, ItemSpecificationsRead(item_id=item_id))
        if row["kind"] == "BASE":
            continue
        attribute = SpecificationAttributeRead.model_validate(
            {name: row[f"attribute_{name}"] for name in ATTRIBUTE_FIELDS}
        )
        if row["kind"] == "VALUE":
            current.values.append(
                ItemSpecificationValueRead.model_validate(
                    {
                        **{name: row[f"value_{name}"] for name in VALUE_FIELDS},
                        "attribute": attribute,
                    }
                )
            )
        else:
            templates.setdefault((item_id, attribute.id), []).append(
                (
                    attribute,
                    row["source_depth"],
                    row["source_category_id"],
                    row["template_sort_order"],
                    row["is_required"],
                )
            )
    for (item_id, attribute_id), inherited in templates.items():
        inherited.sort(key=lambda entry: (entry[1], str(entry[2])))
        nearest = inherited[-1]
        current = result[item_id]
        required = any(entry[4] for entry in inherited)
        missing = required and attribute_id not in {
            value.specification_attribute_id for value in current.values
        }
        current.template.append(
            ResolvedSpecificationRead(
                attribute=nearest[0],
                is_required=required,
                source_category_ids=[entry[2] for entry in inherited],
                sort_order=nearest[3],
                missing_required=missing,
            )
        )
        current.missing_required_count += int(missing)
    for current in result.values():
        current.values.sort(
            key=lambda value: (value.attribute.sort_order, value.attribute.name, str(value.id))
        )
        current.template.sort(
            key=lambda value: (value.sort_order, value.attribute.name, str(value.attribute.id))
        )
    return result


def _accessory_read(row: Any) -> ItemAccessoryRead:
    accessory = row[0]
    return ItemAccessoryRead(
        id=accessory.id,
        item_id=accessory.item_id,
        accessory_item_id=accessory.accessory_item_id,
        accessory_code=row[1],
        accessory_name=row[2],
        accessory_is_active=row[3],
        accessory_can_sell=row[4],
        quantity=accessory.quantity,
        relation=accessory.relation,
        sort_order=accessory.sort_order,
        notes=accessory.notes,
    )


def _require_sellable_optional(relation: AccessoryRelation, accessory: Item) -> None:
    if relation is AccessoryRelation.OPTIONAL and not accessory.can_sell:
        raise ValidationError(
            "An OPTIONAL accessory must be sellable. "
            "Use INCLUDED for an informational item in the box."
        )


async def accessories_for_items(
    session: AsyncSession, tenant: TenantContext, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[ItemAccessoryRead]]:
    """One query, no cost, availability, reservations or inventory side effects."""
    rows = await SpecificationRepository(session, tenant).accessory_rows(_batch_ids(item_ids))
    result: dict[uuid.UUID, list[ItemAccessoryRead]] = {}
    for row in rows:
        read = _accessory_read(row)
        result.setdefault(read.item_id, []).append(read)
    return result


class SpecificationService:
    def __init__(self, session: AsyncSession, tenant: TenantContext):
        self.session, self.tenant = session, tenant
        self.attributes = SpecificationAttributeRepository(session, tenant)
        self.repo = SpecificationRepository(session, tenant)

    async def _audit(
        self,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID,
        summary: str,
        *,
        old: dict[str, Any] | None = None,
        new: dict[str, Any] | None = None,
    ) -> None:
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            user_id=self.tenant.user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            old_values=old,
            new_values=new,
        )

    async def create_attribute(
        self, payload: SpecificationAttributeCreate
    ) -> SpecificationAttributeRead:
        async with transaction(self.session):
            await advisory_xact_lock(
                self.session, "specification_attribute_code", self.tenant.company_id, payload.code
            )
            if await self.attributes.code_exists(payload.code):
                raise DuplicateError(
                    f"Specification attribute code '{payload.code}' is already used "
                    "in this company. Codes of retired records are not reused."
                )
            record = self.attributes.add(SpecificationAttribute(**payload.model_dump()))
            await self.session.flush()
            read = SpecificationAttributeRead.model_validate(record)
            await self._audit(
                AuditAction.CREATE,
                "specification_attribute",
                record.id,
                f"Created specification attribute {record.code}",
                new=read.model_dump(mode="json"),
            )
        return read

    async def get_attribute(self, attribute_id: uuid.UUID) -> SpecificationAttributeRead:
        return SpecificationAttributeRead.model_validate(
            await self.attributes.get_or_404(attribute_id)
        )

    async def list_attributes(
        self,
        params: PageParams,
        sort: SortParams | None = None,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        filterable_only: bool = False,
    ) -> tuple[list[SpecificationAttributeRead], OffsetPagination]:
        rows, pagination = await self.attributes.list_attributes(
            params, sort, search=search, is_active=is_active, filterable_only=filterable_only
        )
        return [SpecificationAttributeRead.model_validate(row) for row in rows], pagination

    async def _refuse_in_use(self, attribute: SpecificationAttribute) -> None:
        values, templates = await self.attributes.usage(attribute.id)
        if values or templates:
            raise referenced_in_use(
                f"Specification attribute {attribute.code} is still referenced by "
                "item values or category templates. Remove those references first.",
                item_values=values,
                category_templates=templates,
            )

    async def update_attribute(
        self, attribute_id: uuid.UUID, payload: SpecificationAttributeUpdate
    ) -> SpecificationAttributeRead:
        async with transaction(self.session):
            record = await self.attributes.lock(attribute_id)
            old = SpecificationAttributeRead.model_validate(record)
            changes = payload.model_dump(exclude_unset=True)
            value_type = SpecificationValueType(changes.get("value_type", record.value_type))
            options = changes.get("options", record.options)
            try:
                validate_options(value_type, options)
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if value_type != record.value_type and (await self.attributes.usage(attribute_id))[0]:
                # ASSUMPTION D5-4: never reinterpret an existing typed value.
                raise referenced_in_use(
                    "A specification's type cannot change while item values reference it."
                )
            if (
                record.value_type == SpecificationValueType.OPTION
                and set(record.options) - set(options)
                and (await self.attributes.used_options(attribute_id)) - set(options)
            ):
                raise referenced_in_use(
                    "An option used by an item cannot be removed. Update those item values first."
                )
            if "is_active" in changes and changes["is_active"] != record.is_active:
                current = (
                    MasterLifecycleState.ACTIVE
                    if record.is_active
                    else MasterLifecycleState.INACTIVE
                )
                target = (
                    MasterLifecycleState.ACTIVE
                    if changes["is_active"]
                    else MasterLifecycleState.INACTIVE
                )
                SPECIFICATION_LIFECYCLE.assert_transition(current, target)
                if target is MasterLifecycleState.INACTIVE:
                    await self._refuse_in_use(record)
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    user_id=self.tenant.user_id,
                    entity_type="specification_attribute",
                    entity_id=record.id,
                    entity_code=record.code,
                    from_status=str(current),
                    to_status=str(target),
                    reason="Specification attribute availability changed",
                )
            for key, value in changes.items():
                setattr(record, key, value)
            self.attributes.touch(record)
            await self.session.flush()
            read = SpecificationAttributeRead.model_validate(record)
            await self._audit(
                AuditAction.UPDATE,
                "specification_attribute",
                record.id,
                f"Updated specification attribute {record.code}",
                old=old.model_dump(mode="json"),
                new=read.model_dump(mode="json"),
            )
        return read

    async def retire_attribute(self, attribute_id: uuid.UUID, *, reason: str) -> None:
        async with transaction(self.session):
            record = await self.attributes.lock(attribute_id)
            current = (
                MasterLifecycleState.ACTIVE if record.is_active else MasterLifecycleState.INACTIVE
            )
            SPECIFICATION_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)
            await self._refuse_in_use(record)
            await self.attributes.soft_delete(record)
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                user_id=self.tenant.user_id,
                entity_type="specification_attribute",
                entity_id=record.id,
                entity_code=record.code,
                from_status=str(current),
                to_status=str(MasterLifecycleState.ARCHIVED),
                reason=reason,
                action=AuditAction.DELETE,
            )

    async def list_categories(
        self, params: PageParams, search: str | None = None
    ) -> tuple[list[SpecificationCategoryOption], OffsetPagination]:
        rows, pagination = await self.repo.categories(params, search)
        return [SpecificationCategoryOption.model_validate(row) for row in rows], pagination

    async def get_category_specifications(
        self, category_id: uuid.UUID
    ) -> CategorySpecificationsRead:
        if await self.repo.category(category_id) is None:
            raise NotFoundError("Item category", category_id)
        rows = await self.repo.category_specifications(category_id)
        return CategorySpecificationsRead(
            category_id=category_id,
            specifications=[
                CategorySpecificationRead(
                    id=row[0].id,
                    category_id=category_id,
                    attribute=SpecificationAttributeRead.model_validate(row[1]),
                    is_required=row[0].is_required,
                    sort_order=row[0].sort_order,
                )
                for row in rows
            ],
        )

    async def _assigned_attributes(
        self, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SpecificationAttribute]:
        records = await self.attributes.for_assignment(ids)
        for attribute_id in ids:
            attribute = records.get(attribute_id)
            if attribute is None:
                raise NotFoundError("Specification attribute", attribute_id)
            if not attribute.is_active:
                raise ValidationError(
                    f"Specification attribute '{attribute.name}' is inactive "
                    "and cannot be assigned."
                )
        return records

    async def replace_category_specifications(
        self, category_id: uuid.UUID, payload: CategorySpecificationsReplace
    ) -> CategorySpecificationsRead:
        async with transaction(self.session):
            if await self.repo.category(category_id, lock=True) is None:
                raise NotFoundError("Item category", category_id)
            await self._assigned_attributes(
                [value.specification_attribute_id for value in payload.specifications]
            )
            old = await self.get_category_specifications(category_id)
            rows = [
                CategorySpecification(
                    company_id=self.tenant.company_id,
                    category_id=category_id,
                    created_by_id=actor_id(self.tenant.user_id),
                    **value.model_dump(),
                )
                for value in payload.specifications
            ]
            await self.repo.replace_templates(category_id, rows)
            read = await self.get_category_specifications(category_id)
            await self._audit(
                AuditAction.UPDATE,
                "category_specifications",
                category_id,
                "Replaced category specification template",
                old=old.model_dump(mode="json"),
                new=read.model_dump(mode="json"),
            )
        return read

    async def get_item_specifications(self, item_id: uuid.UUID) -> ItemSpecificationsRead:
        read = (await specifications_for_items(self.session, self.tenant, [item_id])).get(item_id)
        if read is None:
            raise NotFoundError("Item", item_id)
        return read

    async def replace_item_specifications(
        self, item_id: uuid.UUID, payload: ItemSpecificationsReplace
    ) -> ItemSpecificationsRead:
        async with transaction(self.session):
            if item_id not in await self.repo.lock_items([item_id]):
                raise NotFoundError("Item", item_id)
            attributes = await self._assigned_attributes(
                [value.specification_attribute_id for value in payload.values]
            )
            for value in payload.values:
                validate_specification_value(attributes[value.specification_attribute_id], value)
            old = await self.get_item_specifications(item_id)
            rows = [
                ItemSpecification(
                    company_id=self.tenant.company_id,
                    item_id=item_id,
                    created_by_id=actor_id(self.tenant.user_id),
                    value_type=attributes[value.specification_attribute_id].value_type,
                    **value.model_dump(),
                )
                for value in payload.values
            ]
            await self.repo.replace_values(item_id, rows)
            read = await self.get_item_specifications(item_id)
            await self._audit(
                AuditAction.UPDATE,
                "item_specifications",
                item_id,
                "Replaced item specifications",
                old=old.model_dump(mode="json"),
                new=read.model_dump(mode="json"),
            )
        return read

    async def item_attribute_options(
        self, item_id: uuid.UUID, params: PageParams, *, search: str | None = None
    ) -> tuple[list[SpecificationAttributeRead], OffsetPagination]:
        if await self.repo.item(item_id) is None:
            raise NotFoundError("Item", item_id)
        return await self.list_attributes(params, search=search, is_active=True)

    async def list_accessories(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[list[ItemAccessoryRead], OffsetPagination]:
        if await self.repo.item(item_id) is None:
            raise NotFoundError("Item", item_id)
        rows, pagination = await self.repo.accessory_page(item_id, params)
        return [_accessory_read(row) for row in rows], pagination

    async def _accessory_items(
        self,
        item_id: uuid.UUID,
        accessory_item_id: uuid.UUID,
        relation: AccessoryRelation | None,
    ) -> dict[uuid.UUID, Item]:
        items = await self.repo.lock_items([item_id, accessory_item_id])
        for selected in (item_id, accessory_item_id):
            if selected not in items:
                raise NotFoundError("Item", selected)
            if not items[selected].is_active:
                raise ValidationError(
                    f"Item '{items[selected].code}' is inactive "
                    "and cannot receive an accessory association."
                )
        if item_id == accessory_item_id:
            raise ValidationError("An item cannot be its own accessory.")
        if relation is not None:
            _require_sellable_optional(relation, items[accessory_item_id])
        return items

    async def _get_accessory(
        self, accessory_id: uuid.UUID, item_id: uuid.UUID
    ) -> ItemAccessoryRead:
        row = await self.repo.accessory_detail(item_id, accessory_id)
        if row is None:
            raise NotFoundError("Item accessory", accessory_id)
        return _accessory_read(row)

    async def create_accessory(
        self, item_id: uuid.UUID, payload: ItemAccessoryCreate
    ) -> ItemAccessoryRead:
        async with transaction(self.session):
            await self._accessory_items(item_id, payload.accessory_item_id, payload.relation)
            if await self.repo.accessory_count(item_id) >= MAX_ITEM_ACCESSORIES:
                raise BusinessRuleError(
                    f"An item may have at most {MAX_ITEM_ACCESSORIES} accessories."
                )
            if await self.repo.accessory_exists(item_id, payload.accessory_item_id):
                raise DuplicateError("This accessory is already linked to the item.")
            row = ItemAccessory(
                company_id=self.tenant.company_id,
                item_id=item_id,
                created_by_id=actor_id(self.tenant.user_id),
                **payload.model_dump(),
            )
            self.session.add(row)
            await self.session.flush()
            read = await self._get_accessory(row.id, item_id)
            await self._audit(
                AuditAction.CREATE,
                "item_accessory",
                row.id,
                "Linked informational item accessory",
                new=read.model_dump(mode="json"),
            )
        return read

    async def update_accessory(
        self, accessory_id: uuid.UUID, payload: ItemAccessoryUpdate
    ) -> ItemAccessoryRead:
        async with transaction(self.session):
            original = await self.repo.accessory(accessory_id)
            if original is None:
                raise NotFoundError("Item accessory", accessory_id)
            item_id, target_id = original.item_id, original.accessory_item_id
            items = await self._accessory_items(item_id, target_id, None)
            row = await self.repo.accessory(accessory_id, lock=True)
            if row is None:
                raise NotFoundError("Item accessory", accessory_id)
            # Another edit can reclassify the association while this request
            # waits for its item locks. Validate the refreshed relation, not
            # the earlier unlocked ORM snapshot.
            relation = payload.relation or AccessoryRelation(row.relation)
            _require_sellable_optional(relation, items[target_id])
            old = await self._get_accessory(accessory_id, item_id)
            for key, value in payload.model_dump(exclude_unset=True).items():
                setattr(row, key, value)
            row.updated_by_id = actor_id(self.tenant.user_id)
            await self.session.flush()
            read = await self._get_accessory(accessory_id, item_id)
            await self._audit(
                AuditAction.UPDATE,
                "item_accessory",
                row.id,
                "Updated informational item accessory",
                old=old.model_dump(mode="json"),
                new=read.model_dump(mode="json"),
            )
        return read

    async def delete_accessory(self, accessory_id: uuid.UUID, *, reason: str) -> None:
        async with transaction(self.session):
            row = await self.repo.accessory(accessory_id)
            if row is None:
                raise NotFoundError("Item accessory", accessory_id)
            item_id, target_id = row.item_id, row.accessory_item_id
            await self.repo.lock_items([item_id, target_id])
            row = await self.repo.accessory(accessory_id, lock=True)
            if row is None:
                raise NotFoundError("Item accessory", accessory_id)
            old = await self._get_accessory(accessory_id, item_id)
            await self.repo.remove_accessory(accessory_id)
            await self._audit(
                AuditAction.DELETE,
                "item_accessory",
                accessory_id,
                reason,
                old=old.model_dump(mode="json"),
            )

    async def dealer_facets(
        self, params: PageParams
    ) -> tuple[list[DealerSpecificationFilterRead], OffsetPagination]:
        rows, pagination = await self.repo.dealer_facets(params)
        return [DealerSpecificationFilterRead.model_validate(row) for row in rows], pagination
