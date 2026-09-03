"""HTTP surface for the customer, dealer and distributor master.

Route handlers do three things and nothing else: bind parameters, call one
service method, wrap the result in the standard envelope. Every rule, every
query and every audit write lives behind the service (section 74).

Two conventions specific to this module:

* **Dealer accounts need a second permission.** The routes gate on
  ``customer.*``; the service additionally requires ``dealer.manage`` before it
  will write a DEALER or DISTRIBUTOR account, and the ``/dealers`` list is open
  to either ``dealer.view`` or ``customer.view``. Gating that in the service
  rather than on the route is deliberate - the channel is a property of the
  *stored row*, which a route cannot see before loading it.
* **Route ordering matters.** ``/dealers`` is declared before ``/{customer_id}``:
  FastAPI matches in declaration order, so the reverse order would send
  ``GET /dealers`` into the detail route and reject it as a malformed UUID.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    require_any_permission,
    require_permission,
)
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.customers.models import AddressType, CustomerType, PriceType
from src.modules.customers.schemas import (
    AddressCreate,
    AddressRead,
    AddressUpdate,
    BlockRequest,
    ContactCreate,
    ContactRead,
    ContactUpdate,
    CreditStatus,
    CustomerCreate,
    CustomerDetail,
    CustomerFilters,
    CustomerSummary,
    CustomerUpdate,
    OutstandingAdjustment,
    StatusChangeRequest,
)
from src.modules.customers.service import CustomerService, apply_outstanding_delta

router = APIRouter(prefix="/customers", tags=["Customers"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string rather than a request body:
# a DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being removed")
]


def customer_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, name, legal name or GSTIN")
    ] = None,
    customer_type: Annotated[CustomerType | None, Query()] = None,
    price_list_type: Annotated[PriceType | None, Query()] = None,
    assigned_sales_user_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_blocked: Annotated[bool | None, Query()] = None,
    over_credit_limit: Annotated[
        bool, Query(description="Only accounts whose outstanding exceeds their limit")
    ] = False,
    state_code: Annotated[
        str | None, Query(max_length=4, description="Customers with an address in this GST state")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired customers")] = False,
) -> CustomerFilters:
    return CustomerFilters(
        search=search,
        customer_type=customer_type,
        price_list_type=price_list_type,
        assigned_sales_user_id=assigned_sales_user_id,
        is_active=is_active,
        is_blocked=is_blocked,
        over_credit_limit=over_credit_limit,
        state_code=state_code,
        include_deleted=include_deleted,
    )


CustomerFilterDep = Annotated[CustomerFilters, Depends(customer_filters)]


# -----------------------------------------------------------------------------
# Customers
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[CustomerSummary],
    summary="List customers",
    dependencies=[require_permission(Permission.CUSTOMER_VIEW)],
)
async def list_customers(
    session: DbSession,
    tenant: Tenant,
    filters: CustomerFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[CustomerSummary]:
    items, pagination = await CustomerService(session, tenant).list_customers(filters, params, sort)
    return PaginatedResponse[CustomerSummary](data=items, pagination=pagination)


@router.get(
    "/dealers",
    response_model=PaginatedResponse[CustomerSummary],
    summary="List dealer and distributor accounts",
    dependencies=[require_any_permission(Permission.DEALER_VIEW, Permission.CUSTOMER_VIEW)],
)
async def list_dealers(
    session: DbSession,
    tenant: Tenant,
    filters: CustomerFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[CustomerSummary]:
    """The dealer channel as its own resource.

    A view over the same table, restricted in SQL - not by trimming a page in
    Python, which would return the wrong rows on every page but the first. The
    shared ``customer_type`` filter still narrows it to DEALER or DISTRIBUTOR;
    any other value is refused rather than silently returning nothing.
    """
    items, pagination = await CustomerService(session, tenant).list_dealers(filters, params, sort)
    return PaginatedResponse[CustomerSummary](data=items, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[CustomerDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
    dependencies=[require_permission(Permission.CUSTOMER_CREATE)],
)
async def create_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    payload: CustomerCreate,
) -> ApiResponse[CustomerDetail]:
    created = await CustomerService(session, tenant).create_customer(principal, payload)
    return ApiResponse.ok(created, "Customer created")


@router.get(
    "/{customer_id}",
    response_model=ApiResponse[CustomerDetail],
    summary="Get one customer with its addresses and contacts",
    dependencies=[require_permission(Permission.CUSTOMER_VIEW)],
)
async def get_customer(
    session: DbSession, tenant: Tenant, customer_id: uuid.UUID
) -> ApiResponse[CustomerDetail]:
    return ApiResponse.ok(await CustomerService(session, tenant).get_customer(customer_id))


@router.patch(
    "/{customer_id}",
    response_model=ApiResponse[CustomerDetail],
    summary="Update a customer",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def update_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
) -> ApiResponse[CustomerDetail]:
    updated = await CustomerService(session, tenant).update_customer(
        principal, customer_id, payload
    )
    return ApiResponse.ok(updated, "Customer updated")


@router.post(
    "/{customer_id}/activate",
    response_model=ApiResponse[CustomerDetail],
    summary="Activate a customer",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def activate_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    payload: StatusChangeRequest,
) -> ApiResponse[CustomerDetail]:
    updated = await CustomerService(session, tenant).set_active(
        principal, customer_id, active=True, reason=payload.reason
    )
    return ApiResponse.ok(updated, "Customer activated")


@router.post(
    "/{customer_id}/deactivate",
    response_model=ApiResponse[CustomerDetail],
    summary="Deactivate a customer",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def deactivate_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    payload: StatusChangeRequest,
) -> ApiResponse[CustomerDetail]:
    updated = await CustomerService(session, tenant).set_active(
        principal, customer_id, active=False, reason=payload.reason
    )
    return ApiResponse.ok(updated, "Customer deactivated")


@router.post(
    "/{customer_id}/block",
    response_model=ApiResponse[CustomerDetail],
    summary="Put a customer on credit hold",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def block_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    payload: BlockRequest,
) -> ApiResponse[CustomerDetail]:
    updated = await CustomerService(session, tenant).block(
        principal, customer_id, reason=payload.reason
    )
    return ApiResponse.ok(updated, "Customer blocked")


@router.post(
    "/{customer_id}/unblock",
    response_model=ApiResponse[CustomerDetail],
    summary="Release a customer credit hold",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def unblock_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    payload: StatusChangeRequest,
) -> ApiResponse[CustomerDetail]:
    updated = await CustomerService(session, tenant).unblock(
        principal, customer_id, reason=payload.reason
    )
    return ApiResponse.ok(updated, "Credit hold released")


@router.get(
    "/{customer_id}/credit-status",
    response_model=ApiResponse[CreditStatus],
    summary="Credit position, optionally against a prospective order value",
    dependencies=[require_permission(Permission.CUSTOMER_VIEW)],
)
async def get_credit_status(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    order_value: Annotated[
        Decimal | None,
        Query(ge=0, description="Order value being considered; omit for the current position"),
    ] = None,
) -> ApiResponse[CreditStatus]:
    status_out = await CustomerService(session, tenant).get_credit_status(customer_id, order_value)
    return ApiResponse.ok(status_out)


@router.post(
    "/{customer_id}/outstanding",
    response_model=ApiResponse[CustomerDetail],
    summary="Apply a finance adjustment to the cached outstanding balance",
    dependencies=[require_permission(Permission.PAYMENT_RECORD)],
)
async def adjust_outstanding(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    payload: OutstandingAdjustment,
) -> ApiResponse[CustomerDetail]:
    """Finance-owned: an invoice raises the balance, a receipt reduces it.

    Gated on ``payment.record`` rather than a customer permission - the field is
    a financial figure, and the sales team that maintains the rest of the master
    record must not be able to move it.
    """
    await apply_outstanding_delta(session, tenant, customer_id, payload)
    detail = await CustomerService(session, tenant).get_customer(customer_id)
    return ApiResponse.ok(detail, "Outstanding balance updated")


@router.delete(
    "/{customer_id}",
    response_model=ApiResponse[None],
    summary="Retire a customer",
    dependencies=[require_permission(Permission.CUSTOMER_DELETE)],
)
async def retire_customer(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    customer_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await CustomerService(session, tenant).retire_customer(principal, customer_id, reason=reason)
    return ApiResponse.ok(None, "Customer retired")


@router.post(
    "/{customer_id}/restore",
    response_model=ApiResponse[CustomerDetail],
    summary="Restore a retired customer (returns inactive)",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def restore_customer(
    session: DbSession, tenant: Tenant, principal: CurrentUser, customer_id: uuid.UUID
) -> ApiResponse[CustomerDetail]:
    restored = await CustomerService(session, tenant).restore_customer(principal, customer_id)
    return ApiResponse.ok(restored, "Customer restored; activate it when its terms are confirmed")


# -----------------------------------------------------------------------------
# Addresses
# -----------------------------------------------------------------------------


@router.get(
    "/{customer_id}/addresses",
    response_model=PaginatedResponse[AddressRead],
    summary="List a customer's addresses",
    dependencies=[require_permission(Permission.CUSTOMER_VIEW)],
)
async def list_addresses(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    params: PageDep,
    address_type: Annotated[AddressType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[AddressRead]:
    items, pagination = await CustomerService(session, tenant).list_addresses(
        customer_id, params, address_type=address_type, is_active=is_active
    )
    return PaginatedResponse[AddressRead](data=items, pagination=pagination)


@router.post(
    "/{customer_id}/addresses",
    response_model=ApiResponse[AddressRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add a billing or shipping address",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def create_address(
    session: DbSession, tenant: Tenant, customer_id: uuid.UUID, payload: AddressCreate
) -> ApiResponse[AddressRead]:
    created = await CustomerService(session, tenant).create_address(customer_id, payload)
    return ApiResponse.ok(created, "Address added")


@router.patch(
    "/{customer_id}/addresses/{address_id}",
    response_model=ApiResponse[AddressRead],
    summary="Update an address",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def update_address(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    address_id: uuid.UUID,
    payload: AddressUpdate,
) -> ApiResponse[AddressRead]:
    updated = await CustomerService(session, tenant).update_address(
        customer_id, address_id, payload
    )
    return ApiResponse.ok(updated, "Address updated")


@router.post(
    "/{customer_id}/addresses/{address_id}/set-default",
    response_model=ApiResponse[AddressRead],
    summary="Make this the default address for its type",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def set_default_address(
    session: DbSession, tenant: Tenant, customer_id: uuid.UUID, address_id: uuid.UUID
) -> ApiResponse[AddressRead]:
    updated = await CustomerService(session, tenant).set_default_address(customer_id, address_id)
    return ApiResponse.ok(updated, "Default address updated")


@router.delete(
    "/{customer_id}/addresses/{address_id}",
    response_model=ApiResponse[None],
    summary="Remove an address",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def delete_address(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    address_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await CustomerService(session, tenant).delete_address(customer_id, address_id, reason=reason)
    return ApiResponse.ok(None, "Address removed")


# -----------------------------------------------------------------------------
# Contacts
# -----------------------------------------------------------------------------


@router.get(
    "/{customer_id}/contacts",
    response_model=PaginatedResponse[ContactRead],
    summary="List a customer's contacts",
    dependencies=[require_permission(Permission.CUSTOMER_VIEW)],
)
async def list_contacts(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    params: PageDep,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[ContactRead]:
    items, pagination = await CustomerService(session, tenant).list_contacts(
        customer_id, params, is_active=is_active
    )
    return PaginatedResponse[ContactRead](data=items, pagination=pagination)


@router.post(
    "/{customer_id}/contacts",
    response_model=ApiResponse[ContactRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add a contact person",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def create_contact(
    session: DbSession, tenant: Tenant, customer_id: uuid.UUID, payload: ContactCreate
) -> ApiResponse[ContactRead]:
    created = await CustomerService(session, tenant).create_contact(customer_id, payload)
    return ApiResponse.ok(created, "Contact added")


@router.patch(
    "/{customer_id}/contacts/{contact_id}",
    response_model=ApiResponse[ContactRead],
    summary="Update a contact",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def update_contact(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    contact_id: uuid.UUID,
    payload: ContactUpdate,
) -> ApiResponse[ContactRead]:
    updated = await CustomerService(session, tenant).update_contact(
        customer_id, contact_id, payload
    )
    return ApiResponse.ok(updated, "Contact updated")


@router.post(
    "/{customer_id}/contacts/{contact_id}/set-primary",
    response_model=ApiResponse[ContactRead],
    summary="Make this the primary contact",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def set_primary_contact(
    session: DbSession, tenant: Tenant, customer_id: uuid.UUID, contact_id: uuid.UUID
) -> ApiResponse[ContactRead]:
    updated = await CustomerService(session, tenant).set_primary_contact(customer_id, contact_id)
    return ApiResponse.ok(updated, "Primary contact updated")


@router.delete(
    "/{customer_id}/contacts/{contact_id}",
    response_model=ApiResponse[None],
    summary="Remove a contact",
    dependencies=[require_permission(Permission.CUSTOMER_UPDATE)],
)
async def delete_contact(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    contact_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await CustomerService(session, tenant).delete_contact(customer_id, contact_id, reason=reason)
    return ApiResponse.ok(None, "Contact removed")
