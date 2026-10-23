"""HTTP surface for stored documents.

Handlers bind parameters, call one service method and wrap the result. Every
validation, every filesystem call and every audit write lives behind the service
(section 74) - including the ones this module exists for, so there is no path
that reaches disk without passing the content and containment checks.

The download route is the one place a response is not the standard envelope: a
file transfer is a stream of bytes, not JSON. It is still authenticated, still
permission-gated and still tenant-scoped, and its security headers come from the
service rather than being spelled out here, so the download and any future
byte-serving route cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.core.validation import bind_request_model
from src.modules.documents.models import DocumentCategory
from src.modules.documents.schemas import (
    DocumentFilters,
    DocumentRead,
    DocumentUploadRequest,
)
from src.modules.documents.service import (
    DOWNLOAD_HEADERS,
    DocumentService,
    content_disposition,
    stream_file,
)

router = APIRouter(prefix="/documents", tags=["Documents"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# A DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83) - so it travels in the query string.
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the document is being deleted")
]


def document_filters(
    search: Annotated[str | None, Query(max_length=255, description="Original file name")] = None,
    category: Annotated[DocumentCategory | None, Query()] = None,
    entity_type: Annotated[
        str | None, Query(max_length=48, description="Attached-to type, e.g. goods_receipt")
    ] = None,
    entity_id: Annotated[
        uuid.UUID | None, Query(description="Attached-to id; requires entity_type")
    ] = None,
    mime_type: Annotated[str | None, Query(max_length=128)] = None,
    uploaded_by_id: Annotated[uuid.UUID | None, Query()] = None,
    is_public: Annotated[bool | None, Query()] = None,
    include_expired: Annotated[
        bool, Query(description="Include documents past their retention date")
    ] = False,
) -> DocumentFilters:
    return bind_query_filters(
        DocumentFilters,
        search=search,
        category=category,
        entity_type=entity_type,
        entity_id=entity_id,
        mime_type=mime_type,
        uploaded_by_id=uploaded_by_id,
        is_public=is_public,
        include_expired=include_expired,
    )


FilterDep = Annotated[DocumentFilters, Depends(document_filters)]


@router.post(
    "",
    response_model=ApiResponse[DocumentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file",
    description=(
        "Multipart upload. The declared content type must be permitted by the server "
        "configuration and must match the file's actual leading bytes; the stored path is "
        "generated server-side and never derives from the submitted file name."
    ),
    dependencies=[require_permission(Permission.DOCUMENT_UPLOAD)],
)
async def upload_document(
    session: DbSession,
    tenant: Tenant,
    file: Annotated[UploadFile, File(description="The file to store")],
    category: Annotated[DocumentCategory, Form()] = DocumentCategory.DOCUMENT,
    entity_type: Annotated[str | None, Form(max_length=48)] = None,
    entity_id: Annotated[uuid.UUID | None, Form()] = None,
    is_public: Annotated[bool, Form()] = False,
    expires_at: Annotated[datetime | None, Form()] = None,
) -> ApiResponse[DocumentRead]:
    # The form fields are re-validated as a model rather than trusted as bound
    # scalars: the cross-field rules (an entity reference is both halves or
    # neither, a retention date is in the future) live on the schema.
    payload = bind_request_model(
        DocumentUploadRequest,
        {
            "category": category,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "is_public": is_public,
            "expires_at": expires_at,
        },
    )
    document = await DocumentService(session, tenant).upload(file, payload)
    return ApiResponse[DocumentRead].ok(document, success_message("File", "uploaded"))


@router.get(
    "",
    response_model=PaginatedResponse[DocumentRead],
    summary="List documents",
    dependencies=[require_permission(Permission.DOCUMENT_VIEW)],
)
async def list_documents(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: FilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[DocumentRead]:
    items, pagination = await DocumentService(session, tenant).list_documents(
        filters, params, sort, principal=principal
    )
    return PaginatedResponse[DocumentRead](data=items, pagination=pagination)


@router.get(
    "/{document_id}",
    response_model=ApiResponse[DocumentRead],
    summary="Fetch document metadata",
    dependencies=[require_permission(Permission.DOCUMENT_VIEW)],
)
async def get_document(
    document_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[DocumentRead]:
    document = await DocumentService(session, tenant).get_document(document_id, principal=principal)
    return ApiResponse[DocumentRead].ok(document)


@router.get(
    "/{document_id}/download",
    summary="Download the file",
    description=(
        "Authenticated, tenant-scoped streaming download. Always served as an attachment "
        "with `X-Content-Type-Options: nosniff`."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "File content; the Content-Type header identifies its actual media type",
        }
    },
    dependencies=[require_permission(Permission.DOCUMENT_VIEW)],
)
async def download_document(
    document_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> StreamingResponse:
    download = await DocumentService(session, tenant).open_download(
        document_id, principal=principal
    )
    return StreamingResponse(
        stream_file(download.path),
        media_type=download.document.mime_type,
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(download.document.file_name),
            # Explicit: a streamed response is otherwise sent chunked, and the
            # client cannot show progress or detect a truncated transfer.
            "Content-Length": str(download.document.size_bytes),
        },
    )


@router.delete(
    "/{document_id}",
    response_model=ApiResponse[None],
    summary="Delete a document and its file",
    dependencies=[require_permission(Permission.DOCUMENT_DELETE)],
)
async def delete_document(
    document_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await DocumentService(session, tenant).delete_document(
        document_id, reason=reason, principal=principal
    )
    return ApiResponse[None].ok(None, success_message("Document", "deleted"))


__all__ = ["router"]
