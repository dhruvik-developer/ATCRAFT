"""Bounded descriptive-product contracts; typed presence preserves zero and false."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from src.common.codes import normalise_code
from src.core.constants import (
    MAX_SPECIFICATION_FILTERS,
    MAX_SPECIFICATION_OPTIONS,
    MAX_SPECIFICATIONS_PER_ITEM,
)
from src.modules.products.models import AccessoryRelation, SpecificationValueType

SortOrder = Annotated[int, Field(ge=0, le=9999)]
SpecificationNumber = Annotated[
    Decimal, Field(max_digits=18, decimal_places=4, allow_inf_nan=False)
]
AccessoryQuantity = Annotated[
    Decimal, Field(gt=0, max_digits=18, decimal_places=4, allow_inf_nan=False)
]
OptionValue = Annotated[str, Field(min_length=1, max_length=64)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def validate_options(value_type: SpecificationValueType, options: list[str]) -> None:
    if (value_type is SpecificationValueType.OPTION) != bool(options):
        raise ValueError("Options must be nonempty exactly when the value type is OPTION.")
    if len(set(options)) != len(options):
        raise ValueError("Each option must be unique.")


class SpecificationAttributeCreate(_Input):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    value_type: SpecificationValueType
    unit: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    options: Annotated[list[OptionValue], Field(max_length=MAX_SPECIFICATION_OPTIONS)] = Field(
        default_factory=list
    )
    is_filterable: bool = False
    sort_order: SortOrder = 0
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def canonical_code(cls, value: str) -> str:
        return normalise_code(value)

    @model_validator(mode="after")
    def options_match_type(self) -> Self:
        validate_options(self.value_type, self.options)
        return self


class SpecificationAttributeUpdate(_Input):
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    value_type: SpecificationValueType | None = None
    unit: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    options: Annotated[list[OptionValue], Field(max_length=MAX_SPECIFICATION_OPTIONS)] | None = None
    is_filterable: bool | None = None
    sort_order: SortOrder | None = None
    is_active: bool | None = None

    @field_validator("name", "value_type", "options", "is_filterable", "sort_order", "is_active")
    @classmethod
    def required_if_supplied(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be cleared.")
        return value

    @field_validator("options")
    @classmethod
    def unique_options(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Each option must be unique.")
        return value


class SpecificationAttributeRead(_Read):
    id: uuid.UUID
    code: str
    name: str
    value_type: SpecificationValueType
    unit: str | None
    options: list[str]
    is_filterable: bool
    sort_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SpecificationCategoryOption(_Read):
    id: uuid.UUID
    code: str
    name: str
    parent_id: uuid.UUID | None
    path: str
    depth: int
    is_active: bool


class CategorySpecificationIn(_Input):
    specification_attribute_id: uuid.UUID
    is_required: bool = False
    sort_order: SortOrder = 0


class CategorySpecificationsReplace(_Input):
    specifications: Annotated[
        list[CategorySpecificationIn], Field(max_length=MAX_SPECIFICATIONS_PER_ITEM)
    ]

    @model_validator(mode="after")
    def unique_attributes(self) -> Self:
        ids = [value.specification_attribute_id for value in self.specifications]
        if len(ids) != len(set(ids)):
            raise ValueError("The same specification attribute appears more than once.")
        return self


class CategorySpecificationRead(_Read):
    id: uuid.UUID
    category_id: uuid.UUID
    attribute: SpecificationAttributeRead
    is_required: bool
    sort_order: int


class CategorySpecificationsRead(_Read):
    category_id: uuid.UUID
    specifications: list[CategorySpecificationRead]


class ItemSpecificationValueIn(_Input):
    specification_attribute_id: uuid.UUID
    value_text: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    value_number: SpecificationNumber | None = None
    value_bool: StrictBool | None = None
    value_option: OptionValue | None = None

    @model_validator(mode="after")
    def exactly_one_value(self) -> Self:
        if (
            sum(
                value is not None
                for value in (
                    self.value_text,
                    self.value_number,
                    self.value_bool,
                    self.value_option,
                )
            )
            != 1
        ):
            raise ValueError("Provide exactly one typed specification value.")
        return self


class ItemSpecificationsReplace(_Input):
    values: Annotated[list[ItemSpecificationValueIn], Field(max_length=MAX_SPECIFICATIONS_PER_ITEM)]

    @model_validator(mode="after")
    def unique_attributes(self) -> Self:
        ids = [value.specification_attribute_id for value in self.values]
        if len(ids) != len(set(ids)):
            raise ValueError("The same specification attribute appears more than once.")
        return self


class ItemSpecificationValueRead(_Read):
    id: uuid.UUID
    specification_attribute_id: uuid.UUID
    value_type: SpecificationValueType
    value_text: str | None
    value_number: Decimal | None
    value_bool: bool | None
    value_option: str | None
    attribute: SpecificationAttributeRead


class ResolvedSpecificationRead(_Read):
    attribute: SpecificationAttributeRead
    is_required: bool
    source_category_ids: list[uuid.UUID]
    sort_order: int
    missing_required: bool


class ItemSpecificationsRead(_Read):
    item_id: uuid.UUID
    values: list[ItemSpecificationValueRead] = Field(default_factory=list)
    template: list[ResolvedSpecificationRead] = Field(default_factory=list)
    missing_required_count: int = 0


class ItemAccessoryCreate(_Input):
    accessory_item_id: uuid.UUID
    quantity: AccessoryQuantity = Decimal("1")
    relation: AccessoryRelation = AccessoryRelation.OPTIONAL
    sort_order: SortOrder = 0
    notes: Annotated[str, Field(max_length=2000)] | None = None


class ItemAccessoryUpdate(_Input):
    quantity: AccessoryQuantity | None = None
    relation: AccessoryRelation | None = None
    sort_order: SortOrder | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None

    @field_validator("quantity", "relation", "sort_order")
    @classmethod
    def required_if_supplied(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be cleared.")
        return value


class ItemAccessoryRead(_Read):
    id: uuid.UUID
    item_id: uuid.UUID
    accessory_item_id: uuid.UUID
    accessory_code: str
    accessory_name: str
    accessory_is_active: bool
    accessory_can_sell: bool
    quantity: Decimal
    relation: AccessoryRelation
    sort_order: int
    notes: str | None


class DealerSpecificationFilterRead(_Read):
    id: uuid.UUID
    code: str
    name: str
    value_type: SpecificationValueType
    unit: str | None
    options: list[str]


class SpecificationFilterFields(BaseModel):
    spec: Annotated[
        list[Annotated[str, Field(max_length=128)]], Field(max_length=MAX_SPECIFICATION_FILTERS)
    ] = Field(default_factory=list)
    has_accessories: bool | None = None

    @field_validator("spec")
    @classmethod
    def canonical_filters(cls, expressions: list[str]) -> list[str]:
        result: list[str] = []
        for expression in expressions:
            if not expression.strip():
                result.append("=")
                continue
            code, separator, value = expression.partition("=")
            code = normalise_code(code)
            if not separator or not code or len(code) > 32 or len(value.strip()) > 64:
                raise ValueError(
                    "Each specification filter must be CODE=value "
                    "(code up to 32, value up to 64 characters)."
                )
            result.append(f"{code}={value.strip()}")
        return result
