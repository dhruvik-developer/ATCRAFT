"""Request and response contracts for the item master.

Two things here are load-bearing rather than boilerplate:

* **Cost is a separate response model.** ``ItemDetail`` has no ``standard_cost``
  field at all; ``ItemDetailWithCost`` adds it. The service decides which class
  to build from the principal's permissions, so a caller without
  ``item.cost_view`` never receives the field - not as ``null``, not hidden by
  the frontend. The same split exists for list rows.
* **Capability flags are optional on create.** ``None`` means "use the default
  for this item group"; an explicit ``false`` means the user turned it off.
  Distinguishing the two is only possible because they are nullable in the
  request model and resolved in the service.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common.codes import normalise_code
from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.modules.products.models import (
    ItemAttributeType,
    ItemGroup,
    ItemLifecycleState,
    PriceType,
    ProductionType,
    UomCategory,
)

# Numeric guards mirroring the column definitions, so a value that would violate
# a check constraint is rejected at the edge with a field-level message rather
# than as a database error.
MoneyField = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PriceField = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
QuantityField = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
FactorField = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8)]
GstRateField = Annotated[Decimal, Field(ge=0, le=100, max_digits=9, decimal_places=4)]


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


# -----------------------------------------------------------------------------
# Categories
# -----------------------------------------------------------------------------


class CategoryCreate(_Base):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_code(value)


class CategoryUpdate(_Base):
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    description: str | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None
    #: ``parent_id=None`` is ambiguous - "no change" or "make it a root"? This
    #: flag disambiguates without a sentinel value.
    reparent_to_root: bool = False


class CategoryOut(_Base):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    parent_id: uuid.UUID | None
    path: str
    depth: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# -----------------------------------------------------------------------------
# Units of measure
# -----------------------------------------------------------------------------


class UomCreate(_Base):
    code: Annotated[str, Field(min_length=1, max_length=16)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    uom_category: UomCategory
    is_base: bool = False
    decimal_places: Annotated[int, Field(ge=0, le=6)] = 4
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_code(value)


class UomUpdate(_Base):
    name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    decimal_places: Annotated[int, Field(ge=0, le=6)] | None = None
    is_active: bool | None = None
    is_base: bool | None = None


class UomOut(_Base):
    id: uuid.UUID
    code: str
    name: str
    uom_category: UomCategory
    is_base: bool
    decimal_places: int
    is_active: bool


class ItemAttributeCreate(_Base):
    attribute: ItemAttributeType
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    sort_order: Annotated[int, Field(ge=0, le=9999)] = 0
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_code(value)


class ItemAttributeUpdate(_Base):
    """``None`` means "leave alone" on every field.

    ``attribute`` is absent on purpose: moving a value from FINISH to COLOUR
    would silently retarget every item that references it, and the two are
    unique per kind, so the move could also collide. Retire it and make a new
    one instead.
    """

    name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    sort_order: Annotated[int, Field(ge=0, le=9999)] | None = None
    is_active: bool | None = None


class ItemAttributeOut(_Base):
    id: uuid.UUID
    attribute: ItemAttributeType
    code: str
    name: str
    sort_order: int
    is_active: bool


class UomConversionCreate(_Base):
    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    factor: FactorField

    @model_validator(mode="after")
    def _distinct(self) -> UomConversionCreate:
        if self.from_uom_id == self.to_uom_id:
            raise ValueError("A unit cannot be converted to itself.")
        return self


class UomConversionOut(_Base):
    id: uuid.UUID
    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    factor: Decimal
    is_active: bool


class ConversionResult(_Base):
    """Answer to "how much is X FROM in TO?"."""

    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    input_quantity: Decimal
    factor: Decimal
    converted_quantity: Decimal


# -----------------------------------------------------------------------------
# Items
# -----------------------------------------------------------------------------


class CapabilityFlags(_Base):
    """The nine independent roles an item may play (master rules section 1).

    All optional: unset means "take the default for the item group". They are
    never derived from ``item_group`` after creation.
    """

    can_purchase: bool | None = None
    can_manufacture: bool | None = None
    can_sell: bool | None = None
    can_be_bom_component: bool | None = None
    can_be_assembly_component: bool | None = None
    can_be_spare: bool | None = None
    can_job_work: bool | None = None
    can_return: bool | None = None
    can_scrap: bool | None = None


class ItemCreate(CapabilityFlags):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = None

    item_group: ItemGroup
    category_id: uuid.UUID | None = None

    base_uom_id: uuid.UUID
    purchase_uom_id: uuid.UUID | None = None
    sale_uom_id: uuid.UUID | None = None

    hsn_code: Annotated[str, Field(max_length=16)] | None = None
    gst_rate: GstRateField = Decimal("0")

    standard_cost: MoneyField = Decimal("0")
    sale_price: PriceField = Decimal("0")
    dealer_price: PriceField = Decimal("0")

    finish_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    colour_id: uuid.UUID | None = None

    production_type: ProductionType = ProductionType.BUY
    lead_time_days: Annotated[int, Field(ge=0, le=3650)] = 0
    reorder_level: QuantityField = Decimal("0")
    reorder_quantity: QuantityField = Decimal("0")
    min_order_quantity: QuantityField = Decimal("0")
    safety_stock: QuantityField = Decimal("0")
    shelf_life_days: Annotated[int, Field(gt=0)] | None = None

    is_batch_tracked: bool = False
    is_serial_tracked: bool = False
    default_warehouse_id: uuid.UUID | None = None
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_code(value)


class ItemUpdate(CapabilityFlags):
    """Every field optional; only what is present is written."""

    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    description: str | None = None
    item_group: ItemGroup | None = None
    category_id: uuid.UUID | None = None

    base_uom_id: uuid.UUID | None = None
    purchase_uom_id: uuid.UUID | None = None
    sale_uom_id: uuid.UUID | None = None

    hsn_code: Annotated[str, Field(max_length=16)] | None = None
    gst_rate: GstRateField | None = None

    standard_cost: MoneyField | None = None
    sale_price: PriceField | None = None
    dealer_price: PriceField | None = None

    finish_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    colour_id: uuid.UUID | None = None

    production_type: ProductionType | None = None
    lead_time_days: Annotated[int, Field(ge=0, le=3650)] | None = None
    reorder_level: QuantityField | None = None
    reorder_quantity: QuantityField | None = None
    min_order_quantity: QuantityField | None = None
    safety_stock: QuantityField | None = None
    shelf_life_days: Annotated[int, Field(gt=0)] | None = None

    is_batch_tracked: bool | None = None
    is_serial_tracked: bool | None = None
    default_warehouse_id: uuid.UUID | None = None


class ItemSummary(_Base):
    """List row. Deliberately narrow - a list of 200 items must not ship the
    whole master record, and it must not ship cost."""

    id: uuid.UUID
    code: str
    name: str
    item_group: ItemGroup
    category_id: uuid.UUID | None
    base_uom_id: uuid.UUID
    production_type: ProductionType
    sale_price: Decimal
    is_active: bool
    can_purchase: bool
    can_manufacture: bool
    can_sell: bool
    can_be_bom_component: bool
    can_be_assembly_component: bool
    can_be_spare: bool
    can_job_work: bool


class ItemSummaryWithCost(ItemSummary):
    """List row for a principal holding ``item.cost_view``."""

    standard_cost: Decimal


class ItemDetail(_Base):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    item_group: ItemGroup
    category_id: uuid.UUID | None

    base_uom_id: uuid.UUID
    purchase_uom_id: uuid.UUID | None
    sale_uom_id: uuid.UUID | None

    hsn_code: str | None
    gst_rate: Decimal
    sale_price: Decimal
    dealer_price: Decimal

    finish_id: uuid.UUID | None
    size_id: uuid.UUID | None
    colour_id: uuid.UUID | None

    can_purchase: bool
    can_manufacture: bool
    can_sell: bool
    can_be_bom_component: bool
    can_be_assembly_component: bool
    can_be_spare: bool
    can_job_work: bool
    can_return: bool
    can_scrap: bool

    production_type: ProductionType
    lead_time_days: int
    reorder_level: Decimal
    reorder_quantity: Decimal
    min_order_quantity: Decimal
    safety_stock: Decimal
    shelf_life_days: int | None

    is_batch_tracked: bool
    is_serial_tracked: bool
    default_warehouse_id: uuid.UUID | None
    is_active: bool
    lifecycle_state: ItemLifecycleState

    created_at: datetime
    updated_at: datetime


class ItemDetailWithCost(ItemDetail):
    standard_cost: Decimal


class ItemStatusChange(_Base):
    """Activate/deactivate. The reason is mandatory - section 83 applies to
    master data lifecycle changes too, and "who turned this item off and why"
    is the first question asked when an order line stops validating."""

    is_active: bool
    reason: Annotated[str, Field(min_length=3, max_length=255)]


# -----------------------------------------------------------------------------
# Item search filters
# -----------------------------------------------------------------------------


class ItemFilters(BaseModel):
    """Server-side filter set for the item search.

    Every one of these narrows the SQL. Nothing is filtered in Python, because
    the point of the endpoint is to stay usable when the table has 50k rows.
    """

    search: str | None = None
    category_id: uuid.UUID | None = None
    include_subcategories: bool = True
    item_group: ItemGroup | None = None
    production_type: ProductionType | None = None
    is_active: bool | None = True

    can_purchase: bool | None = None
    can_manufacture: bool | None = None
    can_sell: bool | None = None
    can_be_bom_component: bool | None = None
    can_be_assembly_component: bool | None = None
    can_be_spare: bool | None = None
    can_job_work: bool | None = None

    is_batch_tracked: bool | None = None
    is_serial_tracked: bool | None = None
    default_warehouse_id: uuid.UUID | None = None

    @field_validator("search")
    @classmethod
    def _clean_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        term = value.strip()
        # A one-character term cannot use the trigram index (a trigram needs
        # three characters) and would degrade into a sequential scan of the
        # whole item master, so it is rejected rather than served slowly.
        if len(term) < 2:
            return None
        return term

    @property
    def capability_filters(self) -> dict[str, bool]:
        """Only the capability flags the caller actually constrained."""
        names = (
            "can_purchase",
            "can_manufacture",
            "can_sell",
            "can_be_bom_component",
            "can_be_assembly_component",
            "can_be_spare",
            "can_job_work",
        )
        return {name: getattr(self, name) for name in names if getattr(self, name) is not None}


def item_filters(
    search: Annotated[
        str | None, Query(max_length=64, description="Trigram search on code/name")
    ] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
    include_subcategories: Annotated[
        bool, Query(description="Include the whole category subtree")
    ] = True,
    item_group: Annotated[ItemGroup | None, Query()] = None,
    production_type: Annotated[ProductionType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = True,
    can_purchase: Annotated[bool | None, Query()] = None,
    can_manufacture: Annotated[bool | None, Query()] = None,
    can_sell: Annotated[bool | None, Query()] = None,
    can_be_bom_component: Annotated[bool | None, Query()] = None,
    can_be_assembly_component: Annotated[bool | None, Query()] = None,
    can_be_spare: Annotated[bool | None, Query()] = None,
    can_job_work: Annotated[bool | None, Query()] = None,
    is_batch_tracked: Annotated[bool | None, Query()] = None,
    is_serial_tracked: Annotated[bool | None, Query()] = None,
    default_warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ItemFilters:
    """FastAPI dependency assembling the item search filters."""
    return ItemFilters(
        search=search,
        category_id=category_id,
        include_subcategories=include_subcategories,
        item_group=item_group,
        production_type=production_type,
        is_active=is_active,
        can_purchase=can_purchase,
        can_manufacture=can_manufacture,
        can_sell=can_sell,
        can_be_bom_component=can_be_bom_component,
        can_be_assembly_component=can_be_assembly_component,
        can_be_spare=can_be_spare,
        can_job_work=can_job_work,
        is_batch_tracked=is_batch_tracked,
        is_serial_tracked=is_serial_tracked,
        default_warehouse_id=default_warehouse_id,
    )


# -----------------------------------------------------------------------------
# Variants
# -----------------------------------------------------------------------------


class VariantCreate(_Base):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    finish_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    colour_id: uuid.UUID | None = None
    price_delta: Annotated[Decimal, Field(max_digits=18, decimal_places=6)] = Decimal("0")
    cost_delta: Annotated[Decimal, Field(max_digits=18, decimal_places=4)] = Decimal("0")
    barcode: Annotated[str, Field(max_length=64)] | None = None
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_code(value)

    @model_validator(mode="after")
    def _at_least_one_attribute(self) -> VariantCreate:
        # A variant with no distinguishing attribute is a duplicate of its
        # parent item and would make the attribute-combination index useless.
        if not any((self.finish_id, self.size_id, self.colour_id)):
            raise ValueError("A variant must differ in at least one of finish, size or colour.")
        return self


class VariantUpdate(_Base):
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    finish_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    colour_id: uuid.UUID | None = None
    price_delta: Annotated[Decimal, Field(max_digits=18, decimal_places=6)] | None = None
    cost_delta: Annotated[Decimal, Field(max_digits=18, decimal_places=4)] | None = None
    barcode: Annotated[str, Field(max_length=64)] | None = None
    is_active: bool | None = None


class VariantOut(_Base):
    id: uuid.UUID
    item_id: uuid.UUID
    code: str
    name: str
    finish_id: uuid.UUID | None
    size_id: uuid.UUID | None
    colour_id: uuid.UUID | None
    price_delta: Decimal
    cost_delta: Decimal
    barcode: str | None
    is_active: bool


# -----------------------------------------------------------------------------
# Prices
# -----------------------------------------------------------------------------


class PriceCreate(_Base):
    variant_id: uuid.UUID | None = None
    price_type: PriceType
    currency: Annotated[
        str, Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    ] = DEFAULT_CURRENCY
    price: PriceField
    min_quantity: QuantityField = Decimal("0")
    valid_from: date
    valid_to: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _window_ordered(self) -> PriceCreate:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not be earlier than valid_from.")
        return self


class PriceUpdate(_Base):
    price: PriceField | None = None
    min_quantity: QuantityField | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None
    notes: str | None = None


class PriceOut(_Base):
    id: uuid.UUID
    item_id: uuid.UUID
    variant_id: uuid.UUID | None
    price_type: PriceType
    currency: str
    price: Decimal
    min_quantity: Decimal
    valid_from: date
    valid_to: date | None
    is_active: bool
    notes: str | None


class ResolvedPrice(_Base):
    """What the item actually costs a given channel at a given quantity."""

    item_id: uuid.UUID
    variant_id: uuid.UUID | None
    price_type: PriceType
    quantity: Decimal
    on_date: date
    price: Decimal
    currency: str
    #: ``PRICE_LIST`` when a slab matched, ``ITEM_MASTER`` when the fallback on
    #: the item record was used. The caller needs to know which, because only
    #: one of them is auditable to a dated price list.
    source: str
    price_id: uuid.UUID | None
    min_quantity: Decimal


# -----------------------------------------------------------------------------
# Vendor links
# -----------------------------------------------------------------------------


class VendorLinkCreate(_Base):
    vendor_id: uuid.UUID
    vendor_item_code: Annotated[str, Field(max_length=64)] | None = None
    vendor_item_name: Annotated[str, Field(max_length=255)] | None = None
    is_preferred: bool = False
    lead_time_days: Annotated[int, Field(ge=0, le=3650)] = 0
    min_order_quantity: QuantityField = Decimal("0")
    order_multiple: QuantityField = Decimal("0")
    last_purchase_price: PriceField | None = None
    last_purchase_date: date | None = None
    currency: Annotated[
        str, Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    ] = DEFAULT_CURRENCY


class VendorLinkUpdate(_Base):
    vendor_item_code: Annotated[str, Field(max_length=64)] | None = None
    vendor_item_name: Annotated[str, Field(max_length=255)] | None = None
    is_preferred: bool | None = None
    lead_time_days: Annotated[int, Field(ge=0, le=3650)] | None = None
    min_order_quantity: QuantityField | None = None
    order_multiple: QuantityField | None = None
    is_active: bool | None = None


class VendorLinkOut(_Base):
    id: uuid.UUID
    item_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_item_code: str | None
    vendor_item_name: str | None
    is_preferred: bool
    lead_time_days: int
    min_order_quantity: Decimal
    order_multiple: Decimal
    last_purchase_price: Decimal | None
    last_purchase_date: date | None
    currency: str
    is_active: bool


__all__ = [
    "CapabilityFlags",
    "CategoryCreate",
    "CategoryOut",
    "CategoryUpdate",
    "ConversionResult",
    "ItemCreate",
    "ItemDetail",
    "ItemDetailWithCost",
    "ItemFilters",
    "ItemStatusChange",
    "ItemSummary",
    "ItemSummaryWithCost",
    "ItemUpdate",
    "PriceCreate",
    "PriceOut",
    "PriceUpdate",
    "ResolvedPrice",
    "UomConversionCreate",
    "UomConversionOut",
    "UomCreate",
    "UomOut",
    "UomUpdate",
    "VariantCreate",
    "VariantOut",
    "VariantUpdate",
    "VendorLinkCreate",
    "VendorLinkOut",
    "VendorLinkUpdate",
    "item_filters",
]
