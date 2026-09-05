"""Centralised exception handling (master rules section 53).

One rule governs everything here: a client learns what it did wrong, and nothing
about how the server is built. No stack traces, no SQL fragments, no constraint
names, no table names - just a stable error code, a usable message, and the
correlation id that lets support find the full detail in the logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from src.core.config import settings
from src.core.errors import AppError, ErrorCode
from src.core.logging import get_logger, get_request_id
from src.core.messages import FIELDS_NEED_ATTENTION

log = get_logger("errors")


def _payload(
    code: ErrorCode | str,
    message: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "success": False,
        "error": {"code": str(code), "message": message},
    }
    if details:
        body["error"]["details"] = details  # type: ignore[index]
    request_id = get_request_id()
    if request_id:
        body["request_id"] = request_id
    return body


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Deliberate, expected failures. Logged at info/warning, never as errors."""
    log_method = log.warning if exc.status_code >= 500 else log.info
    log_method(
        "error.application",
        code=str(exc.code),
        status=exc.status_code,
        path=request.url.path,
        detail=exc.message,
    )
    headers = {}
    if exc.status_code == 429:
        headers["Retry-After"] = str(getattr(exc, "retry_after_seconds", 60))
    response = JSONResponse(
        status_code=exc.status_code,
        content=_payload(exc.code, exc.message, exc.details),
        headers=headers,
    )
    # A rejected refresh credential cannot recover on the next attempt. Expire
    # both browser cookies on the actual error response (mutating FastAPI's
    # injected route Response before raising would be lost when this handler
    # constructs its replacement response). CSRF failures are 403 and retain a
    # legitimate session because they may simply be a forged cross-site call.
    refresh_path = f"{settings.api_v1_prefix.rstrip('/')}/auth/refresh"
    if exc.status_code == 401 and request.url.path.rstrip("/") == refresh_path:
        from src.modules.auth.cookies import clear_refresh_cookies

        clear_refresh_cookies(response)
    return response


#: Pydantic v2 prefixes the message of a validator that raised with the kind of
#: exception it caught. The sentence after the prefix is the one written for the
#: user; the prefix is a framework detail the form must never show.
_PYDANTIC_MESSAGE_PREFIXES = ("Value error, ", "Assertion failed, ")


def _field_message(error: Mapping[str, Any]) -> str:
    """The user-facing sentence for one pydantic error.

    A ``ValueError`` raised inside a schema validator is the original exception
    under ``ctx.error``; prefer its text, which is exactly what the validator
    wrote. Fall back to stripping the prefix off ``msg`` for errors that arrive
    already serialised.
    """
    ctx = error.get("ctx") or {}
    if error.get("type") in {"value_error", "assertion_error"} and ctx.get("error") is not None:
        return str(ctx["error"])
    message = str(error.get("msg") or "Invalid value")
    for prefix in _PYDANTIC_MESSAGE_PREFIXES:
        message = message.removeprefix(prefix)
    return message


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic request validation, reshaped into the standard envelope.

    Field paths are flattened into ``body.lines.0.quantity`` form so the frontend
    can map an error straight onto the form control that produced it.
    """
    field_errors: dict[str, list[str]] = {}
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        field_errors.setdefault(location or "_root", []).append(_field_message(error))

    log.info("error.validation", path=request.url.path, fields=sorted(field_errors))
    return JSONResponse(
        status_code=422,
        content=_payload(
            ErrorCode.VALIDATION_ERROR,
            FIELDS_NEED_ATTENTION,
            {"fields": field_errors},
        ),
    )


# PostgreSQL SQLSTATE codes for the integrity-constraint class (23xxx). The
# driver puts them on the wrapped error (``sqlstate`` for asyncpg and psycopg
# 3, ``pgcode`` for psycopg 2 and SQLAlchemy's adapted asyncpg error), so
# classifying on them is exact where matching message text is not: the NOT
# NULL branch below was dead for as long as it matched "not null", because
# PostgreSQL spells it "not-null".
SQLSTATE_NOT_NULL_VIOLATION = "23502"
SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"
SQLSTATE_UNIQUE_VIOLATION = "23505"
SQLSTATE_CHECK_VIOLATION = "23514"

_DUPLICATE = (ErrorCode.DUPLICATE_DOCUMENT, "A record with these details already exists.", 409)
_REFERENCED_IN_USE = (
    ErrorCode.REFERENCED_RECORD_IN_USE,
    "This record is referenced by other data and cannot be changed or removed.",
    409,
)
_DANGLING_REFERENCE = (ErrorCode.VALIDATION_ERROR, "A referenced record does not exist.", 422)
_CHECK_FAILED = (
    ErrorCode.BUSINESS_RULE_VIOLATION,
    "The submitted values break a business rule enforced by the database.",
    422,
)
_REQUIRED_MISSING = (ErrorCode.VALIDATION_ERROR, "A required value is missing.", 422)
_GENERIC_CONFLICT = (ErrorCode.CONFLICT, "The operation conflicts with existing data.", 409)


def _sqlstate(exc: BaseException) -> str | None:
    """The five-character SQLSTATE the driver attached, if any."""
    original = getattr(exc, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        if candidate is None:
            continue
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def _foreign_key_outcome(lowered: str) -> tuple[ErrorCode, str, int]:
    """PostgreSQL raises two different FK violations under one SQLSTATE.

    ``update or delete on table ...`` means the row is still referenced - the
    caller tried to remove something in use. ``insert or update on table ...``
    means the caller supplied an id that does not exist, which is a validation
    failure, not a refusal to delete; reporting it as "referenced by other data"
    is the opposite of what happened.
    """
    if "insert or update on table" in lowered:
        return _DANGLING_REFERENCE
    return _REFERENCED_IN_USE


def classify_integrity_error(exc: BaseException) -> tuple[ErrorCode, str, int]:
    """Map a constraint violation to ``(code, message, status)``.

    SQLSTATE first; message text only as the fallback for a driver that did not
    attach one (SQLite in unit tests, an adapted error that lost it).
    """
    raw = str(getattr(exc, "orig", exc))
    lowered = raw.lower()
    sqlstate = _sqlstate(exc)

    if sqlstate == SQLSTATE_UNIQUE_VIOLATION:
        return _DUPLICATE
    if sqlstate == SQLSTATE_FOREIGN_KEY_VIOLATION:
        return _foreign_key_outcome(lowered)
    if sqlstate == SQLSTATE_CHECK_VIOLATION:
        return _CHECK_FAILED
    if sqlstate == SQLSTATE_NOT_NULL_VIOLATION:
        return _REQUIRED_MISSING

    if "unique" in lowered or "duplicate key" in lowered:
        return _DUPLICATE
    if "foreign key" in lowered:
        return _foreign_key_outcome(lowered)
    if "check constraint" in lowered:
        return _CHECK_FAILED
    if "not-null" in lowered or "not null" in lowered:
        return _REQUIRED_MISSING
    return _GENERIC_CONFLICT


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Database constraint violations, translated without leaking schema detail.

    The driver message names tables, columns and constraints. It goes to the log;
    the client gets a generic sentence keyed to the class of violation.
    """
    raw = str(getattr(exc, "orig", exc))
    log.warning("error.integrity", path=request.url.path, sqlstate=_sqlstate(exc), detail=raw[:500])

    code, message, status = classify_integrity_error(exc)
    return JSONResponse(status_code=status, content=_payload(code, message))


async def stale_data_handler(request: Request, exc: StaleDataError) -> JSONResponse:
    """Optimistic-lock miss: someone else saved first."""
    log.info("error.stale_data", path=request.url.path)
    return JSONResponse(
        status_code=409,
        content=_payload(
            ErrorCode.CONFLICT,
            "This record was changed by someone else while you were editing. "
            "Reload to see the current version, then reapply your change.",
        ),
    )


async def operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """Lock timeouts, statement timeouts and connection loss."""
    raw = str(getattr(exc, "orig", exc)).lower()

    if "lock" in raw and "timeout" in raw:
        log.warning("error.lock_timeout", path=request.url.path)
        return JSONResponse(
            status_code=409,
            content=_payload(
                ErrorCode.STOCK_LOCK_TIMEOUT,
                "Another operation is updating this record. Please retry in a moment.",
            ),
        )
    if "statement timeout" in raw or "canceling statement" in raw:
        log.error("error.statement_timeout", path=request.url.path)
        return JSONResponse(
            status_code=503,
            content=_payload(
                ErrorCode.SERVICE_UNAVAILABLE,
                "This request took too long and was stopped. "
                "Narrow the filters, or run it as a background report.",
            ),
        )

    log.error("error.database_operational", path=request.url.path, detail=raw[:500])
    return JSONResponse(
        status_code=503,
        content=_payload(ErrorCode.SERVICE_UNAVAILABLE, "The service is temporarily unavailable."),
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Any other database failure - always a bug, never explained to the client."""
    log.exception("error.database", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content=_payload(ErrorCode.INTERNAL_ERROR, "An unexpected error occurred."),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Framework HTTPExceptions (404 on unknown route, 405, etc.)."""
    code = {
        400: ErrorCode.VALIDATION_ERROR,
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        429: ErrorCode.RATE_LIMITED,
    }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)

    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(
            code, str(exc.detail) if exc.detail else "Request could not be completed."
        ),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The final net. Anything reaching here is a defect.

    The full traceback is logged with the correlation id. Outside development the
    response carries nothing but the code and that id.
    """
    log.exception("error.unhandled", path=request.url.path, exc_type=type(exc).__name__)

    details: dict[str, object] | None = None
    if settings.app_debug and not settings.is_production:
        details = {"exception": type(exc).__name__, "message": str(exc)[:500]}

    return JSONResponse(
        status_code=500,
        content=_payload(
            ErrorCode.INTERNAL_ERROR,
            "An unexpected error occurred. Quote the request id when reporting this.",
            details,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler. Order is specific-to-general."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StaleDataError, stale_data_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(OperationalError, operational_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DBAPIError, sqlalchemy_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = ["classify_integrity_error", "register_exception_handlers"]
