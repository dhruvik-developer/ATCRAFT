"""Item-master business rules.

Everything that decides *whether* something is allowed lives here; routes only
translate HTTP, repositories only build SQL.

Four rules in this file are worth reading before changing anything:

1. **``item_group`` never gates behaviour.** It seeds default capability flags
   at creation and is used for reporting. Every later decision - can this be
   sold, can it be a BOM component - reads the flag (master rules section 1).
2. **Cost is a response-model decision, not a frontend one.**
   :func:`item_response` returns ``ItemDetailWithCost`` only when the principal
   holds ``Permission.ITEM_COST_VIEW``; otherwise it returns a model that has no
   such field, so the value cannot leak through a schema someone forgot to trim.
3. **Unit conversions resolve through the base unit** and refuse to guess. An
   undefined pair raises a business error naming both units rather than falling
   back to 1:1, which would silently corrupt stock.
4. **Every write is one transaction with its audit row**, and every activation
   or retirement goes through :data:`ITEM_LIFECYCLE`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import normalise_code
from src.core.cache import CacheNamespace, cache_delete, cache_get, cache_set, make_key
from src.core.clock import today
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.ids import new_id
from src.core.pagination import CursorParams, PageParams
from src.core.permissions import Permission
from src.core.responses import CursorPagination, OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_factor, to_quantity, to_unit_price
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.products.models import (
    MAX_CATEGORY_DEPTH,
    Item,
    ItemCategory,
    ItemGroup,
    ItemLifecycleState,
    ItemPrice,
    ItemVariant,
    ItemVendorLink,
    PriceType,
    ProductionType,
    UnitOfMeasure,
    UomConversion,
)
from src.modules.products.repository import (
    ItemCategoryRepository,
    ItemPriceRepository,
    ItemRepository,
    ItemVariantRepository,
    ItemVendorLinkRepository,
    UnitOfMeasureRepository,
    UomConversionRepository,
)
from src.modules.products.schemas import (
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    ConversionResult,
    ItemCreate,
    ItemDetail,
    ItemDetailWithCost,
    ItemFilters,
    ItemStatusChange,
    ItemSummary,
    ItemSummaryWithCost,
    ItemUpdate,
    PriceCreate,
    PriceOut,
    PriceUpdate,
    ResolvedPrice,
    UomConversionCreate,
    UomConversionOut,
    UomCreate,
    UomOut,
    UomUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
    VendorLinkCreate,
    VendorLinkOut,
    VendorLinkUpdate,
)

# Entity type strings used in the audit log. Stable: reports and the audit
# search filter on them.
AUDIT_ITEM = "item"
AUDIT_CATEGORY = "item_category"
AUDIT_UOM = "unit_of_measure"
AUDIT_UOM_CONVERSION = "uom_conversion"
AUDIT_VARIANT = "item_variant"
AUDIT_PRICE = "item_price"
AUDIT_VENDOR_LINK = "item_vendor_link"

#: Fields snapshotted before/after an item update so the audit diff is useful.
ITEM_AUDITED_FIELDS = [
    "code",
    "name",
    "item_group",
    "category_id",
    "base_uom_id",
    "purchase_uom_id",
    "sale_uom_id",
    "hsn_code",
    "gst_rate",
    "standard_cost",
    "sale_price",
    "dealer_price",
    "finish",
    "size",
    "colour",
    "can_purchase",
    "can_manufacture",
    "can_sell",
    "can_be_bom_component",
    "can_be_assembly_component",
    "can_be_spare",
    "can_job_work",
    "can_return",
    "can_scrap",
    "production_type",
    "lead_time_days",
    "reorder_level",
    "reorder_quantity",
    "min_order_quantity",
    "safety_stock",
    "shelf_life_days",
    "is_batch_tracked",
    "is_serial_tracked",
    "default_warehouse_id",
    "is_active",
]

CAPABILITY_FIELDS = (
    "can_purchase",
    "can_manufacture",
    "can_sell",
    "can_be_bom_component",
    "can_be_assembly_component",
    "can_be_spare",
    "can_job_work",
    "can_return",
    "can_scrap",
)

# ASSUMPTION: an item must be able to do at least one thing. An item master row
# with every capability off is unusable by every module and is far more likely to
# be a data-entry mistake than an intent, so creation is refused. Retiring an
# item is expressed by is_active/soft delete, not by clearing its capabilities.
REQUIRE_AT_LEAST_ONE_CAPABILITY = True

# ASSUMPTION: production_type and the capability flags must agree - MAKE needs
# can_manufacture, BUY needs can_purchase, MAKE_OR_BUY needs both, JOB_WORK needs
# can_job_work. Without this, MRP would raise a MANUFACTURE suggestion for an
# item no production order may be created for. Note this reads production_type,
# never item_group.
PRODUCTION_TYPE_REQUIRED_CAPABILITIES: dict[ProductionType, tuple[str, ...]] = {
    ProductionType.MAKE: ("can_manufacture",),
    ProductionType.BUY: ("can_purchase",),
    ProductionType.MAKE_OR_BUY: ("can_manufacture", "can_purchase"),
    ProductionType.JOB_WORK: ("can_job_work",),
}


# -----------------------------------------------------------------------------
# Item lifecycle (section 73)
# -----------------------------------------------------------------------------

ITEM_LIFECYCLE: StateMachine[ItemLifecycleState] = StateMachine(
    entity="Item",
    initial=ItemLifecycleState.ACTIVE,
    transitions=(
        Transition(
            ItemLifecycleState.ACTIVE,
            ItemLifecycleState.INACTIVE,
            Permission.ITEM_UPDATE,
            "Deactivate",
        ),
        Transition(
            ItemLifecycleState.INACTIVE,
            ItemLifecycleState.ACTIVE,
            Permission.ITEM_UPDATE,
            "Activate",
        ),
        # Retirement is irreversible through the API: an item referenced by a
        # posted document must never be resurrected with different meaning.
        Transition(
            ItemLifecycleState.ACTIVE,
            ItemLifecycleState.ARCHIVED,
            Permission.ITEM_DELETE,
            "Retire",
            is_irreversible=True,
        ),
        Transition(
            ItemLifecycleState.INACTIVE,
            ItemLifecycleState.ARCHIVED,
            Permission.ITEM_DELETE,
            "Retire",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({ItemLifecycleState.ARCHIVED}),
)


def lifecycle_state(item: Item) -> ItemLifecycleState:
    """Project ``is_active``/``deleted_at`` onto the state-machine vocabulary."""
    if item.deleted_at is not None:
        return ItemLifecycleState.ARCHIVED
    return ItemLifecycleState.ACTIVE if item.is_active else ItemLifecycleState.INACTIVE


# -----------------------------------------------------------------------------
# Pure logic - no database, unit tested directly
# -----------------------------------------------------------------------------


def default_capabilities(item_group: ItemGroup) -> dict[str, bool]:
    """Sensible starting flags for a group.

    This is the *only* place ``item_group`` influences capability, and it does so
    exactly once, at creation, as a default the user may override. Nothing reads
    the group afterwards to decide whether an operation is permitted
    (master rules section 1).
    """
    base = dict.fromkeys(CAPABILITY_FIELDS, False)
    base["can_return"] = True
    base["can_scrap"] = True

    if item_group in (ItemGroup.RAW_MATERIAL, ItemGroup.COMPONENT):
        base.update(
            can_purchase=True,
            can_be_bom_component=True,
            can_be_assembly_component=True,
            can_job_work=True,
        )
    elif item_group is ItemGroup.SEMI_FINISHED:
        base.update(
            can_manufacture=True,
            can_be_bom_component=True,
            can_be_assembly_component=True,
            can_job_work=True,
        )
    elif item_group is ItemGroup.FINISHED_GOOD:
        base.update(can_manufacture=True, can_sell=True, can_be_assembly_component=True)
    elif item_group in (ItemGroup.CONSUMABLE, ItemGroup.PACKING_MATERIAL):
        base.update(can_purchase=True, can_be_bom_component=True)
    elif item_group is ItemGroup.SPARE:
        base.update(can_purchase=True, can_sell=True, can_be_spare=True)
    elif item_group is ItemGroup.SCRAP:
        base.update(can_sell=True, can_scrap=True)
    elif item_group is ItemGroup.SERVICE:
        # A service is bought and sold but never stocked, so no BOM/assembly
        # component flags and no returns of physical goods.
        base.update(can_purchase=True, can_sell=True, can_return=False, can_scrap=False)

    return base


def resolve_capabilities(
    item_group: ItemGroup, requested: dict[str, bool | None]
) -> dict[str, bool]:
    """Merge explicit flags over the group defaults. ``None`` means "not stated"."""
    resolved = default_capabilities(item_group)
    for field, value in requested.items():
        if value is not None and field in resolved:
            resolved[field] = value
    return resolved


def validate_capabilities(production_type: ProductionType, flags: dict[str, bool]) -> None:
    """Reject a capability set that contradicts the planning policy."""
    if REQUIRE_AT_LEAST_ONE_CAPABILITY and not any(flags.values()):
        raise BusinessRuleError(
            "An item must have at least one capability enabled.",
            details={"capabilities": sorted(CAPABILITY_FIELDS)},
        )

    required = PRODUCTION_TYPE_REQUIRED_CAPABILITIES.get(production_type, ())
    missing = [name for name in required if not flags.get(name)]
    if missing:
        raise BusinessRuleError(
            f"production_type '{production_type}' requires {', '.join(missing)} to be enabled.",
            details={"production_type": str(production_type), "missing_capabilities": missing},
        )


def build_category_path(parent_path: str | None, category_id: uuid.UUID) -> str:
    """``/<ancestor>/.../<self>/`` - the materialised path for one node."""
    prefix = parent_path if parent_path else "/"
    return f"{prefix}{category_id}/"


def category_depth(path: str) -> int:
    """Depth implied by a materialised path. A root is depth 0."""
    return max(0, len([segment for segment in path.split("/") if segment]) - 1)


@dataclass(frozen=True, slots=True)
class UomGraph:
    """The tenant's units and conversions, resolved through base units.

    Built once per request (cached in Redis with a TTL) and then queried in
    memory, because a conversion is needed per document *line* and a round-trip
    per line is exactly the N+1 section 32 forbids.
    """

    category_by_uom: dict[uuid.UUID, str]
    base_by_category: dict[str, uuid.UUID]
    #: ``(from, to) -> factor`` meaning ``1 from = factor to``.
    factors: dict[tuple[uuid.UUID, uuid.UUID], Decimal]

    @classmethod
    def build(cls, uoms: Sequence[UnitOfMeasure], conversions: Sequence[UomConversion]) -> UomGraph:
        category_by_uom = {u.id: str(u.uom_category) for u in uoms}
        base_by_category = {str(u.uom_category): u.id for u in uoms if u.is_base}
        factors = {(c.from_uom_id, c.to_uom_id): Decimal(c.factor) for c in conversions}
        return cls(category_by_uom, base_by_category, factors)

    # --- Redis round-trip ---------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        return {
            "category_by_uom": {str(k): v for k, v in self.category_by_uom.items()},
            "base_by_category": {k: str(v) for k, v in self.base_by_category.items()},
            "factors": [[str(f), str(t), str(v)] for (f, t), v in self.factors.items()],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UomGraph:
        return cls(
            category_by_uom={uuid.UUID(k): v for k, v in payload["category_by_uom"].items()},
            base_by_category={k: uuid.UUID(v) for k, v in payload["base_by_category"].items()},
            factors={(uuid.UUID(f), uuid.UUID(t)): Decimal(v) for f, t, v in payload["factors"]},
        )

    # --- resolution ---------------------------------------------------------

    def _to_base(self, uom_id: uuid.UUID, base_id: uuid.UUID) -> Decimal | None:
        """How many base units one ``uom_id`` is, or None if undefined."""
        if uom_id == base_id:
            return Decimal(1)
        direct = self.factors.get((uom_id, base_id))
        if direct is not None:
            return direct
        inverse = self.factors.get((base_id, uom_id))
        if inverse is not None and inverse != 0:
            # Stored the other way round; invert rather than demanding both rows
            # be maintained, which is how the two halves drift apart.
            with localcontext() as ctx:
                ctx.prec = 28
                return Decimal(1) / inverse
        return None

    def factor(self, from_uom_id: uuid.UUID, to_uom_id: uuid.UUID) -> Decimal:
        """``1 from_uom`` expressed in ``to_uom``.

        Raises a business error naming both units when no path exists. Guessing
        1:1 here would corrupt every stock movement that used it.
        """
        if from_uom_id == to_uom_id:
            return Decimal(1)

        from_category = self.category_by_uom.get(from_uom_id)
        to_category = self.category_by_uom.get(to_uom_id)
        if from_category is None or to_category is None:
            raise BusinessRuleError(
                "Unknown unit of measure in conversion request.",
                details={"from_uom_id": str(from_uom_id), "to_uom_id": str(to_uom_id)},
            )
        if from_category != to_category:
            raise BusinessRuleError(
                f"Cannot convert between measurement categories '{from_category}' and "
                f"'{to_category}'. A weight-to-count relationship is item specific, "
                "not a unit conversion.",
                details={
                    "from_uom_id": str(from_uom_id),
                    "to_uom_id": str(to_uom_id),
                    "from_category": from_category,
                    "to_category": to_category,
                },
            )

        # A direct row is authoritative: it is what the administrator entered.
        direct = self.factors.get((from_uom_id, to_uom_id))
        if direct is not None:
            return direct
        inverse = self.factors.get((to_uom_id, from_uom_id))
        if inverse is not None and inverse != 0:
            with localcontext() as ctx:
                ctx.prec = 28
                return Decimal(1) / inverse

        base_id = self.base_by_category.get(from_category)
        if base_id is None:
            raise BusinessRuleError(
                f"No base unit is defined for measurement category '{from_category}'.",
                details={"uom_category": from_category},
            )

        from_base = self._to_base(from_uom_id, base_id)
        to_base = self._to_base(to_uom_id, base_id)
        if from_base is None or to_base is None or to_base == 0:
            raise BusinessRuleError(
                "No unit conversion is defined for this pair, directly or through "
                "the base unit. Add a uom_conversions row before using it.",
                details={
                    "from_uom_id": str(from_uom_id),
                    "to_uom_id": str(to_uom_id),
                    "base_uom_id": str(base_id),
                },
            )

        # One division at the end rather than two chained ones keeps the
        # round-trip error below the quantity scale.
        with localcontext() as ctx:
            ctx.prec = 28
            return from_base / to_base

    def has_path(self, from_uom_id: uuid.UUID, to_uom_id: uuid.UUID) -> bool:
        try:
            self.factor(from_uom_id, to_uom_id)
        except BusinessRuleError:
            return False
        return True

    def convert(self, quantity: Decimal, from_uom_id: uuid.UUID, to_uom_id: uuid.UUID) -> Decimal:
        """Convert a quantity, rounded to the stored quantity scale."""
        with localcontext() as ctx:
            ctx.prec = 28
            raw = Decimal(quantity) * self.factor(from_uom_id, to_uom_id)
        return to_quantity(raw)


def select_price_row(
    rows: Sequence[ItemPrice],
    quantity: Decimal,
    *,
    variant_id: uuid.UUID | None = None,
) -> ItemPrice | None:
    """Pick the price-list row that applies to ``quantity``.

    Precedence, most significant first:

    1. a row for the specific variant beats an item-wide row
    2. the highest ``min_quantity`` slab the ordered quantity actually reaches
    3. the most recently effective window, so a revision supersedes its predecessor

    Rows whose slab the quantity does not reach are not candidates at all - a
    100-unit price must never be applied to an order for 10. Nor is a row for a
    *different* variant: the repository already filters those out, and this
    keeps the ranking correct for any other caller.
    """
    eligible = [
        row
        for row in rows
        if Decimal(row.min_quantity) <= quantity
        and (row.variant_id is None or row.variant_id == variant_id)
    ]
    if not eligible:
        return None

    def rank(row: ItemPrice) -> tuple[int, Decimal, date, uuid.UUID]:
        specific = 1 if (variant_id is not None and row.variant_id == variant_id) else 0
        # id is UUIDv7, so it breaks a same-day tie in creation order and keeps
        # the choice deterministic instead of dependent on row order.
        return (specific, Decimal(row.min_quantity), row.valid_from, row.id)

    return max(eligible, key=rank)


# -----------------------------------------------------------------------------
# Response construction - where the cost permission is actually enforced
# -----------------------------------------------------------------------------


def item_response(item: Item, *, include_cost: bool) -> ItemDetail | ItemDetailWithCost:
    """Build the detail payload for one item.

    The permission decides the *class*, not a post-hoc field deletion: without
    ``item.cost_view`` the response model has no ``standard_cost`` field, so no
    later refactor can reintroduce it by accident.
    """
    payload = {
        **{
            field: getattr(item, field)
            for field in ItemDetail.model_fields
            if field != "lifecycle_state"
        },
        "lifecycle_state": lifecycle_state(item),
    }
    if include_cost:
        return ItemDetailWithCost(**payload, standard_cost=item.standard_cost)
    return ItemDetail(**payload)


def item_summaries(
    items: Sequence[Item], *, include_cost: bool
) -> list[ItemSummary] | list[ItemSummaryWithCost]:
    if include_cost:
        return [ItemSummaryWithCost.model_validate(item) for item in items]
    return [ItemSummary.model_validate(item) for item in items]


def may_view_cost(principal: Principal) -> bool:
    return principal.has(Permission.ITEM_COST_VIEW)


# -----------------------------------------------------------------------------
# UoM graph loading (Redis is a cache only, always with a TTL - section 33)
# -----------------------------------------------------------------------------

_UOM_GRAPH_KEY = "uom-graph"


def _uom_graph_cache_key(company_id: uuid.UUID) -> str:
    namespace, _ = CacheNamespace.REFERENCE
    return make_key(namespace, company_id, _UOM_GRAPH_KEY)


async def load_uom_graph(session: AsyncSession, tenant: TenantContext) -> UomGraph:
    """The tenant's conversion graph, cached for the reference-data TTL.

    PostgreSQL remains the source of truth: a cache miss, a Redis outage or an
    expired key all fall through to the tables. Every write to a unit or a
    conversion drops the key in the same request.
    """
    key = _uom_graph_cache_key(tenant.company_id)
    cached = await cache_get(key)
    if cached is not None:
        return UomGraph.from_payload(cached)

    _, ttl = CacheNamespace.REFERENCE
    uoms = await UnitOfMeasureRepository(session, tenant).list_all()
    conversions = await UomConversionRepository(session, tenant).list_all()
    graph = UomGraph.build(uoms, conversions)
    await cache_set(key, graph.to_payload(), ttl)
    return graph


async def _invalidate_uom_graph(tenant: TenantContext) -> None:
    await cache_delete(_uom_graph_cache_key(tenant.company_id))


# -----------------------------------------------------------------------------
# Categories
# -----------------------------------------------------------------------------


async def create_category(
    session: AsyncSession, tenant: TenantContext, payload: CategoryCreate
) -> CategoryOut:
    repo = ItemCategoryRepository(session, tenant)

    async with transaction(session):
        if await repo.exists(code=payload.code):
            raise DuplicateError(f"An item category with code '{payload.code}' already exists.")

        parent: ItemCategory | None = None
        if payload.parent_id is not None:
            parent = await repo.get(payload.parent_id)
            if parent is None:
                raise NotFoundError("Item category", payload.parent_id)
            if parent.depth + 1 >= MAX_CATEGORY_DEPTH:
                raise BusinessRuleError(
                    f"Category nesting is limited to {MAX_CATEGORY_DEPTH} levels.",
                    details={"max_depth": MAX_CATEGORY_DEPTH},
                )

        # The id is minted up front because the materialised path contains it.
        category_id = new_id()
        category = ItemCategory(
            id=category_id,
            code=payload.code,
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
            path=build_category_path(parent.path if parent else None, category_id),
            depth=(parent.depth + 1) if parent else 0,
            is_active=payload.is_active,
        )
        repo.add(category)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_CATEGORY,
            entity_id=category.id,
            entity_code=category.code,
            summary=f"Created item category {category.code}",
            user_id=tenant.user_id,
            new_values={"code": category.code, "name": category.name, "path": category.path},
        )

    return CategoryOut.model_validate(category)


async def update_category(
    session: AsyncSession,
    tenant: TenantContext,
    category_id: uuid.UUID,
    payload: CategoryUpdate,
) -> CategoryOut:
    repo = ItemCategoryRepository(session, tenant)

    async with transaction(session):
        category = await repo.get_or_404(category_id)
        before = snapshot(category, ["name", "description", "parent_id", "path", "is_active"])

        if payload.name is not None:
            category.name = payload.name
        if payload.description is not None:
            category.description = payload.description
        if payload.is_active is not None:
            category.is_active = payload.is_active

        if payload.reparent_to_root or payload.parent_id is not None:
            await _reparent_category(
                repo,
                category,
                new_parent_id=None if payload.reparent_to_root else payload.parent_id,
            )

        repo.touch(category)
        await session.flush()

        after = snapshot(category, ["name", "description", "parent_id", "path", "is_active"])
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_CATEGORY,
            entity_id=category.id,
            entity_code=category.code,
            summary=f"Updated item category {category.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=after,
        )

    return CategoryOut.model_validate(category)


async def _reparent_category(
    repo: ItemCategoryRepository, category: ItemCategory, *, new_parent_id: uuid.UUID | None
) -> None:
    """Move a subtree, rewriting descendant paths in one UPDATE."""
    if new_parent_id == category.parent_id:
        return
    if new_parent_id == category.id:
        raise BusinessRuleError("A category cannot be its own parent.")

    new_parent: ItemCategory | None = None
    if new_parent_id is not None:
        new_parent = await repo.get(new_parent_id)
        if new_parent is None:
            raise NotFoundError("Item category", new_parent_id)
        # Moving a node under its own descendant would detach the subtree from
        # the tree entirely - the materialised path makes the check trivial.
        if new_parent.path.startswith(category.path):
            raise BusinessRuleError(
                "A category cannot be moved beneath one of its own descendants.",
                details={"category_id": str(category.id), "new_parent_id": str(new_parent_id)},
            )

    old_path = category.path
    new_path = build_category_path(new_parent.path if new_parent else None, category.id)
    new_depth = (new_parent.depth + 1) if new_parent else 0
    depth_delta = new_depth - category.depth

    deepest = await repo.subtree_max_depth(old_path)
    if deepest + depth_delta >= MAX_CATEGORY_DEPTH:
        raise BusinessRuleError(
            f"Moving this category would exceed the {MAX_CATEGORY_DEPTH}-level nesting limit.",
            details={"max_depth": MAX_CATEGORY_DEPTH},
        )

    category.parent_id = new_parent_id
    category.path = new_path
    category.depth = new_depth
    # The node itself is updated above; the statement rewrites its descendants.
    await repo.rewrite_subtree_paths(
        old_prefix=old_path, new_prefix=new_path, depth_delta=depth_delta
    )


async def delete_category(
    session: AsyncSession, tenant: TenantContext, category_id: uuid.UUID
) -> None:
    """Retire a category.

    Refused while anything still points at it: reassigning items silently would
    change how every historical report groups them.
    """
    repo = ItemCategoryRepository(session, tenant)

    async with transaction(session):
        category = await repo.get_or_404(category_id)

        if await repo.has_children(category_id):
            raise BusinessRuleError(
                "This category still has sub-categories. Move or retire them first.",
                details={"category_id": str(category_id)},
            )
        if await repo.has_items(category_id):
            raise BusinessRuleError(
                "This category still has items assigned. Reassign them first.",
                details={"category_id": str(category_id)},
            )

        await repo.soft_delete(category)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_CATEGORY,
            entity_id=category.id,
            entity_code=category.code,
            summary=f"Retired item category {category.code}",
            user_id=tenant.user_id,
        )


async def list_categories(
    session: AsyncSession,
    tenant: TenantContext,
    params: PageParams,
    *,
    parent_id: uuid.UUID | None = None,
    only_roots: bool = False,
    search: str | None = None,
    is_active: bool | None = None,
) -> tuple[list[CategoryOut], OffsetPagination]:
    repo = ItemCategoryRepository(session, tenant)
    rows, pagination = await repo.list_page_filtered(
        params, parent_id=parent_id, only_roots=only_roots, search=search, is_active=is_active
    )
    return [CategoryOut.model_validate(row) for row in rows], pagination


async def get_category(
    session: AsyncSession, tenant: TenantContext, category_id: uuid.UUID
) -> CategoryOut:
    category = await ItemCategoryRepository(session, tenant).get_or_404(category_id)
    return CategoryOut.model_validate(category)


# -----------------------------------------------------------------------------
# Units of measure and conversions
# -----------------------------------------------------------------------------


async def create_uom(session: AsyncSession, tenant: TenantContext, payload: UomCreate) -> UomOut:
    repo = UnitOfMeasureRepository(session, tenant)

    async with transaction(session):
        if await repo.exists(code=payload.code):
            raise DuplicateError(f"A unit of measure with code '{payload.code}' already exists.")

        if payload.is_base:
            existing_base = await repo.base_for_category(payload.uom_category)
            if existing_base is not None:
                raise BusinessRuleError(
                    f"'{existing_base.code}' is already the base unit for "
                    f"{payload.uom_category}. A category has exactly one base unit.",
                    details={"existing_base_uom_id": str(existing_base.id)},
                )

        uom = UnitOfMeasure(
            code=payload.code,
            name=payload.name,
            uom_category=payload.uom_category,
            is_base=payload.is_base,
            decimal_places=payload.decimal_places,
            is_active=payload.is_active,
        )
        repo.add(uom)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_UOM,
            entity_id=uom.id,
            entity_code=uom.code,
            summary=f"Created unit of measure {uom.code}",
            user_id=tenant.user_id,
            new_values={
                "code": uom.code,
                "uom_category": str(uom.uom_category),
                "is_base": uom.is_base,
            },
        )

    await _invalidate_uom_graph(tenant)
    return UomOut.model_validate(uom)


async def update_uom(
    session: AsyncSession, tenant: TenantContext, uom_id: uuid.UUID, payload: UomUpdate
) -> UomOut:
    repo = UnitOfMeasureRepository(session, tenant)

    async with transaction(session):
        uom = await repo.get_or_404(uom_id)
        before = snapshot(uom, ["name", "decimal_places", "is_active", "is_base"])

        if payload.name is not None:
            uom.name = payload.name
        if payload.decimal_places is not None:
            uom.decimal_places = payload.decimal_places
        if payload.is_active is not None:
            if not payload.is_active and await repo.is_used_by_items(uom_id):
                raise BusinessRuleError(
                    "This unit is still referenced by active items and cannot be deactivated.",
                    details={"uom_id": str(uom_id)},
                )
            uom.is_active = payload.is_active
        if payload.is_base is not None and payload.is_base != uom.is_base:
            if payload.is_base:
                existing_base = await repo.base_for_category(str(uom.uom_category))
                if existing_base is not None and existing_base.id != uom.id:
                    raise BusinessRuleError(
                        f"'{existing_base.code}' is already the base unit for {uom.uom_category}.",
                        details={"existing_base_uom_id": str(existing_base.id)},
                    )
            else:
                # Removing the base leaves every conversion in the family
                # unresolvable, so it is refused rather than silently breaking
                # conversions at the next goods receipt.
                raise BusinessRuleError(
                    "A measurement category must keep a base unit. Promote another "
                    "unit to base instead of clearing this one.",
                    details={"uom_id": str(uom_id)},
                )
            uom.is_base = payload.is_base

        repo.touch(uom)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_UOM,
            entity_id=uom.id,
            entity_code=uom.code,
            summary=f"Updated unit of measure {uom.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(uom, ["name", "decimal_places", "is_active", "is_base"]),
        )

    await _invalidate_uom_graph(tenant)
    return UomOut.model_validate(uom)


async def list_uoms(
    session: AsyncSession,
    tenant: TenantContext,
    params: PageParams,
    *,
    search: str | None = None,
    is_active: bool | None = None,
) -> tuple[list[UomOut], OffsetPagination]:
    rows, pagination = await UnitOfMeasureRepository(session, tenant).list_page_search(
        params, search=search, is_active=is_active
    )
    return [UomOut.model_validate(row) for row in rows], pagination


async def create_conversion(
    session: AsyncSession, tenant: TenantContext, payload: UomConversionCreate
) -> UomConversionOut:
    uom_repo = UnitOfMeasureRepository(session, tenant)
    repo = UomConversionRepository(session, tenant)

    async with transaction(session):
        from_uom = await uom_repo.get(payload.from_uom_id)
        to_uom = await uom_repo.get(payload.to_uom_id)
        if from_uom is None:
            raise NotFoundError("Unit of measure", payload.from_uom_id)
        if to_uom is None:
            raise NotFoundError("Unit of measure", payload.to_uom_id)
        if from_uom.uom_category != to_uom.uom_category:
            raise BusinessRuleError(
                "A conversion may only relate units in the same measurement category.",
                details={
                    "from_category": str(from_uom.uom_category),
                    "to_category": str(to_uom.uom_category),
                },
            )

        for pair in (
            (payload.from_uom_id, payload.to_uom_id),
            (payload.to_uom_id, payload.from_uom_id),
        ):
            # Both directions are checked: storing 1 BOX = 12 NOS *and*
            # 1 NOS = 0.09 BOX lets the two disagree, and whichever the resolver
            # happens to find first wins.
            if await repo.get_pair(*pair) is not None:
                raise DuplicateError(
                    "A conversion already exists between these two units.",
                    details={"from_uom_id": str(pair[0]), "to_uom_id": str(pair[1])},
                )

        conversion = UomConversion(
            from_uom_id=payload.from_uom_id,
            to_uom_id=payload.to_uom_id,
            factor=payload.factor,
        )
        repo.add(conversion)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_UOM_CONVERSION,
            entity_id=conversion.id,
            entity_code=f"{from_uom.code}->{to_uom.code}",
            summary=f"1 {from_uom.code} = {payload.factor} {to_uom.code}",
            user_id=tenant.user_id,
            new_values={"factor": payload.factor},
        )

    await _invalidate_uom_graph(tenant)
    return UomConversionOut.model_validate(conversion)


async def delete_conversion(
    session: AsyncSession, tenant: TenantContext, conversion_id: uuid.UUID
) -> None:
    repo = UomConversionRepository(session, tenant)

    async with transaction(session):
        conversion = await repo.get_or_404(conversion_id)
        await repo.soft_delete(conversion)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_UOM_CONVERSION,
            entity_id=conversion.id,
            summary="Removed unit conversion",
            user_id=tenant.user_id,
            old_values={"factor": conversion.factor},
        )

    await _invalidate_uom_graph(tenant)


async def list_conversions(
    session: AsyncSession, tenant: TenantContext, params: PageParams
) -> tuple[list[UomConversionOut], OffsetPagination]:
    repo = UomConversionRepository(session, tenant)
    rows, pagination = await repo.list_page(params)
    return [UomConversionOut.model_validate(row) for row in rows], pagination


async def convert_quantity(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    quantity: Decimal,
    from_uom_id: uuid.UUID,
    to_uom_id: uuid.UUID,
) -> ConversionResult:
    """Convert a quantity between two units of the same measurement category.

    The public entry point other modules call before posting stock in a
    purchase unit.
    """
    graph = await load_uom_graph(session, tenant)
    factor = graph.factor(from_uom_id, to_uom_id)
    return ConversionResult(
        from_uom_id=from_uom_id,
        to_uom_id=to_uom_id,
        input_quantity=to_quantity(quantity),
        # The ratio is reported at the scale its column stores (18,8), not the
        # unit-price scale: 1 BOX = 12 NOS reported as 0.083333 would make
        # input * factor disagree with converted_quantity by a whole unit at
        # ~10^5 quantities. A ratio is neither money nor a price.
        factor=to_factor(factor),
        converted_quantity=graph.convert(quantity, from_uom_id, to_uom_id),
    )


# -----------------------------------------------------------------------------
# Items
# -----------------------------------------------------------------------------


async def _validate_item_units(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    base_uom_id: uuid.UUID,
    purchase_uom_id: uuid.UUID | None,
    sale_uom_id: uuid.UUID | None,
) -> None:
    """Every unit exists, and the trading units convert to the stocking unit.

    ASSUMPTION: a purchase or sale unit that cannot be converted to the base
    unit is rejected at save time. The alternative is discovering it at the
    goods receipt that cannot post, with the vendor's lorry at the gate.
    """
    uom_repo = UnitOfMeasureRepository(session, tenant)
    if await uom_repo.get(base_uom_id) is None:
        raise NotFoundError("Unit of measure", base_uom_id)

    others = [u for u in (purchase_uom_id, sale_uom_id) if u is not None and u != base_uom_id]
    if not others:
        return

    graph = await load_uom_graph(session, tenant)
    for uom_id in others:
        if uom_id not in graph.category_by_uom:
            raise NotFoundError("Unit of measure", uom_id)
        # Raises a business error naming both units when the pair is undefined.
        graph.factor(uom_id, base_uom_id)


async def _assert_default_warehouse_exists(
    repo: ItemRepository, warehouse_id: uuid.UUID | None
) -> None:
    """The default warehouse must be a live warehouse of *this* company.

    The foreign key only proves the id exists somewhere: ``warehouses.id`` is
    globally unique, so another tenant's warehouse would satisfy it while
    breaking tenant isolation (section 40). Reported as not found, exactly as
    a missing one is - confirming that it exists elsewhere is itself a leak.
    """
    if warehouse_id is not None and not await repo.warehouse_exists(warehouse_id):
        raise NotFoundError("Warehouse", warehouse_id)


async def create_item(session: AsyncSession, tenant: TenantContext, payload: ItemCreate) -> Item:
    repo = ItemRepository(session, tenant)
    code = normalise_code(payload.code)

    async with transaction(session):
        if await repo.code_exists(code):
            raise DuplicateError(
                f"An item with code '{code}' already exists.", details={"code": code}
            )

        if payload.category_id is not None:
            category = await ItemCategoryRepository(session, tenant).get(payload.category_id)
            if category is None:
                raise NotFoundError("Item category", payload.category_id)

        await _validate_item_units(
            session,
            tenant,
            base_uom_id=payload.base_uom_id,
            purchase_uom_id=payload.purchase_uom_id,
            sale_uom_id=payload.sale_uom_id,
        )
        await _assert_default_warehouse_exists(repo, payload.default_warehouse_id)

        flags = resolve_capabilities(
            payload.item_group,
            {field: getattr(payload, field) for field in CAPABILITY_FIELDS},
        )
        validate_capabilities(payload.production_type, flags)

        if payload.shelf_life_days is not None and not payload.is_batch_tracked:
            raise BusinessRuleError(
                "An item with a shelf life must be batch tracked, otherwise nothing "
                "records what expires when.",
                details={"shelf_life_days": payload.shelf_life_days},
            )

        item = Item(
            code=code,
            name=payload.name,
            description=payload.description,
            item_group=payload.item_group,
            category_id=payload.category_id,
            base_uom_id=payload.base_uom_id,
            purchase_uom_id=payload.purchase_uom_id,
            sale_uom_id=payload.sale_uom_id,
            hsn_code=payload.hsn_code,
            gst_rate=payload.gst_rate,
            standard_cost=payload.standard_cost,
            sale_price=payload.sale_price,
            dealer_price=payload.dealer_price,
            finish=payload.finish,
            size=payload.size,
            colour=payload.colour,
            production_type=payload.production_type,
            lead_time_days=payload.lead_time_days,
            reorder_level=payload.reorder_level,
            reorder_quantity=payload.reorder_quantity,
            min_order_quantity=payload.min_order_quantity,
            safety_stock=payload.safety_stock,
            shelf_life_days=payload.shelf_life_days,
            is_batch_tracked=payload.is_batch_tracked,
            is_serial_tracked=payload.is_serial_tracked,
            default_warehouse_id=payload.default_warehouse_id,
            is_active=payload.is_active,
            **flags,
        )
        repo.add(item)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_ITEM,
            entity_id=item.id,
            entity_code=item.code,
            summary=f"Created item {item.code} - {item.name}",
            user_id=tenant.user_id,
            new_values=snapshot(item, ITEM_AUDITED_FIELDS),
        )

    return item


#: The two flags that decide how an item's stock is *identified* rather than
#: what may be done with it. Everything else on the item master describes the
#: item; these two describe the shape of every balance row and every ledger row
#: the item will ever have.
TRACKING_FIELDS = ("is_batch_tracked", "is_serial_tracked")


async def _assert_tracking_change_is_safe(
    repo: ItemRepository, item: Item, scalar_updates: dict[str, Any]
) -> None:
    """Refuse a change to batch or serial tracking while the item holds stock.

    ASSUMPTION: ``is_batch_tracked`` and ``is_serial_tracked`` may only change
    while the item holds nothing anywhere - no on-hand, nothing reserved, in
    transit, out at a job-work vendor, on QC hold, damaged or scrapped.

    "We are starting to lot-track this part" is a legitimate business decision,
    so the flip is not refused outright; it is refused while there is stock for
    it to land on top of, because that is the case where it does damage:

    * ``batch_id`` is part of the balance grain. Turning batch tracking on over
      existing stock leaves that quantity at the batch-NULL position for good -
      no receipt can ever add to it (a receipt must now name a batch, which keys
      a different position) and every report shows an item split across a lot
      that exists and a lot that does not.
    * Serial tracking turned on over existing stock claims a per-unit history
      for units that were never identified. The serial table cannot be
      back-filled without inventing unit numbers, which is worse than the gap.
    * And a document already in flight cannot be re-keyed. A stock transfer that
      has dispatched has no edge out of IN_TRANSIT except RECEIVED, so its
      arrival *must* be accepted whatever the flag now says - the movement
      engine exempts it deliberately - and it lands untraced goods. That is the
      case this guard exists to stop happening in the first place, and it is why
      the check reads ``in_transit_quantity`` and not just ``on_hand_quantity``.

    Balance rows, rather than a sweep of every open document, are the test:
    ``inventory_balances`` is where an in-flight document has *already* put the
    stock (``in_transit_quantity`` at dispatch, ``job_work_quantity`` at issue),
    so the one class of document whose completion cannot be refused is always
    visible here. A document that has not moved anything yet - a draft transfer,
    an approved purchase order - strands nothing: its posting is an *arrival*,
    the engine refuses arrivals that name no batch, and the document can still
    be edited or cancelled.

    Turning tracking *off* is refused on the same terms rather than waved
    through. It strands nothing, but it splits a live item across two grain
    shapes, and the one-sentence rule - "how stock is identified changes only
    while there is no stock" - is worth more than the exception.
    """
    wanted = {
        field: bool(scalar_updates[field])
        for field in TRACKING_FIELDS
        # A field that is absent, or explicitly null, states no new value, so
        # there is nothing for this guard to compare. (What ``update_item`` then
        # does with an explicit null is the same thing it has always done with
        # one on any other column, and is not this rule's business.)
        if scalar_updates.get(field) is not None
    }
    changing = sorted(
        field for field, value in wanted.items() if value != bool(getattr(item, field))
    )
    if not changing:
        return
    if not await repo.has_stock_balance(item.id):
        return

    raise BusinessRuleError(
        f"Item {item.code} still holds stock, so how its stock is identified cannot be "
        "changed. Issue, transfer or write the remaining balance off - including anything "
        "in transit or out at a job-work vendor - and then change the tracking setting.",
        code=ErrorCode.REFERENCED_RECORD_IN_USE,
        details={"item_code": item.code, "item_id": str(item.id), "fields": changing},
    )


async def update_item(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, payload: ItemUpdate
) -> Item:
    repo = ItemRepository(session, tenant)

    async with transaction(session):
        item = await repo.get_or_404(item_id)
        before = snapshot(item, ITEM_AUDITED_FIELDS)

        data = payload.model_dump(exclude_unset=True)
        capability_updates = {k: v for k, v in data.items() if k in CAPABILITY_FIELDS}
        scalar_updates = {k: v for k, v in data.items() if k not in CAPABILITY_FIELDS}

        await _assert_tracking_change_is_safe(repo, item, scalar_updates)

        if "category_id" in scalar_updates and scalar_updates["category_id"] is not None:
            category = await ItemCategoryRepository(session, tenant).get(
                scalar_updates["category_id"]
            )
            if category is None:
                raise NotFoundError("Item category", scalar_updates["category_id"])

        base_uom_id = scalar_updates.get("base_uom_id", item.base_uom_id)
        purchase_uom_id = scalar_updates.get("purchase_uom_id", item.purchase_uom_id)
        sale_uom_id = scalar_updates.get("sale_uom_id", item.sale_uom_id)
        if {"base_uom_id", "purchase_uom_id", "sale_uom_id"} & scalar_updates.keys():
            await _validate_item_units(
                session,
                tenant,
                base_uom_id=base_uom_id,
                purchase_uom_id=purchase_uom_id,
                sale_uom_id=sale_uom_id,
            )
        await _assert_default_warehouse_exists(repo, scalar_updates.get("default_warehouse_id"))

        for field, value in scalar_updates.items():
            setattr(item, field, value)

        # Capability flags are only ever set explicitly on update. Re-deriving
        # them from a changed item_group would silently switch behaviour for an
        # item other modules already depend on (master rules section 1).
        for field, value in capability_updates.items():
            if value is not None:
                setattr(item, field, value)

        flags = {field: bool(getattr(item, field)) for field in CAPABILITY_FIELDS}
        validate_capabilities(item.production_type, flags)

        if item.shelf_life_days is not None and not item.is_batch_tracked:
            raise BusinessRuleError(
                "An item with a shelf life must be batch tracked.",
                details={"item_id": str(item.id)},
            )

        repo.touch(item)
        await session.flush()

        after = snapshot(item, ITEM_AUDITED_FIELDS)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ITEM,
            entity_id=item.id,
            entity_code=item.code,
            summary=f"Updated item {item.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=after,
        )

    return item


async def change_item_status(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, payload: ItemStatusChange
) -> Item:
    """Activate or deactivate an item through the lifecycle state machine."""
    repo = ItemRepository(session, tenant)

    async with transaction(session):
        item = await repo.get_or_404(item_id)
        current = lifecycle_state(item)
        target = ItemLifecycleState.ACTIVE if payload.is_active else ItemLifecycleState.INACTIVE

        # Raises InvalidStateTransitionError for a no-op or an illegal move, so
        # a double-click cannot produce a second audit row claiming a change.
        ITEM_LIFECYCLE.assert_transition(current, target)

        item.is_active = payload.is_active
        repo.touch(item)
        await session.flush()

        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type=AUDIT_ITEM,
            entity_id=item.id,
            entity_code=item.code,
            from_status=str(current),
            to_status=str(target),
            user_id=tenant.user_id,
            reason=payload.reason,
        )

    return item


async def delete_item(session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID) -> None:
    """Retire an item.

    Soft delete only. The item is referenced by ledger rows, BOMs and posted
    documents that must keep resolving its code and name (section 28).
    """
    repo = ItemRepository(session, tenant)

    async with transaction(session):
        item = await repo.get_or_404(item_id)
        current = lifecycle_state(item)
        ITEM_LIFECYCLE.assert_transition(current, ItemLifecycleState.ARCHIVED)

        await repo.soft_delete(item)
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type=AUDIT_ITEM,
            entity_id=item.id,
            entity_code=item.code,
            from_status=str(current),
            to_status=str(ItemLifecycleState.ARCHIVED),
            user_id=tenant.user_id,
            action=AuditAction.DELETE,
            reason="Item retired",
        )


async def get_item(
    session: AsyncSession, tenant: TenantContext, principal: Principal, item_id: uuid.UUID
) -> ItemDetail | ItemDetailWithCost:
    item = await ItemRepository(session, tenant).get_or_404(item_id)
    return item_response(item, include_cost=may_view_cost(principal))


async def search_items(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    filters: ItemFilters,
    params: PageParams,
    *,
    sort_by: Any = None,
) -> tuple[list[ItemSummary] | list[ItemSummaryWithCost], OffsetPagination]:
    """Offset-paginated item search.

    All filtering happens in SQL; the permission decides which columns are even
    selected, so an unprivileged caller's query never reads ``standard_cost``.
    """
    include_cost = may_view_cost(principal)
    repo = ItemRepository(session, tenant)
    rows, pagination = await repo.search_page(
        filters, params, include_cost=include_cost, order_by=sort_by
    )
    return item_summaries(rows, include_cost=include_cost), pagination


async def search_items_keyset(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    filters: ItemFilters,
    params: CursorParams,
) -> tuple[list[ItemSummary] | list[ItemSummaryWithCost], CursorPagination]:
    """Keyset variant - constant cost at any depth, for exports and infinite
    scroll over the full master (section 31)."""
    include_cost = may_view_cost(principal)
    repo = ItemRepository(session, tenant)
    rows, pagination = await repo.search_keyset(filters, params, include_cost=include_cost)
    return item_summaries(rows, include_cost=include_cost), pagination


# -----------------------------------------------------------------------------
# Variants
# -----------------------------------------------------------------------------


async def create_variant(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, payload: VariantCreate
) -> VariantOut:
    item_repo = ItemRepository(session, tenant)
    repo = ItemVariantRepository(session, tenant)

    async with transaction(session):
        item = await item_repo.get_or_404(item_id)

        if await repo.code_exists(payload.code):
            raise DuplicateError(f"A variant with code '{payload.code}' already exists.")
        if await repo.combination_exists(
            item_id, finish=payload.finish, size=payload.size, colour=payload.colour
        ):
            raise DuplicateError(
                "This finish/size/colour combination already exists for the item.",
                details={"finish": payload.finish, "size": payload.size, "colour": payload.colour},
            )

        variant = ItemVariant(
            item_id=item.id,
            code=payload.code,
            name=payload.name,
            finish=payload.finish,
            size=payload.size,
            colour=payload.colour,
            price_delta=payload.price_delta,
            cost_delta=payload.cost_delta,
            barcode=payload.barcode,
            is_active=payload.is_active,
        )
        repo.add(variant)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_VARIANT,
            entity_id=variant.id,
            entity_code=variant.code,
            summary=f"Created variant {variant.code} of item {item.code}",
            user_id=tenant.user_id,
            new_values={
                "item_id": item.id,
                "finish": variant.finish,
                "size": variant.size,
                "colour": variant.colour,
                "price_delta": variant.price_delta,
            },
        )

    return VariantOut.model_validate(variant)


async def update_variant(
    session: AsyncSession, tenant: TenantContext, variant_id: uuid.UUID, payload: VariantUpdate
) -> VariantOut:
    repo = ItemVariantRepository(session, tenant)
    audited = [
        "name",
        "finish",
        "size",
        "colour",
        "price_delta",
        "cost_delta",
        "barcode",
        "is_active",
    ]

    async with transaction(session):
        variant = await repo.get_or_404(variant_id)
        before = snapshot(variant, audited)

        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if value is not None:
                setattr(variant, field, value)

        if not any((variant.finish, variant.size, variant.colour)):
            raise ValidationError("A variant must keep at least one of finish, size or colour.")
        if {"finish", "size", "colour"} & data.keys() and await repo.combination_exists(
            variant.item_id,
            finish=variant.finish,
            size=variant.size,
            colour=variant.colour,
            exclude_id=variant.id,
        ):
            raise DuplicateError("This finish/size/colour combination already exists for the item.")

        repo.touch(variant)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_VARIANT,
            entity_id=variant.id,
            entity_code=variant.code,
            summary=f"Updated variant {variant.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(variant, audited),
        )

    return VariantOut.model_validate(variant)


async def delete_variant(
    session: AsyncSession, tenant: TenantContext, variant_id: uuid.UUID
) -> None:
    repo = ItemVariantRepository(session, tenant)

    async with transaction(session):
        variant = await repo.get_or_404(variant_id)
        await repo.soft_delete(variant)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_VARIANT,
            entity_id=variant.id,
            entity_code=variant.code,
            summary=f"Retired variant {variant.code}",
            user_id=tenant.user_id,
        )


async def list_variants(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    params: PageParams,
    *,
    is_active: bool | None = None,
) -> tuple[list[VariantOut], OffsetPagination]:
    await ItemRepository(session, tenant).get_or_404(item_id)
    rows, pagination = await ItemVariantRepository(session, tenant).list_for_item(
        item_id, params, is_active=is_active
    )
    return [VariantOut.model_validate(row) for row in rows], pagination


# -----------------------------------------------------------------------------
# Prices
# -----------------------------------------------------------------------------

# ASSUMPTION: when no price-list row matches, SALE falls back to items.sale_price
# and DEALER to items.dealer_price. DISTRIBUTOR has no column on the item master,
# so rather than reusing the dealer price - which would quietly invoice the wrong
# party at the wrong rate - the resolver refuses and asks for a price list row.
ITEM_MASTER_FALLBACK_FIELD: dict[PriceType, str | None] = {
    PriceType.SALE: "sale_price",
    PriceType.DEALER: "dealer_price",
    PriceType.DISTRIBUTOR: None,
}


async def create_price(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, payload: PriceCreate
) -> PriceOut:
    item_repo = ItemRepository(session, tenant)
    repo = ItemPriceRepository(session, tenant)

    async with transaction(session):
        item = await item_repo.get_or_404(item_id)

        if payload.variant_id is not None:
            variant = await ItemVariantRepository(session, tenant).get(payload.variant_id)
            if variant is None or variant.item_id != item.id:
                raise NotFoundError("Item variant", payload.variant_id)

        clashes = await repo.overlapping(
            item_id=item.id,
            variant_id=payload.variant_id,
            price_type=payload.price_type,
            min_quantity=payload.min_quantity,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )
        if clashes:
            raise BusinessRuleError(
                "Another active price for this channel and quantity slab already covers "
                "part of that date range. Close the existing price first.",
                details={"conflicting_price_ids": [str(row.id) for row in clashes]},
            )

        price = ItemPrice(
            item_id=item.id,
            variant_id=payload.variant_id,
            price_type=payload.price_type,
            currency=payload.currency.upper(),
            price=payload.price,
            min_quantity=payload.min_quantity,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            notes=payload.notes,
        )
        repo.add(price)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_PRICE,
            entity_id=price.id,
            entity_code=item.code,
            summary=(
                f"Added {price.price_type} price {price.price} for {item.code} "
                f"from {price.valid_from}"
            ),
            user_id=tenant.user_id,
            new_values={
                "price": price.price,
                "min_quantity": price.min_quantity,
                "price_type": str(price.price_type),
                "valid_from": price.valid_from,
                "valid_to": price.valid_to,
            },
        )

    return PriceOut.model_validate(price)


async def update_price(
    session: AsyncSession, tenant: TenantContext, price_id: uuid.UUID, payload: PriceUpdate
) -> PriceOut:
    repo = ItemPriceRepository(session, tenant)
    audited = ["price", "min_quantity", "valid_from", "valid_to", "is_active", "notes"]

    async with transaction(session):
        price = await repo.get_or_404(price_id)
        before = snapshot(price, audited)

        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if value is not None:
                setattr(price, field, value)

        if price.valid_to is not None and price.valid_to < price.valid_from:
            raise ValidationError("valid_to must not be earlier than valid_from.")

        if (
            price.is_active
            and {"min_quantity", "valid_from", "valid_to", "is_active"} & data.keys()
        ):
            clashes = await repo.overlapping(
                item_id=price.item_id,
                variant_id=price.variant_id,
                price_type=price.price_type,
                min_quantity=price.min_quantity,
                valid_from=price.valid_from,
                valid_to=price.valid_to,
                exclude_id=price.id,
            )
            if clashes:
                raise BusinessRuleError(
                    "That change would overlap another active price for the same channel "
                    "and quantity slab.",
                    details={"conflicting_price_ids": [str(row.id) for row in clashes]},
                )

        repo.touch(price)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PRICE,
            entity_id=price.id,
            summary="Updated item price",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(price, audited),
        )

    return PriceOut.model_validate(price)


async def delete_price(session: AsyncSession, tenant: TenantContext, price_id: uuid.UUID) -> None:
    repo = ItemPriceRepository(session, tenant)

    async with transaction(session):
        price = await repo.get_or_404(price_id)
        await repo.soft_delete(price)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_PRICE,
            entity_id=price.id,
            summary="Removed item price",
            user_id=tenant.user_id,
            old_values={"price": price.price, "price_type": str(price.price_type)},
        )


async def list_prices(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    params: PageParams,
    *,
    price_type: PriceType | None = None,
    variant_id: uuid.UUID | None = None,
    is_active: bool | None = None,
) -> tuple[list[PriceOut], OffsetPagination]:
    await ItemRepository(session, tenant).get_or_404(item_id)
    rows, pagination = await ItemPriceRepository(session, tenant).list_for_item(
        item_id, params, price_type=price_type, variant_id=variant_id, is_active=is_active
    )
    return [PriceOut.model_validate(row) for row in rows], pagination


async def resolve_price(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    price_type: PriceType,
    quantity: Decimal,
    variant_id: uuid.UUID | None = None,
    on_date: date | None = None,
) -> ResolvedPrice:
    """What this item costs a given channel, for a given quantity, on a date.

    Sales order entry calls this rather than reading ``items.sale_price``, so a
    dated price list and its quantity slabs actually take effect.
    """
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero to resolve a price.")

    effective_date = on_date or today()
    item = await ItemRepository(session, tenant).get_or_404(item_id)

    variant: ItemVariant | None = None
    if variant_id is not None:
        variant = await ItemVariantRepository(session, tenant).get(variant_id)
        if variant is None or variant.item_id != item.id:
            raise NotFoundError("Item variant", variant_id)

    rows = await ItemPriceRepository(session, tenant).candidates(
        item_id, price_type=price_type, variant_id=variant_id, on_date=effective_date
    )
    chosen = select_price_row(rows, quantity, variant_id=variant_id)

    if chosen is not None:
        # A variant-specific row is already the variant's price; the delta only
        # applies when an item-wide row was matched for a variant.
        delta = (
            Decimal(variant.price_delta)
            if variant is not None and chosen.variant_id is None
            else Decimal(0)
        )
        return ResolvedPrice(
            item_id=item.id,
            variant_id=variant_id,
            price_type=price_type,
            quantity=to_quantity(quantity),
            on_date=effective_date,
            price=to_unit_price(Decimal(chosen.price) + delta),
            currency=chosen.currency,
            source="PRICE_LIST",
            price_id=chosen.id,
            min_quantity=Decimal(chosen.min_quantity),
        )

    fallback_field = ITEM_MASTER_FALLBACK_FIELD[price_type]
    if fallback_field is None:
        raise BusinessRuleError(
            f"No {price_type} price is configured for item {item.code}. "
            "Add a price list entry for this channel.",
            details={"item_code": item.code, "price_type": str(price_type)},
        )

    base_price = Decimal(getattr(item, fallback_field))
    delta = Decimal(variant.price_delta) if variant is not None else Decimal(0)
    return ResolvedPrice(
        item_id=item.id,
        variant_id=variant_id,
        price_type=price_type,
        quantity=to_quantity(quantity),
        on_date=effective_date,
        price=to_unit_price(base_price + delta),
        currency="INR",
        source="ITEM_MASTER",
        price_id=None,
        min_quantity=Decimal(0),
    )


# -----------------------------------------------------------------------------
# Vendor links
# -----------------------------------------------------------------------------


async def link_vendor(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, payload: VendorLinkCreate
) -> VendorLinkOut:
    item_repo = ItemRepository(session, tenant)
    repo = ItemVendorLinkRepository(session, tenant)

    async with transaction(session):
        item = await item_repo.get_or_404(item_id)
        if not item.can_purchase:
            # Capability flag, never item_group: a finished good that is also
            # bought in is perfectly legitimate and must be linkable.
            raise BusinessRuleError(
                f"Item {item.code} is not marked as purchasable, so it cannot have a "
                "supplying vendor. Enable can_purchase first.",
                details={"item_id": str(item.id)},
            )

        # Tenant-scoped, not just the foreign key: a valid id from another
        # company's vendor would satisfy the constraint, and MRP reads the lead
        # time off this link (section 40).
        if not await item_repo.vendor_exists(payload.vendor_id):
            raise NotFoundError("Vendor", payload.vendor_id)

        if await repo.get_pair(item.id, payload.vendor_id) is not None:
            raise DuplicateError("This vendor is already linked to the item.")

        if payload.is_preferred:
            # Demote BEFORE inserting. The partial unique index is evaluated per
            # statement, not at commit, so inserting a second preferred row and
            # clearing the old one afterwards violates it - even though the
            # transaction would have ended in a valid state.
            await repo.clear_preferred(item.id)

        link = ItemVendorLink(
            item_id=item.id,
            vendor_id=payload.vendor_id,
            vendor_item_code=payload.vendor_item_code,
            vendor_item_name=payload.vendor_item_name,
            is_preferred=payload.is_preferred,
            lead_time_days=payload.lead_time_days,
            min_order_quantity=payload.min_order_quantity,
            order_multiple=payload.order_multiple,
            last_purchase_price=payload.last_purchase_price,
            last_purchase_date=payload.last_purchase_date,
            currency=payload.currency.upper(),
        )
        repo.add(link)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_VENDOR_LINK,
            entity_id=link.id,
            entity_code=item.code,
            summary=f"Linked vendor to item {item.code}",
            user_id=tenant.user_id,
            new_values={
                "vendor_id": link.vendor_id,
                "is_preferred": link.is_preferred,
                "lead_time_days": link.lead_time_days,
            },
        )

    return VendorLinkOut.model_validate(link)


async def update_vendor_link(
    session: AsyncSession, tenant: TenantContext, link_id: uuid.UUID, payload: VendorLinkUpdate
) -> VendorLinkOut:
    repo = ItemVendorLinkRepository(session, tenant)
    audited = [
        "vendor_item_code",
        "vendor_item_name",
        "is_preferred",
        "lead_time_days",
        "min_order_quantity",
        "order_multiple",
        "is_active",
    ]

    async with transaction(session):
        link = await repo.get_or_404(link_id)
        before = snapshot(link, audited)

        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if value is not None:
                setattr(link, field, value)

        if data.get("is_preferred"):
            await repo.clear_preferred(link.item_id, keep_id=link.id)

        repo.touch(link)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_VENDOR_LINK,
            entity_id=link.id,
            summary="Updated item-vendor link",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(link, audited),
        )

    return VendorLinkOut.model_validate(link)


async def unlink_vendor(session: AsyncSession, tenant: TenantContext, link_id: uuid.UUID) -> None:
    repo = ItemVendorLinkRepository(session, tenant)

    async with transaction(session):
        link = await repo.get_or_404(link_id)
        await repo.soft_delete(link)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_VENDOR_LINK,
            entity_id=link.id,
            summary="Removed item-vendor link",
            user_id=tenant.user_id,
            old_values={"vendor_id": link.vendor_id},
        )


async def list_vendor_links(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    params: PageParams,
    *,
    is_active: bool | None = None,
) -> tuple[list[VendorLinkOut], OffsetPagination]:
    await ItemRepository(session, tenant).get_or_404(item_id)
    rows, pagination = await ItemVendorLinkRepository(session, tenant).list_for_item(
        item_id, params, is_active=is_active
    )
    return [VendorLinkOut.model_validate(row) for row in rows], pagination


__all__ = [
    "CAPABILITY_FIELDS",
    "ITEM_LIFECYCLE",
    "ITEM_MASTER_FALLBACK_FIELD",
    "PRODUCTION_TYPE_REQUIRED_CAPABILITIES",
    "UomGraph",
    "build_category_path",
    "category_depth",
    "change_item_status",
    "convert_quantity",
    "create_category",
    "create_conversion",
    "create_item",
    "create_price",
    "create_uom",
    "create_variant",
    "default_capabilities",
    "delete_category",
    "delete_conversion",
    "delete_item",
    "delete_price",
    "delete_variant",
    "get_category",
    "get_item",
    "item_response",
    "item_summaries",
    "lifecycle_state",
    "link_vendor",
    "list_categories",
    "list_conversions",
    "list_prices",
    "list_uoms",
    "list_variants",
    "list_vendor_links",
    "load_uom_graph",
    "may_view_cost",
    "resolve_capabilities",
    "resolve_price",
    "search_items",
    "search_items_keyset",
    "select_price_row",
    "unlink_vendor",
    "update_category",
    "update_item",
    "update_price",
    "update_uom",
    "update_variant",
    "update_vendor_link",
    "validate_capabilities",
]
