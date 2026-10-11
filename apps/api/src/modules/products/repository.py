"""Queries for the item master.

The interesting piece is :meth:`ItemRepository.search`, which has to stay usable
on a 50k-row item master. Three choices make that true:

* **Everything is filtered in SQL.** Capability flags, category subtree, active
  state and the search term all become predicates. Nothing is loaded and then
  discarded in Python.
* **The category subtree is one predicate, not a recursion.** The materialised
  path lets "everything under Hardware" be expressed as a correlated subquery
  against ``item_categories`` - a single statement, no per-node query
  (section 32).
* **List rows load an explicit column set.** ``load_only(..., raiseload=True)``
  means the SELECT names the columns the list actually renders, and touching an
  unloaded one raises instead of silently issuing another query per row
  (section 30: no SELECT * on a wide table).
* :meth:`ItemRepository.has_stock_balance` reaches into ``inventory_balances``
  with raw SQL rather than importing the inventory module. The item master sits
  *below* the inventory engine in the dependency order - inventory reads
  ``items``, not the other way round - and the table may not exist yet in a
  partially migrated database, which is what the ``to_regclass`` probe handles.
  Same shape, and for the same reasons, as
  ``WarehouseRepository.has_stock_balance``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, exists, func, or_, select, text, update
from sqlalchemy.orm import InstrumentedAttribute, aliased, load_only

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import CursorParams, PageParams, SortParams
from src.core.responses import CursorPagination, OffsetPagination
from src.modules.products.models import (
    Brand,
    Item,
    ItemAttribute,
    ItemAttributeType,
    ItemCategory,
    ItemPrice,
    ItemVariant,
    ItemVendorLink,
    Manufacturer,
    PriceType,
    UnitOfMeasure,
    UomConversion,
)
from src.modules.products.schemas import ItemFilters
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse

#: Balance columns that mean "this item is still holding something, somewhere".
#: ``available_quantity`` is deliberately absent: it is a generated column
#: derived from these, so including it would double-count. ``in_transit`` and
#: ``job_work`` matter as much as ``on_hand`` here - they are precisely the
#: quantities an in-flight document has already moved out of on-hand, and the
#: ones a guard that looked only at on-hand would wave through.
_ITEM_BALANCE_COLUMNS = (
    "on_hand_quantity",
    "reserved_quantity",
    "allocated_quantity",
    "in_transit_quantity",
    "qc_hold_quantity",
    "damaged_quantity",
    "job_work_quantity",
    "scrap_quantity",
)

_NON_ZERO_BALANCE = " OR ".join(f"COALESCE({c}, 0) <> 0" for c in _ITEM_BALANCE_COLUMNS)

# Built once from the constant column list above - no request data is ever
# interpolated; the parameters are bound.
_ITEM_BALANCE_SQL = (
    "SELECT 1 FROM inventory_balances "  # noqa: S608 - only the constant above is interpolated
    "WHERE company_id = :company_id AND item_id = :item_id "
    f"AND ({_NON_ZERO_BALANCE}) LIMIT 1"
)


async def _inventory_balances_exists(session: Any) -> bool:
    """Is the ``inventory_balances`` table present in this database?

    ``to_regclass`` returns NULL instead of raising for an unknown relation,
    which is the only way to ask the question without aborting the surrounding
    transaction. It must be a separate round trip: a statement that *references*
    a missing table fails at parse time, so the probe cannot be folded into the
    same SELECT.
    """
    found = await session.execute(text("SELECT to_regclass('public.inventory_balances')"))
    return found.scalar() is not None


#: How a brand or a manufacturer is named wherever the application names one -
#: by ``get_or_404``, by the service's own messages and by the lifecycle
#: machines - so one record never answers to two spellings.
BRAND_ENTITY = "Brand"
MANUFACTURER_ENTITY = "Manufacturer"


#: Columns every item list row needs. Cost is deliberately absent - it is added
#: only for a principal holding ``item.cost_view``. Every field of
#: ``ItemSummary`` must be here: the list is loaded with ``raiseload``, so a
#: field the schema reads and this tuple omits fails every item list.
ITEM_SUMMARY_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    Item.id,
    Item.code,
    Item.name,
    Item.item_group,
    Item.category_id,
    Item.brand_id,
    Item.base_uom_id,
    Item.production_type,
    Item.sale_price,
    Item.is_active,
    Item.can_purchase,
    Item.can_manufacture,
    Item.can_sell,
    Item.can_be_bom_component,
    Item.can_be_assembly_component,
    Item.can_be_spare,
    Item.can_job_work,
    # Keyset pagination orders on these, so they must be loaded.
    Item.created_at,
)


class ItemCategoryRepository(BaseRepository[ItemCategory]):
    model = ItemCategory
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012 - class-level configuration, mirrors the base class
        "code": ItemCategory.code,
        "name": ItemCategory.name,
        "depth": ItemCategory.depth,
        "created_at": ItemCategory.created_at,
    }

    async def get_by_code(self, code: str) -> ItemCategory | None:
        return await self.get_by_field("code", code)

    async def list_children(self, parent_id: uuid.UUID | None) -> list[ItemCategory]:
        """Direct children only - what a lazily-expanded tree control needs.

        Bounded by the branching factor of the tree rather than by page size,
        which is why it is not paginated: a category with thousands of direct
        children is a data-entry error, not a use case.
        """
        stmt = self._scoped().where(ItemCategory.parent_id == parent_id).order_by(ItemCategory.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_subtree(self, root: ItemCategory) -> list[ItemCategory]:
        """The root and everything beneath it, in path order.

        One indexed range scan on the materialised path. Used by re-parenting,
        which has to rewrite every descendant's path.
        """
        stmt = (
            self._scoped()
            .where(ItemCategory.path.like(f"{root.path}%"))
            .order_by(ItemCategory.path)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def has_items(self, category_id: uuid.UUID) -> bool:
        """EXISTS probe used before retiring a category."""
        stmt = select(
            exists().where(
                Item.company_id == self.tenant.company_id,
                Item.category_id == category_id,
                Item.deleted_at.is_(None),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def has_children(self, category_id: uuid.UUID) -> bool:
        stmt = select(
            exists().where(
                ItemCategory.company_id == self.tenant.company_id,
                ItemCategory.parent_id == category_id,
                ItemCategory.deleted_at.is_(None),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def list_page_filtered(
        self,
        params: PageParams,
        *,
        parent_id: uuid.UUID | None = None,
        only_roots: bool = False,
        search: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[ItemCategory], OffsetPagination]:
        stmt = self._scoped()
        if only_roots:
            stmt = stmt.where(ItemCategory.parent_id.is_(None))
        elif parent_id is not None:
            stmt = stmt.where(ItemCategory.parent_id == parent_id)
        if is_active is not None:
            stmt = stmt.where(ItemCategory.is_active.is_(is_active))
        stmt = self._apply_search(stmt, search)
        return await self.list_page(params, stmt=stmt, order_by=ItemCategory.path)

    async def subtree_max_depth(self, path_prefix: str) -> int:
        """Deepest level anywhere under ``path_prefix``.

        One aggregate rather than loading the subtree, because the only thing
        re-parenting needs to know is whether the move would breach the nesting
        limit.
        """
        stmt = select(func.max(ItemCategory.depth)).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.deleted_at.is_(None),
            ItemCategory.path.like(f"{path_prefix}%"),
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def rewrite_subtree_paths(
        self, *, old_prefix: str, new_prefix: str, depth_delta: int
    ) -> int:
        """Move a subtree in one UPDATE.

        Re-parenting is the price of path materialisation. Doing it as a single
        statement keeps that price at one round-trip regardless of how many
        descendants exist, instead of a Python loop of updates.
        """
        stmt = (
            update(ItemCategory)
            .where(
                ItemCategory.company_id == self.tenant.company_id,
                ItemCategory.path.like(f"{old_prefix}%"),
            )
            .values(
                path=func.concat(new_prefix, func.substr(ItemCategory.path, len(old_prefix) + 1)),
                depth=ItemCategory.depth + depth_delta,
                updated_by_id=self.tenant.user_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


class UnitOfMeasureRepository(BaseRepository[UnitOfMeasure]):
    model = UnitOfMeasure
    entity_label = "Unit of measure"
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012
        "code": UnitOfMeasure.code,
        "name": UnitOfMeasure.name,
    }

    async def get_by_code(self, code: str) -> UnitOfMeasure | None:
        return await self.get_by_field("code", code)

    async def list_all(self) -> list[UnitOfMeasure]:
        """Every unit for the tenant.

        Not paginated by deliberate exception to section 31: a company has tens
        of units, the set is loaded whole to resolve conversions, and paging it
        would force the conversion resolver into N round-trips.
        """
        stmt = self._scoped().order_by(UnitOfMeasure.uom_category, UnitOfMeasure.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page_search(
        self, params: PageParams, *, search: str | None = None, is_active: bool | None = None
    ) -> tuple[list[UnitOfMeasure], OffsetPagination]:
        stmt = self._scoped()
        if is_active is not None:
            stmt = stmt.where(UnitOfMeasure.is_active.is_(is_active))
        stmt = self._apply_search(stmt, search)
        return await self.list_page(params, stmt=stmt, order_by=UnitOfMeasure.code.asc())

    async def base_for_category(self, uom_category: str) -> UnitOfMeasure | None:
        stmt = self._scoped().where(
            UnitOfMeasure.uom_category == uom_category,
            UnitOfMeasure.is_base.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def is_used_by_items(self, uom_id: uuid.UUID) -> bool:
        stmt = select(
            exists().where(
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                or_(
                    Item.base_uom_id == uom_id,
                    Item.purchase_uom_id == uom_id,
                    Item.sale_uom_id == uom_id,
                ),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())


class ItemAttributeRepository(BaseRepository[ItemAttribute]):
    model = ItemAttribute
    entity_label = "Item attribute"
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012
        "code": ItemAttribute.code,
        "name": ItemAttribute.name,
        "sort_order": ItemAttribute.sort_order,
    }

    async def get_by_code(self, attribute: ItemAttributeType, code: str) -> ItemAttribute | None:
        """Scoped by kind as well as code - the pair is what is unique."""
        stmt = self._scoped().where(
            ItemAttribute.attribute == attribute, ItemAttribute.code == code
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page_search(
        self,
        params: PageParams,
        *,
        attribute: ItemAttributeType | None = None,
        search: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[ItemAttribute], OffsetPagination]:
        stmt = self._scoped()
        if attribute is not None:
            stmt = stmt.where(ItemAttribute.attribute == attribute)
        if is_active is not None:
            stmt = stmt.where(ItemAttribute.is_active.is_(is_active))
        stmt = self._apply_search(stmt, search)
        # `list_page` takes ONE order clause, not a tuple - a tuple reaches
        # `query.order_by(...)` as a single argument and SQLAlchemy raises
        # "ORDER BY expression expected". The multi-column order goes on the
        # statement; the single clause below is what `list_page` appends.
        stmt = stmt.order_by(ItemAttribute.attribute.asc(), ItemAttribute.sort_order.asc())
        return await self.list_page(params, stmt=stmt, order_by=ItemAttribute.code.asc())

    async def is_used(self, attribute_id: uuid.UUID) -> bool:
        """Whether any live item or variant still points at this value.

        Both tables and all six columns, because retiring a finish that a
        variant still carries would leave that variant describing itself with a
        value the picker no longer offers.
        """
        item_used = exists().where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            or_(
                Item.finish_id == attribute_id,
                Item.size_id == attribute_id,
                Item.colour_id == attribute_id,
            ),
        )
        variant_used = exists().where(
            ItemVariant.company_id == self.tenant.company_id,
            ItemVariant.deleted_at.is_(None),
            or_(
                ItemVariant.finish_id == attribute_id,
                ItemVariant.size_id == attribute_id,
                ItemVariant.colour_id == attribute_id,
            ),
        )
        stmt = select(or_(item_used, variant_used))
        return bool((await self.session.execute(stmt)).scalar())


async def _code_taken(repo: BaseRepository[Any], model: Any, code: str) -> bool:
    """Has this company ever used ``code`` for this master - retired rows included?

    The unique constraint covers soft-deleted rows too, so a probe that skipped
    them would report "available" for a retired code and then lose to an
    IntegrityError on INSERT. A retired brand's code also still sits on the
    audit trail, which is the business reason it is never reissued
    (ASSUMPTION: docs/assumptions.md §A1-1).
    """
    stmt = (
        select(model.id)
        .where(model.company_id == repo.tenant.company_id, model.code == code)
        .limit(1)
    )
    return (await repo.session.execute(stmt)).first() is not None


# Assigning a brand or maker and deactivating or retiring it are both
# check-then-act, and they check each other's writes: the deactivation asks
# "does anything live still name this?", the assignment asks "is this still
# offered?". Unserialised, each check can pass against a world the other is
# about to change - the deactivation's probe cannot see an item whose INSERT has
# not committed, and the assignment reads the last committed version of a row
# an uncommitted deactivation is rewriting. The foreign key does not help: its
# check takes FOR KEY SHARE, which an UPDATE that changes no key column does not
# conflict with. So an assignment reads the row FOR SHARE and a lifecycle change
# reads it FOR NO KEY UPDATE. Those two conflict, so whichever comes second
# waits and then - under READ COMMITTED, re-reading the row once the lock is
# granted - decides on the first one's committed result. Neither lock blocks the
# foreign-key checks of unrelated writes, nor a plain read.
# tests/integration/test_brand_manufacturer_concurrency.py proves both orders.


def _locked_read(stmt: Select[Any], *, share: bool) -> Select[Any]:
    """``stmt`` as a locked re-read: ``FOR SHARE``, or ``FOR NO KEY UPDATE``.

    ``populate_existing`` is not optional, for the reason the inventory and
    allocation repositories give: without it SQLAlchemy hands back the identity
    map's copy of a row the session already holds, so the lock is taken, the
    row is re-read under it, and the answer is thrown away.
    """
    locked = stmt.with_for_update(read=True) if share else stmt.with_for_update(key_share=True)
    return locked.execution_options(populate_existing=True)


@dataclass(frozen=True, slots=True)
class ManufacturerUsage:
    """Which live rows still name a manufacturer - one probe, three answers.

    Three answers rather than one boolean so a refusal can say *what* still
    points at the maker, which is the thing the person has to go and change.
    """

    items: bool
    vendor_links: bool
    brands: bool

    @property
    def in_use(self) -> bool:
        return self.items or self.vendor_links or self.brands

    def holders(self) -> list[str]:
        """The kinds still referencing the maker, in the words a person uses."""
        named = (
            (self.items, "items"),
            (self.vendor_links, "item-vendor links"),
            (self.brands, "brands"),
        )
        return [label for present, label in named if present]


class ManufacturerRepository(BaseRepository[Manufacturer]):
    model = Manufacturer
    entity_label = MANUFACTURER_ENTITY
    searchable_fields = ("code", "name", "legal_name")
    sortable_fields = {  # noqa: RUF012
        "code": Manufacturer.code,
        "name": Manufacturer.name,
    }

    async def code_taken(self, code: str) -> bool:
        return await _code_taken(self, Manufacturer, code)

    async def get_for_assignment(self, manufacturer_id: uuid.UUID) -> Manufacturer | None:
        """Read a live maker about to be named somewhere, ``FOR SHARE``.

        See the note above ``ManufacturerUsage``: a deactivation or retirement in
        flight makes this wait, and the row it then returns is the committed one.
        """
        stmt = _locked_read(self._scoped().where(Manufacturer.id == manufacturer_id), share=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_update(self, manufacturer_id: uuid.UUID) -> Manufacturer | None:
        """Read a live maker about to change, ``FOR NO KEY UPDATE``.

        Waits for any assignment of it in flight, so the "still in use?" probe
        that follows sees that assignment's committed rows.
        """
        stmt = _locked_read(self._scoped().where(Manufacturer.id == manufacturer_id), share=False)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def build_list_stmt(
        self, *, search: str | None, is_active: bool | None, sort: SortParams
    ) -> Select[Any]:
        stmt = self._scoped()
        if is_active is not None:
            stmt = stmt.where(Manufacturer.is_active.is_(is_active))
        stmt = self._apply_search(stmt, search)
        # `resolve` returns the default untouched when no column was asked for,
        # so the default here is None and the picker order (name) is spelled
        # out below rather than through `or` - a clause has no truth value.
        chosen = sort.resolve(self.sortable_fields, None)
        return stmt.order_by(Manufacturer.name.asc() if chosen is None else chosen)

    async def list_page_search(
        self,
        params: PageParams,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        sort: SortParams | None = None,
    ) -> tuple[list[Manufacturer], OffsetPagination]:
        stmt = self.build_list_stmt(search=search, is_active=is_active, sort=sort or SortParams())
        # ONE clause for `list_page`: the id, as the tiebreak for a sort column
        # whose values repeat, so rows cannot repeat or vanish across pages.
        return await self.list_page(params, stmt=stmt, order_by=Manufacturer.id.asc())

    async def usage(self, manufacturer_id: uuid.UUID) -> ManufacturerUsage:
        """Live items, live item-vendor links and live brands naming the maker.

        One statement, three EXISTS. "Live" is ``deleted_at IS NULL``: an
        inactive item still shows its maker on screen, so it still counts.
        """
        company_id = self.tenant.company_id
        on_items = exists().where(
            Item.company_id == company_id,
            Item.manufacturer_id == manufacturer_id,
            Item.deleted_at.is_(None),
        )
        on_links = exists().where(
            ItemVendorLink.company_id == company_id,
            ItemVendorLink.manufacturer_id == manufacturer_id,
            ItemVendorLink.deleted_at.is_(None),
        )
        on_brands = exists().where(
            Brand.company_id == company_id,
            Brand.manufacturer_id == manufacturer_id,
            Brand.deleted_at.is_(None),
        )
        stmt = select(
            on_items.label("items"), on_links.label("vendor_links"), on_brands.label("brands")
        )
        row = (await self.session.execute(stmt)).one()
        return ManufacturerUsage(
            items=bool(row.items), vendor_links=bool(row.vendor_links), brands=bool(row.brands)
        )


class BrandRepository(BaseRepository[Brand]):
    model = Brand
    entity_label = BRAND_ENTITY
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012
        "code": Brand.code,
        "name": Brand.name,
        "sort_order": Brand.sort_order,
    }

    async def code_taken(self, code: str) -> bool:
        return await _code_taken(self, Brand, code)

    async def get_for_assignment(self, brand_id: uuid.UUID) -> Brand | None:
        """Read a live brand about to be given to an item, ``FOR SHARE``.

        See the note above ``ManufacturerUsage``: a deactivation or retirement in
        flight makes this wait, and the row it then returns is the committed one.
        """
        stmt = _locked_read(self._scoped().where(Brand.id == brand_id), share=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_update(self, brand_id: uuid.UUID) -> Brand | None:
        """Read a live brand about to change, ``FOR NO KEY UPDATE``.

        Waits for any assignment of it in flight, so the "still in use?" probe
        that follows sees that assignment's committed item.
        """
        stmt = _locked_read(self._scoped().where(Brand.id == brand_id), share=False)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def build_list_stmt(
        self,
        *,
        search: str | None,
        is_active: bool | None,
        manufacturer_id: uuid.UUID | None,
        sort: SortParams,
    ) -> Select[Any]:
        stmt = self._scoped()
        if is_active is not None:
            stmt = stmt.where(Brand.is_active.is_(is_active))
        if manufacturer_id is not None:
            stmt = stmt.where(Brand.manufacturer_id == manufacturer_id)
        stmt = self._apply_search(stmt, search)
        chosen = sort.resolve(self.sortable_fields, None)
        if chosen is None:
            # The picker order: the client's own position first, then the name
            # a person reads. Both on the statement, because `list_page` below
            # takes one clause and that one is the id tiebreak.
            return stmt.order_by(Brand.sort_order.asc(), Brand.name.asc())
        return stmt.order_by(chosen)

    async def list_page_search(
        self,
        params: PageParams,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        manufacturer_id: uuid.UUID | None = None,
        sort: SortParams | None = None,
    ) -> tuple[list[Brand], OffsetPagination]:
        stmt = self.build_list_stmt(
            search=search,
            is_active=is_active,
            manufacturer_id=manufacturer_id,
            sort=sort or SortParams(),
        )
        return await self.list_page(params, stmt=stmt, order_by=Brand.id.asc())

    async def is_used(self, brand_id: uuid.UUID) -> bool:
        """Does any live item carry this brand? Served by ``ix_items_company_brand``."""
        stmt = select(
            exists().where(
                Item.company_id == self.tenant.company_id,
                Item.brand_id == brand_id,
                Item.deleted_at.is_(None),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())


class UomConversionRepository(BaseRepository[UomConversion]):
    model = UomConversion
    entity_label = "Unit conversion"

    async def list_all(self) -> list[UomConversion]:
        """The whole conversion table for the tenant.

        Same reasoning as :meth:`UnitOfMeasureRepository.list_all`: the resolver
        needs the graph, and the graph is small (units per company, not items).
        """
        stmt = self._scoped().where(UomConversion.is_active.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_pair(self, from_uom_id: uuid.UUID, to_uom_id: uuid.UUID) -> UomConversion | None:
        stmt = self._scoped().where(
            UomConversion.from_uom_id == from_uom_id,
            UomConversion.to_uom_id == to_uom_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ItemRepository(BaseRepository[Item]):
    model = Item
    searchable_fields = ("code", "name")
    sortable_fields = {  # noqa: RUF012
        "code": Item.code,
        "name": Item.name,
        "created_at": Item.created_at,
        "updated_at": Item.updated_at,
    }

    async def get_by_code(self, code: str) -> Item | None:
        return await self.get_by_field("code", code)

    async def code_exists(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Duplicate probe.

        Includes soft-deleted rows on purpose: the unique constraint covers them
        too, so reporting "available" for a retired code would only turn into an
        IntegrityError on INSERT.
        """
        stmt = select(Item.id).where(
            Item.company_id == self.tenant.company_id,
            Item.code == code,
        )
        if exclude_id is not None:
            stmt = stmt.where(Item.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    # --- cross-module existence probes -------------------------------------

    async def vendor_exists(self, vendor_id: uuid.UUID) -> bool:
        return await self._tenant_row_exists(Vendor, vendor_id)

    async def warehouse_exists(self, warehouse_id: uuid.UUID) -> bool:
        return await self._tenant_row_exists(Warehouse, warehouse_id)

    async def _tenant_row_exists(self, model: Any, entity_id: uuid.UUID) -> bool:
        """EXISTS probe against another module's master table, tenant-scoped.

        ``item_vendor_links.vendor_id`` and ``items.default_warehouse_id`` are
        plain foreign keys to globally unique ids, so a uuid belonging to
        another company's vendor or warehouse satisfies the constraint. The
        tenant filter is what makes the reference safe (section 40); the users
        and warehouses modules guard their references the same way.
        """
        stmt = select(
            exists().where(
                model.id == entity_id,
                model.company_id == self.tenant.company_id,
                model.deleted_at.is_(None),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def has_stock_balance(self, item_id: uuid.UUID) -> bool:
        """Does this item hold anything, anywhere in this company?

        One indexed probe over every position the item has: on hand, reserved,
        in transit on a vehicle, out at a job-work vendor, held, damaged or set
        aside as scrap. Any of those is "there is stock whose identification
        cannot be changed underneath it".

        Deliberately *not* an import of the inventory module - see the note at
        the top of this file. Returns False when ``inventory_balances`` does not
        exist yet, which is correct: a table that has never been created cannot
        hold stock.
        """
        if not await _inventory_balances_exists(self.session):
            return False

        found = await self.session.execute(
            text(_ITEM_BALANCE_SQL),
            {"company_id": self.tenant.company_id, "item_id": item_id},
        )
        return found.first() is not None

    async def get_codes(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Batch id -> code map, for audit summaries and cross-module labelling."""
        if not ids:
            return {}
        stmt = select(Item.id, Item.code).where(
            Item.company_id == self.tenant.company_id,
            Item.id.in_(list(ids)),
        )
        return {row.id: row.code for row in (await self.session.execute(stmt)).all()}

    # --- search --------------------------------------------------------------

    def _subtree_predicate(
        self, category_id: uuid.UUID, include_subcategories: bool
    ) -> ColumnElement[bool]:
        """Category filter, optionally covering the whole subtree.

        The subtree form is one statement: an uncorrelated scalar subquery reads
        the root's materialised path, and the descendant test becomes an indexed
        ``LIKE 'path%'`` range scan. No recursion, no query per node (section 32).
        """
        if not include_subcategories:
            return Item.category_id == category_id

        root_path = (
            select(ItemCategory.path)
            .where(
                ItemCategory.id == category_id,
                ItemCategory.company_id == self.tenant.company_id,
            )
            .scalar_subquery()
        )
        node = aliased(ItemCategory, name="subtree_category")
        descendants = select(node.id).where(
            node.company_id == self.tenant.company_id,
            node.path.like(func.concat(root_path, "%")),
        )
        return Item.category_id.in_(descendants)

    def build_search_statement(self, filters: ItemFilters, *, include_cost: bool) -> Select[Any]:
        """Assemble the filtered SELECT for the item search."""
        columns = list(ITEM_SUMMARY_COLUMNS)
        if include_cost:
            columns.append(Item.standard_cost)

        # raiseload=True turns "someone serialised a column the list did not
        # select" into a loud error instead of one extra query per row.
        stmt = self._scoped().options(load_only(*columns, raiseload=True))

        if filters.is_active is not None:
            stmt = stmt.where(Item.is_active.is_(filters.is_active))
        if filters.item_group is not None:
            stmt = stmt.where(Item.item_group == filters.item_group)
        if filters.production_type is not None:
            stmt = stmt.where(Item.production_type == filters.production_type)
        if filters.brand_id is not None:
            stmt = stmt.where(Item.brand_id == filters.brand_id)
        if filters.manufacturer_id is not None:
            stmt = stmt.where(Item.manufacturer_id == filters.manufacturer_id)
        if filters.default_warehouse_id is not None:
            stmt = stmt.where(Item.default_warehouse_id == filters.default_warehouse_id)
        if filters.is_batch_tracked is not None:
            stmt = stmt.where(Item.is_batch_tracked.is_(filters.is_batch_tracked))
        if filters.is_serial_tracked is not None:
            stmt = stmt.where(Item.is_serial_tracked.is_(filters.is_serial_tracked))

        # Capability flags: independent booleans, ANDed. "Sellable spares" is a
        # legitimate question and must not require a client-side intersection.
        for flag, wanted in filters.capability_filters.items():
            stmt = stmt.where(getattr(Item, flag).is_(wanted))

        if filters.category_id is not None:
            stmt = stmt.where(
                self._subtree_predicate(filters.category_id, filters.include_subcategories)
            )

        if filters.search:
            # ILIKE '%term%' on both trigram-indexed columns; PostgreSQL serves
            # this from a BitmapOr of the two GIN indexes (section 62).
            pattern = f"%{filters.search}%"
            stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))

        # A vendor principal may only see the categories it is scoped to
        # (section 16). Applied here so no caller can forget it.
        if self.tenant.vendor_id is not None and self.tenant.allowed_category_ids:
            stmt = stmt.where(Item.category_id.in_(list(self.tenant.allowed_category_ids)))

        return stmt

    async def search_page(
        self,
        filters: ItemFilters,
        params: PageParams,
        *,
        include_cost: bool,
        order_by: Any = None,
    ) -> tuple[list[Item], OffsetPagination]:
        """Offset pagination - the default for a filtered master-data browse."""
        stmt = self.build_search_statement(filters, include_cost=include_cost)
        # `order_by or Item.code.asc()` raises: a SQLAlchemy clause has no
        # truth value, so `or` evaluating the left operand throws
        # ``TypeError: Boolean value of this clause is not defined`` for every
        # request that actually names a sort column. The unsorted path survived
        # it - ``None`` is falsy legally - which is why this reached a browser
        # before it was noticed, as a 500 on the item list's default sort.
        resolved = Item.code.asc() if order_by is None else order_by
        return await self.list_page(params, stmt=stmt, order_by=resolved)

    async def search_keyset(
        self,
        filters: ItemFilters,
        params: CursorParams,
        *,
        include_cost: bool,
    ) -> tuple[list[Item], CursorPagination]:
        """Keyset pagination - constant cost at any depth, for exports and for
        clients that scroll rather than page (section 31)."""
        stmt = self.build_search_statement(filters, include_cost=include_cost)
        return await self.list_keyset(params, stmt=stmt)

    async def list_below_reorder_level(
        self, params: PageParams
    ) -> tuple[list[Item], OffsetPagination]:
        """Items with a reorder policy configured.

        Stock comparison is *not* done here: on-hand lives in the inventory
        module and availability is never cached (section 34). This returns the
        planning parameters; MRP joins them to live balances.
        """
        stmt = (
            self._scoped()
            .where(Item.is_active.is_(True), Item.reorder_level > 0)
            .options(
                load_only(
                    *ITEM_SUMMARY_COLUMNS, Item.reorder_level, Item.reorder_quantity, raiseload=True
                )
            )
        )
        return await self.list_page(params, stmt=stmt, order_by=Item.code.asc())


class ItemVariantRepository(BaseRepository[ItemVariant]):
    model = ItemVariant
    searchable_fields = ("code", "name")
    sortable_fields = {"code": ItemVariant.code, "name": ItemVariant.name}  # noqa: RUF012

    async def code_exists(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = select(ItemVariant.id).where(
            ItemVariant.company_id == self.tenant.company_id,
            ItemVariant.code == code,
        )
        if exclude_id is not None:
            stmt = stmt.where(ItemVariant.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def combination_exists(
        self,
        item_id: uuid.UUID,
        *,
        finish_id: uuid.UUID | None,
        size_id: uuid.UUID | None,
        colour_id: uuid.UUID | None,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        """Pre-flight for the COALESCE unique index.

        The index is the real guarantee; this exists so the common case returns
        a business error rather than a 409 raised from an IntegrityError.
        """
        stmt = self._scoped().where(
            ItemVariant.item_id == item_id,
            ItemVariant.finish_id.is_not_distinct_from(finish_id),
            ItemVariant.size_id.is_not_distinct_from(size_id),
            ItemVariant.colour_id.is_not_distinct_from(colour_id),
        )
        if exclude_id is not None:
            stmt = stmt.where(ItemVariant.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def list_for_item(
        self, item_id: uuid.UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[list[ItemVariant], OffsetPagination]:
        stmt = self._scoped().where(ItemVariant.item_id == item_id)
        if is_active is not None:
            stmt = stmt.where(ItemVariant.is_active.is_(is_active))
        return await self.list_page(params, stmt=stmt, order_by=ItemVariant.code.asc())


class ItemPriceRepository(BaseRepository[ItemPrice]):
    model = ItemPrice
    sortable_fields = {  # noqa: RUF012
        "valid_from": ItemPrice.valid_from,
        "min_quantity": ItemPrice.min_quantity,
        "price": ItemPrice.price,
    }

    async def list_for_item(
        self,
        item_id: uuid.UUID,
        params: PageParams,
        *,
        price_type: PriceType | None = None,
        variant_id: uuid.UUID | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[ItemPrice], OffsetPagination]:
        stmt = self._scoped().where(ItemPrice.item_id == item_id)
        if price_type is not None:
            stmt = stmt.where(ItemPrice.price_type == price_type)
        if variant_id is not None:
            stmt = stmt.where(ItemPrice.variant_id == variant_id)
        if is_active is not None:
            stmt = stmt.where(ItemPrice.is_active.is_(is_active))
        # Ordered on the statement rather than through ``order_by=``: that
        # parameter takes a single expression, and the price list reads best
        # grouped by channel then by descending slab.
        stmt = stmt.order_by(
            ItemPrice.price_type,
            ItemPrice.min_quantity.desc(),
            ItemPrice.valid_from.desc(),
        )
        return await self.list_page(params, stmt=stmt)

    async def candidates(
        self,
        item_id: uuid.UUID,
        *,
        price_type: PriceType,
        variant_id: uuid.UUID | None,
        on_date: date,
    ) -> list[ItemPrice]:
        """Every live price row that could apply on ``on_date``.

        Slab selection happens on this bounded set in pure Python (see
        ``service.select_price_row``) rather than as a clever single-row query,
        because the rule - highest slab reached, most specific row, newest window
        on a tie - is a business rule worth unit-testing without a database.
        """
        # A variant-specific row wins over the item-wide row, but both have to be
        # fetched so they can be ranked together.
        variant_clause = (
            ItemPrice.variant_id.is_(None)
            if variant_id is None
            else or_(ItemPrice.variant_id.is_(None), ItemPrice.variant_id == variant_id)
        )
        stmt = self._scoped().where(
            ItemPrice.item_id == item_id,
            ItemPrice.price_type == price_type,
            ItemPrice.is_active.is_(True),
            ItemPrice.valid_from <= on_date,
            or_(ItemPrice.valid_to.is_(None), ItemPrice.valid_to >= on_date),
            variant_clause,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def overlapping(
        self,
        *,
        item_id: uuid.UUID,
        variant_id: uuid.UUID | None,
        price_type: PriceType,
        min_quantity: Decimal,
        valid_from: date,
        valid_to: date | None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[ItemPrice]:
        """Live rows for the same slab whose validity window overlaps.

        Two prices for the same channel, slab and day make the resolved price
        depend on row order, which is how a customer ends up invoiced at two
        different rates in one week.
        """
        stmt = self._scoped().where(
            ItemPrice.item_id == item_id,
            ItemPrice.price_type == price_type,
            ItemPrice.min_quantity == min_quantity,
            ItemPrice.is_active.is_(True),
            ItemPrice.variant_id.is_not_distinct_from(variant_id),
            # Overlap test with NULL valid_to meaning "open ended".
            or_(ItemPrice.valid_to.is_(None), ItemPrice.valid_to >= valid_from),
        )
        if valid_to is not None:
            stmt = stmt.where(ItemPrice.valid_from <= valid_to)
        if exclude_id is not None:
            stmt = stmt.where(ItemPrice.id != exclude_id)
        return list((await self.session.execute(stmt)).scalars().all())


class ItemVendorLinkRepository(BaseRepository[ItemVendorLink]):
    model = ItemVendorLink
    sortable_fields = {  # noqa: RUF012
        "lead_time_days": ItemVendorLink.lead_time_days,
        "last_purchase_price": ItemVendorLink.last_purchase_price,
    }

    async def get_pair(self, item_id: uuid.UUID, vendor_id: uuid.UUID) -> ItemVendorLink | None:
        stmt = self._scoped().where(
            ItemVendorLink.item_id == item_id,
            ItemVendorLink.vendor_id == vendor_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_item(
        self, item_id: uuid.UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[list[ItemVendorLink], OffsetPagination]:
        stmt = self._scoped().where(ItemVendorLink.item_id == item_id)
        if is_active is not None:
            stmt = stmt.where(ItemVendorLink.is_active.is_(is_active))
        # Preferred vendor first, then fastest - the order procurement reads it
        # in. Applied to the statement because ``order_by=`` takes one expression.
        stmt = stmt.order_by(
            ItemVendorLink.is_preferred.desc(), ItemVendorLink.lead_time_days.asc()
        )
        return await self.list_page(params, stmt=stmt)

    async def clear_preferred(self, item_id: uuid.UUID, *, keep_id: uuid.UUID | None = None) -> int:
        """Demote every other preferred vendor for the item in one UPDATE.

        Required before promoting a new one: the partial unique index allows
        exactly one preferred link per item, so the demotion and the promotion
        must happen in the same transaction.
        """
        stmt = (
            update(ItemVendorLink)
            .where(
                ItemVendorLink.company_id == self.tenant.company_id,
                ItemVendorLink.item_id == item_id,
                ItemVendorLink.is_preferred.is_(True),
            )
            .values(is_preferred=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(ItemVendorLink.id != keep_id)
        return affected_rows(await self.session.execute(stmt))


__all__ = [
    "BRAND_ENTITY",
    "ITEM_SUMMARY_COLUMNS",
    "MANUFACTURER_ENTITY",
    "BrandRepository",
    "ItemCategoryRepository",
    "ItemPriceRepository",
    "ItemRepository",
    "ItemVariantRepository",
    "ItemVendorLinkRepository",
    "ManufacturerRepository",
    "ManufacturerUsage",
    "UnitOfMeasureRepository",
    "UomConversionRepository",
]
