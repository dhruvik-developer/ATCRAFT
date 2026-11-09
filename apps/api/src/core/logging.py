"""Structured logging and the request-scoped context.

Master rules sections 54/55: production logs are structured JSON, every record
carries the correlation id, and secrets never reach a log line.

The context variables set here are read by the logging processor, by the audit
service and by Celery task wrappers, so one identifier follows a request from
Nginx through FastAPI into a worker.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import traceback
import uuid
from contextvars import ContextVar
from ipaddress import ip_address as parse_ip_address
from typing import Any

import structlog

from src.core.config import AppEnv, settings
from src.core.constants import MAX_REQUEST_ID_LENGTH
from src.core.database_errors import database_error_redactor

# --- Request-scoped context ---------------------------------------------------

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
company_id_ctx: ContextVar[str | None] = ContextVar("company_id", default=None)
ip_address_ctx: ContextVar[str | None] = ContextVar("ip_address", default=None)
user_agent_ctx: ContextVar[str | None] = ContextVar("user_agent", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def resolve_request_id(value: str | None) -> str:
    """Reuse a safe upstream id, otherwise generate one that fits the audit row.

    Correlation headers are untrusted even behind a proxy. Oversized values can
    otherwise abort an audited transaction; control characters must never be
    reflected into a response header or attached to every log line.
    """
    if (
        value
        and len(value) <= MAX_REQUEST_ID_LENGTH
        and value.isascii()
        and all(character.isalnum() or character in "-_." for character in value)
    ):
        return value
    return new_request_id()


def resolve_client_ip(*addresses: str | None) -> str | None:
    """Use the first valid address that PostgreSQL's INET can persist."""
    for address in addresses:
        if not address or "%" in address:
            continue
        try:
            return str(parse_ip_address(address))
        except ValueError:
            continue
    return None


def get_request_id() -> str | None:
    return request_id_ctx.get()


def bind_request_context(
    *,
    request_id: str | None = None,
    user_id: str | uuid.UUID | None = None,
    company_id: str | uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Attach identifiers to the current execution context."""
    if request_id is not None:
        request_id_ctx.set(request_id)
    if user_id is not None:
        user_id_ctx.set(str(user_id))
    if company_id is not None:
        company_id_ctx.set(str(company_id))
    if ip_address is not None:
        ip_address_ctx.set(resolve_client_ip(ip_address))
    if user_agent is not None:
        user_agent_ctx.set(user_agent[:512])


def clear_request_context() -> None:
    for var in (request_id_ctx, user_id_ctx, company_id_ctx, ip_address_ctx, user_agent_ctx):
        var.set(None)


# --- Redaction ----------------------------------------------------------------

# Section 54: these must never appear in a log line, whatever the call site does.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "confirm_password",
        "token",
        "access_token",
        "refresh_token",
        "reset_token",
        "password_reset_token",
        "authorization",
        "api_key",
        "secret",
        "mfa_secret",
        "smtp_password",
        "sms_auth_key",
        "whatsapp_access_token",
        "jwt_secret",
        "client_secret",
        "cookie",
        "set-cookie",
        "session",
        "otp",
        "pan",
        "aadhaar",
        "card_number",
        "cvv",
        "bank_account",
        "ifsc",
        "phone",
        "phone_e164",
        "to_e164",
        "recipient",
        "mobiles",
        "sms_text",
        "body",
        "variables",
        "code_hash",
        "endpoint",
        "p256dh",
        "auth",
    }
)
REDACTED = "[REDACTED]"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Recursively blank out sensitive keys before a record is emitted.

    A tuple stays a tuple. A standard-library record hands its ``exc_info`` over
    as one, and structlog renders a traceback only from a real ``(type, value,
    traceback)`` tuple; turned into a list it fell back to whatever exception
    was being handled when the line was *formatted* - none, for a record
    logged outside its ``except`` block (asyncio's "Task exception was never
    retrieved"), so its traceback silently vanished.
    """

    def scrub(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return value
        if isinstance(value, dict):
            return {
                k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else scrub(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, tuple):
            return tuple(scrub(v, depth + 1) for v in value)
        if isinstance(value, list):
            return [scrub(v, depth + 1) for v in value]
        return value

    return scrub(event_dict)


def _logged_exception(
    exc_info: Any,
) -> tuple[type[BaseException], BaseException, Any] | None:
    """The ``(type, value, traceback)`` a record's ``exc_info`` stands for, if any.

    An exception, a stdlib record's tuple, or ``True`` - "the exception being
    handled", which this processor still runs inside, as ``format_exc_info``
    would read it.
    """
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        return type(exc_info), exc_info, exc_info.__traceback__
    if (
        isinstance(exc_info, tuple)
        and len(exc_info) == 3
        and isinstance(exc_info[1], BaseException)
    ):
        return exc_info[0], exc_info[1], exc_info[2]
    return None


def _withhold_database_error_text(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Keep what a database error quotes of its row out of the record.

    A driver's text quotes values - an integrity violation's ``DETAIL``, a data
    error's rejected input - and SQLAlchemy embeds it in its own, so it reached
    every traceback and every ``error=str(exc)`` logged beside one, where the
    key-based redaction above cannot see it. For a record whose exception chain
    holds such an error, the traceback is rendered here, as ``format_exc_info``
    renders it, with that text replaced by the SQLSTATE and the constraint,
    table and column (:mod:`src.core.database_errors`), and so is every string
    field that repeats it. Any other record is left for ``format_exc_info``.
    """
    logged = _logged_exception(event_dict.get("exc_info"))
    redact = database_error_redactor(logged[1] if logged else None)
    if logged is None or redact is None:
        return event_dict
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact(value)
    del event_dict["exc_info"]
    # format_exc_info's own shape: the stdlib rendering, less its final newline.
    rendered = "".join(traceback.format_exception(*logged)).removesuffix("\n")
    event_dict["exception"] = redact(rendered)
    return event_dict


def _add_request_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Stamp every record with the identifiers of the current request."""
    for key, var in (
        ("request_id", request_id_ctx),
        ("user_id", user_id_ctx),
        ("company_id", company_id_ctx),
    ):
        value = var.get()
        if value and key not in event_dict:
            event_dict[key] = value
    return event_dict


# --- One traceback per failure -------------------------------------------------

#: Set on an exception once the application has logged it with its traceback.
_TRACEBACK_LOGGED = "_atcraft_traceback_logged"

#: The server logger that repeats an exception the application already handled.
SERVER_ERROR_LOGGER = "uvicorn.error"


def mark_traceback_logged(exc: BaseException) -> None:
    """Note that ``exc`` is in the log with its traceback, so no server repeats it."""
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, _TRACEBACK_LOGGED, True)


def traceback_already_logged(exc: BaseException | None) -> bool:
    return exc is not None and getattr(exc, _TRACEBACK_LOGGED, False) is True


class _RepeatedTracebackFilter(logging.Filter):
    """Drop a server's repeat of a traceback the application already logged.

    Starlette's ``ServerErrorMiddleware`` answers 500 through the application's
    handler - which logs ``error.unhandled`` with the traceback - and then
    re-raises, so uvicorn logged the same exception again as "Exception in
    ASGI application": a second full traceback for every defect. Only a record
    carrying an exception the handler marked is dropped; one the handler never
    saw - the handler itself failing, a lifespan error - is still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exception = record.exc_info[1] if record.exc_info else None
        return not traceback_already_logged(exception)


_REPEATED_TRACEBACK_FILTER = _RepeatedTracebackFilter()


def configure_logging() -> None:
    """Install the structlog pipeline. Called once during application startup.

    structlog and the standard library are bridged deliberately rather than run
    side by side. Uvicorn, SQLAlchemy and Celery all log through ``logging``, and
    section 54 asks for one structured format in production - not JSON from the
    application interleaved with plain text from its libraries.

    ``ProcessorFormatter`` is what makes that work: application events are handed
    to it by ``wrap_for_formatter``, foreign stdlib records go through
    ``foreign_pre_chain`` first, and both end up at the same renderer.
    """
    level = logging.getLevelNamesMapping()[settings.log_level]

    # Applied to application events and to foreign stdlib records alike, so a
    # SQLAlchemy warning carries the same request id as our own logs.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _add_request_context,
        _redact,
        _withhold_database_error_text,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # LoggerFactory, not PrintLoggerFactory: add_logger_name and the stdlib
        # bridge both need a real ``logging.Logger`` underneath.
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for noisy, level in (
        ("uvicorn.access", logging.WARNING),  # replaced by our own access log
        ("uvicorn.error", logging.INFO),
        ("sqlalchemy.engine", logging.WARNING),
        ("aiosqlite", logging.WARNING),
        ("asyncio", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(level)
    # Adding the same filter twice is a no-op, so a second call changes nothing.
    logging.getLogger(SERVER_ERROR_LOGGER).addFilter(_REPEATED_TRACEBACK_FILTER)

    if settings.app_env is AppEnv.production and settings.app_debug:  # pragma: no cover
        raise RuntimeError("APP_DEBUG must not be enabled in production")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Module-level logger factory: ``log = get_logger(__name__)``."""
    return structlog.get_logger(name)


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "SERVER_ERROR_LOGGER",
    "bind_request_context",
    "clear_request_context",
    "company_id_ctx",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "ip_address_ctx",
    "mark_traceback_logged",
    "new_request_id",
    "request_id_ctx",
    "resolve_client_ip",
    "resolve_request_id",
    "traceback_already_logged",
    "user_agent_ctx",
    "user_id_ctx",
]
