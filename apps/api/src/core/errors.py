"""Application error hierarchy and stable error codes.

Master rules sections 27/53: errors use consistent codes, carry a useful
user-facing message, never leak stack traces or SQL, and always include the
correlation id so a support ticket maps to a log line.

Error codes are part of the API contract - the frontend switches on them.
Rename one only with the same care as renaming an endpoint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # --- generic ------------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

    # --- authentication / authorisation -------------------------------------
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    PASSWORD_POLICY = "PASSWORD_POLICY"
    FORBIDDEN = "FORBIDDEN"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    SCOPE_DENIED = "SCOPE_DENIED"

    # --- business rules -----------------------------------------------------
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DUPLICATE_DOCUMENT = "DUPLICATE_DOCUMENT"
    REFERENCED_RECORD_IN_USE = "REFERENCED_RECORD_IN_USE"

    # --- inventory ----------------------------------------------------------
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    INSUFFICIENT_AVAILABLE_STOCK = "INSUFFICIENT_AVAILABLE_STOCK"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    RESERVATION_ALREADY_CONSUMED = "RESERVATION_ALREADY_CONSUMED"
    STOCK_LOCK_TIMEOUT = "STOCK_LOCK_TIMEOUT"
    NEGATIVE_STOCK_BLOCKED = "NEGATIVE_STOCK_BLOCKED"
    BATCH_REQUIRED = "BATCH_REQUIRED"
    SERIAL_REQUIRED = "SERIAL_REQUIRED"

    # --- BOM / MRP ----------------------------------------------------------
    BOM_NOT_FOUND = "BOM_NOT_FOUND"
    BOM_CYCLE_DETECTED = "BOM_CYCLE_DETECTED"
    BOM_DEPTH_EXCEEDED = "BOM_DEPTH_EXCEEDED"
    BOM_NOT_APPROVED = "BOM_NOT_APPROVED"
    MRP_RUN_IN_PROGRESS = "MRP_RUN_IN_PROGRESS"

    # --- files --------------------------------------------------------------
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"

    # --- background jobs ----------------------------------------------------
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_FAILED = "JOB_FAILED"


class AppError(Exception):
    """Base class for every error the application raises deliberately.

    Anything that reaches the handler *without* being an ``AppError`` is treated
    as a bug: it is logged with a traceback and reported to the caller as a bare
    INTERNAL_ERROR with no detail (section 53).
    """

    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": False,
            "error": {"code": str(self.code), "message": self.message},
        }
        if self.details:
            payload["error"]["details"] = self.details
        if request_id:
            payload["request_id"] = request_id
        return payload


# --- 4xx ---------------------------------------------------------------------


class ValidationError(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "The submitted data is not valid."


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND
    message = "The requested record does not exist."

    def __init__(self, entity: str = "Record", identifier: object = None, **kw: Any) -> None:
        msg = f"{entity} not found" + (f": {identifier}" if identifier is not None else "")
        super().__init__(msg, **kw)


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT
    message = "The record was modified by someone else. Reload and try again."


class DuplicateError(AppError):
    status_code = 409
    code = ErrorCode.DUPLICATE_DOCUMENT
    message = "A record with these details already exists."


class AuthenticationError(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHENTICATED
    message = "Authentication is required."


class InvalidCredentialsError(AuthenticationError):
    code = ErrorCode.INVALID_CREDENTIALS
    # Deliberately identical for unknown-user and wrong-password so the endpoint
    # cannot be used to enumerate accounts.
    message = "Incorrect email or password."


class AccountLockedError(AuthenticationError):
    code = ErrorCode.ACCOUNT_LOCKED
    message = "Account temporarily locked after repeated failed sign-in attempts."


class TokenExpiredError(AuthenticationError):
    code = ErrorCode.TOKEN_EXPIRED
    message = "Session expired. Please sign in again."


class AuthorizationError(AppError):
    status_code = 403
    code = ErrorCode.FORBIDDEN
    message = "You do not have permission to perform this action."


class PermissionDeniedError(AuthorizationError):
    code = ErrorCode.PERMISSION_DENIED

    def __init__(self, permission: str, **kw: Any) -> None:
        super().__init__(
            f"Missing required permission: {permission}",
            details={"required_permission": permission},
            **kw,
        )


class TenantMismatchError(AuthorizationError):
    code = ErrorCode.TENANT_MISMATCH
    # Phrased as "not found" to the user: confirming a record exists in another
    # tenant is itself a leak.
    message = "The requested record does not exist."


class RateLimitedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please slow down."

    def __init__(self, retry_after_seconds: int = 60, **kw: Any) -> None:
        super().__init__(details={"retry_after_seconds": retry_after_seconds}, **kw)
        self.retry_after_seconds = retry_after_seconds


# --- business ----------------------------------------------------------------


class BusinessRuleError(AppError):
    status_code = 422
    code = ErrorCode.BUSINESS_RULE_VIOLATION
    message = "This operation is not allowed by the configured business rules."


class InvalidStateTransitionError(BusinessRuleError):
    code = ErrorCode.INVALID_STATE_TRANSITION

    def __init__(self, entity: str, current: str, requested: str, allowed: list[str]) -> None:
        super().__init__(
            f"{entity} cannot move from '{current}' to '{requested}'.",
            details={"current_status": current, "requested_status": requested, "allowed": allowed},
        )


class ApprovalRequiredError(BusinessRuleError):
    code = ErrorCode.APPROVAL_REQUIRED
    message = "This action requires approval before it can be completed."


class InsufficientStockError(BusinessRuleError):
    code = ErrorCode.INSUFFICIENT_AVAILABLE_STOCK

    def __init__(
        self,
        item_code: str,
        warehouse_code: str,
        requested: object,
        available: object,
    ) -> None:
        super().__init__(
            f"Insufficient available stock for {item_code} at {warehouse_code}: "
            f"requested {requested}, available {available}.",
            details={
                "item_code": item_code,
                "warehouse_code": warehouse_code,
                "requested": str(requested),
                "available": str(available),
            },
        )


class StockLockTimeoutError(AppError):
    status_code = 409
    code = ErrorCode.STOCK_LOCK_TIMEOUT
    message = "Stock for this item is being updated by another operation. Please retry."


class BomCycleError(BusinessRuleError):
    code = ErrorCode.BOM_CYCLE_DETECTED

    def __init__(self, path: list[str]) -> None:
        super().__init__(
            "The bill of materials contains a cycle: " + " -> ".join(path),
            details={"cycle_path": path},
        )


class FileValidationError(AppError):
    status_code = 400
    code = ErrorCode.FILE_TYPE_NOT_ALLOWED
    message = "This file type is not allowed."


__all__ = [
    "AccountLockedError",
    "AppError",
    "ApprovalRequiredError",
    "AuthenticationError",
    "AuthorizationError",
    "BomCycleError",
    "BusinessRuleError",
    "ConflictError",
    "DuplicateError",
    "ErrorCode",
    "FileValidationError",
    "InsufficientStockError",
    "InvalidCredentialsError",
    "InvalidStateTransitionError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "StockLockTimeoutError",
    "TenantMismatchError",
    "TokenExpiredError",
    "ValidationError",
]
