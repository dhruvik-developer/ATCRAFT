"""Master data - the foundation every other demonstration stage resolves against.

Nothing in this stage is a document. It makes the *things* the documents will
later be about: somewhere for stock to live, a classification tree to file it
under, the items themselves, the prices they sell at, and the customers, vendors
and carriers the commercial documents are raised against.

What the later stages address, and by what code
-----------------------------------------------
Everything created *or found* here is registered, because a later stage cannot
tell the difference and must not have to:

======================  =====================================================
kind                    codes
======================  =====================================================
``uom``                 ``NOS``, ``KG``, ``SET``, ``PAIR``, ``LTR`` ...
``warehouse``           ``WH-RM``, ``WH-CMP``, ``WH-FG``, ``WH-QC`` ...
``location``            ``DM-LOC-RM-A``, ``DM-LOC-RM-A-01`` ...
``category``            the baseline codes (``RAW-BRASS``) plus ``DM-CAT-*``
``item``                ``DM-RM-BRASS``, ``DM-BO-SCREW-CSK``, ``DM-FG-*`` ...
``manufacturer``        ``DM-MFR-ATCRAFT``, ``DM-MFR-CYLINDER``, ``DM-MFR-SPRINGS``
``brand``               ``DM-BRAND-ATCRAFT``, ``DM-BRAND-ATCRAFT-PRO``, ``DM-BRAND-SECUREPIN``
``customer``            ``DM-CUST-*``, ``DM-DLR-*``, ``DM-DIST-WEST``
``vendor``              ``DM-VEN-BRASS``, ``DM-VEN-SS``, ``DM-VEN-FAST`` ...
``transporter``         ``DM-TRN-VRL``, ``DM-TRN-GATI``, ``DM-TRN-CITY``
``user``                ``DM-USER-BUYER``, ``DM-USER-SALES``, and the vendor
                        managers ``DM-USER-VEN-LOCKS``, ``-VEN-HANDLES``,
                        ``-VEN-PACKING``
``cost_centre``         ``DM-CC-PROD`` and its process children
                        (``DM-CC-PROD-DIE`` ... ``DM-CC-PROD-PACK``),
                        ``DM-CC-QC``, ``DM-CC-STORES``, ``DM-CC-MAINT``,
                        ``DM-CC-ADMIN``, ``DM-CC-SALES`` (+ ``-NORTH``,
                        ``-WEST``)
``department``          ``DM-DEPT-PROD``, ``DM-DEPT-QC`` ... each with its
                        default cost centre
======================  =====================================================

Units, warehouses and the baseline categories keep the codes
:mod:`src.core.seed` gave them. They exist before this file runs, and a
``DM-``-prefixed duplicate would put two raw material stores on every picker.
Units and warehouses are *also* registered under the ``DM-`` spelling a sibling
stage might reasonably guess at, pointing at the same row - an alias costs a
dictionary entry, a failed :meth:`DemoContext.id_of` costs a whole stage.

Why a second and third user exist
---------------------------------
:data:`src.modules.vendors.service.REQUIRE_SEPARATE_APPROVER` is on, so the
account that creates a vendor may never approve it - and an unapproved vendor
cannot receive a purchase order
(:func:`~src.modules.vendors.service.can_receive_purchase_orders`), which would
leave the whole procurement stage with nobody to buy from. So the demo hires a
buyer, creates the vendors as the buyer, and approves them as the administrator.
That is also how the maker-checker rule is meant to look on screen. The sales
executive exists for the same practical reason: ``assigned_sales_user_id`` on a
customer wants somebody other than the system administrator.

The three vendor managers are the proposal's own example of the category-wise
purchase order split - one per category at the vendors that supply them - so a
converted requisition has somebody at each vendor to hand each order to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.common.codes import normalise_code
from src.core.clock import today
from src.core.demo_data import DemoContext, find_by_code
from src.core.deps import TenantContext
from src.core.errors import AuthorizationError, BusinessRuleError, DuplicateError, NotFoundError
from src.core.seed import WAREHOUSES as BASELINE_WAREHOUSES
from src.modules.customers.models import (
    AddressType,
    Customer,
    CustomerAddress,
    CustomerContact,
    CustomerType,
)
from src.modules.customers.schemas import AddressCreate, ContactCreate, CustomerCreate
from src.modules.customers.service import CustomerService
from src.modules.organization.models import Branch, CostCentre, CostCentreType, Department
from src.modules.organization.schemas import CostCentreCreate, DepartmentCreate, DepartmentUpdate
from src.modules.organization.service import CostCentreService, DepartmentService
from src.modules.products.models import (
    Brand,
    Item,
    ItemAttribute,
    ItemAttributeType,
    ItemCategory,
    ItemGroup,
    ItemPrice,
    ItemVendorLink,
    Manufacturer,
    PriceType,
    ProductionType,
    UnitOfMeasure,
)
from src.modules.products.repository import ItemAttributeRepository, ItemRepository
from src.modules.products.schemas import (
    BrandCreate,
    CategoryCreate,
    ItemCreate,
    ItemUpdate,
    ManufacturerCreate,
    PriceCreate,
    VendorLinkCreate,
    VendorLinkUpdate,
)
from src.modules.products.service import (
    create_brand,
    create_category,
    create_item,
    create_manufacturer,
    create_price,
    link_vendor,
    update_item,
    update_vendor_link,
)
from src.modules.users.models import Role, User
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService, generate_temporary_password
from src.modules.vendors.models import (
    Transporter,
    TransporterType,
    Vendor,
    VendorCategory,
    VendorContact,
    VendorContactRole,
    VendorType,
)
from src.modules.vendors.schemas import (
    TransporterCreate,
    VendorCategoryAssign,
    VendorContactCreate,
    VendorCreate,
)
from src.modules.vendors.service import (
    TransporterService,
    VendorCategoryService,
    VendorContactService,
    VendorService,
)
from src.modules.warehouses.models import (
    StorageLocation,
    StorageLocationType,
    Warehouse,
    WarehouseType,
)
from src.modules.warehouses.schemas import StorageLocationCreate, WarehouseCreate
from src.modules.warehouses.service import StorageLocationService, WarehouseService

STAGE = "masters"

# --- dates --------------------------------------------------------------------
# Every date in this file is an offset from one anchor, taken once at the top of
# `seed`. Nothing calls the clock again: two rows that should share a date must
# share it exactly, and a second reading of "today" across a midnight boundary is
# how a re-run stops being idempotent.
#
# Price lists start well in the past so `resolve_price` finds a live slab for any
# order date a later stage chooses, and are left open-ended so they never lapse
# while the demo database sits unused.
PRICE_START_DAYS_AGO = 180
#: The last purchase recorded on a vendor link. Recent enough to look live on the
#: item's Vendors tab, old enough that it is plainly history and not this week.
LAST_PURCHASE_DAYS_AGO = 45


# --- items --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ItemSpec:
    """One row of the item master.

    Quantities and money are strings, converted to :class:`Decimal` at the point
    of use. A literal like ``612.50`` in this table would be a float, and a float
    anywhere near a standard cost is a defect that survives all the way to a
    valuation report.
    """

    code: tuple[str, ...]
    name: str
    group: ItemGroup
    category: str
    base_uom: str
    hsn: str
    standard_cost: str
    production_type: ProductionType = ProductionType.BUY
    warehouse: str = "WH-RM"
    sale_price: str = "0"
    dealer_price: str = "0"
    reorder_level: str = "0"
    reorder_quantity: str = "0"
    safety_stock: str = "0"
    min_order_quantity: str = "0"
    lead_time_days: int = 0
    gst_rate: str = "18"
    is_batch_tracked: bool = False
    is_serial_tracked: bool = False
    shelf_life_days: int | None = None
    finish: str | None = None
    size: str | None = None
    colour: str | None = None
    description: str | None = None
    #: Explicit capability override. ``None`` leaves the item group's default.
    can_manufacture: bool | None = None


#: The item master. Twenty rows, chosen so that every shape the rest of the
#: system has to handle exists at least once: metal bought by weight, components
#: bought by the thousand, a batch-tracked consumable, a serial-tracked finished
#: good, sub-assemblies that will hang under a BOM, and scrap coming back off the
#: floor. Costs are in rupees and are the ones an architectural hardware
#: manufacturer would recognise, so a cost roll-up produces a believable number
#: rather than an obviously invented one.
ITEMS: tuple[_ItemSpec, ...] = (
    # --- raw materials, all bought by weight --------------------------------
    # Reorder levels are set on the three metals and the three fastest-moving
    # bought-out parts, so the reorder report has rows on it without every item
    # in the company screaming for attention at once.
    _ItemSpec(
        code=("RM", "BRASS"),
        name="Brass Rod 12mm - IS 319 Free Cutting",
        description="Extruded free-cutting brass rod, the base stock for lever handles.",
        group=ItemGroup.RAW_MATERIAL,
        category="RAW-BRASS",
        base_uom="KG",
        hsn="74072110",
        standard_cost="612.50",
        reorder_level="500",
        reorder_quantity="1000",
        safety_stock="250",
        min_order_quantity="250",
        lead_time_days=10,
    ),
    _ItemSpec(
        code=("RM", "SS"),
        name="Stainless Steel Rod 10mm - SS 304",
        description="SS 304 bright bar for rose plates and hinge knuckles.",
        group=ItemGroup.RAW_MATERIAL,
        category="RAW-SS",
        base_uom="KG",
        hsn="72222011",
        standard_cost="285.00",
        reorder_level="300",
        reorder_quantity="750",
        safety_stock="150",
        min_order_quantity="150",
        lead_time_days=14,
    ),
    _ItemSpec(
        code=("RM", "ZINC"),
        name="Zinc Alloy Ingot ZA-5",
        description="Pressure die-casting alloy for lock bodies and back plates.",
        group=ItemGroup.RAW_MATERIAL,
        category="RAW-ZINC",
        base_uom="KG",
        hsn="79011200",
        standard_cost="268.75",
        reorder_level="400",
        reorder_quantity="1000",
        safety_stock="200",
        lead_time_days=12,
    ),
    # --- the batch-tracked item ---------------------------------------------
    # Lacquer is the natural batch item in this trade: it arrives in lots, the
    # lot decides the shade, and it goes off. `shelf_life_days` is only legal on
    # a batch-tracked item, which is exactly why it is set here - it puts the
    # expiry rules within reach of the demonstration data.
    _ItemSpec(
        code=("CN", "LACQUER"),
        name="Clear Lacquer - Air Drying",
        description="Air-drying clear lacquer, applied over plated hardware.",
        group=ItemGroup.CONSUMABLE,
        category="CONSUMABLE",
        base_uom="LTR",
        hsn="32089029",
        standard_cost="412.00",
        is_batch_tracked=True,
        shelf_life_days=365,
        lead_time_days=7,
    ),
    # --- bought-out components ----------------------------------------------
    _ItemSpec(
        code=("BO", "SCREW-CSK"),
        name="CSK Machine Screw M4 x 25 - SS 304",
        group=ItemGroup.COMPONENT,
        category="FASTENERS",
        base_uom="NOS",
        hsn="73181500",
        standard_cost="1.85",
        warehouse="WH-CMP",
        reorder_level="20000",
        reorder_quantity="50000",
        safety_stock="10000",
        min_order_quantity="5000",
        lead_time_days=7,
    ),
    _ItemSpec(
        code=("BO", "SPRING"),
        name="Torsion Spring 1.2mm - Lever Return",
        group=ItemGroup.COMPONENT,
        category="ACCESSORIES",
        base_uom="NOS",
        hsn="73201019",
        standard_cost="4.20",
        warehouse="WH-CMP",
        reorder_level="8000",
        reorder_quantity="20000",
        safety_stock="4000",
        lead_time_days=9,
    ),
    _ItemSpec(
        code=("BO", "CYL-60"),
        name="Pin Cylinder 60mm - Brass, 5 Pin",
        group=ItemGroup.COMPONENT,
        category="LOCK-CYL",
        base_uom="NOS",
        hsn="83013000",
        standard_cost="168.00",
        warehouse="WH-CMP",
        reorder_level="1200",
        reorder_quantity="3000",
        safety_stock="600",
        lead_time_days=15,
    ),
    # Bought in alongside the made versions when the plating line or the
    # foundry is the constraint. Two categories on one supplier is what the
    # category-wise purchase order split exists for: the procurement stage
    # raises one requisition for these and a carton, ready to split.
    _ItemSpec(
        code=("BO", "LOCKBODY"),
        name="Mortise Lock Body 6572 - Bought Out, Unplated",
        description="Die-cast zinc lock body, bought in when the in-house line is full.",
        group=ItemGroup.COMPONENT,
        category="LOCK-MORT",
        base_uom="NOS",
        hsn="83014090",
        standard_cost="188.00",
        warehouse="WH-CMP",
        lead_time_days=12,
    ),
    _ItemSpec(
        code=("BO", "HANDLE"),
        name="Lever Handle Blank - Zinc Die Cast",
        description="Unfinished lever blank, machined and plated in house.",
        group=ItemGroup.COMPONENT,
        category="HANDLE-LEVER",
        base_uom="NOS",
        hsn="83024110",
        standard_cost="98.00",
        warehouse="WH-CMP",
        lead_time_days=10,
    ),
    # --- packing -------------------------------------------------------------
    _ItemSpec(
        code=("PK", "BOX"),
        name="Printed Carton - Handle Set, 3 Ply",
        group=ItemGroup.PACKING_MATERIAL,
        category="PACKING",
        base_uom="NOS",
        hsn="48191010",
        standard_cost="14.60",
        warehouse="WH-PKG",
        # Cartons attract 12% GST, not 18%. One item on a different rate means a
        # tax summary with more than one line on it.
        gst_rate="12",
        lead_time_days=8,
    ),
    _ItemSpec(
        code=("PK", "POLYBAG"),
        name="LDPE Poly Bag 150 x 250mm",
        group=ItemGroup.PACKING_MATERIAL,
        category="PACKING",
        base_uom="NOS",
        hsn="39232990",
        standard_cost="0.95",
        warehouse="WH-PKG",
        lead_time_days=6,
    ),
    # --- manufactured sub-assemblies ----------------------------------------
    # These are what the engineering stage hangs multi-level BOMs off. The rose
    # plate and the screw are deliberately usable by more than one finished good,
    # so common-part consolidation has something real to consolidate.
    _ItemSpec(
        code=("SF", "LEVER-BRASS"),
        name="Lever Handle Casting - Brass, Machined",
        group=ItemGroup.SEMI_FINISHED,
        category="DM-CAT-LEVER-BRASS",
        base_uom="NOS",
        hsn="83024110",
        standard_cost="142.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-ASM",
    ),
    _ItemSpec(
        code=("SF", "ROSE-SS"),
        name="Rose Plate 52mm - SS 304, Polished",
        group=ItemGroup.SEMI_FINISHED,
        category="HANDLE-ROSE",
        base_uom="NOS",
        hsn="83024190",
        standard_cost="68.40",
        production_type=ProductionType.MAKE,
        warehouse="WH-ASM",
        size="52mm",
    ),
    # Plating is sent out, so this one is JOB_WORK rather than MAKE. It gives the
    # job-work module a component whose planning policy already says that
    # somebody else performs a step in the middle of it.
    _ItemSpec(
        code=("SF", "LOCKBODY"),
        name="Mortise Lock Body 6572 - Zinc, Plated",
        group=ItemGroup.SEMI_FINISHED,
        category="LOCK-MORT",
        base_uom="NOS",
        hsn="83014090",
        standard_cost="196.30",
        production_type=ProductionType.JOB_WORK,
        warehouse="WH-ASM",
    ),
    # --- finished goods ------------------------------------------------------
    # Sold as a SET or a PAIR, which is how the trade quotes them. Priced on the
    # item master as well as in the price list, because `resolve_price` falls
    # back to the master when no dated slab matches and a zero there would
    # quietly invoice a customer nothing.
    _ItemSpec(
        code=("FG", "LEVER-AB"),
        name="Lever Handle on Rose - Antique Brass",
        group=ItemGroup.FINISHED_GOOD,
        category="DM-CAT-LEVER-BRASS",
        base_uom="SET",
        hsn="83024110",
        standard_cost="486.00",
        sale_price="1250.00",
        dealer_price="925.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-FG",
        finish="Antique Brass",
        size="52mm rose",
    ),
    _ItemSpec(
        code=("FG", "LEVER-SS"),
        name="Lever Handle on Rose - Satin Stainless",
        group=ItemGroup.FINISHED_GOOD,
        category="DM-CAT-LEVER-SS",
        base_uom="SET",
        hsn="83024190",
        standard_cost="452.00",
        sale_price="1150.00",
        dealer_price="860.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-FG",
        finish="Satin Stainless",
        size="52mm rose",
    ),
    _ItemSpec(
        code=("FG", "MORTISE"),
        name="Mortise Lockset 6572 - Stainless Finish",
        group=ItemGroup.FINISHED_GOOD,
        category="DM-CAT-MORT-6572",
        base_uom="SET",
        hsn="83014090",
        standard_cost="812.00",
        sale_price="2150.00",
        dealer_price="1620.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-FG",
        finish="Satin Stainless",
    ),
    _ItemSpec(
        code=("FG", "HINGE-SS"),
        name="Butt Hinge 100 x 75 x 3mm - SS 304",
        group=ItemGroup.FINISHED_GOOD,
        category="HINGES",
        base_uom="PAIR",
        hsn="83021000",
        standard_cost="196.00",
        sale_price="480.00",
        dealer_price="365.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-FG",
        size="100 x 75 x 3mm",
    ),
    # --- the serial-tracked item --------------------------------------------
    # An electronic lock carries a serial number on the body and on the warranty
    # card, so it is the honest place to put serial tracking. It matters that
    # this is a *different* item from the batch-tracked one: `batch_id` is part
    # of the balance grain and `serial_id` is not, so the two obey deliberately
    # different inventory rules (docs/assumptions.md section 23), and a demo
    # database has to be able to exercise both of them.
    _ItemSpec(
        code=("FG", "DIGILOCK"),
        name="Digital Mortise Lock DL-900",
        description="Keypad and card mortise lock; each unit carries a serial number.",
        group=ItemGroup.FINISHED_GOOD,
        category="DM-CAT-LOCK-SMART",
        base_uom="NOS",
        hsn="83014010",
        standard_cost="4250.00",
        sale_price="9850.00",
        dealer_price="7900.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-FG",
        is_serial_tracked=True,
        finish="Black Nickel",
    ),
    # --- scrap ---------------------------------------------------------------
    # Turnings come off the lathe, so the item is produced rather than bought and
    # `can_manufacture` is set explicitly: the item group's defaults assume scrap
    # is only ever sold, and a MAKE policy without that flag is refused outright.
    _ItemSpec(
        code=("SC", "BRASS"),
        name="Brass Turnings and Rejects",
        description="Recovered brass, graded for re-melt or sold to the smelter.",
        group=ItemGroup.SCRAP,
        category="RAW-BRASS",
        base_uom="KG",
        hsn="74040022",
        standard_cost="385.00",
        sale_price="410.00",
        production_type=ProductionType.MAKE,
        warehouse="WH-SCR",
        can_manufacture=True,
    ),
)


@dataclass(frozen=True, slots=True)
class _ManufacturerSpec:
    code: tuple[str, ...]
    name: str
    legal_name: str | None
    notes: str


#: Who makes things. The company's own works, which is the maker of record for
#: everything made in-house, and two outside makers whose parts arrive through a
#: distributor - which is exactly why a maker is not a vendor. The outside two
#: are fictional and say so in their notes.
MANUFACTURERS: tuple[_ManufacturerSpec, ...] = (
    _ManufacturerSpec(
        code=("MFR", "ATCRAFT"),
        name="Atcraft Works",
        legal_name=None,
        notes=(
            "Demonstration data. The company's own works - the maker of record for every "
            "item made in-house."
        ),
    ),
    _ManufacturerSpec(
        code=("MFR", "CYLINDER"),
        name="Saurashtra Cylinder Works (demo)",
        legal_name="Saurashtra Cylinder Works Private Limited (demo)",
        notes=(
            "Demonstration data - a fictional lock-cylinder maker. Its cylinders are bought "
            "through a fastener distributor, not from the maker directly."
        ),
    ),
    _ManufacturerSpec(
        code=("MFR", "SPRINGS"),
        name="Western Springs and Fasteners (demo)",
        legal_name="Western Springs and Fasteners LLP (demo)",
        notes="Demonstration data - a fictional maker of springs and machine screws.",
    ),
)

#: ``(code parts, name, owner manufacturer parts or None, position, description)``.
#: The house brand first, then its premium line, then the partner brand the
#: bought-in cylinders are sold under.
BRANDS: tuple[tuple[tuple[str, ...], str, tuple[str, ...] | None, int, str], ...] = (
    (("BRAND", "ATCRAFT"), "ATCRAFT", ("MFR", "ATCRAFT"), 0, "The house brand."),
    (
        ("BRAND", "ATCRAFT-PRO"),
        "ATCRAFT PRO",
        ("MFR", "ATCRAFT"),
        10,
        "The premium line: electronic locks and heavy-duty sets.",
    ),
    (
        ("BRAND", "SECUREPIN"),
        "SecurePin (demo)",
        ("MFR", "CYLINDER"),
        20,
        "Demonstration data - a fictional partner brand, carried on the cylinders bought in "
        "from its maker.",
    ),
)

#: ``(item parts, brand parts or None, maker parts or None, maker's part number)``.
#: Finished goods are ATCRAFT, made by the company's own works; the premium
#: digital lock is ATCRAFT PRO. The sub-assemblies are made in-house but sold
#: under no brand. The bought-in cylinder carries the partner brand and its
#: maker; the spring and the screw carry only their maker, because nobody sells a
#: screw by its brand.
ITEM_BRANDING: tuple[
    tuple[tuple[str, ...], tuple[str, ...] | None, tuple[str, ...] | None, str | None], ...
] = (
    (("FG", "LEVER-AB"), ("BRAND", "ATCRAFT"), ("MFR", "ATCRAFT"), None),
    (("FG", "LEVER-SS"), ("BRAND", "ATCRAFT"), ("MFR", "ATCRAFT"), None),
    (("FG", "MORTISE"), ("BRAND", "ATCRAFT"), ("MFR", "ATCRAFT"), None),
    (("FG", "HINGE-SS"), ("BRAND", "ATCRAFT"), ("MFR", "ATCRAFT"), None),
    (("FG", "DIGILOCK"), ("BRAND", "ATCRAFT-PRO"), ("MFR", "ATCRAFT"), None),
    (("SF", "LEVER-BRASS"), None, ("MFR", "ATCRAFT"), None),
    (("SF", "ROSE-SS"), None, ("MFR", "ATCRAFT"), None),
    (("SF", "LOCKBODY"), None, ("MFR", "ATCRAFT"), None),
    (("BO", "CYL-60"), ("BRAND", "SECUREPIN"), ("MFR", "CYLINDER"), "SCW-PC60-5P"),
    (("BO", "SPRING"), None, ("MFR", "SPRINGS"), "WSF-TS12-LR"),
    (("BO", "SCREW-CSK"), None, ("MFR", "SPRINGS"), "WSF-M4X25-CSK"),
)


#: New leaves on the category tree, hung off what the baseline seed already made.
#: ``LOCKS > Mortise Locks > 6572 Series`` and ``HANDLES > Lever Handles > Brass``
#: take the tree to three levels, which is what the category picker and the
#: subtree (``path LIKE``) queries need before they are worth looking at.
CATEGORIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("CAT", "LEVER-BRASS"), "Brass Lever Handles", "HANDLE-LEVER"),
    (("CAT", "LEVER-SS"), "Stainless Lever Handles", "HANDLE-LEVER"),
    (("CAT", "MORT-6572"), "6572 Series Mortise Locks", "LOCK-MORT"),
    (("CAT", "LOCK-SMART"), "Smart and Digital Locks", "LOCKS"),
)


#: Storage positions, as ``(warehouse, code parts, name, type, parent parts)``.
#: Racks come before the bins that hang off them, and a bin names its rack as its
#: parent, so the warehouse screen shows a location tree with depth rather than a
#: flat list of fourteen codes.
#:
#: Bin codes are unique per *warehouse* in the schema, but this file's register is
#: keyed per company, so each code carries its warehouse - ``DM-LOC-RM-A-01``,
#: never a bare ``A-01``. Two warehouses each holding an ``A-01`` would otherwise
#: resolve to whichever row the lookup happened to return first.
LOCATIONS: tuple[
    tuple[str, tuple[str, ...], str, StorageLocationType, tuple[str, ...] | None], ...
] = (
    ("WH-RM", ("LOC", "RM", "A"), "Rack A - Brass Stock", StorageLocationType.RACK, None),
    (
        "WH-RM",
        ("LOC", "RM", "A-01"),
        "A-01 Brass Rod 12mm",
        StorageLocationType.BIN,
        ("LOC", "RM", "A"),
    ),
    (
        "WH-RM",
        ("LOC", "RM", "A-02"),
        "A-02 Brass Rod - Offcuts",
        StorageLocationType.BIN,
        ("LOC", "RM", "A"),
    ),
    ("WH-RM", ("LOC", "RM", "B"), "Rack B - Steel and Zinc", StorageLocationType.RACK, None),
    (
        "WH-RM",
        ("LOC", "RM", "B-01"),
        "B-01 SS Rod 10mm",
        StorageLocationType.BIN,
        ("LOC", "RM", "B"),
    ),
    ("WH-RM", ("LOC", "RM", "FLOOR"), "Ingot Floor Stack", StorageLocationType.FLOOR, None),
    ("WH-CMP", ("LOC", "CMP", "A-01"), "A-01 Fasteners", StorageLocationType.BIN, None),
    (
        "WH-CMP",
        ("LOC", "CMP", "A-02"),
        "A-02 Springs and Cylinders",
        StorageLocationType.BIN,
        None,
    ),
    ("WH-FG", ("LOC", "FG", "P1"), "Pallet Rack 1 - Handles", StorageLocationType.RACK, None),
    (
        "WH-FG",
        ("LOC", "FG", "P1-01"),
        "P1-01 Lever Handle Sets",
        StorageLocationType.BIN,
        ("LOC", "FG", "P1"),
    ),
    (
        "WH-FG",
        ("LOC", "FG", "P1-02"),
        "P1-02 Locksets",
        StorageLocationType.BIN,
        ("LOC", "FG", "P1"),
    ),
    ("WH-FG", ("LOC", "FG", "DOCK"), "Dispatch Staging Lane", StorageLocationType.STAGING, None),
    ("WH-QC", ("LOC", "QC", "HOLD"), "Incoming Hold Area", StorageLocationType.FLOOR, None),
    ("WH-QC", ("LOC", "QC", "REJECT"), "Rejection Cage", StorageLocationType.FLOOR, None),
)


#: Price list rows, as ``(item code parts, sale, sale at the break, dealer,
#: distributor)``. Four rows per finished good rather than one, so the price list
#: screen shows a real ladder and `resolve_price` has both a quantity break and a
#: channel to choose between. Scrap is priced on the item master only: it goes at
#: whatever the smelter pays that week, not from a list.
PRICES: tuple[tuple[tuple[str, ...], str, str, str, str], ...] = (
    (("FG", "LEVER-AB"), "1250.00", "1150.00", "925.00", "860.00"),
    (("FG", "LEVER-SS"), "1150.00", "1060.00", "860.00", "800.00"),
    (("FG", "MORTISE"), "2150.00", "1980.00", "1620.00", "1510.00"),
    (("FG", "HINGE-SS"), "480.00", "440.00", "365.00", "340.00"),
    (("FG", "DIGILOCK"), "9850.00", "9200.00", "7900.00", "7400.00"),
)

#: The quantity at which the bulk slab takes over. One break, not five: the point
#: is to show that slabs exist and resolve correctly, not to model a real
#: discount matrix.
BULK_SLAB_QUANTITY = "50"


# --- commercial partners ------------------------------------------------------

#: Carriers, as ``(code parts, name, type, GSTIN, city, state, state code,
#: tracking template)``. Two live and one retired, because the transporter screen
#: has an active filter and a list where every row says the same thing tells an
#: evaluator nothing. The GSTINs are structurally valid including the check digit
#: - the vendors module verifies it, so an invented number is rejected at the
#: schema rather than stored.
TRANSPORTERS: tuple[tuple[tuple[str, ...], str, TransporterType, str, str, str, str, str], ...] = (
    (
        ("TRN", "VRL"),
        "VRL Logistics Ltd",
        TransporterType.TRANSPORT,
        "29AAACV1234G1ZJ",
        "Hubballi",
        "Karnataka",
        "29",
        "https://www.vrlgroup.in/track?lr={tracking_id}",
    ),
    (
        ("TRN", "GATI"),
        "Gati Express",
        TransporterType.COURIER,
        "27AAACG2345H1ZU",
        "Mumbai",
        "Maharashtra",
        "27",
        "https://www.gati.com/track/{tracking_id}",
    ),
    # No tracking template on the local carrier: the dispatch screen has to cope
    # with a consignment nobody can look up online, and here is one.
    (
        ("TRN", "CITY"),
        "Rajkot City Carriers",
        TransporterType.TRANSPORT,
        "24AABCR3456J1ZD",
        "Rajkot",
        "Gujarat",
        "24",
        "",
    ),
)

#: The carrier switched off after creation, so the transporter list carries both
#: states. Deactivated through ``set_active`` rather than by writing the column,
#: which keeps the state machine and the status-change audit row in play.
RETIRED_TRANSPORTER = ("TRN", "CITY")


@dataclass(frozen=True, slots=True)
class _CustomerSpec:
    code: tuple[str, ...]
    name: str
    legal_name: str
    customer_type: CustomerType
    price_list_type: PriceType
    gstin: str
    city: str
    state: str
    state_code: str
    address_line1: str
    postal_code: str
    contact_name: str
    contact_designation: str
    contact_phone: str
    credit_days: int
    #: ``None`` is an unlimited limit, which is a genuinely different state from
    #: a large one and is worth having on the screen exactly once.
    credit_limit: str | None
    transporter: str | None = None
    notes: str | None = None


#: Six accounts across all four channels. The dealer screen filters on
#: ``customer_type`` and accepts only DEALER and DISTRIBUTOR, so there are two of
#: the former and one of the latter. The price list each account is charged on
#: matches its channel, which is what makes the same sales order price itself
#: differently depending on who it is for.
CUSTOMERS: tuple[_CustomerSpec, ...] = (
    _CustomerSpec(
        code=("CUST", "ARCH"),
        name="Archway Interiors",
        legal_name="Archway Interiors Private Limited",
        customer_type=CustomerType.CUSTOMER,
        price_list_type=PriceType.SALE,
        gstin="27AACCA1234F1Z8",
        city="Mumbai",
        state="Maharashtra",
        state_code="27",
        address_line1="Unit 4, Sunmill Compound, Lower Parel",
        postal_code="400013",
        contact_name="Nikhil Deshpande",
        contact_designation="Purchase Manager",
        contact_phone="+91 22 2491 7788",
        credit_days=30,
        credit_limit="500000",
        transporter="DM-TRN-GATI",
        notes="Fit-out contractor. Orders against project schedules, not against stock.",
    ),
    _CustomerSpec(
        code=("CUST", "SKYLINE"),
        name="Skyline Builders",
        legal_name="Skyline Builders LLP",
        customer_type=CustomerType.CUSTOMER,
        price_list_type=PriceType.SALE,
        gstin="29AAECS5678K1ZL",
        city="Bengaluru",
        state="Karnataka",
        state_code="29",
        address_line1="Plot 22, Whitefield Main Road",
        postal_code="560066",
        contact_name="Anitha Rao",
        contact_designation="Procurement Lead",
        contact_phone="+91 80 4128 3300",
        credit_days=45,
        credit_limit="1200000",
        transporter="DM-TRN-VRL",
    ),
    _CustomerSpec(
        code=("DLR", "VASTU"),
        name="Vastu Hardware Mart",
        legal_name="Vastu Hardware Mart",
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.DEALER,
        gstin="24AAFFV2345L1Z1",
        city="Ahmedabad",
        state="Gujarat",
        state_code="24",
        address_line1="Shop 7, Relief Road",
        postal_code="380001",
        contact_name="Jignesh Patel",
        contact_designation="Proprietor",
        contact_phone="+91 79 2550 6611",
        credit_days=21,
        credit_limit="250000",
    ),
    _CustomerSpec(
        code=("DLR", "METRO"),
        name="Metro Door Solutions",
        legal_name="Metro Door Solutions Private Limited",
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.DEALER,
        gstin="07AAGCM3456N1Z1",
        city="New Delhi",
        state="Delhi",
        state_code="07",
        address_line1="B-14, Naraina Industrial Area Phase I",
        postal_code="110028",
        contact_name="Rohit Sethi",
        contact_designation="Owner",
        contact_phone="+91 11 4155 2020",
        credit_days=15,
        credit_limit="150000",
    ),
    _CustomerSpec(
        code=("DIST", "WEST"),
        name="Westline Distributors",
        legal_name="Westline Distributors Private Limited",
        customer_type=CustomerType.DISTRIBUTOR,
        price_list_type=PriceType.DISTRIBUTOR,
        gstin="27AABCW4567P1ZK",
        city="Pune",
        state="Maharashtra",
        state_code="27",
        address_line1="Gat 145, Chakan MIDC Phase II",
        postal_code="410501",
        contact_name="Sameer Kulkarni",
        contact_designation="General Manager",
        contact_phone="+91 20 6725 4400",
        credit_days=60,
        credit_limit="2000000",
        transporter="DM-TRN-VRL",
        notes="Regional distributor for western India. Monthly consolidated orders.",
    ),
    _CustomerSpec(
        code=("CUST", "KALYAN"),
        name="Kalyan Modular Furniture",
        legal_name="Kalyan Modular Furniture Private Limited",
        customer_type=CustomerType.OEM,
        price_list_type=PriceType.SALE,
        gstin="33AAHCK5678R1ZN",
        city="Chennai",
        state="Tamil Nadu",
        state_code="33",
        address_line1="Survey 88/3, Ambattur Industrial Estate",
        postal_code="600058",
        contact_name="Karthik Subramanian",
        contact_designation="Sourcing Head",
        contact_phone="+91 44 2625 9090",
        credit_days=30,
        credit_limit=None,
        notes="OEM account; the hardware is fitted into their own product, unbranded.",
    ),
)


@dataclass(frozen=True, slots=True)
class _VendorSpec:
    code: tuple[str, ...]
    name: str
    legal_name: str
    vendor_types: tuple[VendorType, ...]
    gstin: str
    city: str
    state: str
    state_code: str
    address_line1: str
    postal_code: str
    bank_name: str
    bank_branch: str
    bank_ifsc: str
    bank_account_number: str
    payment_terms_days: int
    lead_time_days: int
    owner_name: str
    owner_phone: str
    accounts_name: str
    accounts_email: str
    #: Item categories this vendor is qualified to supply, and whether it is the
    #: first call for each. The schema allows one preferred vendor per category,
    #: so no category is marked preferred twice anywhere in this table.
    categories: tuple[tuple[str, bool], ...]
    notes: str | None = None


#: Five suppliers, covering the four things this business buys in: metal,
#: bought-out components, packing, and outsourced plating. Bank details and a
#: GSTIN are on every one of them because a vendor cannot be approved without
#: them, and an unapproved vendor cannot take a purchase order.
VENDORS: tuple[_VendorSpec, ...] = (
    _VendorSpec(
        code=("VEN", "BRASS"),
        name="Shree Metals and Alloys",
        legal_name="Shree Metals and Alloys Private Limited",
        vendor_types=(VendorType.RAW_MATERIAL_SUPPLIER,),
        gstin="24AAECS1122B1Z7",
        city="Jamnagar",
        state="Gujarat",
        state_code="24",
        address_line1="Plot 214, GIDC Phase III, Dared",
        postal_code="361004",
        bank_name="HDFC Bank",
        bank_branch="Jamnagar Main",
        bank_ifsc="HDFC0001204",
        bank_account_number="50200041127788",
        payment_terms_days=45,
        lead_time_days=10,
        owner_name="Bhavesh Shah",
        owner_phone="+91 288 267 4411",
        accounts_name="Mitesh Trivedi",
        accounts_email="accounts@shreemetals.example",
        categories=(("RAW-BRASS", True), ("RAW-ZINC", True)),
        notes="Brass and zinc. Mill test certificate with every lot.",
    ),
    _VendorSpec(
        code=("VEN", "SS"),
        name="Deepak Steel Corporation",
        legal_name="Deepak Steel Corporation",
        vendor_types=(VendorType.RAW_MATERIAL_SUPPLIER,),
        gstin="27AABCD2233C1ZB",
        city="Mumbai",
        state="Maharashtra",
        state_code="27",
        address_line1="12 Kalbadevi Road, Marine Lines",
        postal_code="400002",
        bank_name="ICICI Bank",
        bank_branch="Kalbadevi",
        bank_ifsc="ICIC0000104",
        bank_account_number="010405001234",
        payment_terms_days=30,
        lead_time_days=14,
        owner_name="Deepak Jain",
        owner_phone="+91 22 2205 6677",
        accounts_name="Sunita Bafna",
        accounts_email="payables@deepaksteel.example",
        categories=(("RAW-SS", True),),
    ),
    _VendorSpec(
        code=("VEN", "FAST"),
        name="Precision Fasteners Co",
        legal_name="Precision Fasteners Company",
        vendor_types=(VendorType.COMPONENT_SUPPLIER,),
        gstin="27AAFCP3344D1ZN",
        city="Pune",
        state="Maharashtra",
        state_code="27",
        address_line1="J-88, MIDC Bhosari",
        postal_code="411026",
        bank_name="Axis Bank",
        bank_branch="Bhosari MIDC",
        bank_ifsc="UTIB0001188",
        bank_account_number="918020055412345",
        payment_terms_days=30,
        lead_time_days=7,
        owner_name="Ashish Kulkarni",
        owner_phone="+91 20 2712 3344",
        accounts_name="Vaibhav Shinde",
        accounts_email="accounts@precisionfasteners.example",
        # Not preferred for mortise locks or handles - the plater is - but
        # qualified for both, which is what lets its lock-body and handle
        # managers be scoped to them (users.allowed_category_ids must be a
        # subset of these rows).
        categories=(
            ("FASTENERS", True),
            ("LOCK-CYL", True),
            ("ACCESSORIES", True),
            ("LOCK-MORT", False),
            ("HANDLES", False),
        ),
        notes=(
            "Screws, springs and cylinders, and bought-out lock bodies and handle blanks. "
            "Reliable on quantity, slow on paperwork."
        ),
    ),
    _VendorSpec(
        code=("VEN", "PACK"),
        name="Kraft Pack Industries",
        legal_name="Kraft Pack Industries",
        vendor_types=(VendorType.PACKING_MATERIAL_SUPPLIER,),
        gstin="24AAGCK4455E1ZP",
        city="Rajkot",
        state="Gujarat",
        state_code="24",
        address_line1="Survey 41, Shapar Veraval",
        postal_code="360024",
        bank_name="Bank of Baroda",
        bank_branch="Shapar",
        bank_ifsc="BARB0SHAPAR",
        bank_account_number="04120200001199",
        payment_terms_days=21,
        lead_time_days=8,
        owner_name="Hardik Vyas",
        owner_phone="+91 2827 252 900",
        accounts_name="Rekha Joshi",
        accounts_email="accounts@kraftpack.example",
        # Stretch film and tape as well as cartons - qualified, not preferred,
        # so the lacquer still has nobody nominated to supply it.
        categories=(("PACKING", True), ("CONSUMABLE", False)),
    ),
    _VendorSpec(
        code=("VEN", "PLATE"),
        name="Chromeline Electroplaters",
        legal_name="Chromeline Electroplaters",
        vendor_types=(VendorType.JOB_WORK, VendorType.SERVICE),
        gstin="24AAHCC5566F1ZO",
        city="Rajkot",
        state="Gujarat",
        state_code="24",
        address_line1="Shed 9, Bhaktinagar Industrial Estate",
        postal_code="360002",
        bank_name="State Bank of India",
        bank_branch="Bhaktinagar",
        bank_ifsc="SBIN0003472",
        bank_account_number="30124455667",
        payment_terms_days=15,
        lead_time_days=5,
        owner_name="Chirag Dave",
        owner_phone="+91 281 246 7788",
        accounts_name="Nilesh Barot",
        accounts_email="accounts@chromeline.example",
        categories=(("HANDLE-LEVER", True), ("LOCK-MORT", True)),
        notes="Antique brass and satin nickel plating. Material is issued on job work.",
    ),
)


#: Which vendor supplies which item, as ``(item parts, vendor parts, the vendor's
#: own part number, last price paid, lead time)``. Every one is marked preferred
#: because each of these items has exactly one supplier - the partial unique
#: index allows one preferred vendor per item, and a second flagged row would
#: violate it mid-transaction rather than at commit.
#:
#: The lacquer is deliberately left with no linked vendor. An item nobody is
#: nominated to supply is a real state, and the sourcing screen should have one
#: on it.
ITEM_VENDORS: tuple[tuple[tuple[str, ...], tuple[str, ...], str, str, int], ...] = (
    (("RM", "BRASS"), ("VEN", "BRASS"), "BR-12-IS319", "608.00", 10),
    (("RM", "ZINC"), ("VEN", "BRASS"), "ZA5-INGOT", "264.00", 12),
    (("RM", "SS"), ("VEN", "SS"), "SS304-BB-10", "281.50", 14),
    (("BO", "SCREW-CSK"), ("VEN", "FAST"), "PF-CSK-M4-25", "1.78", 7),
    (("BO", "SPRING"), ("VEN", "FAST"), "PF-TS-12", "4.05", 9),
    (("BO", "CYL-60"), ("VEN", "FAST"), "PF-CYL-60-5P", "164.00", 15),
    (("PK", "BOX"), ("VEN", "PACK"), "KP-CTN-3PLY-HS", "14.20", 8),
    (("PK", "POLYBAG"), ("VEN", "PACK"), "KP-LDPE-150250", "0.91", 6),
    (("BO", "LOCKBODY"), ("VEN", "FAST"), "PF-LB-6572-ZN", "182.00", 12),
    (("BO", "HANDLE"), ("VEN", "FAST"), "PF-LH-ZN-BLANK", "96.00", 10),
)

#: Whose part a source supplies, keyed on ``(item parts, vendor parts)``: the
#: fastener distributor sells the cylinder maker's cylinders and the spring
#: maker's springs and screws. The maker's own part number sits beside the
#: distributor's code, because the two never match.
LINK_MAKERS: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[tuple[str, ...], str]] = {
    (("BO", "CYL-60"), ("VEN", "FAST")): (("MFR", "CYLINDER"), "SCW-PC60-5P"),
    (("BO", "SPRING"), ("VEN", "FAST")): (("MFR", "SPRINGS"), "WSF-TS12-LR"),
    (("BO", "SCREW-CSK"), ("VEN", "FAST")): (("MFR", "SPRINGS"), "WSF-M4X25-CSK"),
}


#: The two staff accounts, as ``(code parts, full name, e-mail, role, employee
#: code)``. See the module docstring for why the buyer has to exist at all.
STAFF: tuple[tuple[tuple[str, ...], str, str, str, str], ...] = (
    (
        ("USER", "BUYER"),
        "Priya Nair",
        "priya.nair@atcraft.example",
        "PURCHASE_EXECUTIVE",
        "EMP-1042",
    ),
    (
        ("USER", "SALES"),
        "Rahul Mehta",
        "rahul.mehta@atcraft.example",
        "SALES_EXECUTIVE",
        "EMP-1088",
    ),
)

#: The shop-floor operators the demonstration shift roster plans, in the shape
#: of :data:`STAFF`. An operator is a user account - there is no employee master
#: without a login, which docs/assumptions.md lists as an open question - and no
#: operator role exists, so they hold the production supervisor's. The employee
#: code is what the roster prints beside the name.
OPERATORS: tuple[tuple[tuple[str, ...], str, str, str, str], ...] = (
    (
        ("USER", "OP1"),
        "Amit Shah",
        "amit.shah@atcraft.example",
        "PRODUCTION_SUPERVISOR",
        "EMP-OP-001",
    ),
    (
        ("USER", "OP2"),
        "Neha Patel",
        "neha.patel@atcraft.example",
        "PRODUCTION_SUPERVISOR",
        "EMP-OP-002",
    ),
    (
        ("USER", "OP3"),
        "Ravi Desai",
        "ravi.desai@atcraft.example",
        "PRODUCTION_SUPERVISOR",
        "EMP-OP-003",
    ),
    (
        ("USER", "OP4"),
        "Sunita Rao",
        "sunita.rao@atcraft.example",
        "PRODUCTION_SUPERVISOR",
        "EMP-OP-004",
    ),
)

#: Which of :data:`STAFF` creates the vendor records.
BUYER = ("USER", "BUYER")

#: Vendor-portal managers, as ``(code parts, full name, e-mail, vendor parts,
#: role, category codes)`` - the proposal's own table: Manager 1 for lock
#: bodies, Manager 2 for handles, Manager 3 for packaging. Each scope is a
#: category its vendor is qualified for (the users service refuses anything
#: else), and VENDOR_MANAGER carries ``purchase_order.update``, which is what
#: makes them someone a purchase order can be assigned to. The addresses use
#: the reserved ``.example`` domain, as every seeded account does (``seed.py``).
VENDOR_PORTAL_USERS: tuple[
    tuple[tuple[str, ...], str, str, tuple[str, ...], str, tuple[str, ...]], ...
] = (
    (
        ("USER", "VEN-LOCKS"),
        "Sandeep Patil",
        "lockbody.manager@precisionfasteners.example",
        ("VEN", "FAST"),
        "VENDOR_MANAGER",
        ("LOCK-MORT",),
    ),
    (
        ("USER", "VEN-HANDLES"),
        "Meera Joshi",
        "handles.manager@precisionfasteners.example",
        ("VEN", "FAST"),
        "VENDOR_MANAGER",
        ("HANDLES",),
    ),
    (
        ("USER", "VEN-PACKING"),
        "Kiran Rathod",
        "packaging.manager@kraftpack.example",
        ("VEN", "PACK"),
        "VENDOR_MANAGER",
        ("PACKING",),
    ),
)


# --- cost centres and departments --------------------------------------------
# A typical breakdown for an architectural-hardware works, offered as an
# EXAMPLE to be confirmed with Atcraft - not a rule anything reads. Production is
# a parent with one child per process the hardware passes through, plating is
# its own centre because it is job-worked (the vendor stands in for the
# process), and sales is split by zone so the selling cost of each region can be
# read on its own. Every centre's description says it is demonstration data.

#: Said on every demonstration centre, so nobody mistakes the example for a rule.
COST_CENTRE_NOTE = "Demonstration example - confirm the breakdown with Atcraft before use."


@dataclass(frozen=True)
class _CostCentreSpec:
    #: Code parts after ``DM-CC``: ``("PROD", "DIE")`` is ``DM-CC-PROD-DIE``.
    suffix: tuple[str, ...]
    name: str
    cost_centre_type: CostCentreType
    #: The parent's suffix. Parents are listed before their children.
    parent: tuple[str, ...] | None = None


COST_CENTRES: tuple[_CostCentreSpec, ...] = (
    _CostCentreSpec(("PROD",), "Production", CostCentreType.PRODUCTION),
    _CostCentreSpec(("PROD", "DIE"), "Die-casting", CostCentreType.PRODUCTION, ("PROD",)),
    _CostCentreSpec(
        ("PROD", "MACH"), "Machining (CNC / VMC)", CostCentreType.PRODUCTION, ("PROD",)
    ),
    _CostCentreSpec(
        ("PROD", "BUFF"), "Buffing and Polishing", CostCentreType.PRODUCTION, ("PROD",)
    ),
    _CostCentreSpec(
        ("PROD", "PLAT"), "Plating / PVD (job-worked)", CostCentreType.PRODUCTION, ("PROD",)
    ),
    _CostCentreSpec(("PROD", "ASSY"), "Assembly", CostCentreType.PRODUCTION, ("PROD",)),
    _CostCentreSpec(("PROD", "PACK"), "Packing", CostCentreType.PRODUCTION, ("PROD",)),
    _CostCentreSpec(("QC",), "Quality Control", CostCentreType.SERVICE),
    _CostCentreSpec(("STORES",), "Stores", CostCentreType.SERVICE),
    _CostCentreSpec(("MAINT",), "Maintenance and Utilities", CostCentreType.SERVICE),
    _CostCentreSpec(("ADMIN",), "Administration", CostCentreType.ADMINISTRATION),
    _CostCentreSpec(("SALES",), "Sales and Marketing", CostCentreType.SELLING),
    _CostCentreSpec(("SALES", "NORTH"), "Sales - North Zone", CostCentreType.SELLING, ("SALES",)),
    _CostCentreSpec(("SALES", "WEST"), "Sales - West Zone", CostCentreType.SELLING, ("SALES",)),
)


@dataclass(frozen=True)
class _DepartmentSpec:
    suffix: tuple[str, ...]
    name: str
    #: The cost centre the department's documents default to.
    cost_centre: tuple[str, ...]


DEPARTMENTS: tuple[_DepartmentSpec, ...] = (
    _DepartmentSpec(("DEPT", "PROD"), "Production", ("PROD",)),
    _DepartmentSpec(("DEPT", "QC"), "Quality Assurance", ("QC",)),
    _DepartmentSpec(("DEPT", "STORES"), "Stores", ("STORES",)),
    _DepartmentSpec(("DEPT", "MAINT"), "Maintenance", ("MAINT",)),
    _DepartmentSpec(("DEPT", "ADMIN"), "Administration and Accounts", ("ADMIN",)),
    _DepartmentSpec(("DEPT", "SALES"), "Sales", ("SALES",)),
)


def cost_centre_code(ctx: DemoContext, *suffix: str) -> str:
    """``DM-CC-PROD-DIE``: the code a cost centre is created and registered under.

    The later stages that tag work centres, orders and bills spell the same
    ``ctx.code("CC", ...)`` themselves rather than importing this stage, which
    the loader keeps independently importable.
    """
    return ctx.code("CC", *suffix)


# --- helpers ------------------------------------------------------------------


def _seen(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID, *, created: bool) -> uuid.UUID:
    """Register a row and count it, whether this run made it or found it.

    Both cases register: a later stage cannot tell which happened and must not
    have to care, which is the whole basis of re-running this tool.
    """
    ctx.note(STAGE, created=1 if created else 0, existing=0 if created else 1)
    return ctx.remember(kind, code, row_id)


def _alias(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID) -> None:
    """Also register a pre-existing row under the ``DM-`` code a stage may guess.

    Units and warehouses are named by the baseline seed, not by this file, so a
    sibling stage reaching for ``ctx.code("WH", "RM")`` would otherwise miss a
    warehouse that is sitting right in front of it. The alias points at the same
    row - it is not a second record and it is not counted as one.
    """
    ctx.remember(kind, ctx.code(*code.split("-")), row_id)


async def _branch_id(ctx: DemoContext) -> uuid.UUID | None:
    """The head office branch, if the baseline seed made one."""
    stmt = select(Branch).where(Branch.company_id == ctx.company_id, Branch.code == "HO")
    branch = (await ctx.session.execute(stmt)).scalars().first()
    return branch.id if branch else None


# --- stages -------------------------------------------------------------------


async def _seed_units(ctx: DemoContext) -> None:
    """Register every unit the company already has.

    The baseline seed owns this table - NOS, KG, SET, PAIR, LTR and the rest -
    and this stage only needs to be able to name them, so nothing is created. A
    unit invented here would be a second spelling of one the application is
    already using.

    No conversion is added either. Every item below trades in the unit it is
    stocked in, so the base-unit graph is never consulted for any of them, and a
    company-wide ``BOX -> NOS`` factor becomes a lie the moment two items pack a
    different number to a box.
    """
    stmt = select(UnitOfMeasure).where(UnitOfMeasure.company_id == ctx.company_id)
    for uom in (await ctx.session.execute(stmt)).scalars():
        _seen(ctx, "uom", uom.code, uom.id, created=False)
        _alias(ctx, "uom", f"UOM-{uom.code}", uom.id)


async def _seed_item_attributes(ctx: DemoContext) -> None:
    """Create the finish, size and colour vocabulary the item specs reference.

    Derived from ``ITEMS`` rather than listed separately, so a spec that names a
    new finish gets one without a second edit here - and so the two can never
    disagree, which is the failure this whole table exists to prevent.

    Existence is keyed on ``(attribute, code)``, which is what identifies the
    row, and never on anything derived from the clock: a demo loader whose
    idempotence depends on the day it runs stops being idempotent tomorrow.
    """
    repo = ItemAttributeRepository(ctx.session, ctx.tenant)

    wanted: dict[tuple[ItemAttributeType, str], str] = {}
    for spec in ITEMS:
        for kind, value in (
            (ItemAttributeType.FINISH, spec.finish),
            (ItemAttributeType.SIZE, spec.size),
            (ItemAttributeType.COLOUR, spec.colour),
        ):
            if value:
                wanted.setdefault((kind, _attribute_code(value)), value)

    for (kind, code), name in sorted(wanted.items()):
        existing = await repo.get_by_code(kind, code)
        if existing is not None:
            _seen(ctx, "item_attribute", f"{kind.value}:{code}", existing.id, created=False)
            continue

        attribute = ItemAttribute(company_id=ctx.company_id, attribute=kind, code=code, name=name)
        repo.add(attribute)
        await ctx.session.flush()
        _seen(ctx, "item_attribute", f"{kind.value}:{code}", attribute.id, created=True)


def _attribute_code(value: str) -> str:
    """The same derivation the backfill migration uses, so the two agree."""
    return normalise_code(value).replace(" ", "-")[:32]


def _usable(ctx: DemoContext, kind: str, code: str, row: Brand | Manufacturer) -> None:
    """Register a pre-existing brand or maker - unless it can no longer be assigned.

    A client may have retired or deactivated a demo row since the last run.
    Registering it anyway would hand the branding step an id the service refuses,
    and one refusal fails the whole stage; so it is counted, reported as
    skipped, and left out of the registry - which is all "do not assign it"
    takes.
    """
    if row.deleted_at is not None or not row.is_active:
        ctx.note(STAGE, existing=1)
        ctx.skipped.append((STAGE, f"{kind} {code} is retired or inactive, so nothing is given it"))
        return
    _seen(ctx, kind, code, row.id, created=False)


async def _seed_manufacturers(ctx: DemoContext) -> None:
    """The three makers. Existence is keyed on the code, never on the clock."""
    for spec in MANUFACTURERS:
        code = ctx.code(*spec.code)
        existing = await find_by_code(ctx.session, Manufacturer, ctx.company_id, code)
        if existing is not None:
            _usable(ctx, "manufacturer", code, existing)
            continue

        made = await create_manufacturer(
            ctx.session,
            ctx.tenant,
            ManufacturerCreate(
                code=code, name=spec.name, legal_name=spec.legal_name, notes=spec.notes
            ),
        )
        _seen(ctx, "manufacturer", code, made.id, created=True)


async def _seed_brands(ctx: DemoContext) -> None:
    """The house brand, its premium line and one partner brand, each with its owner."""
    for parts, name, owner, sort_order, description in BRANDS:
        code = ctx.code(*parts)
        existing = await find_by_code(ctx.session, Brand, ctx.company_id, code)
        if existing is not None:
            _usable(ctx, "brand", code, existing)
            continue

        made = await create_brand(
            ctx.session,
            ctx.tenant,
            BrandCreate(
                code=code,
                name=name,
                description=description,
                # An owner the client has since retired is simply not recorded,
                # rather than failing the stage on a refused reference.
                manufacturer_id=ctx.maybe_id("manufacturer", ctx.code(*owner)) if owner else None,
                sort_order=sort_order,
            ),
        )
        _seen(ctx, "brand", code, made.id, created=True)


async def _seed_item_branding(ctx: DemoContext) -> None:
    """Give the demo items their brand, their maker and the maker's part number.

    Fills blanks only, field by field, through the item service - so a database
    whose items were loaded before brands existed gains them, a second run
    changes nothing, and a value already recorded is never overwritten. Keyed on
    the item code, never on anything derived from the clock.
    """
    repo = ItemRepository(ctx.session, ctx.tenant)

    for item_parts, brand_parts, maker_parts, part_number in ITEM_BRANDING:
        code = ctx.code(*item_parts)
        item_id = ctx.maybe_id("item", code)
        item = await repo.get(item_id) if item_id is not None else None
        if item is None:
            ctx.skipped.append((STAGE, f"no live item {code} to brand"))
            continue

        brand_id = ctx.maybe_id("brand", ctx.code(*brand_parts)) if brand_parts else None
        maker_id = ctx.maybe_id("manufacturer", ctx.code(*maker_parts)) if maker_parts else None
        wanted: dict[str, object] = {}
        if brand_id is not None and item.brand_id is None:
            wanted["brand_id"] = brand_id
        if maker_id is not None and item.manufacturer_id is None:
            wanted["manufacturer_id"] = maker_id
        if part_number is not None and item.manufacturer_part_number is None:
            wanted["manufacturer_part_number"] = part_number

        if not wanted:
            ctx.note(STAGE, existing=1)
            continue
        await update_item(ctx.session, ctx.tenant, item.id, ItemUpdate.model_validate(wanted))
        ctx.note(STAGE, created=1)


def _attribute_id(ctx: DemoContext, kind: ItemAttributeType, value: str | None) -> uuid.UUID | None:
    return (
        ctx.maybe_id("item_attribute", f"{kind.value}:{_attribute_code(value)}") if value else None
    )


async def _seed_cost_centres(ctx: DemoContext) -> None:
    """Create the example cost-centre tree through the organisation service.

    Keyed on code, which identifies the row - never on anything derived from
    the clock. Parents come before children in :data:`COST_CENTRES`, so a
    child's parent is always registered by the time the child is created.
    """
    service = CostCentreService(ctx.session, ctx.tenant)

    for spec in COST_CENTRES:
        code = cost_centre_code(ctx, *spec.suffix)
        existing = await find_by_code(ctx.session, CostCentre, ctx.company_id, code)
        if existing is not None:
            _seen(ctx, "cost_centre", code, existing.id, created=False)
            continue

        parent_id = (
            ctx.maybe_id("cost_centre", cost_centre_code(ctx, *spec.parent))
            if spec.parent
            else None
        )
        created = await service.create_cost_centre(
            CostCentreCreate(
                code=code,
                name=spec.name,
                cost_centre_type=spec.cost_centre_type,
                parent_id=parent_id,
                description=COST_CENTRE_NOTE,
            )
        )
        _seen(ctx, "cost_centre", code, created.id, created=True)


async def _seed_departments(ctx: DemoContext) -> None:
    """Departments, each naming the cost centre its documents default to.

    A department this tool made earlier but that has no default yet - one from
    before cost centres existed - is given its default, through the service so
    the change is audited. One a person has since pointed elsewhere is left
    alone: the tool fills in what is missing and never overrides a decision.
    """
    service = DepartmentService(ctx.session, ctx.tenant)

    for spec in DEPARTMENTS:
        code = ctx.code(*spec.suffix)
        centre_id = ctx.maybe_id("cost_centre", cost_centre_code(ctx, *spec.cost_centre))
        existing = await find_by_code(ctx.session, Department, ctx.company_id, code)
        if existing is not None:
            department_id = existing.id
            missing_default = existing.cost_centre_id is None and existing.deleted_at is None
            _seen(ctx, "department", code, department_id, created=False)
            if missing_default and centre_id is not None:
                await service.update_department(
                    department_id, DepartmentUpdate(cost_centre_id=centre_id)
                )
            continue

        created = await service.create_department(
            DepartmentCreate(code=code, name=spec.name, cost_centre_id=centre_id)
        )
        _seen(ctx, "department", code, created.id, created=True)


async def _seed_warehouses(ctx: DemoContext) -> None:
    """Register the baseline warehouses, creating any that are missing.

    The raw material store, the finished goods store and the QC hold area already
    exist under the codes :mod:`src.core.seed` gave them, and they are looked up
    rather than duplicated: a second "Raw Material Store" under a ``DM-`` code
    would sit beside the real one on every receipt picker and nobody could tell
    which was which.

    The baseline list is imported rather than retyped, so a warehouse type added
    there arrives here without an edit to this file.
    """
    service = WarehouseService(ctx.session, ctx.tenant)
    branch_id = await _branch_id(ctx)

    for spec in BASELINE_WAREHOUSES:
        code = spec["code"]
        existing = await find_by_code(ctx.session, Warehouse, ctx.company_id, code)
        if existing is not None:
            warehouse_id = _seen(ctx, "warehouse", code, existing.id, created=False)
        else:
            created = await service.create_warehouse(
                WarehouseCreate(
                    code=code,
                    name=spec["name"],
                    warehouse_type=WarehouseType(spec["warehouse_type"]),
                    branch_id=branch_id,
                    city="Rajkot",
                    state="Gujarat",
                    state_code="24",
                    country="India",
                )
            )
            warehouse_id = _seen(ctx, "warehouse", code, created.id, created=True)
        _alias(ctx, "warehouse", code, warehouse_id)


async def _seed_locations(ctx: DemoContext) -> None:
    """Racks, bins, a staging lane and two QC floor areas.

    The baseline seed makes warehouses but no positions inside them, so without
    this the location picker on a goods receipt is empty and the put-away screen
    has nowhere to put anything away.
    """
    service = StorageLocationService(ctx.session, ctx.tenant)

    for warehouse_code, parts, name, location_type, parent_parts in LOCATIONS:
        warehouse_id = ctx.maybe_id("warehouse", warehouse_code)
        if warehouse_id is None:
            ctx.skipped.append((STAGE, f"no warehouse {warehouse_code} for its storage locations"))
            continue

        code = ctx.code(*parts)
        existing = await find_by_code(ctx.session, StorageLocation, ctx.company_id, code)
        if existing is not None:
            _seen(ctx, "location", code, existing.id, created=False)
            continue

        parent_id = ctx.maybe_id("location", ctx.code(*parent_parts)) if parent_parts else None
        is_bin = location_type is StorageLocationType.BIN
        location = await service.create_location(
            warehouse_id,
            StorageLocationCreate(
                code=code,
                name=name,
                location_type=location_type,
                parent_location_id=parent_id,
                # Capacity is advisory: it drives the put-away suggestion and the
                # over-capacity report and never blocks a movement. Only bins
                # carry one, because that is the grain a storekeeper counts at.
                capacity_quantity=Decimal("40") if is_bin else None,
                capacity_uom="BOX" if is_bin else None,
                barcode=code,
            ),
        )
        _seen(ctx, "location", code, location.id, created=True)


async def _seed_categories(ctx: DemoContext) -> None:
    """Register the baseline tree, then deepen it by one level.

    The baseline gives two levels (``HANDLES > Lever Handles``). The finish is
    what a person actually files a handle under, so a third level goes in under
    the branches this demonstration's items live on. Nothing baseline is
    recreated - it is looked up and registered as it stands.
    """
    stmt = select(ItemCategory).where(ItemCategory.company_id == ctx.company_id)
    for category in (await ctx.session.execute(stmt)).scalars():
        _seen(ctx, "category", category.code, category.id, created=False)

    for parts, name, parent_code in CATEGORIES:
        code = ctx.code(*parts)
        # Already registered - and already counted - by the sweep above, which on
        # a second run picks up the leaves this file created on the first.
        if ctx.has("category", code):
            continue

        parent_id = ctx.maybe_id("category", parent_code)
        if parent_id is None:
            # The baseline parent is genuinely absent, and hanging the leaf
            # anywhere else would misfile it. Say so and carry on.
            ctx.skipped.append((STAGE, f"category {code} needs missing parent {parent_code}"))
            continue

        category_out = await create_category(
            ctx.session,
            ctx.tenant,
            CategoryCreate(code=code, name=name, parent_id=parent_id),
        )
        _seen(ctx, "category", code, category_out.id, created=True)


async def _seed_items(ctx: DemoContext) -> None:
    """The item master.

    A standard cost is set on every row so the BOM cost roll-up has something to
    roll: a tree of zeros rolls up to zero and proves nothing. Reorder level and
    quantity are set on the six items that actually move, so the reorder report
    opens with rows on it instead of an empty state.
    """
    for spec in ITEMS:
        code = ctx.code(*spec.code)
        existing = await find_by_code(ctx.session, Item, ctx.company_id, code)
        if existing is not None:
            _seen(ctx, "item", code, existing.id, created=False)
            continue

        item = await create_item(
            ctx.session,
            ctx.tenant,
            ItemCreate(
                code=code,
                name=spec.name,
                description=spec.description,
                item_group=spec.group,
                # An uncategorised item is legal; a wrongly categorised one is
                # not, so a missing category leaves the field empty rather than
                # guessing at a neighbour.
                category_id=ctx.maybe_id("category", spec.category),
                base_uom_id=ctx.id_of("uom", spec.base_uom),
                hsn_code=spec.hsn,
                gst_rate=Decimal(spec.gst_rate),
                standard_cost=Decimal(spec.standard_cost),
                sale_price=Decimal(spec.sale_price),
                dealer_price=Decimal(spec.dealer_price),
                finish_id=_attribute_id(ctx, ItemAttributeType.FINISH, spec.finish),
                size_id=_attribute_id(ctx, ItemAttributeType.SIZE, spec.size),
                colour_id=_attribute_id(ctx, ItemAttributeType.COLOUR, spec.colour),
                production_type=spec.production_type,
                lead_time_days=spec.lead_time_days,
                reorder_level=Decimal(spec.reorder_level),
                reorder_quantity=Decimal(spec.reorder_quantity),
                min_order_quantity=Decimal(spec.min_order_quantity),
                safety_stock=Decimal(spec.safety_stock),
                shelf_life_days=spec.shelf_life_days,
                is_batch_tracked=spec.is_batch_tracked,
                is_serial_tracked=spec.is_serial_tracked,
                default_warehouse_id=ctx.maybe_id("warehouse", spec.warehouse),
                can_manufacture=spec.can_manufacture,
            ),
        )
        _seen(ctx, "item", code, item.id, created=True)


async def _price_exists(
    ctx: DemoContext,
    item_id: uuid.UUID,
    price_type: PriceType,
    min_quantity: Decimal,
) -> bool:
    """Is there already a live, open-ended price for this slab?

    A price list row has no code of its own, so idempotence is keyed on what
    actually identifies a slab: item, channel and break quantity. The start
    date is deliberately NOT part of the key. It is computed from the day the
    loader runs, so a re-run a day later used to look for a row dated one day
    earlier, find nothing, and then collide with the open-ended row the first
    run had written: ``create_price`` refused the overlap and the whole stage
    failed. What makes the demo slab "present" is a live row that is still
    open - which is exactly what ``create_price`` would reject a duplicate of.
    """
    stmt = select(ItemPrice.id).where(
        ItemPrice.company_id == ctx.company_id,
        ItemPrice.item_id == item_id,
        ItemPrice.variant_id.is_(None),
        ItemPrice.price_type == price_type,
        ItemPrice.min_quantity == min_quantity,
        ItemPrice.is_active.is_(True),
        ItemPrice.valid_to.is_(None),
    )
    return (await ctx.session.execute(stmt)).first() is not None


async def _seed_prices(ctx: DemoContext, anchor: date) -> None:
    """Dated price list rows, so a sales order can price itself from a list.

    Left open-ended (``valid_to`` is NULL) on purpose: a demonstration database
    gets opened weeks after it is built, and a window that has closed in the
    meantime makes every order fall back to the item master and look broken.
    """
    start = anchor - timedelta(days=PRICE_START_DAYS_AGO)
    bulk = Decimal(BULK_SLAB_QUANTITY)
    zero = Decimal("0")

    for parts, sale, sale_bulk, dealer, distributor in PRICES:
        item_id = ctx.id_of("item", ctx.code(*parts))
        slabs: tuple[tuple[PriceType, Decimal, Decimal], ...] = (
            (PriceType.SALE, zero, Decimal(sale)),
            (PriceType.SALE, bulk, Decimal(sale_bulk)),
            (PriceType.DEALER, zero, Decimal(dealer)),
            (PriceType.DISTRIBUTOR, zero, Decimal(distributor)),
        )
        for price_type, min_quantity, amount in slabs:
            if await _price_exists(ctx, item_id, price_type, min_quantity):
                ctx.note(STAGE, existing=1)
                continue
            await create_price(
                ctx.session,
                ctx.tenant,
                item_id,
                PriceCreate(
                    price_type=price_type,
                    price=amount,
                    min_quantity=min_quantity,
                    valid_from=start,
                    notes="Demonstration price list.",
                ),
            )
            ctx.note(STAGE, created=1)


async def _seed_staff(ctx: DemoContext) -> TenantContext | None:
    """A buyer, a sales executive and the operators, and the buyer's tenant context.

    Returns the context to create vendors under, or ``None`` when the accounts
    could not be made - in which case the caller falls back to creating vendors
    as the administrator, and they stay pending approval, which the caller
    reports rather than hides.

    The password is generated, never chosen: both accounts are flagged
    ``must_change_password`` and nothing ever signs in as them, so the value is
    write-only. A literal here would be a credential in source control.
    """
    service = UserService(ctx.session, ctx.tenant)
    buyer_id: uuid.UUID | None = None

    for parts, full_name, email, role_code, employee_code in (*STAFF, *OPERATORS):
        code = ctx.code(*parts)
        # Users are keyed by e-mail, not by a code column, so `find_by_code` does
        # not apply to them.
        stmt = select(User).where(User.company_id == ctx.company_id, User.email == email)
        existing = (await ctx.session.execute(stmt)).scalars().first()
        if existing is not None:
            user_id = _seen(ctx, "user", code, existing.id, created=False)
        else:
            role = await find_by_code(ctx.session, Role, ctx.company_id, role_code)
            try:
                detail = await service.create_user(
                    ctx.principal,
                    UserCreate(
                        email=email,
                        full_name=full_name,
                        password=generate_temporary_password(email),
                        employee_code=employee_code,
                        role_ids=[role.id] if role is not None else [],
                    ),
                )
            except (BusinessRuleError, DuplicateError, NotFoundError, AuthorizationError) as exc:
                ctx.skipped.append((STAGE, f"staff account {email} not created: {exc}"))
                continue
            user_id = _seen(ctx, "user", code, detail.id, created=True)

        if parts == BUYER:
            buyer_id = user_id

    if buyer_id is None:
        return None
    return TenantContext(company_id=ctx.company_id, user_id=buyer_id, is_superuser=True)


async def _seed_transporters(ctx: DemoContext) -> None:
    """Carriers, one of them switched off.

    The deactivation runs through ``set_active`` so the transporter state machine
    arbitrates it and a status-change audit row is written carrying a reason -
    the same path the screen takes. Writing ``is_active = False`` here would
    produce the same column value and no history whatsoever.
    """
    service = TransporterService(ctx.session, ctx.tenant)

    for parts, name, kind, gstin, city, state, state_code, template in TRANSPORTERS:
        code = ctx.code(*parts)
        existing = await find_by_code(ctx.session, Transporter, ctx.company_id, code)
        if existing is not None:
            transporter_id = _seen(ctx, "transporter", code, existing.id, created=False)
            is_active = existing.is_active
        else:
            created = await service.create_transporter(
                TransporterCreate(
                    code=code,
                    name=name,
                    transporter_type=kind,
                    gstin=gstin,
                    # The PAN is the middle ten characters of the GSTIN by
                    # construction, and the schema refuses the pair if the two
                    # disagree - so it is derived rather than typed twice.
                    pan=gstin[2:12],
                    city=city,
                    state=state,
                    state_code=state_code,
                    country="India",
                    tracking_url_template=template or None,
                )
            )
            transporter_id = _seen(ctx, "transporter", code, created.id, created=True)
            is_active = created.is_active

        # Guarded, because ACTIVE -> INACTIVE is a legal transition and
        # INACTIVE -> INACTIVE is not: a second run must not raise.
        if parts == RETIRED_TRANSPORTER and is_active:
            await service.set_active(
                transporter_id,
                active=False,
                reason="Local carrier withdrawn from the approved list.",
            )


async def _seed_customers(ctx: DemoContext) -> None:
    """Customers, dealers, a distributor and an OEM, each with an address.

    A billing address is created for every account because the place of supply
    decides the tax on every invoice raised against it, and an account without
    one makes the first invoice ask a question the demonstration cannot answer.
    The service makes the first address of a type the default and the first
    contact the primary one, so neither is flagged here.
    """
    service = CustomerService(ctx.session, ctx.tenant)
    # Assigning the accounts to the sales executive rather than to the system
    # administrator is what makes the "my customers" filter mean anything.
    sales_user_id = ctx.maybe_id("user", ctx.code("USER", "SALES")) or ctx.user_id

    for spec in CUSTOMERS:
        code = ctx.code(*spec.code)
        existing = await find_by_code(ctx.session, Customer, ctx.company_id, code)
        if existing is not None:
            customer_id = _seen(ctx, "customer", code, existing.id, created=False)
        else:
            detail = await service.create_customer(
                ctx.principal,
                CustomerCreate(
                    code=code,
                    name=spec.name,
                    legal_name=spec.legal_name,
                    customer_type=spec.customer_type,
                    gstin=spec.gstin,
                    pan=spec.gstin[2:12],
                    price_list_type=spec.price_list_type,
                    credit_limit=Decimal(spec.credit_limit) if spec.credit_limit else None,
                    credit_days=spec.credit_days,
                    assigned_sales_user_id=sales_user_id,
                    default_transporter_id=(
                        ctx.maybe_id("transporter", spec.transporter) if spec.transporter else None
                    ),
                    notes=spec.notes,
                ),
            )
            customer_id = _seen(ctx, "customer", code, detail.id, created=True)

        await _seed_customer_address(ctx, service, customer_id, spec)
        await _seed_customer_contact(ctx, service, customer_id, spec)


async def _seed_customer_address(
    ctx: DemoContext, service: CustomerService, customer_id: uuid.UUID, spec: _CustomerSpec
) -> None:
    """One billing address per account, keyed by its label so a re-run is a no-op."""
    label = "Registered Office"
    stmt = select(CustomerAddress.id).where(
        CustomerAddress.company_id == ctx.company_id,
        CustomerAddress.customer_id == customer_id,
        CustomerAddress.label == label,
    )
    if (await ctx.session.execute(stmt)).first() is not None:
        ctx.note(STAGE, existing=1)
        return

    await service.create_address(
        customer_id,
        AddressCreate(
            address_type=AddressType.BILLING,
            label=label,
            address_line1=spec.address_line1,
            city=spec.city,
            state=spec.state,
            # Mandatory for a domestic address: without a state code the invoice
            # cannot decide between CGST+SGST and IGST.
            state_code=spec.state_code,
            postal_code=spec.postal_code,
            country="India",
            gstin=spec.gstin,
            contact_person=spec.contact_name,
            phone=spec.contact_phone,
        ),
    )
    ctx.note(STAGE, created=1)


async def _seed_customer_contact(
    ctx: DemoContext, service: CustomerService, customer_id: uuid.UUID, spec: _CustomerSpec
) -> None:
    """One named contact per account, keyed by name so a re-run is a no-op."""
    stmt = select(CustomerContact.id).where(
        CustomerContact.company_id == ctx.company_id,
        CustomerContact.customer_id == customer_id,
        CustomerContact.name == spec.contact_name,
    )
    if (await ctx.session.execute(stmt)).first() is not None:
        ctx.note(STAGE, existing=1)
        return

    await service.create_contact(
        customer_id,
        ContactCreate(
            name=spec.contact_name,
            designation=spec.contact_designation,
            phone=spec.contact_phone,
        ),
    )
    ctx.note(STAGE, created=1)


async def _seed_vendors(ctx: DemoContext, buyer: TenantContext | None) -> None:
    """Vendors, their contacts, their category qualifications, and approval.

    Created as the buyer and approved as the administrator, because a vendor may
    not be approved by whoever entered it. The approval is not decoration: an
    unapproved vendor fails
    :func:`~src.modules.vendors.service.can_receive_purchase_orders`, so the
    procurement stage would find nobody it is allowed to raise an order on.
    """
    author = buyer or ctx.tenant
    vendors = VendorService(ctx.session, author)
    approver = VendorService(ctx.session, ctx.tenant)
    contacts = VendorContactService(ctx.session, author)
    categories = VendorCategoryService(ctx.session, author)

    for spec in VENDORS:
        code = ctx.code(*spec.code)
        existing = await find_by_code(ctx.session, Vendor, ctx.company_id, code)
        if existing is not None:
            vendor_id = _seen(ctx, "vendor", code, existing.id, created=False)
            is_approved = existing.is_approved
        else:
            created = await vendors.create_vendor(
                VendorCreate(
                    code=code,
                    name=spec.name,
                    legal_name=spec.legal_name,
                    vendor_types=list(spec.vendor_types),
                    gstin=spec.gstin,
                    pan=spec.gstin[2:12],
                    payment_terms_days=spec.payment_terms_days,
                    lead_time_days=spec.lead_time_days,
                    currency="INR",
                    # Bank details are not decoration here: a vendor with no
                    # payee on file cannot be approved at all, and an unapproved
                    # vendor cannot be bought from.
                    bank_name=spec.bank_name,
                    bank_branch=spec.bank_branch,
                    bank_account_name=spec.legal_name,
                    bank_account_number=spec.bank_account_number,
                    bank_ifsc=spec.bank_ifsc,
                    address_line1=spec.address_line1,
                    city=spec.city,
                    state=spec.state,
                    state_code=spec.state_code,
                    postal_code=spec.postal_code,
                    country="India",
                    contact_person=spec.owner_name,
                    phone=spec.owner_phone,
                    notes=spec.notes,
                )
            )
            vendor_id = _seen(ctx, "vendor", code, created.id, created=True)
            is_approved = created.is_approved

        await _seed_vendor_contacts(ctx, contacts, vendor_id, spec)
        await _seed_vendor_categories(ctx, categories, vendor_id, spec)

        # Guarded: APPROVED -> APPROVED is not a legal transition, so a second
        # run must not attempt it.
        if is_approved:
            continue
        try:
            await approver.approve_vendor(
                vendor_id,
                reason="Documents and bank details verified for the demonstration set.",
            )
        except (AuthorizationError, BusinessRuleError) as exc:
            # Almost always the maker-checker rule, reached when the buyer
            # account could not be created and the administrator entered the
            # vendor itself. Left pending rather than forced: that flag is what
            # procurement reads, and faking it would hide a real gap.
            ctx.skipped.append((STAGE, f"vendor {code} left pending approval: {exc}"))


async def _seed_vendor_contacts(
    ctx: DemoContext, service: VendorContactService, vendor_id: uuid.UUID, spec: _VendorSpec
) -> None:
    """An owner and an accounts contact, so the vendor portal has people in it."""
    people: tuple[tuple[str, VendorContactRole, str | None, str | None, bool], ...] = (
        (spec.owner_name, VendorContactRole.OWNER, None, spec.owner_phone, True),
        (spec.accounts_name, VendorContactRole.ACCOUNTS, spec.accounts_email, None, False),
    )
    for name, role, email, phone, is_primary in people:
        stmt = select(VendorContact.id).where(
            VendorContact.company_id == ctx.company_id,
            VendorContact.vendor_id == vendor_id,
            VendorContact.name == name,
        )
        if (await ctx.session.execute(stmt)).first() is not None:
            ctx.note(STAGE, existing=1)
            continue
        await service.create_contact(
            vendor_id,
            VendorContactCreate(
                name=name,
                contact_role=role,
                designation="Proprietor" if role is VendorContactRole.OWNER else "Accounts",
                email=email,
                phone=phone,
                is_primary=is_primary,
            ),
        )
        ctx.note(STAGE, created=1)


async def _seed_vendor_categories(
    ctx: DemoContext, service: VendorCategoryService, vendor_id: uuid.UUID, spec: _VendorSpec
) -> None:
    """What this vendor is qualified to supply.

    The sourcing screen and the MRP purchase suggestion both read these rows, so
    a vendor with no qualification never gets proposed for anything at all.
    """
    for category_code, is_preferred in spec.categories:
        category_id = ctx.maybe_id("category", category_code)
        if category_id is None:
            ctx.skipped.append((STAGE, f"no category {category_code} to qualify {spec.name} for"))
            continue

        stmt = select(VendorCategory.id).where(
            VendorCategory.company_id == ctx.company_id,
            VendorCategory.vendor_id == vendor_id,
            VendorCategory.item_category_id == category_id,
        )
        if (await ctx.session.execute(stmt)).first() is not None:
            ctx.note(STAGE, existing=1)
            continue

        await service.assign(
            vendor_id,
            VendorCategoryAssign(item_category_id=category_id, is_preferred=is_preferred),
        )
        ctx.note(STAGE, created=1)


async def _seed_item_vendor_links(ctx: DemoContext, anchor: date) -> None:
    """Who supplies each bought item, at what price, and how long they take.

    ``last_purchase_price`` sits a little under the standard cost on every line,
    which is the normal direction of travel and leaves the purchase price
    variance report a non-zero number to show.
    """
    last_purchase = anchor - timedelta(days=LAST_PURCHASE_DAYS_AGO)

    for item_parts, vendor_parts, vendor_item_code, price, lead_time in ITEM_VENDORS:
        item_id = ctx.id_of("item", ctx.code(*item_parts))
        vendor_id = ctx.maybe_id("vendor", ctx.code(*vendor_parts))
        if vendor_id is None:
            ctx.skipped.append((STAGE, f"no vendor {ctx.code(*vendor_parts)} to link to an item"))
            continue

        maker = LINK_MAKERS.get((item_parts, vendor_parts))
        maker_id = ctx.maybe_id("manufacturer", ctx.code(*maker[0])) if maker else None
        # The part number belongs to the maker, so it goes nowhere without one.
        maker_part_number = maker[1] if maker is not None and maker_id is not None else None

        stmt = select(ItemVendorLink).where(
            ItemVendorLink.company_id == ctx.company_id,
            ItemVendorLink.item_id == item_id,
            ItemVendorLink.vendor_id == vendor_id,
        )
        existing = (await ctx.session.execute(stmt)).scalars().first()
        if existing is not None:
            # A link made before makers existed gains its maker; one that already
            # names a maker - or that a client has retired - is left as it is.
            if (
                maker_id is not None
                and existing.manufacturer_id is None
                and existing.deleted_at is None
            ):
                await update_vendor_link(
                    ctx.session,
                    ctx.tenant,
                    existing.id,
                    VendorLinkUpdate(
                        manufacturer_id=maker_id, manufacturer_part_number=maker_part_number
                    ),
                )
            ctx.note(STAGE, existing=1)
            continue

        await link_vendor(
            ctx.session,
            ctx.tenant,
            item_id,
            VendorLinkCreate(
                vendor_id=vendor_id,
                vendor_item_code=vendor_item_code,
                manufacturer_id=maker_id,
                manufacturer_part_number=maker_part_number,
                is_preferred=True,
                lead_time_days=lead_time,
                last_purchase_price=Decimal(price),
                last_purchase_date=last_purchase,
                currency="INR",
            ),
        )
        ctx.note(STAGE, created=1)


async def _seed_vendor_portal_users(ctx: DemoContext) -> None:
    """One vendor manager per category, at the vendors that supply them.

    The same mechanism as :func:`_seed_staff`: created through the users
    service, keyed by e-mail, with a generated password the account must change
    - nothing signs in as them, and a literal would be a credential in source
    control. An administrator gives a demonstrator a sign-in with Reset
    password, which shows a fresh temporary one. Skipped, with the reason, when
    the vendor, the role or a category is missing.
    """
    service = UserService(ctx.session, ctx.tenant)
    for parts, full_name, email, vendor_parts, role_code, category_codes in VENDOR_PORTAL_USERS:
        code = ctx.code(*parts)
        stmt = select(User).where(User.company_id == ctx.company_id, User.email == email)
        existing = (await ctx.session.execute(stmt)).scalars().first()
        if existing is not None:
            _seen(ctx, "user", code, existing.id, created=False)
            continue

        vendor_id = ctx.maybe_id("vendor", ctx.code(*vendor_parts))
        role = await find_by_code(ctx.session, Role, ctx.company_id, role_code)
        category_ids = [ctx.maybe_id("category", category) for category in category_codes]
        scope = [category_id for category_id in category_ids if category_id is not None]
        if vendor_id is None or role is None or len(scope) != len(category_ids):
            ctx.skipped.append(
                (
                    STAGE,
                    f"vendor portal account {email} not created: vendor, role or category missing",
                )
            )
            continue
        try:
            detail = await service.create_user(
                ctx.principal,
                UserCreate(
                    email=email,
                    full_name=full_name,
                    password=generate_temporary_password(email),
                    vendor_id=vendor_id,
                    allowed_category_ids=scope,
                    role_ids=[role.id],
                ),
            )
        except (BusinessRuleError, DuplicateError, NotFoundError, AuthorizationError) as exc:
            ctx.skipped.append((STAGE, f"vendor portal account {email} not created: {exc}"))
            continue
        _seen(ctx, "user", code, detail.id, created=True)


async def seed(ctx: DemoContext) -> None:
    """Fill in the master data every later stage addresses by code.

    The order is a dependency order and is not arbitrary: items need units,
    warehouses and categories; prices need items; customers name a carrier and a
    salesperson; vendors are entered by the buyer and approved by somebody else;
    the item-vendor links need both sides to exist already; and a vendor
    manager's category scope must name categories the vendor is qualified for.
    """
    # One reading of the clock for the whole stage - see PRICE_START_DAYS_AGO.
    anchor = today()

    await _seed_units(ctx)
    # Organisation first: departments name a default cost centre, and later
    # stages tag work centres, orders and bills with these codes.
    await _seed_cost_centres(ctx)
    await _seed_departments(ctx)
    await _seed_item_attributes(ctx)
    await _seed_manufacturers(ctx)
    await _seed_brands(ctx)
    await _seed_warehouses(ctx)
    await _seed_locations(ctx)
    await _seed_categories(ctx)
    await _seed_items(ctx)
    await _seed_item_branding(ctx)
    await _seed_prices(ctx, anchor)

    buyer = await _seed_staff(ctx)
    await _seed_transporters(ctx)
    await _seed_customers(ctx)
    await _seed_vendors(ctx, buyer)
    await _seed_item_vendor_links(ctx, anchor)
    await _seed_vendor_portal_users(ctx)
