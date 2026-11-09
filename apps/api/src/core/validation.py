"""One field-error format for HTTP validation and manually bound request models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationInfo
from pydantic import ValidationError as PydanticValidationError

from src.core.errors import ValidationError
from src.core.messages import FIELDS_NEED_ATTENTION, INVALID_VALUE, MALFORMED_JSON, cannot_be_null

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


def reject_null(value: object, info: ValidationInfo) -> object:
    """Refuse an explicit ``null`` on an update field whose column is NOT NULL.

    Bind it on an update schema as ``field_validator("a", "b")(reject_null)``.
    Update schemas are applied with ``exclude_unset=True``, so an omitted key
    keeps the stored value and pydantic never runs this for it: it runs only
    when the client sent the field. A null let through is written to a NOT NULL
    column, which the integrity handler answers with a 422 naming no field, or
    is handed to a service's arithmetic, which is a 500. Refused here, the 422
    names the field before any service runs.
    """
    if value is None:
        raise ValueError(cannot_be_null(info.field_name or "This value"))
    return value


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


__all__ = ["bind_request_model", "reject_null", "validation_field_errors"]
