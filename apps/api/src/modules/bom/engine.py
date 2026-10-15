"""Multi-level BOM explosion, reverse explosion and cost roll-up.

This is the calculation the rest of the plant is planned from, so the three
properties that make it correct are stated here and enforced structurally rather
than left to review.

**Common parts are consolidated, and the merge stays auditable.**
``SCREW-M4`` under both ``Rose Handle`` (2 per) and ``Lock Body`` (4 per) yields
*one* row for 600 when 100 kits are exploded, not two rows a buyer has to add up
by hand (master rules section 11). The merge is keyed on ``item_id`` and each
contributing branch is retained in ``source_paths``, so a planner can always ask
where a number came from.

**Cycles and depth are caught during traversal, not after.** A component that
reaches one of its own ancestors raises :class:`~src.core.errors.BomCycleError`
with the offending path at the moment the edge is followed, before any further
work is done. Nothing here builds a full graph and then looks for a loop in it -
a loop makes "build the full graph" the thing that never finishes.

**Loading is batched by level, never by node.** The traversal holds a *set* of
items at each level and issues exactly three statements for that whole set: the
effective structures, their lines, and the item facts. A structure with two
hundred parts therefore costs three round-trips per level and not two hundred
(section 32). The :class:`BomDataSource` seam exists so that property is
*testable*: the unit suite drives the traversal through an in-memory source that
counts calls, which turns "no N+1" from an assertion in a comment into a failing
test if anyone reintroduces a per-node fetch.

Rounding note: every quantity is ``Decimal``, and each level's requirement is
quantised with :func:`~src.core.types.to_quantity` as it is produced. Quantising
per level rather than only at the end is deliberate - the intermediate figure is
a real quantity that will be issued to a shop-floor job, and a plan whose totals
do not equal the sum of its own printed lines is not defensible to a storekeeper.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import TenantContext
from src.core.errors import AppError, BomCycleError, BusinessRuleError, ErrorCode, NotFoundError
from src.core.logging import get_logger
from src.core.types import to_money, to_quantity
from src.modules.bom.models import (
    DEFAULT_MAX_EXPLOSION_DEPTH,
    Bom,
    BomLine,
    BomOperation,
    BomStatus,
    BomType,
)
from src.modules.bom.repository import (
    LIVE_BOM_STATUSES,
    BomLineRepository,
    BomOperationRepository,
    BomRepository,
    ItemInfo,
    ItemLookupRepository,
)

log = get_logger(__name__)

# Working precision for the multiplier chain. Wide enough that a twenty-level
# structure with awkward yields (3 pieces from one casting) does not accumulate
# a visible error before the per-level quantisation.
EXPLOSION_PRECISION = 34

#: Hard stop on traversal size. A legitimate architectural-hardware product is
#: hundreds of nodes; twenty thousand means a data error that would otherwise
#: turn one HTTP request into a multi-second CPU burn.
#: ASSUMPTION: no real structure exceeds this. Raised as a business error naming
#: the root so the offending data can be found.
MAX_EXPLOSION_NODES = 20_000

#: How many distinct branches are kept per consolidated component. The quantity
#: is always exact; only the *listing* of contributing paths is capped, and
#: ``source_path_count`` reports the true number so a truncated list can never be
#: mistaken for the whole story.
MAX_SOURCE_PATHS_PER_COMPONENT = 50

#: Reverse explosion bounds. ``levels`` is capped because each level is another
#: fan-out, and the row budget stops a universal part (a common screw) from
#: materialising every structure in the company.
MAX_WHERE_USED_LEVELS = 10
WHERE_USED_ROW_LIMIT = 1_000

# Cost provenance labels, part of the API contract for GET /bom/{id}/cost.
COST_SOURCE_ROLLED_UP = "ROLLED_UP"
COST_SOURCE_STANDARD = "STANDARD_COST"
COST_SOURCE_PURCHASE = "LAST_PURCHASE_PRICE"
COST_SOURCE_MISSING = "NONE"


class BomDepthExceededError(BusinessRuleError):
    """The structure is deeper than the caller allowed.

    Almost always a cycle that the ancestor check could not see because the
    repeating item is reached by a different item each time, or a structure that
    was built by pointing every part at a sub-assembly.
    """

    code = ErrorCode.BOM_DEPTH_EXCEEDED

    def __init__(self, path: Sequence[str], max_depth: int) -> None:
        super().__init__(
            f"The bill of materials is deeper than the {max_depth}-level limit at: "
            + " -> ".join(path),
            details={"path": list(path), "max_depth": max_depth},
        )


class BomTooLargeError(BusinessRuleError):
    """The traversal exceeded :data:`MAX_EXPLOSION_NODES`."""

    def __init__(self, root_code: str, node_budget: int) -> None:
        super().__init__(
            f"Exploding {root_code} visits more than {node_budget} nodes, which is a "
            "data error rather than a product structure.",
            details={"root_item_code": root_code, "node_budget": node_budget},
        )


# -----------------------------------------------------------------------------
# Pure quantity arithmetic - unit tested without a database
# -----------------------------------------------------------------------------


def line_requirement(
    parent_quantity: Decimal, quantity_per: Decimal, output_quantity: Decimal
) -> Decimal:
    """Net requirement for one line, before its scrap allowance.

    ``quantity_per`` is stated per ``output_quantity`` units of the parent, not
    per unit, so a structure that yields ten handles from one billet divides
    rather than multiplies. The division happens at high precision and is
    quantised once, so 300 units of a 3-per-run structure needs exactly 100
    runs' worth and not 99.99.
    """
    if output_quantity <= 0:
        raise ValueError("output_quantity must be positive")
    with localcontext() as ctx:
        ctx.prec = EXPLOSION_PRECISION
        raw = parent_quantity * quantity_per / output_quantity
    return to_quantity(raw)


def apply_scrap(quantity: Decimal, scrap_percentage: Decimal) -> Decimal:
    """Inflate a requirement by its process-loss allowance.

    ``qty * (1 + scrap% / 100)``. This is the number to buy or make: the scrap is
    consumed by the process, so planning against the un-inflated figure is how a
    job runs short on its last piece.
    """
    if scrap_percentage < 0:
        raise ValueError("scrap_percentage cannot be negative")
    if not scrap_percentage:
        return to_quantity(quantity)
    with localcontext() as ctx:
        ctx.prec = EXPLOSION_PRECISION
        inflated = quantity * (Decimal(1) + scrap_percentage / Decimal(100))
    return to_quantity(inflated)


def is_explodable(line: BomLine) -> bool:
    """Whether a line contributes to requirements.

    ASSUMPTION: substitutes and optional components are excluded.

    A substitute is an approved *replacement* for another line - both are never
    consumed together, so counting it would double the requirement. An optional
    component is a configurable extra whose demand comes from the order that
    selects it, not from the structure. Both appear on the BOM detail and in
    costing inputs; neither creates demand on its own.
    """
    return line.substitute_for_line_id is None and not line.is_optional


def operation_cost_per_run(operations: Sequence[BomOperation], output_quantity: Decimal) -> Decimal:
    """Total operation cost for one run yielding ``output_quantity`` units.

    ASSUMPTION: setup time and fixed charges are per *run* and are therefore
    amortised over ``output_quantity`` when a per-unit cost is derived, while run
    time is charged per unit. Treating setup as per-unit would price a
    hundred-piece batch as if the machine were set up a hundred times.
    """
    if output_quantity <= 0:
        raise ValueError("output_quantity must be positive")

    with localcontext() as ctx:
        ctx.prec = EXPLOSION_PRECISION
        total = Decimal(0)
        for operation in operations:
            per_run = operation.fixed_cost + (
                operation.setup_time_minutes / Decimal(60) * operation.cost_per_hour
            )
            per_unit = operation.run_time_minutes_per_unit / Decimal(60) * operation.cost_per_hour
            total += per_run + per_unit * output_quantity
    return to_money(total)


def subcontract_operation_cost_per_run(
    operations: Sequence[BomOperation], output_quantity: Decimal
) -> Decimal:
    """The outsourced portion of a run's operation cost, already in its total."""
    return operation_cost_per_run(
        [operation for operation in operations if operation.is_outsourced], output_quantity
    )


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExplodedComponent:
    """One consolidated requirement line."""

    item_id: uuid.UUID
    item_code: str
    item_name: str
    #: Consolidated across every branch, before each branch's scrap allowance.
    total_quantity: Decimal
    #: The same requirement with scrap applied. Plan against this one.
    scrap_adjusted_quantity: Decimal
    uom_id: uuid.UUID
    #: Shallowest level the item appears at; 1 is a direct component of the root.
    level: int
    is_purchased: bool
    is_manufactured: bool
    #: Item codes from the root down to this component, one tuple per branch.
    source_paths: tuple[tuple[str, ...], ...]
    #: True number of contributing branches - ``source_paths`` may be capped.
    source_path_count: int


@dataclass(frozen=True, slots=True)
class BomExplosion:
    """The result of exploding one structure for a quantity."""

    root_item_id: uuid.UUID
    root_item_code: str
    bom_id: uuid.UUID
    bom_code: str
    root_bom_status: str
    quantity: Decimal
    as_of: date
    max_level_reached: int
    phantom_item_codes: tuple[str, ...]
    components: tuple[ExplodedComponent, ...]

    def component_for(self, item_id: uuid.UUID) -> ExplodedComponent | None:
        return next((c for c in self.components if c.item_id == item_id), None)


@dataclass(frozen=True, slots=True)
class WhereUsedEntry:
    """One parent structure that consumes the queried component."""

    parent_item_id: uuid.UUID
    parent_item_code: str
    parent_item_name: str
    bom_id: uuid.UUID
    bom_code: str
    bom_version: int
    bom_status: str
    is_default: bool
    line_number: int
    quantity_per: Decimal
    level: int


@dataclass(frozen=True, slots=True)
class BomCostComponent:
    item_id: uuid.UUID
    item_code: str
    #: Scrap-adjusted quantity of this component per one unit of the parent.
    quantity_per_unit: Decimal
    unit_cost: Decimal
    extended_cost: Decimal
    cost_source: str


@dataclass(frozen=True, slots=True)
class BomCost:
    """A costed structure: material rolled up through every level, plus operations."""

    item_id: uuid.UUID
    item_code: str
    bom_id: uuid.UUID
    bom_code: str
    output_quantity: Decimal
    material_cost: Decimal
    operation_cost: Decimal
    #: Direct outsourced operations for this BOM run, included in operation_cost.
    subcontract_operation_cost: Decimal
    total_cost: Decimal
    unit_cost: Decimal
    components: tuple[BomCostComponent, ...]
    #: Items anywhere in the structure with no cost on file. Reported rather than
    #: silently treated as free, because a roll-up that quietly understates is
    #: worse than one that admits what it does not know.
    items_without_cost: tuple[str, ...]


# -----------------------------------------------------------------------------
# Data source - the seam that makes batching testable
# -----------------------------------------------------------------------------


class BomDataSource(Protocol):
    """Everything the traversal needs, always for a *set* of ids.

    There is deliberately no single-id method on this protocol. The engine
    physically cannot ask per node, which is what keeps the explosion at
    O(depth) round-trips instead of O(nodes).
    """

    async def effective_boms(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, Bom]: ...

    async def lines_for_boms(
        self, bom_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[BomLine]]: ...

    async def items(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ItemInfo]: ...

    async def operations_for_boms(
        self, bom_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[BomOperation]]: ...

    async def last_purchase_prices(
        self, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]: ...


class DatabaseBomSource:
    """The production :class:`BomDataSource`, backed by the module repositories."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self._boms = BomRepository(session, tenant)
        self._lines = BomLineRepository(session, tenant)
        self._operations = BomOperationRepository(session, tenant)
        self._items = ItemLookupRepository(session, tenant)

    async def effective_boms(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, Bom]:
        return await self._boms.effective_boms_for_items(item_ids, as_of)

    async def lines_for_boms(self, bom_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[BomLine]]:
        return await self._lines.lines_for_boms(bom_ids)

    async def items(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ItemInfo]:
        return await self._items.load(item_ids)

    async def operations_for_boms(
        self, bom_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[BomOperation]]:
        return await self._operations.operations_for_boms(bom_ids)

    async def last_purchase_prices(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        return await self._items.last_purchase_prices(item_ids)


# -----------------------------------------------------------------------------
# Traversal
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Node:
    """One occurrence of an item on one branch of the structure.

    Nodes stay distinct per branch even when the same item appears twice:
    consolidation happens in the accumulator, while the traversal keeps the
    ancestry it needs to answer "is this a cycle *on this path*?" precisely. A
    merged frontier would have to answer that question against the union of
    every branch's ancestors and would report cycles that do not exist.
    """

    item_id: uuid.UUID
    #: Requirement before this occurrence's own scrap allowance.
    net_quantity: Decimal
    #: Requirement after scrap - the multiplier passed to the children, because
    #: the scrapped pieces have to be built too.
    gross_quantity: Decimal
    path: tuple[str, ...]
    ancestors: frozenset[uuid.UUID]


@dataclass
class _Accumulated:
    """Mutable consolidation bucket for one item id."""

    info: ItemInfo
    total_quantity: Decimal
    scrap_adjusted_quantity: Decimal
    level: int
    paths: list[tuple[str, ...]]
    path_count: int = 0


@dataclass
class _Traversal:
    """Everything one walk of the structure produced.

    Both consumers - the explosion and the cost roll-up - are built from this,
    so a structure is never walked twice for one request.
    """

    root_info: ItemInfo
    root_bom: Bom
    accumulated: dict[uuid.UUID, _Accumulated] = field(default_factory=dict)
    bom_by_item: dict[uuid.UUID, Bom] = field(default_factory=dict)
    lines_by_bom: dict[uuid.UUID, list[BomLine]] = field(default_factory=dict)
    item_info: dict[uuid.UUID, ItemInfo] = field(default_factory=dict)
    deepest_level: dict[uuid.UUID, int] = field(default_factory=dict)
    phantom_item_ids: set[uuid.UUID] = field(default_factory=set)
    max_level_reached: int = 0


async def _resolve_root(
    source: BomDataSource,
    *,
    item_id: uuid.UUID,
    bom: Bom | None,
    as_of: date,
) -> tuple[ItemInfo, Bom]:
    """Find the item and the structure the explosion starts from."""
    info = (await source.items([item_id])).get(item_id)
    if info is None:
        raise NotFoundError("Item", item_id)

    if bom is None:
        bom = (await source.effective_boms([item_id], as_of)).get(item_id)
        if bom is None:
            raise AppError(
                f"No approved bill of materials is effective for {info.code} on {as_of}.",
                code=ErrorCode.BOM_NOT_FOUND,
                status_code=404,
                details={
                    "item_id": str(item_id),
                    "item_code": info.code,
                    "as_of": as_of.isoformat(),
                },
            )
    return info, bom


async def _walk(
    source: BomDataSource,
    *,
    root_info: ItemInfo,
    root_bom: Bom,
    quantity: Decimal,
    as_of: date,
    max_depth: int,
) -> _Traversal:
    """Breadth-first descent, three batched statements per level.

    The loop shape is the contract: at the top of each iteration ``frontier``
    holds every occurrence at one level, and the queries that follow are issued
    once for the whole of it. Anything that needs a fetch inside the per-node
    loop belongs in one of those three batches instead.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    walk = _Traversal(root_info=root_info, root_bom=root_bom)
    walk.item_info[root_info.id] = root_info
    walk.deepest_level[root_info.id] = 0

    frontier = [
        _Node(
            item_id=root_info.id,
            net_quantity=to_quantity(quantity),
            gross_quantity=to_quantity(quantity),
            path=(root_info.code,),
            ancestors=frozenset({root_info.id}),
        )
    ]
    level = 0
    visited_nodes = 0

    while frontier:
        visited_nodes += len(frontier)
        if visited_nodes > MAX_EXPLOSION_NODES:
            raise BomTooLargeError(root_info.code, MAX_EXPLOSION_NODES)

        # BATCH 1 - the structures for every item at this level. The root's
        # structure is already known (the caller may have named a specific one),
        # so level 0 never re-resolves it.
        if level == 0:
            boms = {root_info.id: root_bom}
        else:
            boms = await source.effective_boms([node.item_id for node in frontier], as_of)
        walk.bom_by_item.update(boms)

        # Record this level's occurrences. The root itself is the thing being
        # made, not a requirement for making it, so it is never recorded.
        for node in frontier:
            if level == 0:
                continue
            structure = boms.get(node.item_id)
            if structure is not None and structure.bom_type == BomType.PHANTOM:
                # A phantom is a drawing-level grouping that is never stocked,
                # bought or made: the explosion blows straight through it and
                # raises requirements for its children only (master rules
                # section 11). It is reported separately so the omission is
                # visible rather than mysterious.
                walk.phantom_item_ids.add(node.item_id)
                continue
            _accumulate(walk, node, level)

        expandable = [node for node in frontier if node.item_id in boms]
        if not expandable:
            break

        # Descending now would create level ``level + 1``. Checked before the
        # work is done, so a runaway structure costs one comparison, not a level.
        if level >= max_depth:
            raise BomDepthExceededError(expandable[0].path, max_depth)

        # BATCH 2 - every line of every structure at this level.
        bom_ids = [boms[node.item_id].id for node in expandable]
        lines_by_bom = await source.lines_for_boms(bom_ids)
        walk.lines_by_bom.update(lines_by_bom)

        component_ids = {
            line.component_item_id
            for lines in lines_by_bom.values()
            for line in lines
            if is_explodable(line)
        }
        # BATCH 3 - item facts for the components this level introduced. Ones
        # already seen higher up are not re-fetched, so a part used on twenty
        # branches is read once for the whole explosion.
        unknown = sorted(component_ids - walk.item_info.keys(), key=str)
        if unknown:
            walk.item_info.update(await source.items(unknown))

        frontier = _expand(
            walk,
            expandable,
            boms,
            lines_by_bom,
            level,
            budget_remaining=MAX_EXPLOSION_NODES - visited_nodes,
            root_code=root_info.code,
        )
        level += 1
        if frontier:
            walk.max_level_reached = max(walk.max_level_reached, level)

    return walk


def _accumulate(walk: _Traversal, node: _Node, level: int) -> None:
    """Merge one occurrence into the consolidated result.

    Keyed on ``item_id`` alone - that is the whole point of consolidation
    (master rules section 11 / database.md section 5). ``level`` keeps the
    *shallowest* occurrence so a part that is both a direct component and a
    sub-sub-component is reported at the level a planner will look for it.
    """
    info = walk.item_info.get(node.item_id)
    if info is None:  # pragma: no cover - the walk loads every node's info first
        return

    existing = walk.accumulated.get(node.item_id)
    if existing is None:
        walk.accumulated[node.item_id] = _Accumulated(
            info=info,
            total_quantity=node.net_quantity,
            scrap_adjusted_quantity=node.gross_quantity,
            level=level,
            paths=[node.path],
            path_count=1,
        )
        return

    existing.total_quantity += node.net_quantity
    existing.scrap_adjusted_quantity += node.gross_quantity
    existing.level = min(existing.level, level)
    existing.path_count += 1
    if len(existing.paths) < MAX_SOURCE_PATHS_PER_COMPONENT:
        existing.paths.append(node.path)


def _expand(
    walk: _Traversal,
    expandable: Sequence[_Node],
    boms: dict[uuid.UUID, Bom],
    lines_by_bom: dict[uuid.UUID, list[BomLine]],
    level: int,
    *,
    budget_remaining: int,
    root_code: str,
) -> list[_Node]:
    """Turn one level's occurrences into the next level's frontier.

    Pure bookkeeping: everything it reads was fetched by the three batched
    queries above, so there is no ``await`` in this loop by construction.

    ``budget_remaining`` is checked as the frontier is built rather than only
    when the caller reaches the top of the next iteration. A level of 500
    occurrences whose structures each carry 500 lines produces a quarter of a
    million ``_Node`` objects before control returns, so a budget enforced only
    afterwards is a budget the process can run out of memory before reaching.
    """
    next_frontier: list[_Node] = []

    for node in expandable:
        structure = boms[node.item_id]
        for line in lines_by_bom.get(structure.id, ()):
            if not is_explodable(line):
                continue

            component_info = walk.item_info.get(line.component_item_id)
            if component_info is None:
                # The component was retired out from under an approved
                # structure. Refusing is the only safe answer: silently
                # dropping it would understate the requirement.
                raise NotFoundError("BOM component item", line.component_item_id)

            # Cycle check on this branch's ancestry, at the moment the edge is
            # followed - not after building a graph a cycle would prevent from
            # ever being finished.
            if line.component_item_id in node.ancestors:
                raise BomCycleError([*node.path, component_info.code])

            net = line_requirement(
                node.gross_quantity, line.quantity_per, structure.output_quantity
            )
            gross = apply_scrap(net, line.scrap_percentage)
            child_level = level + 1

            walk.deepest_level[line.component_item_id] = max(
                walk.deepest_level.get(line.component_item_id, 0), child_level
            )
            next_frontier.append(
                _Node(
                    item_id=line.component_item_id,
                    net_quantity=net,
                    gross_quantity=gross,
                    path=(*node.path, component_info.code),
                    ancestors=node.ancestors | {line.component_item_id},
                )
            )
            if len(next_frontier) > budget_remaining:
                raise BomTooLargeError(root_code, MAX_EXPLOSION_NODES)

    return next_frontier


# -----------------------------------------------------------------------------
# Public engine API
# -----------------------------------------------------------------------------


async def explode(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    quantity: Decimal,
    *,
    bom_id: uuid.UUID | None = None,
    max_depth: int = DEFAULT_MAX_EXPLOSION_DEPTH,
    as_of: date | None = None,
    source: BomDataSource | None = None,
) -> BomExplosion:
    """Explode ``quantity`` of ``item_id`` into a consolidated component list.

    ``bom_id`` names a specific structure to start from; otherwise the approved
    structure effective on ``as_of`` is resolved.

    ASSUMPTION: an explicitly named root structure is exploded whatever its
    status, so an engineer can check a draft before submitting it. Every level
    *below* the root is resolved strictly from APPROVED structures whose
    effectivity window contains ``as_of`` (database.md section 5), and the
    status actually used is reported on the result so a draft explosion cannot
    be mistaken for the plan of record.

    ``source`` is an injection point for tests; production callers leave it
    unset and get the database-backed source.
    """
    if quantity <= 0:
        raise BusinessRuleError(
            "Explosion quantity must be greater than zero.",
            details={"quantity": str(quantity)},
        )

    effective_date = as_of or today()
    data = source or DatabaseBomSource(session, tenant)

    root_bom: Bom | None = None
    if bom_id is not None:
        root_bom = await BomRepository(session, tenant).get_or_404(bom_id)
        if root_bom.item_id != item_id:
            raise BusinessRuleError(
                "The requested bill of materials does not belong to this item.",
                details={"bom_id": str(bom_id), "item_id": str(item_id)},
            )

    root_info, resolved_bom = await _resolve_root(
        data, item_id=item_id, bom=root_bom, as_of=effective_date
    )
    walk = await _walk(
        data,
        root_info=root_info,
        root_bom=resolved_bom,
        quantity=quantity,
        as_of=effective_date,
        max_depth=max_depth,
    )

    components = tuple(
        ExplodedComponent(
            item_id=item,
            item_code=bucket.info.code,
            item_name=bucket.info.name,
            total_quantity=bucket.total_quantity,
            scrap_adjusted_quantity=bucket.scrap_adjusted_quantity,
            uom_id=bucket.info.base_uom_id,
            level=bucket.level,
            is_purchased=bucket.info.is_purchased,
            is_manufactured=bucket.info.is_manufactured,
            source_paths=tuple(bucket.paths),
            source_path_count=bucket.path_count,
        )
        for item, bucket in walk.accumulated.items()
    )
    # Level first, then code: the natural reading order of an indented
    # structure, and stable so two runs of the same explosion diff cleanly.
    ordered = tuple(sorted(components, key=lambda c: (c.level, c.item_code)))

    phantom_codes = tuple(
        sorted(walk.item_info[i].code for i in walk.phantom_item_ids if i in walk.item_info)
    )

    return BomExplosion(
        root_item_id=root_info.id,
        root_item_code=root_info.code,
        bom_id=resolved_bom.id,
        bom_code=resolved_bom.code,
        root_bom_status=str(resolved_bom.status),
        quantity=to_quantity(quantity),
        as_of=effective_date,
        max_level_reached=walk.max_level_reached,
        phantom_item_codes=phantom_codes,
        components=ordered,
    )


async def where_used(
    session: AsyncSession,
    tenant: TenantContext,
    component_item_id: uuid.UUID,
    *,
    levels: int = 1,
    statuses: Sequence[BomStatus] = LIVE_BOM_STATUSES,
    row_limit: int = WHERE_USED_ROW_LIMIT,
) -> list[WhereUsedEntry]:
    """Reverse explosion: which structures consume this component?

    Level 1 is the direct parents, level 2 their parents, and so on. Each level
    is three batched statements (lines, their structures, the parent items), so
    the cost is bounded by ``levels`` rather than by how widely the part is used.

    OBSOLETE structures are excluded by default: the question being asked is
    "what does changing this part break *now*".
    """
    if levels < 1 or levels > MAX_WHERE_USED_LEVELS:
        raise BusinessRuleError(
            f"where-used supports 1 to {MAX_WHERE_USED_LEVELS} levels.",
            details={"levels": levels, "max_levels": MAX_WHERE_USED_LEVELS},
        )

    line_repo = BomLineRepository(session, tenant)
    bom_repo = BomRepository(session, tenant)
    item_repo = ItemLookupRepository(session, tenant)

    entries: list[WhereUsedEntry] = []
    # Guards against a structure that is (incorrectly) cyclic making the reverse
    # walk loop forever, and stops a diamond from being reported twice.
    seen_items: set[uuid.UUID] = {component_item_id}
    frontier: list[uuid.UUID] = [component_item_id]

    for level in range(1, levels + 1):
        remaining = row_limit - len(entries)
        if remaining <= 0 or not frontier:
            break

        lines = await line_repo.parent_lines_for_components(
            frontier, statuses=list(statuses), limit=remaining
        )
        if not lines:
            break

        bom_ids = sorted({line.bom_id for line in lines}, key=str)
        boms = {bom.id: bom for bom in await bom_repo.get_many(bom_ids)}
        parent_item_ids = sorted({bom.item_id for bom in boms.values()}, key=str)
        infos = await item_repo.load(parent_item_ids)

        next_frontier: list[uuid.UUID] = []
        for line in lines:
            bom = boms.get(line.bom_id)
            if bom is None:
                continue
            info = infos.get(bom.item_id)
            if info is None:
                continue
            entries.append(
                WhereUsedEntry(
                    parent_item_id=bom.item_id,
                    parent_item_code=info.code,
                    parent_item_name=info.name,
                    bom_id=bom.id,
                    bom_code=bom.code,
                    bom_version=bom.version,
                    bom_status=str(bom.status),
                    is_default=bom.is_default,
                    line_number=line.line_number,
                    quantity_per=line.quantity_per,
                    level=level,
                )
            )
            if bom.item_id not in seen_items:
                seen_items.add(bom.item_id)
                next_frontier.append(bom.item_id)

        frontier = next_frontier

    if len(entries) >= row_limit:
        log.warning(
            "bom.where_used.truncated",
            component_item_id=str(component_item_id),
            row_limit=row_limit,
        )
    return entries


async def cost_roll_up(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    *,
    bom_id: uuid.UUID | None = None,
    as_of: date | None = None,
    max_depth: int = DEFAULT_MAX_EXPLOSION_DEPTH,
    source: BomDataSource | None = None,
) -> BomCost:
    """Cost one structure, rolling material up through every level.

    Leaves are costed from ``items.standard_cost``, falling back to the last
    purchase price recorded against the preferred vendor.

    ASSUMPTION: an item that has an effective structure of its own is costed
    *from that structure*, not from its stored standard cost - that is what a
    roll-up is, and reading a stale standard cost for a sub-assembly would hide
    exactly the drift the roll-up exists to surface. An item with neither a
    structure nor a cost is treated as zero and named in ``items_without_cost``.
    """
    effective_date = as_of or today()
    data = source or DatabaseBomSource(session, tenant)

    root_bom: Bom | None = None
    if bom_id is not None:
        root_bom = await BomRepository(session, tenant).get_or_404(bom_id)
        if root_bom.item_id != item_id:
            raise BusinessRuleError(
                "The requested bill of materials does not belong to this item.",
                details={"bom_id": str(bom_id), "item_id": str(item_id)},
            )

    root_info, resolved_bom = await _resolve_root(
        data, item_id=item_id, bom=root_bom, as_of=effective_date
    )
    # One unit is enough: the roll-up needs the structure, and the quantities it
    # costs come from the lines themselves rather than from a demand figure.
    walk = await _walk(
        data,
        root_info=root_info,
        root_bom=resolved_bom,
        quantity=Decimal(1),
        as_of=effective_date,
        max_depth=max_depth,
    )

    operations_by_bom = await data.operations_for_boms(
        sorted({bom.id for bom in walk.bom_by_item.values()}, key=str)
    )

    # Leaves are everything with no structure of its own. Their fallback prices
    # are fetched in one statement rather than per leaf.
    leaf_ids = [i for i in walk.item_info if i not in walk.bom_by_item]
    fallback_prices = await data.last_purchase_prices(sorted(leaf_ids, key=str))

    unit_costs, sources, missing = _roll_up_unit_costs(walk, operations_by_bom, fallback_prices)

    return _build_cost_result(
        walk,
        unit_costs=unit_costs,
        sources=sources,
        missing=missing,
        operations=operations_by_bom.get(resolved_bom.id, []),
    )


def _roll_up_unit_costs(
    walk: _Traversal,
    operations_by_bom: dict[uuid.UUID, list[BomOperation]],
    fallback_prices: dict[uuid.UUID, Decimal],
) -> tuple[dict[uuid.UUID, Decimal], dict[uuid.UUID, str], list[str]]:
    """Cost every item in the structure, deepest first.

    Deepest-first is a valid topological order here: if X is a component of Y
    then every occurrence of Y at level L puts X at L+1, so X's deepest level is
    strictly greater than Y's. The traversal has already rejected cycles, so the
    order is total and each cost is available before it is needed.
    """
    unit_costs: dict[uuid.UUID, Decimal] = {}
    sources: dict[uuid.UUID, str] = {}
    missing: list[str] = []

    ordered = sorted(
        walk.item_info.values(),
        key=lambda info: (-walk.deepest_level.get(info.id, 0), info.code),
    )

    for info in ordered:
        structure = walk.bom_by_item.get(info.id)
        if structure is None:
            cost = info.standard_cost or Decimal(0)
            if cost > 0:
                sources[info.id] = COST_SOURCE_STANDARD
            else:
                fallback = fallback_prices.get(info.id)
                if fallback is not None and fallback > 0:
                    cost = fallback
                    sources[info.id] = COST_SOURCE_PURCHASE
                else:
                    sources[info.id] = COST_SOURCE_MISSING
                    missing.append(info.code)
            unit_costs[info.id] = to_money(cost)
            continue

        material = _material_cost_for_run(walk, structure, unit_costs)
        operation = operation_cost_per_run(
            operations_by_bom.get(structure.id, []), structure.output_quantity
        )
        with localcontext() as ctx:
            ctx.prec = EXPLOSION_PRECISION
            per_unit = (material + operation) / structure.output_quantity
        unit_costs[info.id] = to_money(per_unit)
        sources[info.id] = COST_SOURCE_ROLLED_UP

    return unit_costs, sources, sorted(set(missing))


def _priced_lines(
    walk: _Traversal, structure: Bom, unit_costs: dict[uuid.UUID, Decimal]
) -> list[tuple[BomLine, Decimal, Decimal]]:
    """Cost every explodable line of one run: ``(line, quantity, extended cost)``.

    One function decides how a line's money is rounded, and the material total
    is the sum of exactly those figures. Rounding each printed line to money
    scale and rounding the total independently is not the same number - two
    lines of 1.05 x 0.25 print as 0.26 each while the separately rounded total
    is 0.53 - so a costing screen's column would not add up to its own footer
    and ``boms.total_material_cost`` could not be reconciled against the
    evidence stored beside it. The drift grows with the line count, which is
    exactly where nobody re-adds the column by hand.
    """
    priced: list[tuple[BomLine, Decimal, Decimal]] = []
    for line in walk.lines_by_bom.get(structure.id, ()):
        if not is_explodable(line):
            continue
        quantity = apply_scrap(line.quantity_per, line.scrap_percentage)
        unit_cost = unit_costs.get(line.component_item_id, Decimal(0))
        with localcontext() as ctx:
            ctx.prec = EXPLOSION_PRECISION
            extended = quantity * unit_cost
        priced.append((line, quantity, to_money(extended)))
    return priced


def _material_cost_for_run(
    walk: _Traversal, structure: Bom, unit_costs: dict[uuid.UUID, Decimal]
) -> Decimal:
    """Material cost of one run of ``structure`` yielding its output quantity.

    The sum of the per-line extended costs, never a separately rounded total -
    see :func:`_priced_lines`.
    """
    priced = _priced_lines(walk, structure, unit_costs)
    return to_money(sum((extended for _, _, extended in priced), Decimal(0)))


def _build_cost_result(
    walk: _Traversal,
    *,
    unit_costs: dict[uuid.UUID, Decimal],
    sources: dict[uuid.UUID, str],
    missing: Sequence[str],
    operations: Sequence[BomOperation],
) -> BomCost:
    structure = walk.root_bom
    output = structure.output_quantity

    # The same priced lines back both the component listing and the material
    # total, so the printed lines always add up to the printed total.
    priced = _priced_lines(walk, structure, unit_costs)

    components: list[BomCostComponent] = []
    for line, quantity_per_run, extended in priced:
        info = walk.item_info.get(line.component_item_id)
        if info is None:  # pragma: no cover - the walk loads every component
            continue
        with localcontext() as ctx:
            ctx.prec = EXPLOSION_PRECISION
            per_unit = quantity_per_run / output
        components.append(
            BomCostComponent(
                item_id=info.id,
                item_code=info.code,
                quantity_per_unit=to_quantity(per_unit),
                unit_cost=unit_costs.get(line.component_item_id, Decimal(0)),
                extended_cost=extended,
                cost_source=sources.get(info.id, COST_SOURCE_MISSING),
            )
        )

    material = to_money(sum((extended for _, _, extended in priced), Decimal(0)))
    operation = operation_cost_per_run(operations, output)
    subcontract_operation = subcontract_operation_cost_per_run(operations, output)
    total = to_money(material + operation)
    with localcontext() as ctx:
        ctx.prec = EXPLOSION_PRECISION
        unit = total / output

    return BomCost(
        item_id=walk.root_info.id,
        item_code=walk.root_info.code,
        bom_id=structure.id,
        bom_code=structure.code,
        output_quantity=output,
        material_cost=material,
        operation_cost=operation,
        subcontract_operation_cost=subcontract_operation,
        total_cost=total,
        unit_cost=to_money(unit),
        components=tuple(sorted(components, key=lambda c: c.item_code)),
        items_without_cost=tuple(missing),
    )


__all__ = [
    "COST_SOURCE_MISSING",
    "COST_SOURCE_PURCHASE",
    "COST_SOURCE_ROLLED_UP",
    "COST_SOURCE_STANDARD",
    "EXPLOSION_PRECISION",
    "MAX_EXPLOSION_NODES",
    "MAX_SOURCE_PATHS_PER_COMPONENT",
    "MAX_WHERE_USED_LEVELS",
    "WHERE_USED_ROW_LIMIT",
    "BomCost",
    "BomCostComponent",
    "BomDataSource",
    "BomDepthExceededError",
    "BomExplosion",
    "BomTooLargeError",
    "DatabaseBomSource",
    "ExplodedComponent",
    "WhereUsedEntry",
    "apply_scrap",
    "cost_roll_up",
    "explode",
    "is_explodable",
    "line_requirement",
    "operation_cost_per_run",
    "subcontract_operation_cost_per_run",
    "where_used",
]
