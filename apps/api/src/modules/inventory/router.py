"""HTTP surface for the inventory engine.

Handlers bind parameters, call one service method and wrap the result in the
standard envelope. Every rule, every query, every lock and every audit write
lives behind the service (section 74).

Two deliberate absences:

* **There is no endpoint that posts a raw stock movement.** Stock moves through a
  document - a receipt, an issue, a transfer, an adjustment, a count - each of
  which carries its own approval and reason controls. A free-form movement
  endpoint would be a way around all of them.
* **Nothing here is cached.** Availability in particular is read from the
  balance table on every request (section 34); a stale "yes, we have it" is
  worse than a slow one.

Route ordering: the literal segments (``/balances``, ``/ledger``, ``/transfers``)
are declared before any ``/{id}`` route. FastAPI matches in declaration order,
so the reverse would capture ``/balances`` as an id and fail on the UUID parse.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission
from src.core.pagination import (
    CursorParams,
    PageParams,
    SortParams,
    bind_query_filters,
    cursor_params,
    page_params,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.inventory.models import (
    AdjustmentReason,
    AdjustmentStatus,
    PhysicalCountStatus,
    StockState,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.schemas import (
    AdjustmentFilters,
    AvailabilityRead,
    BalanceFilters,
    BalanceRead,
    BalanceReadWithCost,
    DecisionRequest,
    ItemStockSummary,
    ItemStockSummaryWithCost,
    LedgerEntryRead,
    LedgerFilters,
    PhysicalCountCreate,
    PhysicalCountEntryRequest,
    PhysicalCountFilters,
    PhysicalCountLineRead,
    PhysicalCountRead,
    ReasonRequest,
    StockAdjustmentCreate,
    StockAdjustmentRead,
    StockBatchCreate,
    StockBatchRead,
    StockSerialCreate,
    StockSerialRead,
    StockTransferCreate,
    StockTransferRead,
    StockTransferUpdate,
    TransferFilters,
)
from src.modules.inventory.service import (
    InventoryQueryService,
    PhysicalCountService,
    StockAdjustmentService,
    StockTransferService,
    TraceabilityService,
)

router = APIRouter(prefix="/inventory", tags=["Inventory"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
CursorDep = Annotated[CursorParams, Depends(cursor_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]


# -----------------------------------------------------------------------------
# Filter dependencies
# -----------------------------------------------------------------------------


def balance_filters(
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    location_id: Annotated[uuid.UUID | None, Query()] = None,
    batch_id: Annotated[uuid.UUID | None, Query()] = None,
    below_reorder: Annotated[
        bool, Query(description="Only positions at or under the item's reorder level")
    ] = False,
    only_in_stock: Annotated[bool, Query(description="Hide positions that are at zero")] = False,
    search: Annotated[str | None, Query(max_length=120, description="Item code or name")] = None,
) -> BalanceFilters:
    return bind_query_filters(
        BalanceFilters,
        item_id=item_id,
        warehouse_id=warehouse_id,
        location_id=location_id,
        batch_id=batch_id,
        below_reorder=below_reorder,
        only_in_stock=only_in_stock,
        search=search,
    )


def ledger_filters(
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    batch_id: Annotated[uuid.UUID | None, Query()] = None,
    serial_id: Annotated[uuid.UUID | None, Query()] = None,
    transaction_type: Annotated[TransactionType | None, Query()] = None,
    stock_state: Annotated[StockState | None, Query()] = None,
    reference_type: Annotated[str | None, Query(max_length=48)] = None,
    reference_id: Annotated[uuid.UUID | None, Query()] = None,
    occurred_from: Annotated[datetime | None, Query(description="ISO 8601 lower bound")] = None,
    occurred_to: Annotated[datetime | None, Query(description="ISO 8601 upper bound")] = None,
) -> LedgerFilters:
    return bind_query_filters(
        LedgerFilters,
        item_id=item_id,
        warehouse_id=warehouse_id,
        batch_id=batch_id,
        serial_id=serial_id,
        transaction_type=transaction_type,
        stock_state=stock_state,
        reference_type=reference_type,
        reference_id=reference_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )


def transfer_filters(
    status_filter: Annotated[TransferStatus | None, Query(alias="status")] = None,
    from_warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    to_warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(description="ISO date lower bound")] = None,
    date_to: Annotated[date | None, Query(description="ISO date upper bound")] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> TransferFilters:
    return bind_query_filters(
        TransferFilters,
        status=status_filter,
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


def adjustment_filters(
    status_filter: Annotated[AdjustmentStatus | None, Query(alias="status")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    reason_code: Annotated[AdjustmentReason | None, Query()] = None,
    date_from: Annotated[date | None, Query(description="ISO date lower bound")] = None,
    date_to: Annotated[date | None, Query(description="ISO date upper bound")] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> AdjustmentFilters:
    return bind_query_filters(
        AdjustmentFilters,
        status=status_filter,
        warehouse_id=warehouse_id,
        reason_code=reason_code,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


def count_filters(
    status_filter: Annotated[PhysicalCountStatus | None, Query(alias="status")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(description="ISO date lower bound")] = None,
    date_to: Annotated[date | None, Query(description="ISO date upper bound")] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> PhysicalCountFilters:
    return bind_query_filters(
        PhysicalCountFilters,
        status=status_filter,
        warehouse_id=warehouse_id,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


BalanceFilterDep = Annotated[BalanceFilters, Depends(balance_filters)]
LedgerFilterDep = Annotated[LedgerFilters, Depends(ledger_filters)]
TransferFilterDep = Annotated[TransferFilters, Depends(transfer_filters)]
AdjustmentFilterDep = Annotated[AdjustmentFilters, Depends(adjustment_filters)]
CountFilterDep = Annotated[PhysicalCountFilters, Depends(count_filters)]


# -----------------------------------------------------------------------------
# Stock positions
# -----------------------------------------------------------------------------


@router.get(
    "/balances",
    # `response_model=None`, as the item reads do and for the same reason: a
    # fixed response model re-widens the payload to the declared union and
    # reintroduces the very field the route withheld. See the module docstring
    # of `products/router.py`.
    response_model=None,
    summary="List stock positions",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_balances(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: BalanceFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[BalanceRead] | PaginatedResponse[BalanceReadWithCost]:
    """Stock positions, with their value only for a caller entitled to it.

    ``inventory.view`` answers "how much is there"; ``item.cost_view`` answers
    "what is it worth", and they are separate rights because a store keeper
    needs the first and is deliberately not given the second. The permission
    picks the response *model*, so without it the payload has no cost field
    rather than a blanked one - the pattern ``products.service.item_response``
    established for ``standard_cost``.

    """
    items, pagination = await InventoryQueryService(session, tenant).list_balances(
        filters, params, sort
    )
    if principal.has(Permission.ITEM_COST_VIEW):
        return PaginatedResponse[BalanceReadWithCost](data=items, pagination=pagination)
    return PaginatedResponse[BalanceRead](
        data=[BalanceRead.model_validate(row, from_attributes=True) for row in items],
        pagination=pagination,
    )


@router.get(
    "/availability",
    response_model=ApiResponse[AvailabilityRead],
    summary="Available quantity of an item",
    description=(
        "Sum of available stock, read from the balance table on every call. "
        "Never cached: a stale availability answer becomes an over-promise."
    ),
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def get_availability(
    session: DbSession,
    tenant: Tenant,
    item_id: Annotated[uuid.UUID, Query()],
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ApiResponse[AvailabilityRead]:
    result = await InventoryQueryService(session, tenant).availability(item_id, warehouse_id)
    return ApiResponse[AvailabilityRead].ok(result)


@router.get(
    "/ledger",
    response_model=CursorPaginatedResponse[LedgerEntryRead],
    summary="Stock movement ledger (cursor paginated)",
    description=(
        "Append-only movement history. Cursor paginated rather than offset "
        "paginated: this table only ever grows, and OFFSET on it gets slower "
        "with every page."
    ),
    dependencies=[require_permission(Permission.INVENTORY_LEDGER_VIEW)],
)
async def list_ledger(
    session: DbSession,
    tenant: Tenant,
    filters: LedgerFilterDep,
    params: CursorDep,
) -> CursorPaginatedResponse[LedgerEntryRead]:
    items, pagination = await InventoryQueryService(session, tenant).list_ledger(filters, params)
    return CursorPaginatedResponse[LedgerEntryRead](data=items, pagination=pagination)


@router.get(
    "/items/{item_id}/summary",
    response_model=None,
    summary="Stock of one item across warehouses",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def item_stock_summary(
    item_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    params: PageDep,
) -> PaginatedResponse[ItemStockSummary] | PaginatedResponse[ItemStockSummaryWithCost]:
    """The per-warehouse breakdown is paginated; the totals cover the whole item.

    Returned as a single-element page so the pagination block describes the
    ``warehouses`` list the client is actually paging through.
    """
    summary, pagination = await InventoryQueryService(session, tenant).item_summary(item_id, params)
    # A whole-item valuation is precisely what `inventory.valuation_view` gates,
    # so that is the right consulted here rather than the per-item cost right.
    if principal.has(Permission.INVENTORY_VALUATION_VIEW):
        return PaginatedResponse[ItemStockSummaryWithCost](data=[summary], pagination=pagination)
    return PaginatedResponse[ItemStockSummary](
        data=[ItemStockSummary.model_validate(summary, from_attributes=True)],
        pagination=pagination,
    )


# -----------------------------------------------------------------------------
# Batches and serials
# -----------------------------------------------------------------------------


@router.get(
    "/batches",
    response_model=PaginatedResponse[StockBatchRead],
    summary="List stock batches",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_batches(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> PaginatedResponse[StockBatchRead]:
    items, pagination = await TraceabilityService(session, tenant).list_batches(
        item_id, search, params
    )
    return PaginatedResponse[StockBatchRead](data=items, pagination=pagination)


@router.post(
    "/batches",
    response_model=ApiResponse[StockBatchRead],
    status_code=status.HTTP_201_CREATED,
    summary="Record a stock batch",
    description=(
        "A batch-tracked item cannot receive stock until the lot exists: the "
        "engine refuses any movement of such an item without a batch."
    ),
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def create_batch(
    payload: StockBatchCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[StockBatchRead]:
    created = await TraceabilityService(session, tenant).create_batch(payload)
    return ApiResponse[StockBatchRead].ok(created, "Batch recorded")


@router.get(
    "/serials",
    response_model=PaginatedResponse[StockSerialRead],
    summary="List serial numbers",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_serials(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> PaginatedResponse[StockSerialRead]:
    items, pagination = await TraceabilityService(session, tenant).list_serials(
        item_id, warehouse_id, search, params
    )
    return PaginatedResponse[StockSerialRead](data=items, pagination=pagination)


@router.post(
    "/serials",
    response_model=ApiResponse[StockSerialRead],
    status_code=status.HTTP_201_CREATED,
    summary="Record a serial number",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def create_serial(
    payload: StockSerialCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[StockSerialRead]:
    created = await TraceabilityService(session, tenant).create_serial(payload)
    return ApiResponse[StockSerialRead].ok(created, "Serial recorded")


# -----------------------------------------------------------------------------
# Stock transfers
# -----------------------------------------------------------------------------


@router.get(
    "/transfers",
    response_model=PaginatedResponse[StockTransferRead],
    summary="List stock transfers",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_transfers(
    session: DbSession,
    tenant: Tenant,
    filters: TransferFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[StockTransferRead]:
    items, pagination = await StockTransferService(session, tenant).list_transfers(
        filters, params, sort
    )
    return PaginatedResponse[StockTransferRead](data=items, pagination=pagination)


@router.post(
    "/transfers",
    response_model=ApiResponse[StockTransferRead],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a stock transfer",
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def create_transfer(
    payload: StockTransferCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[StockTransferRead]:
    created = await StockTransferService(session, tenant).create(payload)
    return ApiResponse[StockTransferRead].ok(created, "Stock transfer created")


@router.get(
    "/transfers/{transfer_id}",
    response_model=ApiResponse[StockTransferRead],
    summary="Fetch one stock transfer with its lines",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def get_transfer(
    transfer_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockTransferRead]:
    return ApiResponse[StockTransferRead].ok(
        await StockTransferService(session, tenant).get_transfer(transfer_id)
    )


@router.patch(
    "/transfers/{transfer_id}",
    response_model=ApiResponse[StockTransferRead],
    summary="Edit a draft stock transfer",
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def update_transfer(
    transfer_id: uuid.UUID,
    payload: StockTransferUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StockTransferRead]:
    updated = await StockTransferService(session, tenant).update(transfer_id, payload)
    return ApiResponse[StockTransferRead].ok(updated, "Stock transfer updated")


@router.post(
    "/transfers/{transfer_id}/approve",
    response_model=ApiResponse[StockTransferRead],
    summary="Approve a stock transfer",
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def approve_transfer(
    transfer_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockTransferRead]:
    updated = await StockTransferService(session, tenant).approve(transfer_id)
    return ApiResponse[StockTransferRead].ok(updated, "Stock transfer approved")


@router.post(
    "/transfers/{transfer_id}/dispatch",
    response_model=ApiResponse[StockTransferRead],
    summary="Dispatch a stock transfer",
    description=(
        "Posts TRANSFER_OUT at the source and puts the quantity in transit at "
        "the destination. The stock is company property throughout and appears "
        "in the destination's in-transit figure until it is received."
    ),
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def dispatch_transfer(
    transfer_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockTransferRead]:
    updated = await StockTransferService(session, tenant).dispatch(transfer_id)
    return ApiResponse[StockTransferRead].ok(updated, "Stock transfer dispatched")


@router.post(
    "/transfers/{transfer_id}/receive",
    response_model=ApiResponse[StockTransferRead],
    summary="Receive a stock transfer",
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def receive_transfer(
    transfer_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockTransferRead]:
    updated = await StockTransferService(session, tenant).receive(transfer_id)
    return ApiResponse[StockTransferRead].ok(updated, "Stock transfer received")


@router.post(
    "/transfers/{transfer_id}/cancel",
    response_model=ApiResponse[StockTransferRead],
    summary="Cancel a stock transfer",
    dependencies=[require_permission(Permission.INVENTORY_TRANSFER)],
)
async def cancel_transfer(
    transfer_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StockTransferRead]:
    updated = await StockTransferService(session, tenant).cancel(transfer_id, payload.reason)
    return ApiResponse[StockTransferRead].ok(updated, "Stock transfer cancelled")


# -----------------------------------------------------------------------------
# Stock adjustments
# -----------------------------------------------------------------------------


@router.get(
    "/adjustments",
    response_model=PaginatedResponse[StockAdjustmentRead],
    summary="List stock adjustments",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_adjustments(
    session: DbSession,
    tenant: Tenant,
    filters: AdjustmentFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[StockAdjustmentRead]:
    items, pagination = await StockAdjustmentService(session, tenant).list_adjustments(
        filters, params, sort
    )
    return PaginatedResponse[StockAdjustmentRead](data=items, pagination=pagination)


@router.post(
    "/adjustments",
    response_model=ApiResponse[StockAdjustmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Draft a stock adjustment",
    description=(
        "Lines are valued at what the position's FIFO cost layers say the stock "
        "cost. If the total exceeds the approval threshold - a fixed value, "
        "ADJUSTMENT_APPROVAL_VALUE_THRESHOLD in the inventory service, echoed in "
        "the error's details when posting is refused - the document must be "
        "approved by a second person before it can post."
    ),
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def create_adjustment(
    payload: StockAdjustmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[StockAdjustmentRead]:
    created = await StockAdjustmentService(session, tenant).create(payload)
    return ApiResponse[StockAdjustmentRead].ok(created, "Stock adjustment drafted")


@router.get(
    "/adjustments/{adjustment_id}",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Fetch one stock adjustment with its lines",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def get_adjustment(
    adjustment_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockAdjustmentRead]:
    return ApiResponse[StockAdjustmentRead].ok(
        await StockAdjustmentService(session, tenant).get_adjustment(adjustment_id)
    )


@router.post(
    "/adjustments/{adjustment_id}/submit",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Submit an adjustment for approval",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def submit_adjustment(
    adjustment_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockAdjustmentRead]:
    updated = await StockAdjustmentService(session, tenant).submit(adjustment_id)
    return ApiResponse[StockAdjustmentRead].ok(updated, "Submitted for approval")


@router.post(
    "/adjustments/{adjustment_id}/approve",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Approve a stock adjustment",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST_APPROVE)],
)
async def approve_adjustment(
    adjustment_id: uuid.UUID,
    payload: DecisionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StockAdjustmentRead]:
    updated = await StockAdjustmentService(session, tenant).approve(adjustment_id, payload.note)
    return ApiResponse[StockAdjustmentRead].ok(updated, "Stock adjustment approved")


@router.post(
    "/adjustments/{adjustment_id}/reject",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Reject a stock adjustment",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST_APPROVE)],
)
async def reject_adjustment(
    adjustment_id: uuid.UUID,
    payload: DecisionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StockAdjustmentRead]:
    updated = await StockAdjustmentService(session, tenant).reject(adjustment_id, payload.note)
    return ApiResponse[StockAdjustmentRead].ok(updated, "Stock adjustment rejected")


@router.post(
    "/adjustments/{adjustment_id}/post",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Post a stock adjustment to the ledger",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def post_adjustment(
    adjustment_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StockAdjustmentRead]:
    updated = await StockAdjustmentService(session, tenant).post(adjustment_id)
    return ApiResponse[StockAdjustmentRead].ok(updated, "Stock adjustment posted")


@router.post(
    "/adjustments/{adjustment_id}/cancel",
    response_model=ApiResponse[StockAdjustmentRead],
    summary="Cancel a stock adjustment",
    dependencies=[require_permission(Permission.INVENTORY_ADJUST)],
)
async def cancel_adjustment(
    adjustment_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StockAdjustmentRead]:
    updated = await StockAdjustmentService(session, tenant).cancel(adjustment_id, payload.reason)
    return ApiResponse[StockAdjustmentRead].ok(updated, "Stock adjustment cancelled")


# -----------------------------------------------------------------------------
# Physical counts
# -----------------------------------------------------------------------------


@router.get(
    "/physical-counts",
    response_model=PaginatedResponse[PhysicalCountRead],
    summary="List stock count sheets",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_counts(
    session: DbSession,
    tenant: Tenant,
    filters: CountFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[PhysicalCountRead]:
    items, pagination = await PhysicalCountService(session, tenant).list_counts(
        filters, params, sort
    )
    return PaginatedResponse[PhysicalCountRead](data=items, pagination=pagination)


@router.post(
    "/physical-counts",
    response_model=ApiResponse[PhysicalCountRead],
    status_code=status.HTTP_201_CREATED,
    summary="Open a stock count sheet",
    description=(
        "Snapshots the system quantity of every position in scope. The snapshot "
        "is what the counter compares against; it is not what the variance is "
        "posted from - see the confirm endpoint."
    ),
    dependencies=[require_permission(Permission.INVENTORY_PHYSICAL_COUNT)],
)
async def create_count(
    payload: PhysicalCountCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[PhysicalCountRead]:
    created = await PhysicalCountService(session, tenant).create(payload)
    return ApiResponse[PhysicalCountRead].ok(created, "Stock count opened")


@router.get(
    "/physical-counts/{count_id}",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Fetch one stock count sheet",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def get_count(
    count_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PhysicalCountRead]:
    return ApiResponse[PhysicalCountRead].ok(
        await PhysicalCountService(session, tenant).get_count(count_id)
    )


@router.get(
    "/physical-counts/{count_id}/lines",
    response_model=PaginatedResponse[PhysicalCountLineRead],
    summary="List the lines of a count sheet",
    dependencies=[require_permission(Permission.INVENTORY_VIEW)],
)
async def list_count_lines(
    count_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
) -> PaginatedResponse[PhysicalCountLineRead]:
    items, pagination = await PhysicalCountService(session, tenant).list_lines(count_id, params)
    return PaginatedResponse[PhysicalCountLineRead](data=items, pagination=pagination)


@router.post(
    "/physical-counts/{count_id}/start",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Start counting",
    dependencies=[require_permission(Permission.INVENTORY_PHYSICAL_COUNT)],
)
async def start_count(
    count_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PhysicalCountRead]:
    updated = await PhysicalCountService(session, tenant).start(count_id)
    return ApiResponse[PhysicalCountRead].ok(updated, "Counting started")


@router.post(
    "/physical-counts/{count_id}/entries",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Record counted quantities",
    dependencies=[require_permission(Permission.INVENTORY_PHYSICAL_COUNT)],
)
async def enter_counts(
    count_id: uuid.UUID,
    payload: PhysicalCountEntryRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[PhysicalCountRead]:
    updated = await PhysicalCountService(session, tenant).enter_counts(count_id, payload)
    return ApiResponse[PhysicalCountRead].ok(updated, "Counts recorded")


@router.post(
    "/physical-counts/{count_id}/recount",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Reopen a finished sheet for a recount",
    dependencies=[require_permission(Permission.INVENTORY_PHYSICAL_COUNT)],
)
async def recount(
    count_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[PhysicalCountRead]:
    updated = await PhysicalCountService(session, tenant).recount(count_id, payload.reason)
    return ApiResponse[PhysicalCountRead].ok(updated, "Sheet reopened for recount")


@router.post(
    "/physical-counts/{count_id}/confirm",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Confirm a count and post the variance",
    description=(
        "Re-reads current stock under a row lock and posts the difference "
        "between the counted figure and that value - not the difference from the "
        "snapshot taken when the sheet was opened, which would silently reverse "
        "every movement made while the count was in progress."
    ),
    dependencies=[require_permission(Permission.INVENTORY_RECONCILE)],
)
async def confirm_count(
    count_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PhysicalCountRead]:
    updated = await PhysicalCountService(session, tenant).confirm(count_id)
    return ApiResponse[PhysicalCountRead].ok(updated, "Stock count confirmed and variance posted")


@router.post(
    "/physical-counts/{count_id}/cancel",
    response_model=ApiResponse[PhysicalCountRead],
    summary="Cancel a stock count",
    dependencies=[require_permission(Permission.INVENTORY_PHYSICAL_COUNT)],
)
async def cancel_count(
    count_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[PhysicalCountRead]:
    updated = await PhysicalCountService(session, tenant).cancel(count_id, payload.reason)
    return ApiResponse[PhysicalCountRead].ok(updated, "Stock count cancelled")


__all__ = ["router"]
