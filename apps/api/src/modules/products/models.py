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
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY, MAX_SPECIFICATION_OPTIONS
from src.core.models import TenantEntity, TenantMasterEntity, sql_in_list
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

#: Where a manufacturer is assumed to be when nobody says otherwise - the same
#: default the vendor and customer address columns carry. ASSUMPTION: a default,
#: never a rule; docs/assumptions.md §40.
DEFAULT_MANUFACTURER_COUNTRY = "India"

#: Upper bound of a brand's presentation position, mirrored by a CHECK so a
#: value the schema would refuse cannot arrive by another route either. The same
#: bound ``ItemAttribute.sort_order`` takes at the API.
MAX_BRAND_SORT_ORDER = 9999


class ItemGroup(StrEnum):
    """Reporting classification only.

    Master rules section 1: no business rule may branch on this value to decide
    whether an operation is allowed. It drives reports, default capability flags
    at creation time, and nothing else.

    ``SUB_ASSEMBLY`` and ``ASSEMBLY`` classify what the assembly bench builds - a
    sub-assembly is consumed by a higher assembly or a production order, an
    assembly (a lock set) is sold as built. Like every other member they only
    seed defaults; an assembly order checks ``can_manufacture`` and
    ``can_be_assembly_component``, never this value. ``items.item_group`` has no
    CHECK, so adding a member needs no migration.
    """

    RAW_MATERIAL = "RAW_MATERIAL"
    COMPONENT = "COMPONENT"
    SEMI_FINISHED = "SEMI_FINISHED"
    SUB_ASSEMBLY = "SUB_ASSEMBLY"
    FINISHED_GOOD = "FINISHED_GOOD"
    ASSEMBLY = "ASSEMBLY"
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


#: Stand-in for NULL in the variant combination index below. The nil UUID is
#: not a valid id anywhere in the system, so COALESCE-ing to it makes "no
#: finish" one indexable value instead of an unenforceable NULL.
ATTRIBUTE_NULL_SENTINEL = "00000000-0000-0000-0000-000000000000"


class ItemAttributeType(StrEnum):
    """Which physical attribute a permitted value belongs to.

    The *kinds* are fixed in code because each one is a column on ``items`` and
    ``item_variants``; a fourth kind is a schema change either way. The *values*
    are the client's, which is the entire point of the table below - so nothing
    anywhere may branch on an ``ItemAttribute.code``.
    """

    FINISH = "FINISH"
    SIZE = "SIZE"
    COLOUR = "COLOUR"


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


class ItemAttribute(TenantMasterEntity):
    """A permitted finish, size or colour - the client's own vocabulary.

    Before this table the three attributes were free text on the item, so
    "Brushed", "brushed" and "Brushed Steel" were three different finishes and
    no report could group by finish. Making them rows rather than strings is
    what makes them governable: a rename reaches every item at once, because
    items reference the row and not the spelling.

    One table with a discriminator rather than three tables. The row shape,
    the dialog and the screen are identical for all three kinds, and the same
    choice is already made for item prices, which discriminate on
    ``price_type``.

    Nothing in the application may branch on a value here. That is the property
    that makes the list safe to hand to an administrator, and it is the reason
    this is a master table rather than an enum.
    """

    __tablename__ = "item_attributes"

    attribute: Mapped[ItemAttributeType] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Presentation order. Sizes are the reason this exists: "12mm, 25mm, 50mm"
    #: is the order a person expects and is not the order they sort in.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        # Per kind, not globally: a finish and a colour may both be called
        # "BLACK" without either being a duplicate.
        UniqueConstraint(
            "company_id",
            "attribute",
            "code",
            name="uq_item_attributes_company_id_attribute_code",
        ),
        CheckConstraint(
            f"attribute IN ({sql_in_list(ItemAttributeType)})",
            name="attribute_known",
        ),
        # The picker's query: one kind, live rows, in presentation order.
        Index(
            "ix_item_attributes_company_attribute",
            "company_id",
            "attribute",
            "sort_order",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
    )


class Manufacturer(TenantMasterEntity):
    """Who actually makes a part - which is not always who sells it to us.

    A cylinder made by one company is routinely bought through a distributor,
    so a manufacturer is deliberately *not* a vendor: vendors carry payables,
    approval and a portal login, and a maker carries none of that. The item
    records its maker (and the maker's part number) for traceability; the
    item-vendor link records which maker a particular source supplies.

    Like the brand below, this is client-editable classification. Nothing in the
    application may branch on a manufacturer - not on its code, not on its name,
    not on its country. See ``docs/database.md`` section 3, "Brands and
    manufacturers".
    """

    __tablename__ = "manufacturers"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The registered name, when it differs from the name the trade uses.
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default=DEFAULT_MANUFACTURER_COUNTRY,
        server_default=DEFAULT_MANUFACTURER_COUNTRY,
    )
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_manufacturers_company_id_code"),
        # The picker's query: live makers, alphabetically.
        Index(
            "ix_manufacturers_company_active",
            "company_id",
            "name",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
    )


class Brand(TenantMasterEntity):
    """The name a product is sold under - ATCRAFT, a premium line, a partner's.

    Separate from the item attributes on purpose: a brand is not a physical
    property and does not vary by finish, so it lives on the item and never on a
    variant. It may name the manufacturer that owns it; at most one, because a
    brand with two owners is a licensing arrangement, not a master-data fact.

    Nothing may branch on a brand. A dealer catalogue may *filter* by it - that
    is what ``ix_items_company_brand`` exists for - but no price, permission or
    posting rule may read it to decide anything.
    """

    __tablename__ = "brands"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The brand owner. RESTRICT, so a maker cannot be removed from under the
    #: brands it owns - and the service refuses to even retire one while a live
    #: brand still names it. ASSUMPTION: at most one owner per brand, recorded
    #: for reference only (docs/assumptions.md §40).
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("manufacturers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Presentation order on pickers and in the catalogue: the house brand first,
    #: whatever its code sorts as.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_brands_company_id_code"),
        CheckConstraint(
            f"sort_order >= 0 AND sort_order <= {MAX_BRAND_SORT_ORDER}",
            name="sort_order_bounded",
        ),
        # The picker's query: live brands, in presentation order.
        Index(
            "ix_brands_company_active",
            "company_id",
            "sort_order",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
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
    # RESTRICT rather than SET NULL: a finish that items still use must be
    # retired (soft-deleted, so history still resolves), never removed out
    # from under them. See ItemAttribute.
    finish_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    size_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    colour_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # --- brand and maker -----------------------------------------------------
    # Classification and traceability only - nothing branches on either (see
    # Brand). RESTRICT for the same reason as the attributes above: a brand that
    # items carry is retired, never removed out from under them.
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("manufacturers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: The maker's own part number, as free text - every maker numbers parts its
    #: own way, so nothing here may parse it. ASSUMPTION: not unique and not
    #: normalised beyond trimming (docs/assumptions.md §40).
    manufacturer_part_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    #: Output of this item is held for production QC: a production entry books
    #: good output into QC_HOLD and raises an in-process inspection, and the
    #: order cannot complete while any of it is held (docs/assumptions.md §E5-1).
    requires_production_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Packing a line of this item raises a FINAL inspection, and the packing
    #: order cannot complete or hand over until it is accepted (§E5-8).
    requires_final_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
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
    default_location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    image_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

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
        # Browsing by brand - the item list's brand filter today, the dealer
        # catalogue tomorrow - and the brand's own "still in use?" probe.
        Index(
            "ix_items_company_brand",
            "company_id",
            "brand_id",
            postgresql_where=text("deleted_at IS NULL"),
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

    finish_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    size_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    colour_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_attributes.id", ondelete="RESTRICT"), nullable=True, index=True
    )

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
        # entered twice - the classic nullable-unique hole. The sentinel is the
        # nil UUID rather than '' now that these are foreign keys: coalesce on a
        # uuid column needs a uuid, and the nil UUID is not a valid id anywhere
        # in the system so it cannot collide with a real attribute row. Same
        # device, same reasoning, as GRAIN_NULL_SENTINEL in inventory/models.py.
        Index(
            "uq_item_variants_attribute_combo",
            "item_id",
            text(f"coalesce(finish_id, '{ATTRIBUTE_NULL_SENTINEL}'::uuid)"),
            text(f"coalesce(size_id, '{ATTRIBUTE_NULL_SENTINEL}'::uuid)"),
            text(f"coalesce(colour_id, '{ATTRIBUTE_NULL_SENTINEL}'::uuid)"),
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

    #: Who makes what this source supplies, and the maker's part number for it.
    #: Per link rather than only per item: a distributor and the maker's own
    #: depot may both supply "the same" cylinder, and only the link knows which
    #: maker's part arrives from which source. Traceability only - nothing
    #: branches on it.
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("manufacturers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    manufacturer_part_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

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


class SpecificationValueType(StrEnum):
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    OPTION = "OPTION"


class AccessoryRelation(StrEnum):
    OPTIONAL = "OPTIONAL"
    INCLUDED = "INCLUDED"


class SpecificationAttribute(TenantMasterEntity):
    """Governed descriptive vocabulary; no operational rule branches on its value."""

    __tablename__ = "specification_attributes"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    value_type: Mapped[SpecificationValueType] = mapped_column(String(16), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    options: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    is_filterable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_specification_attributes_company_code"),
        UniqueConstraint(
            "company_id", "id", "value_type", name="uq_specification_attributes_typed_identity"
        ),
        CheckConstraint(
            f"value_type IN ({sql_in_list(SpecificationValueType)})", name="value_type_valid"
        ),
        CheckConstraint(
            "(value_type = 'OPTION' AND cardinality(options) > 0 "
            f"AND cardinality(options) <= {MAX_SPECIFICATION_OPTIONS} "
            "AND array_position(options, NULL) IS NULL) "
            "OR (value_type <> 'OPTION' AND cardinality(options) = 0)",
            name="options_match_type",
        ),
        CheckConstraint("sort_order BETWEEN 0 AND 9999", name="sort_order_bounded"),
        Index("ix_specification_attributes_picker", "company_id", "is_active", "sort_order"),
    )


class CategorySpecification(TenantEntity):
    __tablename__ = "category_specifications"

    category_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=False
    )
    # Named explicitly: the fk convention would render 78 bytes, past PostgreSQL's 63.
    specification_attribute_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey(
            "specification_attributes.id",
            ondelete="RESTRICT",
            name="fk_category_specifications_attribute",
        ),
        nullable=False,
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint(
            "category_id", "specification_attribute_id", name="uq_category_specifications_attribute"
        ),
        Index("ix_category_specifications_company_category", "company_id", "category_id"),
        Index("ix_category_specifications_attribute", "company_id", "specification_attribute_id"),
        CheckConstraint("sort_order BETWEEN 0 AND 9999", name="sort_order_bounded"),
    )


class ItemSpecification(TenantEntity):
    __tablename__ = "item_specifications"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    specification_attribute_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    # The discriminator is server-owned and tied to the master by this composite
    # foreign key. A client cannot bypass the typed CHECK by inventing a type.
    value_type: Mapped[SpecificationValueType] = mapped_column(String(16), nullable=False)
    value_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    value_number: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    value_bool: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_option: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "specification_attribute_id", "value_type"],
            [
                "specification_attributes.company_id",
                "specification_attributes.id",
                "specification_attributes.value_type",
            ],
            name="fk_item_specifications_typed_attribute",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "item_id", "specification_attribute_id", name="uq_item_specifications_attribute"
        ),
        CheckConstraint(
            "(value_type = 'TEXT' AND value_text IS NOT NULL AND length(btrim(value_text)) > 0 "
            "AND value_number IS NULL AND value_bool IS NULL AND value_option IS NULL) OR "
            "(value_type = 'NUMBER' AND value_number IS NOT NULL "
            "AND value_number <> 'NaN'::numeric "
            "AND value_text IS NULL AND value_bool IS NULL AND value_option IS NULL) OR "
            "(value_type = 'BOOLEAN' AND value_bool IS NOT NULL AND value_text IS NULL "
            "AND value_number IS NULL AND value_option IS NULL) OR "
            "(value_type = 'OPTION' AND value_option IS NOT NULL "
            "AND length(btrim(value_option)) > 0 "
            "AND value_text IS NULL AND value_number IS NULL AND value_bool IS NULL)",
            name="exactly_one_typed_value",
        ),
        Index("ix_item_specifications_company_item", "company_id", "item_id"),
        Index(
            "ix_item_specifications_option",
            "company_id",
            "specification_attribute_id",
            "value_option",
        ),
        Index(
            "ix_item_specifications_boolean",
            "company_id",
            "specification_attribute_id",
            "value_bool",
        ),
    )


class ItemAccessory(TenantEntity):
    """Informational association only; never a BOM, reservation or stock issue."""

    __tablename__ = "item_accessories"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    accessory_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("1"), server_default="1"
    )
    relation: Mapped[AccessoryRelation] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    __table_args__ = (
        UniqueConstraint("item_id", "accessory_item_id", name="uq_item_accessories_pair"),
        CheckConstraint("item_id <> accessory_item_id", name="not_self"),
        CheckConstraint("quantity > 0 AND quantity <> 'NaN'::numeric", name="quantity_positive"),
        CheckConstraint(f"relation IN ({sql_in_list(AccessoryRelation)})", name="relation_valid"),
        CheckConstraint("sort_order BETWEEN 0 AND 9999", name="sort_order_bounded"),
        Index("ix_item_accessories_company_item", "company_id", "item_id"),
        Index("ix_item_accessories_company_accessory", "company_id", "accessory_item_id"),
    )


__all__ = [
    "CATEGORY_PATH_LENGTH",
    "DEFAULT_MANUFACTURER_COUNTRY",
    "MAX_BRAND_SORT_ORDER",
    "MAX_CATEGORY_DEPTH",
    "AccessoryRelation",
    "Brand",
    "CategorySpecification",
    "ConversionFactor",
    "Item",
    "ItemAccessory",
    "ItemCategory",
    "ItemGroup",
    "ItemLifecycleState",
    "ItemPrice",
    "ItemSpecification",
    "ItemVariant",
    "ItemVendorLink",
    "Manufacturer",
    "PriceType",
    "ProductionType",
    "SpecificationAttribute",
    "SpecificationValueType",
    "UnitOfMeasure",
    "UomCategory",
    "UomConversion",
]
