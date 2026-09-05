"""Structured logging and the request-scoped context.

Master rules sections 54/55: production logs are structured JSON, every record
carries the correlation id, and secrets never reach a log line.

The context variables set here are read by the logging processor, by the audit
service and by Celery task wrappers, so one identifier follows a request from
Nginx through FastAPI into a worker.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

from src.core.config import AppEnv, settings

# --- Request-scoped context ---------------------------------------------------

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
company_id_ctx: ContextVar[str | None] = ContextVar("company_id", default=None)
ip_address_ctx: ContextVar[str | None] = ContextVar("ip_address", default=None)
user_agent_ctx: ContextVar[str | None] = ContextVar("user_agent", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


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
        ip_address_ctx.set(ip_address)
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
    }
)
REDACTED = "[REDACTED]"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Recursively blank out sensitive keys before a record is emitted."""

    def scrub(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return value
        if isinstance(value, dict):
            return {
                k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else scrub(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(v, depth + 1) for v in value]
        return value

    return scrub(event_dict)


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

    if settings.app_env is AppEnv.production and settings.app_debug:  # pragma: no cover
        raise RuntimeError("APP_DEBUG must not be enabled in production")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Module-level logger factory: ``log = get_logger(__name__)``."""
    return structlog.get_logger(name)


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "bind_request_context",
    "clear_request_context",
    "company_id_ctx",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "ip_address_ctx",
    "new_request_id",
    "request_id_ctx",
    "user_agent_ctx",
    "user_id_ctx",
]
