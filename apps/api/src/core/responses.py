"""The single API response envelope (master rules section 27).

Every successful response is::

    {"success": true, "data": ..., "message": "..."}

Every paginated response adds a ``pagination`` block. Every error is::

    {"success": false, "error": {"code": "...", "message": "...", "details": null},
     "request_id": "..."}

Routes return these models directly so the shape is enforced by the type system
and reflected accurately in the generated OpenAPI document.
"""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.core.config import settings
from src.core.messages import JOB_QUEUED, OPERATION_COMPLETED

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standard success envelope."""

    model_config = ConfigDict(from_attributes=True)

    success: Literal[True] = True
    data: T | None = None
    message: str = OPERATION_COMPLETED

    @classmethod
    def ok(cls, data: T | None = None, message: str | None = None) -> ApiResponse[T]:
        return cls(data=data, message=message or OPERATION_COMPLETED)


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

    success: Literal[True] = True
    data: list[T] = Field(default_factory=list)
    pagination: OffsetPagination = Field(default_factory=OffsetPagination)
    message: str = OPERATION_COMPLETED


class CursorPaginatedResponse(BaseModel, Generic[T]):
    """Cursor-paginated list envelope."""

    success: Literal[True] = True
    data: list[T] = Field(default_factory=list)
    pagination: CursorPagination = Field(default_factory=CursorPagination)
    message: str = OPERATION_COMPLETED


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Documented error shape - referenced by every route's responses map."""

    success: Literal[False] = False
    error: ErrorDetail
    request_id: str | None = None

    @classmethod
    def create(
        cls,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ErrorResponse:
        """Build the same error body for routes, handlers and middleware.

        Optional values remain explicit JSON nulls so callers never have to
        distinguish an absent field from an error without additional details.
        """
        return cls(
            error=ErrorDetail(code=code, message=message, details=details),
            request_id=request_id,
        )


class AcceptedJobResponse(BaseModel):
    """Returned by endpoints that hand work to Celery (sections 35, 77).

    The client polls ``status_url`` instead of holding a long request open.
    """

    success: Literal[True] = True
    data: dict[str, Any]
    message: str = JOB_QUEUED

    @classmethod
    def create(
        cls, job_id: str, status_url: str, message: str | None = None
    ) -> AcceptedJobResponse:
        return cls(
            data={"job_id": job_id, "status": "queued", "status_url": status_url},
            message=message or JOB_QUEUED,
        )


def api_path(path: str) -> str:
    """Absolute API path under the mounted prefix, for URLs returned in bodies.

    Read from ``settings`` on every call rather than at import so a test - or a
    deployment that sets ``API_V1_PREFIX`` - sees the prefix the routers were
    actually mounted under. A literal ``/api/v1`` in a service would point at a
    route that stops existing the day an operator changes the prefix.
    """
    return f"{settings.api_v1_prefix.rstrip('/')}/{path.lstrip('/')}"


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
    405: {"model": ErrorResponse, "description": "HTTP method not allowed"},
    409: {
        "model": ErrorResponse,
        "description": "Conflict: concurrent edit, duplicate, or invalid state transition",
    },
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Validation or business-rule failure"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
    503: {"model": ErrorResponse, "description": "Service temporarily unavailable"},
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
    "api_path",
]
