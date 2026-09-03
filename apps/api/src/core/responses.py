"""The single API response envelope (master rules section 27).

Every successful response is::

    {"success": true, "data": ..., "message": "..."}

Every paginated response adds a ``pagination`` block. Every error is::

    {"success": false, "error": {"code": "...", "message": "..."}, "request_id": "..."}

Routes return these models directly so the shape is enforced by the type system
and reflected accurately in the generated OpenAPI document.
"""

from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standard success envelope."""

    model_config = ConfigDict(from_attributes=True)

    success: bool = True
    data: T | None = None
    message: str = "Operation completed successfully"

    @classmethod
    def ok(cls, data: T | None = None, message: str | None = None) -> ApiResponse[T]:
        return cls(data=data, message=message or "Operation completed successfully")


class OffsetPagination(BaseModel):
    """Page/limit metadata.

    ``total`` is optional on purpose: counting a large filtered set costs a
    second full scan, so list endpoints over very large tables may omit it
    rather than pay for a number nobody reads (section 31).
    """

    page: Annotated[int, Field(ge=1)] = 1
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    total: int | None = None
    total_pages: int | None = None
    has_next: bool = False
    has_previous: bool = False


class CursorPagination(BaseModel):
    """Keyset pagination metadata for very large tables (ledger, audit log)."""

    limit: Annotated[int, Field(ge=1, le=200)] = 50
    next_cursor: str | None = None
    has_next: bool = False


class PaginatedResponse(BaseModel, Generic[T]):
    """Offset-paginated list envelope."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    pagination: OffsetPagination = Field(default_factory=OffsetPagination)
    message: str = "Operation completed successfully"


class CursorPaginatedResponse(BaseModel, Generic[T]):
    """Cursor-paginated list envelope."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    pagination: CursorPagination = Field(default_factory=CursorPagination)
    message: str = "Operation completed successfully"


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Documented error shape - referenced by every route's responses map."""

    success: bool = False
    error: ErrorDetail
    request_id: str | None = None


class AcceptedJobResponse(BaseModel):
    """Returned by endpoints that hand work to Celery (sections 35, 77).

    The client polls ``status_url`` instead of holding a long request open.
    """

    success: bool = True
    data: dict[str, Any]
    message: str = "Job accepted"

    @classmethod
    def create(
        cls, job_id: str, status_url: str, message: str | None = None
    ) -> AcceptedJobResponse:
        return cls(
            data={"job_id": job_id, "status": "queued", "status_url": status_url},
            message=message or "Job queued for background processing",
        )


# Reusable OpenAPI responses map so every route documents its failure modes
# without repeating the schema (section 68).
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed request"},
    401: {"model": ErrorResponse, "description": "Authentication required or token invalid"},
    403: {
        "model": ErrorResponse,
        "description": "Authenticated but missing the required permission",
    },
    404: {"model": ErrorResponse, "description": "Record not found in this tenant"},
    409: {
        "model": ErrorResponse,
        "description": "Conflict: concurrent edit, duplicate, or invalid state transition",
    },
    422: {"model": ErrorResponse, "description": "Validation or business-rule failure"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
}

__all__ = [
    "COMMON_ERROR_RESPONSES",
    "AcceptedJobResponse",
    "ApiResponse",
    "CursorPaginatedResponse",
    "CursorPagination",
    "ErrorDetail",
    "ErrorResponse",
    "OffsetPagination",
    "PaginatedResponse",
]
