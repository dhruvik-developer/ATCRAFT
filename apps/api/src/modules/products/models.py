"""Item master - the table every other module joins to (master rules section 1).

Design decisions worth stating, because they are the ones that go wrong in ERP
schemas and are expensive to undo later:

**Capabilities are flags, not a type.** A single physical part is routinely
sold, purchased, consumed in a BOM, sent for job work and sold again as a spare.
Modelling that as one ``item_type`` discriminator forces a duplicate item master
the first time reality disagrees with the enum. ``items`` therefore carries nine
independent booleans, and every business rule reads the relevant flag.
``item_group`` exists for reporting and for seeding sensible defaults on
creation - no rule anywhere may read it to decide whether an operation is
permitted (section 1 / database.md section 3).

**Categories materialise their path.** ``item_categories.path`` holds the
slash-separated chain of ancestor ids ending with the row's own id, so "every
item under Hardware" is one indexed ``LIKE 'prefix%'`` predicate instead of a
recursive CTE per request. The cost is that a re-parent has to rewrite the
subtree's paths - a single UPDATE, and re-parenting is rare.

**UoM conversion is a table, not a constant.** Factors live in
``uom_conversions`` and are resolved through the base unit of the measurement
category, so a purchase in BOX and an issue in NOS reconcile against one stored
quantity.

**Search is PostgreSQL-first** (section 62): ``items.code`` and ``items.name``
carry GIN trigram indexes so an ``ILIKE '%term%'`` over a 50k-row item master is
an index scan, not a sequential one. The migration that creates these must run
``CREATE EXTENSION IF NOT EXISTS pg_trgm`` first.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.core.models import TenantMasterEntity
from src.core.types import GUID, Money, Quantity, UnitPrice

# A conversion factor is neither money nor a quantity: it is a ratio that has to
# survive round-tripping (1 BOX = 12 NOS, 1 NOS = 0.083333... BOX). Eight places
# keep the inverse of a realistic factor lossless enough that converting there
# and back does not drift a whole unit on a large receipt.
ConversionFactor = Numeric(18, 8)

#: Path materialisation bounds the category tree. Eight levels is far deeper
#: than any real product classification, and the limit is what keeps ``path``
#: inside its column width.
MAX_CATEGORY_DEPTH = 8

#: ``/<uuid>/`` per level, 37 characters each, plus headroom.
CATEGORY_PATH_LENGTH = 512


class ItemGroup(StrEnum):
    """Reporting classification only.

    Master rules section 1: no business rule may branch on this value to decide
    whether an operation is allowed. It drives reports, default capability flags
    at creation time, and nothing else.
    """

    RAW_MATERIAL = "RAW_MATERIAL"
    COMPONENT = "COMPONENT"
    SEMI_FINISHED = "SEMI_FINISHED"
    FINISHED_GOOD = "FINISHED_GOOD"
    CONSUMABLE = "CONSUMABLE"
    PACKING_MATERIAL = "PACKING_MATERIAL"
    SPARE = "SPARE"
    SCRAP = "SCRAP"
    SERVICE = "SERVICE"


class ProductionType(StrEnum):
    """How the item is normally obtained. Drives MRP suggestion type."""

    MAKE = "MAKE"
    BUY = "BUY"
    MAKE_OR_BUY = "MAKE_OR_BUY"
    JOB_WORK = "JOB_WORK"


class PriceType(StrEnum):
    """Which commercial channel a price list row applies to."""

    SALE = "SALE"
    DEALER = "DEALER"
    DISTRIBUTOR = "DISTRIBUTOR"


class UomCategory(StrEnum):
    """Measurement family. Conversions only exist inside one family.

    KG -> NOS is not a unit conversion, it is an item-specific weight, so the
    conversion resolver refuses it rather than inventing a factor.
    """

    COUNT = "COUNT"
    WEIGHT = "WEIGHT"
    LENGTH = "LENGTH"
    AREA = "AREA"
    VOLUME = "VOLUME"
    TIME = "TIME"


class ItemLifecycleState(StrEnum):
    """Derived, not stored.

    ``items`` has no status column in the contract - it has ``is_active`` and
    ``deleted_at``. Section 73 still requires status changes to go through a
    state machine, so the two columns are projected onto this vocabulary and the
    machine in ``service.py`` arbitrates every activation, deactivation and
    retirement.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


class ItemCategory(TenantMasterEntity):
    """Hierarchical item classification with a materialised ancestor path."""

    __tablename__ = "item_categories"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    #: ``/<root id>/.../<own id>/`` - always starts and ends with a separator so
    #: ``path LIKE parent.path || '%'`` cannot match a sibling whose id happens
    #: to share a prefix.
    path: Mapped[str] = mapped_column(String(CATEGORY_PATH_LENGTH), nullable=False)
    #: Root categories are depth 0. Denormalised from ``path`` so "top-level
    #: categories only" does not need a string operation.
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_item_categories_company_id_code"),
        # varchar_pattern_ops is what makes the subtree predicate
        # (path LIKE '/a/b/%') an index range scan under any collation; the
        # default operator class only supports it under the C locale.
        Index(
            "ix_item_categories_path",
            "company_id",
            "path",
            postgresql_ops={"path": "varchar_pattern_ops"},
        ),
        Index("ix_item_categories_parent", "company_id", "parent_id"),
        CheckConstraint(f"depth >= 0 AND depth < {MAX_CATEGORY_DEPTH}", name="depth_bounded"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
    )


class UnitOfMeasure(TenantMasterEntity):
    """A unit quantities are expressed in."""

    __tablename__ = "units_of_measure"

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    uom_category: Mapped[UomCategory] = mapped_column(String(16), nullable=False)

    #: Exactly one unit per (company, category) is the base. Every other unit in
    #: the family converts through it, which keeps the conversion table linear
    #: in the number of units instead of quadratic.
    is_base: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Rounding scale for display and for quantity validation. A unit counted in
    #: pieces must not accept 1.5.
    decimal_places: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_units_of_measure_company_id_code"),
        # Partial unique index rather than a constraint: only the base rows must
        # be unique, and only while the unit is live.
        Index(
            "uq_units_of_measure_one_base_per_category",
            "company_id",
            "uom_category",
            unique=True,
            postgresql_where=text("is_base AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "decimal_places >= 0 AND decimal_places <= 6", name="decimal_places_bounded"
        ),
    )


class UomConversion(TenantMasterEntity):
    """``1 from_uom = factor to_uom``.

    Stored one-directional; the resolver inverts a row when it needs the other
    direction, so an administrator never has to enter the same relationship
    twice and the two halves cannot drift apart.
    """

    __tablename__ = "uom_conversions"

    from_uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    to_uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    factor: Mapped[Decimal] = mapped_column(ConversionFactor, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "from_uom_id", "to_uom_id", name="uq_uom_conversions_company_id_from_to"
        ),
        # A zero or negative factor would silently destroy stock on the first
        # conversion, so it is rejected by the database and not only by the API.
        CheckConstraint("factor > 0", name="factor_positive"),
        CheckConstraint("from_uom_id <> to_uom_id", name="from_differs_from_to"),
    )


class Item(TenantMasterEntity):
    """The item master. One row per thing the company buys, makes, sells or ships."""

    __tablename__ = "items"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    item_group: Mapped[ItemGroup] = mapped_column(String(24), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # --- units --------------------------------------------------------------
    # Stock is always held in base_uom. purchase_uom and sale_uom are the units
    # the outside world uses; the service refuses to save an item whose
    # purchase/sale unit cannot be converted to its base unit.
    base_uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purchase_uom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )
    sale_uom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )

    # --- statutory ----------------------------------------------------------
    hsn_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gst_rate: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- money --------------------------------------------------------------
    # standard_cost is commercially sensitive: it is only serialised for a
    # principal holding Permission.ITEM_COST_VIEW (see service.item_detail).
    standard_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    sale_price: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )
    dealer_price: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- physical attributes -------------------------------------------------
    # Denormalised onto the item as the *default* combination; genuinely
    # variant-bearing items carry their combinations in item_variants.
    finish: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    colour: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- capability flags: master rules section 1 ----------------------------
    # Independent on purpose. Every one of these is read by a different module,
    # and none of them may be inferred from item_group.
    can_purchase: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_manufacture: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_sell: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_be_bom_component: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_be_assembly_component: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_be_spare: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_job_work: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_return: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    can_scrap: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- planning ------------------------------------------------------------
    production_type: Mapped[ProductionType] = mapped_column(
        String(16), nullable=False, default=ProductionType.BUY, server_default=ProductionType.BUY
    )
    lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reorder_level: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    reorder_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    min_order_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    safety_stock: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- traceability --------------------------------------------------------
    # Both may be true: a batch of castings can also be serial numbered.
    is_batch_tracked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_serial_tracked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    default_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    # NOTE: there is deliberately no ``valuation_method`` column. Stock is
    # valued FIFO, for every item, by ``inventory.service`` - see
    # ``docs/assumptions.md`` and revision ``fdb4d5a8c211``. A per-item column
    # was carried briefly and removed: it described a choice the engine did not
    # honour, and a schema field that lies is worse than an absent one. If
    # per-item methods are ever wanted, the column comes back WITH the costing
    # dispatch that reads it, not before.

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_items_company_id_code"),
        # Section 62: trigram GIN so "%grip%" over 50k items is an index scan.
        # Two single-column indexes rather than one composite: the search
        # predicate is `code ILIKE ... OR name ILIKE ...`, which PostgreSQL
        # satisfies with a BitmapOr over both.
        Index(
            "ix_items_code_trgm",
            "code",
            postgresql_using="gin",
            postgresql_ops={"code": "gin_trgm_ops"},
        ),
        Index(
            "ix_items_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # The default browse: a category subtree filtered to live items.
        Index(
            "ix_items_company_category_active",
            "company_id",
            "category_id",
            "is_active",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Capability-scoped pickers (sales order line, BOM component chooser)
        # are the highest-traffic reads of this table. Partial indexes keep them
        # small - a company with 50k items typically sells a few thousand.
        Index(
            "ix_items_sellable",
            "company_id",
            "code",
            postgresql_where=text("can_sell AND is_active AND deleted_at IS NULL"),
        ),
        Index(
            "ix_items_purchasable",
            "company_id",
            "code",
            postgresql_where=text("can_purchase AND is_active AND deleted_at IS NULL"),
        ),
        Index(
            "ix_items_bom_components",
            "company_id",
            "code",
            postgresql_where=text("can_be_bom_component AND is_active AND deleted_at IS NULL"),
        ),
        # MRP reorder sweep: which live items are below their reorder level.
        Index(
            "ix_items_reorder",
            "company_id",
            "reorder_level",
            postgresql_where=text("reorder_level > 0 AND is_active AND deleted_at IS NULL"),
        ),
        CheckConstraint("gst_rate >= 0 AND gst_rate <= 100", name="gst_rate_percentage"),
        CheckConstraint("standard_cost >= 0", name="standard_cost_non_negative"),
        CheckConstraint("sale_price >= 0", name="sale_price_non_negative"),
        CheckConstraint("dealer_price >= 0", name="dealer_price_non_negative"),
        CheckConstraint("lead_time_days >= 0", name="lead_time_non_negative"),
        CheckConstraint("reorder_level >= 0", name="reorder_level_non_negative"),
        CheckConstraint("reorder_quantity >= 0", name="reorder_quantity_non_negative"),
        CheckConstraint("min_order_quantity >= 0", name="min_order_quantity_non_negative"),
        CheckConstraint("safety_stock >= 0", name="safety_stock_non_negative"),
        CheckConstraint(
            "shelf_life_days IS NULL OR shelf_life_days > 0", name="shelf_life_positive"
        ),
        # An expiring item that is not batch tracked has no way to know what
        # expires when, so the combination is rejected outright.
        CheckConstraint(
            "shelf_life_days IS NULL OR is_batch_tracked", name="shelf_life_requires_batch"
        ),
    )


class ItemVariant(TenantMasterEntity):
    """A concrete finish/size/colour combination of an item.

    Variants are deliberately *not* separate items: they share the item's BOM,
    planning parameters and capability flags, and differ only in identity and
    price. Anything that needs its own BOM is a new item, not a variant.
    """

    __tablename__ = "item_variants"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    finish: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    colour: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Deltas rather than absolute prices: a price revision on the parent item
    # then applies to every variant without touching 200 variant rows.
    price_delta: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )
    cost_delta: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_item_variants_company_id_code"),
        # COALESCE, because NULL <> NULL would let "no finish, no size, red" be
        # entered twice - the classic nullable-unique hole.
        Index(
            "uq_item_variants_attribute_combo",
            "item_id",
            text("coalesce(finish, '')"),
            text("coalesce(size, '')"),
            text("coalesce(colour, '')"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_item_variants_item_active",
            "company_id",
            "item_id",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
        Index(
            "ix_item_variants_code_trgm",
            "code",
            postgresql_using="gin",
            postgresql_ops={"code": "gin_trgm_ops"},
        ),
    )


class ItemPrice(TenantMasterEntity):
    """One price-list row: a price for a channel, a quantity slab and a period."""

    __tablename__ = "item_prices"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL means "applies to every variant of the item".
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_variants.id", ondelete="CASCADE"), nullable=True, index=True
    )

    price_type: Mapped[PriceType] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)

    #: Lower bound of the slab. A row with 0 is the base price; a row with 100
    #: applies from 100 units up. The resolver picks the highest slab the
    #: ordered quantity reaches.
    min_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means open-ended.
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Covers the resolver's access path: item + channel, newest window first.
        Index(
            "ix_item_prices_lookup",
            "company_id",
            "item_id",
            "price_type",
            "valid_from",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("min_quantity >= 0", name="min_quantity_non_negative"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="validity_window_ordered"
        ),
    )


class ItemVendorLink(TenantMasterEntity):
    """Which vendors supply this item, and on what commercial terms.

    Owned by ``products`` rather than ``vendors`` because the interesting
    queries are item-first: procurement asks "who supplies this part and how
    long do they take?", and MRP reads ``lead_time_days`` per item-vendor pair
    when it turns a shortage into a suggested order date.
    """

    __tablename__ = "item_vendor_links"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: The vendor's own part number - printed on the PO so their order desk can
    #: match it without a phone call.
    vendor_item_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_preferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    min_order_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Vendors that ship in cartons of 50 cannot supply 51.
    order_multiple: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # Written by goods receipt / vendor invoice posting; read by procurement to
    # show price movement. Not a substitute for the purchase history itself.
    last_purchase_price: Mapped[Decimal | None] = mapped_column(UnitPrice, nullable=True)
    last_purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "item_id", "vendor_id", name="uq_item_vendor_links_company_id_item_vendor"
        ),
        # At most one preferred vendor per item, enforced by the database so a
        # concurrent "make this one preferred" cannot leave two.
        Index(
            "uq_item_vendor_links_one_preferred",
            "company_id",
            "item_id",
            unique=True,
            postgresql_where=text("is_preferred AND deleted_at IS NULL"),
        ),
        Index("ix_item_vendor_links_vendor", "company_id", "vendor_id"),
        CheckConstraint("lead_time_days >= 0", name="lead_time_non_negative"),
        CheckConstraint("min_order_quantity >= 0", name="min_order_quantity_non_negative"),
        CheckConstraint("order_multiple >= 0", name="order_multiple_non_negative"),
        CheckConstraint(
            "last_purchase_price IS NULL OR last_purchase_price >= 0",
            name="last_purchase_price_non_negative",
        ),
    )


__all__ = [
    "CATEGORY_PATH_LENGTH",
    "MAX_CATEGORY_DEPTH",
    "ConversionFactor",
    "Item",
    "ItemCategory",
    "ItemGroup",
    "ItemLifecycleState",
    "ItemPrice",
    "ItemVariant",
    "ItemVendorLink",
    "PriceType",
    "ProductionType",
    "UnitOfMeasure",
    "UomCategory",
    "UomConversion",
]
