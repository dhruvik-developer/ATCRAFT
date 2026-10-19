from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.core.deps import (
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.pagination import PageParams, page_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.sales_returns import service
from src.modules.sales_returns.models import SalesReturnStatus
from src.modules.sales_returns.schemas import (
    SalesReturnCancel,
    SalesReturnCreate,
    SalesReturnDetail,
    SalesReturnSummary,
    SalesReturnUnits,
)

router = APIRouter(
    prefix="/sales-returns",
    tags=["Sales Returns"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[Depends(forbid_vendor_users), Depends(forbid_dealer_users)],
)
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get(
    "",
    response_model=PaginatedResponse[SalesReturnSummary],
    dependencies=[require_permission(Permission.SALES_RETURN_VIEW)],
)
async def list_sales_returns(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[SalesReturnSummary]:
    rows, page = await service.list_returns(session, tenant, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "",
    response_model=ApiResponse[SalesReturnDetail],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.SALES_RETURN_CREATE)],
)
async def create_sales_return(
    payload: SalesReturnCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(
        await service.create_return(session, tenant, payload), "Sales return created"
    )


@router.get(
    "/{return_id}",
    response_model=ApiResponse[SalesReturnDetail],
    dependencies=[require_permission(Permission.SALES_RETURN_VIEW)],
)
async def get_sales_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(await service.get_return(session, tenant, return_id))


@router.post(
    "/{return_id}/receive",
    response_model=ApiResponse[SalesReturnDetail],
    dependencies=[require_permission(Permission.SALES_RETURN_CREATE)],
)
async def receive_sales_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(
        await service.transition_return(session, tenant, return_id, SalesReturnStatus.RECEIVED),
        "Sales return received",
    )


@router.post(
    "/{return_id}/units",
    response_model=ApiResponse[SalesReturnDetail],
    dependencies=[require_permission(Permission.SALES_RETURN_CREATE)],
)
async def dispose_sales_return_units(
    return_id: uuid.UUID,
    payload: SalesReturnUnits,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(
        await service.dispose_return_units(session, tenant, return_id, payload),
        "Returned serial units dispositioned",
    )


@router.post(
    "/{return_id}/close",
    response_model=ApiResponse[SalesReturnDetail],
    dependencies=[require_permission(Permission.SALES_RETURN_CREATE)],
)
async def close_sales_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(
        await service.transition_return(session, tenant, return_id, SalesReturnStatus.CLOSED),
        "Sales return closed",
    )


@router.post(
    "/{return_id}/cancel",
    response_model=ApiResponse[SalesReturnDetail],
    dependencies=[require_permission(Permission.SALES_RETURN_CREATE)],
)
async def cancel_sales_return(
    return_id: uuid.UUID,
    payload: SalesReturnCancel,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[SalesReturnDetail]:
    return ApiResponse[SalesReturnDetail].ok(
        await service.transition_return(
            session,
            tenant,
            return_id,
            SalesReturnStatus.CANCELLED,
            reason=payload.reason,
        ),
        "Sales return cancelled",
    )
