"""HTTP surface for vendor and transporter master data.

Route handlers do three things and nothing else: bind parameters, call one
service method, wrap the result in the standard envelope. Every rule, every
query and every audit write lives behind the service (section 74).

Two structural notes:

* The module exposes **two top-level resources**, ``/vendors`` and
  ``/transporters``. They are separate sub-routers with their own prefixes and
  tags, mounted on the module's ``router``, so a transporter does not end up
  addressed as a kind of vendor in the URL space or in the OpenAPI groups.
* Literal segments (``/contacts``, ``/categories``, ``/options``) are declared
  **before** ``/{vendor_id}``. FastAPI matches in declaration order, so with the
  reverse order a request for ``/contacts/{id}`` would be captured by the vendor
  route and rejected as a malformed UUID.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.vendors.models import (
    DEFAULT_RATING_WINDOW_DAYS,
    MAX_RATING_WINDOW_DAYS,
    MIN_RATING_WINDOW_DAYS,
    TransporterType,
    VendorContactRole,
    VendorType,
)
from src.modules.vendors.schemas import (
    ApprovalRequest,
    StatusChangeRequest,
    TrackingLinkRead,
    TransporterCreate,
    TransporterFilters,
    TransporterOption,
    TransporterRead,
    TransporterUpdate,
    VendorCategoryAssign,
    VendorCategoryFilters,
    VendorCategoryRead,
    VendorCategoryUpdate,
    VendorContactCreate,
    VendorContactFilters,
    VendorContactRead,
    VendorContactUpdate,
    VendorCreate,
    VendorFilters,
    VendorOption,
    VendorRatingRead,
    VendorRatingRequest,
    VendorRead,
    VendorUpdate,
)
from src.modules.vendors.service import (
    TransporterService,
    VendorCategoryService,
    VendorContactService,
    VendorService,
)

# The module router is a bare container: the two resources below carry their own
# prefixes and tags. src/api/router.py mounts this and adds /api/v1.
router = APIRouter()

vendors_router = APIRouter(prefix="/vendors", tags=["Vendors"], responses=COMMON_ERROR_RESPONSES)
transporters_router = APIRouter(
    prefix="/transporters", tags=["Transporters"], responses=COMMON_ERROR_RESPONSES
)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string rather than a request body:
# a DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being retired")
]


def vendor_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, name, legal name, GSTIN or city")
    ] = None,
    vendor_type: Annotated[
        VendorType | None, Query(description="Vendors holding this type among their roles")
    ] = None,
    item_category_id: Annotated[
        uuid.UUID | None, Query(description="Vendors qualified for this item category")
    ] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_approved: Annotated[bool | None, Query()] = None,
    state_code: Annotated[str | None, Query(max_length=4, description="GST state code")] = None,
    min_rating: Annotated[
        float | None, Query(ge=0, le=5, description="Excludes unrated vendors")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired vendors")] = False,
) -> VendorFilters:
    return VendorFilters(
        search=search,
        vendor_type=vendor_type,
        item_category_id=item_category_id,
        is_active=is_active,
        is_approved=is_approved,
        state_code=state_code,
        # Pydantic converts to Decimal on the model; the query parameter is a
        # float only because that is what a query string can express.
        min_rating=min_rating,
        include_deleted=include_deleted,
    )


def contact_filters(
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    contact_role: Annotated[VendorContactRole | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> VendorContactFilters:
    return VendorContactFilters(
        vendor_id=vendor_id,
        contact_role=contact_role,
        is_active=is_active,
        search=search,
        include_deleted=include_deleted,
    )


def nested_contact_filters(
    contact_role: Annotated[VendorContactRole | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> VendorContactFilters:
    """Filters for the vendor-nested contact list.

    Deliberately has no ``vendor_id``: on ``/{vendor_id}/contacts`` that name is
    a path parameter, and offering it as a query parameter too would both
    confuse the OpenAPI document and invite a caller to try to widen the scope
    the URL already fixed.
    """
    return contact_filters(
        vendor_id=None,
        contact_role=contact_role,
        is_active=is_active,
        search=search,
        include_deleted=include_deleted,
    )


def category_filters(
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    item_category_id: Annotated[uuid.UUID | None, Query()] = None,
    is_preferred: Annotated[bool | None, Query()] = None,
) -> VendorCategoryFilters:
    return VendorCategoryFilters(
        vendor_id=vendor_id,
        item_category_id=item_category_id,
        is_preferred=is_preferred,
    )


def transporter_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    transporter_type: Annotated[TransporterType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query()] = False,
) -> TransporterFilters:
    return TransporterFilters(
        search=search,
        transporter_type=transporter_type,
        is_active=is_active,
        include_deleted=include_deleted,
    )


VendorFilterDep = Annotated[VendorFilters, Depends(vendor_filters)]
ContactFilterDep = Annotated[VendorContactFilters, Depends(contact_filters)]
NestedContactFilterDep = Annotated[VendorContactFilters, Depends(nested_contact_filters)]
CategoryFilterDep = Annotated[VendorCategoryFilters, Depends(category_filters)]
TransporterFilterDep = Annotated[TransporterFilters, Depends(transporter_filters)]


# -----------------------------------------------------------------------------
# Vendor contacts (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@vendors_router.get(
    "/contacts",
    response_model=PaginatedResponse[VendorContactRead],
    summary="List vendor contacts across vendors",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_contacts(
    session: DbSession,
    tenant: Tenant,
    filters: ContactFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[VendorContactRead]:
    items, pagination = await VendorContactService(session, tenant).list_contacts(
        filters, params, sort
    )
    return PaginatedResponse[VendorContactRead](data=items, pagination=pagination)


@vendors_router.get(
    "/contacts/{contact_id}",
    response_model=ApiResponse[VendorContactRead],
    summary="Fetch one vendor contact",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def get_contact(
    contact_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorContactRead]:
    contact = await VendorContactService(session, tenant).get_contact(contact_id)
    return ApiResponse[VendorContactRead].ok(contact)


@vendors_router.patch(
    "/contacts/{contact_id}",
    response_model=ApiResponse[VendorContactRead],
    summary="Update a vendor contact",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def update_contact(
    contact_id: uuid.UUID,
    payload: VendorContactUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorContactRead]:
    contact = await VendorContactService(session, tenant).update_contact(contact_id, payload)
    return ApiResponse[VendorContactRead].ok(contact, "Contact updated")


@vendors_router.post(
    "/contacts/{contact_id}/set-primary",
    response_model=ApiResponse[VendorContactRead],
    summary="Make this the vendor's primary contact",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def set_primary_contact(
    contact_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorContactRead]:
    contact = await VendorContactService(session, tenant).set_primary(contact_id)
    return ApiResponse[VendorContactRead].ok(contact, "Primary contact updated")


@vendors_router.delete(
    "/contacts/{contact_id}",
    response_model=ApiResponse[None],
    summary="Remove a vendor contact",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def delete_contact(
    contact_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await VendorContactService(session, tenant).delete_contact(contact_id, reason=reason)
    return ApiResponse[None].ok(None, "Contact removed")


# -----------------------------------------------------------------------------
# Vendor category qualifications
# -----------------------------------------------------------------------------


@vendors_router.get(
    "/categories",
    response_model=PaginatedResponse[VendorCategoryRead],
    summary="List vendor category qualifications",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_category_links(
    session: DbSession,
    tenant: Tenant,
    filters: CategoryFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[VendorCategoryRead]:
    items, pagination = await VendorCategoryService(session, tenant).list_categories(
        filters, params, sort
    )
    return PaginatedResponse[VendorCategoryRead](data=items, pagination=pagination)


@vendors_router.patch(
    "/categories/{link_id}",
    response_model=ApiResponse[VendorCategoryRead],
    summary="Update a vendor category qualification",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def update_category_link(
    link_id: uuid.UUID,
    payload: VendorCategoryUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorCategoryRead]:
    link = await VendorCategoryService(session, tenant).update(link_id, payload)
    return ApiResponse[VendorCategoryRead].ok(link, "Qualification updated")


@vendors_router.delete(
    "/categories/{link_id}",
    response_model=ApiResponse[None],
    summary="Revoke a vendor category qualification",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def delete_category_link(
    link_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await VendorCategoryService(session, tenant).remove(link_id, reason=reason)
    return ApiResponse[None].ok(None, "Qualification revoked")


# -----------------------------------------------------------------------------
# Vendors
# -----------------------------------------------------------------------------


@vendors_router.get(
    "",
    response_model=PaginatedResponse[VendorRead],
    summary="List vendors",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_vendors(
    session: DbSession,
    tenant: Tenant,
    filters: VendorFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[VendorRead]:
    items, pagination = await VendorService(session, tenant).list_vendors(filters, params, sort)
    return PaginatedResponse[VendorRead](data=items, pagination=pagination)


@vendors_router.get(
    "/options",
    response_model=ApiResponse[list[VendorOption]],
    summary="Active vendors for a picker",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_vendor_options(
    session: DbSession,
    tenant: Tenant,
    vendor_type: Annotated[VendorType | None, Query()] = None,
    approved_only: Annotated[
        bool,
        Query(description="Only vendors a purchase order may legally be raised on"),
    ] = True,
) -> ApiResponse[list[VendorOption]]:
    options = await VendorService(session, tenant).list_options(
        vendor_type, approved_only=approved_only
    )
    return ApiResponse[list[VendorOption]].ok(options)


@vendors_router.post(
    "",
    response_model=ApiResponse[VendorRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a vendor",
    dependencies=[require_permission(Permission.VENDOR_CREATE)],
)
async def create_vendor(
    payload: VendorCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).create_vendor(payload)
    return ApiResponse[VendorRead].ok(vendor, "Vendor created, pending approval")


@vendors_router.get(
    "/{vendor_id}",
    response_model=ApiResponse[VendorRead],
    summary="Fetch one vendor",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def get_vendor(
    vendor_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).get_vendor(vendor_id)
    return ApiResponse[VendorRead].ok(vendor)


@vendors_router.patch(
    "/{vendor_id}",
    response_model=ApiResponse[VendorRead],
    summary="Update a vendor",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).update_vendor(vendor_id, payload)
    return ApiResponse[VendorRead].ok(vendor, "Vendor updated")


@vendors_router.post(
    "/{vendor_id}/approve",
    response_model=ApiResponse[VendorRead],
    summary="Approve a vendor so purchase orders may be raised on it",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def approve_vendor(
    vendor_id: uuid.UUID,
    payload: ApprovalRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).approve_vendor(vendor_id, reason=payload.reason)
    return ApiResponse[VendorRead].ok(vendor, "Vendor approved")


@vendors_router.post(
    "/{vendor_id}/withdraw-approval",
    response_model=ApiResponse[VendorRead],
    summary="Withdraw a vendor's approval",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def withdraw_vendor_approval(
    vendor_id: uuid.UUID,
    payload: ApprovalRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).withdraw_approval(
        vendor_id, reason=payload.reason
    )
    return ApiResponse[VendorRead].ok(vendor, "Vendor approval withdrawn")


@vendors_router.post(
    "/{vendor_id}/activate",
    response_model=ApiResponse[VendorRead],
    summary="Reactivate a suspended vendor",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def activate_vendor(
    vendor_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).set_active(
        vendor_id, active=True, reason=payload.reason
    )
    return ApiResponse[VendorRead].ok(vendor, "Vendor reactivated")


@vendors_router.post(
    "/{vendor_id}/deactivate",
    response_model=ApiResponse[VendorRead],
    summary="Suspend a vendor",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def deactivate_vendor(
    vendor_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).set_active(
        vendor_id, active=False, reason=payload.reason
    )
    return ApiResponse[VendorRead].ok(vendor, "Vendor suspended")


@vendors_router.post(
    "/{vendor_id}/restore",
    response_model=ApiResponse[VendorRead],
    summary="Restore a retired vendor (returns it suspended and unapproved)",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def restore_vendor(
    vendor_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorRead]:
    vendor = await VendorService(session, tenant).restore_vendor(vendor_id)
    return ApiResponse[VendorRead].ok(vendor, "Vendor restored, suspended and unapproved")


@vendors_router.delete(
    "/{vendor_id}",
    response_model=ApiResponse[None],
    summary="Retire a vendor",
    dependencies=[require_permission(Permission.VENDOR_DELETE)],
)
async def delete_vendor(
    vendor_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await VendorService(session, tenant).delete_vendor(vendor_id, reason=reason)
    return ApiResponse[None].ok(None, "Vendor retired")


# --- rating ------------------------------------------------------------------


@vendors_router.get(
    "/{vendor_id}/rating",
    response_model=ApiResponse[VendorRatingRead],
    summary="Preview a vendor's rating without storing it",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def preview_vendor_rating(
    vendor_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    window_days: Annotated[
        int,
        Query(
            ge=MIN_RATING_WINDOW_DAYS,
            le=MAX_RATING_WINDOW_DAYS,
            description="Measurement window in days",
        ),
    ] = DEFAULT_RATING_WINDOW_DAYS,
) -> ApiResponse[VendorRatingRead]:
    rating = await VendorService(session, tenant).preview_rating(vendor_id, window_days=window_days)
    return ApiResponse[VendorRatingRead].ok(rating)


@vendors_router.post(
    "/{vendor_id}/rating",
    response_model=ApiResponse[VendorRatingRead],
    summary="Recompute and store a vendor's rating",
    dependencies=[require_permission(Permission.VENDOR_RATE)],
)
async def recompute_vendor_rating(
    vendor_id: uuid.UUID,
    payload: VendorRatingRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorRatingRead]:
    rating = await VendorService(session, tenant).recompute_rating(
        vendor_id, window_days=payload.window_days
    )
    message = "Vendor rating updated" if rating.applied else "Not enough data to rate this vendor"
    return ApiResponse[VendorRatingRead].ok(rating, message)


# --- contacts and categories nested under a vendor ---------------------------


@vendors_router.get(
    "/{vendor_id}/contacts",
    response_model=PaginatedResponse[VendorContactRead],
    summary="List the contacts of one vendor",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_vendor_contacts(
    vendor_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    filters: NestedContactFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[VendorContactRead]:
    # The path wins over any query parameter of the same name: the URL is the
    # authoritative scope.
    scoped = filters.model_copy(update={"vendor_id": vendor_id})
    items, pagination = await VendorContactService(session, tenant).list_contacts(
        scoped, params, sort
    )
    return PaginatedResponse[VendorContactRead](data=items, pagination=pagination)


@vendors_router.post(
    "/{vendor_id}/contacts",
    response_model=ApiResponse[VendorContactRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add a contact to a vendor",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def create_vendor_contact(
    vendor_id: uuid.UUID,
    payload: VendorContactCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorContactRead]:
    contact = await VendorContactService(session, tenant).create_contact(vendor_id, payload)
    return ApiResponse[VendorContactRead].ok(contact, "Contact added")


@vendors_router.get(
    "/{vendor_id}/categories",
    response_model=PaginatedResponse[VendorCategoryRead],
    summary="List the item categories a vendor is qualified for",
    dependencies=[require_permission(Permission.VENDOR_VIEW)],
)
async def list_vendor_categories(
    vendor_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    is_preferred: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[VendorCategoryRead]:
    filters = VendorCategoryFilters(vendor_id=vendor_id, is_preferred=is_preferred)
    items, pagination = await VendorCategoryService(session, tenant).list_categories(
        filters, params, sort
    )
    return PaginatedResponse[VendorCategoryRead](data=items, pagination=pagination)


@vendors_router.post(
    "/{vendor_id}/categories",
    response_model=ApiResponse[VendorCategoryRead],
    status_code=status.HTTP_201_CREATED,
    summary="Qualify a vendor for an item category",
    dependencies=[require_permission(Permission.VENDOR_UPDATE)],
)
async def assign_vendor_category(
    vendor_id: uuid.UUID,
    payload: VendorCategoryAssign,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[VendorCategoryRead]:
    link = await VendorCategoryService(session, tenant).assign(vendor_id, payload)
    return ApiResponse[VendorCategoryRead].ok(link, "Vendor qualified for category")


# -----------------------------------------------------------------------------
# Transporters
# -----------------------------------------------------------------------------


@transporters_router.get(
    "",
    response_model=PaginatedResponse[TransporterRead],
    summary="List transporters",
    dependencies=[require_permission(Permission.TRANSPORTER_VIEW)],
)
async def list_transporters(
    session: DbSession,
    tenant: Tenant,
    filters: TransporterFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[TransporterRead]:
    items, pagination = await TransporterService(session, tenant).list_transporters(
        filters, params, sort
    )
    return PaginatedResponse[TransporterRead](data=items, pagination=pagination)


@transporters_router.get(
    "/options",
    response_model=ApiResponse[list[TransporterOption]],
    summary="Active transporters for a picker",
    dependencies=[require_permission(Permission.TRANSPORTER_VIEW)],
)
async def list_transporter_options(
    session: DbSession,
    tenant: Tenant,
    transporter_type: Annotated[TransporterType | None, Query()] = None,
) -> ApiResponse[list[TransporterOption]]:
    options = await TransporterService(session, tenant).list_options(transporter_type)
    return ApiResponse[list[TransporterOption]].ok(options)


@transporters_router.post(
    "",
    response_model=ApiResponse[TransporterRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def create_transporter(
    payload: TransporterCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).create_transporter(payload)
    return ApiResponse[TransporterRead].ok(transporter, "Transporter created")


@transporters_router.get(
    "/{transporter_id}",
    response_model=ApiResponse[TransporterRead],
    summary="Fetch one transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_VIEW)],
)
async def get_transporter(
    transporter_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).get_transporter(transporter_id)
    return ApiResponse[TransporterRead].ok(transporter)


@transporters_router.get(
    "/{transporter_id}/tracking-url",
    response_model=ApiResponse[TrackingLinkRead],
    summary="Resolve a consignment number into a tracking link",
    dependencies=[require_permission(Permission.TRANSPORTER_VIEW)],
)
async def get_tracking_url(
    transporter_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    tracking_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ApiResponse[TrackingLinkRead]:
    link = await TransporterService(session, tenant).tracking_link(transporter_id, tracking_id)
    return ApiResponse[TrackingLinkRead].ok(link)


@transporters_router.patch(
    "/{transporter_id}",
    response_model=ApiResponse[TransporterRead],
    summary="Update a transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def update_transporter(
    transporter_id: uuid.UUID,
    payload: TransporterUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).update_transporter(
        transporter_id, payload
    )
    return ApiResponse[TransporterRead].ok(transporter, "Transporter updated")


@transporters_router.post(
    "/{transporter_id}/activate",
    response_model=ApiResponse[TransporterRead],
    summary="Activate a transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def activate_transporter(
    transporter_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).set_active(
        transporter_id, active=True, reason=payload.reason
    )
    return ApiResponse[TransporterRead].ok(transporter, "Transporter activated")


@transporters_router.post(
    "/{transporter_id}/deactivate",
    response_model=ApiResponse[TransporterRead],
    summary="Deactivate a transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def deactivate_transporter(
    transporter_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).set_active(
        transporter_id, active=False, reason=payload.reason
    )
    return ApiResponse[TransporterRead].ok(transporter, "Transporter deactivated")


@transporters_router.post(
    "/{transporter_id}/restore",
    response_model=ApiResponse[TransporterRead],
    summary="Restore a retired transporter (returns it inactive)",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def restore_transporter(
    transporter_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[TransporterRead]:
    transporter = await TransporterService(session, tenant).restore_transporter(transporter_id)
    return ApiResponse[TransporterRead].ok(transporter, "Transporter restored, inactive")


@transporters_router.delete(
    "/{transporter_id}",
    response_model=ApiResponse[None],
    summary="Retire a transporter",
    dependencies=[require_permission(Permission.TRANSPORTER_MANAGE)],
)
async def delete_transporter(
    transporter_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await TransporterService(session, tenant).delete_transporter(transporter_id, reason=reason)
    return ApiResponse[None].ok(None, "Transporter retired")


router.include_router(vendors_router)
router.include_router(transporters_router)

__all__ = ["router", "transporters_router", "vendors_router"]
