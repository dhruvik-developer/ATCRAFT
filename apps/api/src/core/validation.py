"""One field-error format for HTTP validation and manually bound request models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from src.core.errors import ValidationError
from src.core.messages import FIELDS_NEED_ATTENTION, INVALID_VALUE, MALFORMED_JSON

_PYDANTIC_MESSAGE_PREFIXES = ("Value error, ", "Assertion failed, ")
ModelT = TypeVar("ModelT", bound=BaseModel)


def _field_message(error: Mapping[str, Any]) -> str:
    """Keep a validator's own sentence without exposing exception metadata."""
    if error.get("type") == "json_invalid":
        return MALFORMED_JSON
    context = error.get("ctx") or {}
    if error.get("type") in {"value_error", "assertion_error"}:
        original = context.get("error")
        if original is not None and str(original):
            return str(original)
    message = str(error.get("msg") or INVALID_VALUE)
    for prefix in _PYDANTIC_MESSAGE_PREFIXES:
        message = message.removeprefix(prefix)
    return message or INVALID_VALUE


def validation_field_errors(
    errors: Iterable[Mapping[str, Any]], *, prefix: str | None = None
) -> dict[str, list[str]]:
    """Map validation errors to field paths, excluding submitted inputs/context.

    Body fields use form names such as ``lines.0.quantity``; query, path,
    header and cookie fields keep their transport prefix. Only the leading
    body transport marker is removed: an actual nested field named ``body``
    remains part of its path. Model-level errors use ``_root`` or ``query``.
    """
    fields: dict[str, list[str]] = {}
    for error in errors:
        location = tuple(error.get("loc") or ())
        if error.get("type") == "json_invalid":
            # The last location component is a byte offset into malformed JSON,
            # not a form field to which the client could attach a message.
            location = ()
        elif prefix is not None:
            location = (prefix, *location)
        if location[:1] == ("body",):
            location = location[1:]
        path = ".".join(str(part) for part in location) or "_root"
        fields.setdefault(path, []).append(_field_message(error))
    return fields


def bind_request_model(
    model: type[ModelT],
    values: Mapping[str, object],
    *,
    source: Literal["body", "query"] = "body",
) -> ModelT:
    """Validate parsed request values with the same 422 contract as FastAPI.

    Use only at an input boundary, such as assembling a multipart form or query
    model after FastAPI has validated individual parameters. Output models and
    internal data must be constructed normally so their defects remain 500s.
    """
    try:
        return model(**values)
    except PydanticValidationError as exc:
        fields = validation_field_errors(exc.errors(), prefix=source)
        raise ValidationError(FIELDS_NEED_ATTENTION, details={"fields": fields}) from exc


__all__ = ["bind_request_model", "validation_field_errors"]
