"""Every request string has a length bound, never past the column that stores it.

Two rules over every model a route body is validated against:

* **Bounded.** A string with no limit can carry megabytes into a TEXT column that
  every list and detail read then returns; 44 notes and description fields did,
  past the 422 field-error contract. Each string must state a ``maxLength``, an
  ``enum``/``const``, a fixed-size format or an anchored pattern of finite width,
  unless :data:`UNBOUNDED_STRINGS_ALLOWED` names the validator that bounds it.
* **Fits its column.** A ``max_length`` above its column's width passes
  validation and then fails in PostgreSQL (SQLSTATE 22001) - a 500 that names no
  field. ``RoleCreate.name`` carried the person-sized ``Name`` (255) into
  ``roles.name`` VARCHAR(120). Request models are matched to the ORM class of the
  same name less its verb (``RoleCreate`` -> ``Role``), and every field sharing a
  name with one of that class's bounded string columns must admit nothing longer.

The longest admitted value is read from the request's JSON schema (``EmailStr``
is held to 254 characters by email-validator). Self-tests feed each rule the
violations it exists to catch.
"""

from __future__ import annotations

import re
import re._parser as regex_parser  # the stdlib's own pattern parser, for a pattern's width
import typing
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from re._constants import AT, AT_BEGINNING, AT_BEGINNING_STRING, AT_END, AT_END_STRING, MAXREPEAT
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import String

from src.core.models import Base
from src.core.registry import import_all_models
from tests.route_support import assert_matches_contract, module_routes

pytestmark = pytest.mark.unit

#: The verbs a request model's name adds to the entity it writes.
_VERB = re.compile(r"(Create|Update|In|Patch|Request|Upsert)$")

#: The longest value each string format admits. email-validator refuses an
#: address past 254 characters (RFC 5321's path limit).
_FORMAT_LENGTHS = {"email": 254, "uuid": 36, "date": 10, "date-time": 32, "time": 21}

#: Not one of a string's admitted values: the property is not a string at all.
NOT_A_STRING = -1

_SERIAL_LABELS = "each label is normalised and held to 1-64 characters by the field validator"
_PERMISSION_CODES = "each code must name a registered Permission, checked by the field validator"

#: Body strings bounded by a validator the schema cannot show, each with the reason.
#: Keyed ``"module.Model.field"`` exactly as the rule reports an offender.
UNBOUNDED_STRINGS_ALLOWED: dict[str, str] = {
    "assembly.CompleteRequest.output_serial_numbers.[]": _SERIAL_LABELS,
    "inventory.PhysicalCountEntry.serial_numbers.[]": _SERIAL_LABELS,
    "inventory.StockAdjustmentLineCreate.serial_numbers.[]": _SERIAL_LABELS,
    "inventory.StockSerialBulkCreate.serial_numbers.[]": _SERIAL_LABELS,
    "inventory.StockTransferLineCreate.serial_numbers.[]": _SERIAL_LABELS,
    "procurement.GoodsReceiptLineIn.serial_numbers.[]": _SERIAL_LABELS,
    "production.CompleteRequest.output_serial_numbers.[]": _SERIAL_LABELS,
    "production.ProductionEntryCreate.output_serial_numbers.[]": _SERIAL_LABELS,
    "users.RoleCreate.permissions.[]": _PERMISSION_CODES,
    "users.RoleUpdate.permissions.[]": _PERMISSION_CODES,
    "users.UserCreate.denied_permissions.[]": _PERMISSION_CODES,
    "users.UserCreate.extra_permissions.[]": _PERMISSION_CODES,
    "users.UserUpdate.denied_permissions.[]": _PERMISSION_CODES,
    "users.UserUpdate.extra_permissions.[]": _PERMISSION_CODES,
}


def _label(model: type[BaseModel]) -> str:
    return f"{model.__module__.removeprefix('src.modules.').split('.')[0]}.{model.__name__}"


def pattern_width(pattern: str) -> int | None:
    """The longest string a pattern admits, or ``None`` if it admits any length.

    pydantic searches with a pattern rather than matching it whole, so only a
    pattern anchored at both ends bounds the string - ``^[0-9]{6}$`` does,
    ``[0-9]{6}`` and ``^[A-Z][A-Z0-9_]*$`` do not.
    """
    try:
        parsed = regex_parser.parse(pattern)
    except re.error:
        return None
    items = list(parsed)
    anchored = bool(items) and (
        items[0] in {(AT, AT_BEGINNING), (AT, AT_BEGINNING_STRING)}
        and items[-1] in {(AT, AT_END), (AT, AT_END_STRING)}
    )
    longest = parsed.getwidth()[1]
    return longest if anchored and longest < MAXREPEAT else None


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for argument in typing.get_args(annotation):
        yield from _models_in(argument)


def request_models() -> list[type[BaseModel]]:
    """Every model a route's body is validated against, nested line models included."""
    found: dict[type[BaseModel], None] = {}

    def visit(model: type[BaseModel]) -> None:
        if model in found:
            return
        found[model] = None
        for field in model.model_fields.values():
            for nested in _models_in(field.annotation):
                visit(nested)

    for mounted in module_routes():
        for param in mounted.route.dependant.body_params:
            for model in _models_in(param.field_info.annotation):
                visit(model)
    return list(found)


def column_widths() -> dict[str, dict[str, int]]:
    """``{ORM class name: {attribute: VARCHAR width}}`` for every bounded string column."""
    import_all_models()
    widths: dict[str, dict[str, int]] = {}
    for mapper in Base.registry.mappers:
        widths[mapper.class_.__name__] = {
            attribute.key: column.type.length
            for attribute in mapper.column_attrs
            for column in attribute.columns[:1]
            if isinstance(column.type, String) and column.type.length is not None
        }
    return widths


def longest_admitted(prop: Mapping[str, Any], definitions: Mapping[str, Any]) -> int | None:
    """The longest string a property admits; ``None`` if nothing caps it.

    ``NOT_A_STRING`` when no branch of the property is a string, which is no
    concern of a rule about string widths.
    """
    longest = NOT_A_STRING
    for option in prop.get("anyOf", [prop]):
        if "$ref" in option:
            option = definitions[option["$ref"].rsplit("/", 1)[-1]]
        if "enum" in option:
            length = max(len(str(value)) for value in option["enum"])
        elif "const" in option:
            length = len(str(option["const"]))
        elif option.get("type") != "string":
            continue
        elif "maxLength" in option:
            length = option["maxLength"]
        elif option.get("format") in _FORMAT_LENGTHS:
            length = _FORMAT_LENGTHS[option["format"]]
        elif (width := pattern_width(option.get("pattern", ""))) is not None:
            length = width
        else:
            return None
        longest = max(longest, length)
    return longest


def _unbounded(
    schema: Mapping[str, Any], definitions: Mapping[str, Any], where: str
) -> Iterator[str]:
    """Paths under one property schema where a string admits any length."""
    if "$ref" in schema:
        target = definitions[schema["$ref"].rsplit("/", 1)[-1]]
        if target.get("type") == "object" and "properties" in target:
            return  # a nested model: a request model of its own, checked on its own
        yield from _unbounded(target, definitions, where)
        return
    options = schema.get("anyOf") or schema.get("oneOf") or schema.get("allOf")
    if options:
        if any(option.get("type") in {"number", "integer"} for option in options):
            return  # a Decimal: its string branch is the number's own lexical form
        for option in options:
            yield from _unbounded(option, definitions, where)
        return
    kind = schema.get("type")
    if kind == "array":
        yield from _unbounded(schema.get("items", {}), definitions, f"{where}.[]")
    elif kind == "object" and isinstance(schema.get("additionalProperties"), Mapping):
        yield from _unbounded(schema["additionalProperties"], definitions, f"{where}.{{}}")
    elif kind == "string" and longest_admitted(schema, definitions) is None:
        yield where


def unbounded_strings(models: Iterable[type[BaseModel]]) -> tuple[list[str], int]:
    """Request strings that admit any length, and how many string properties were read."""
    found: set[str] = set()
    read = 0
    for model in models:
        schema = model.model_json_schema()
        definitions = schema.get("$defs", {})
        for name, prop in schema.get("properties", {}).items():
            read += 1
            found.update(
                f"{_label(model)}.{where}" for where in _unbounded(prop, definitions, name)
            )
    return sorted(found), read


def width_violations(
    models: Iterable[type[BaseModel]], widths: Mapping[str, Mapping[str, int]]
) -> tuple[list[str], int]:
    """Fields admitting more than their column holds, and how many fields were compared."""
    violations: list[str] = []
    compared = 0
    for model in models:
        columns = widths.get(_VERB.sub("", model.__name__), {})
        schema = model.model_json_schema()
        definitions = schema.get("$defs", {})
        for name, prop in schema.get("properties", {}).items():
            if name not in columns:
                continue
            longest = longest_admitted(prop, definitions)
            if longest == NOT_A_STRING:
                continue
            compared += 1
            if longest is None or longest > columns[name]:
                violations.append(
                    f"{model.__name__}.{name} admits {longest or 'any length'}, "
                    f"the column holds {columns[name]}"
                )
    return sorted(violations), compared


def test_the_width_rule_reads_the_models_the_routes_write() -> None:
    assert_matches_contract(module_routes())
    models = request_models()
    _, compared = width_violations(models, column_widths())

    # Hundreds today; a broken name match or schema walk would compare a handful.
    assert len(models) > 200
    assert compared > 250


def test_no_request_string_is_longer_than_its_column() -> None:
    violations, _ = width_violations(request_models(), column_widths())

    assert not violations, (
        "Bound these request fields to their column's width - or widen the column in a "
        f"migration if the longer value is wanted: {violations}"
    )


class _Widget(BaseModel):
    name: str = Field(max_length=20)
    nickname: str | None = None
    colour: Literal["RED", "ULTRAMARINE"] = "RED"
    email: EmailStr
    code: str = Field(max_length=8)
    quantity: int = 0


class _WidgetCreate(_Widget):
    pass


def test_the_width_rule_reports_a_field_wider_than_its_column() -> None:
    widths = {"_Widget": {"name": 10, "nickname": 10, "colour": 8, "email": 255, "code": 8}}

    violations, compared = width_violations([_WidgetCreate], widths)

    assert [finding.split(" ")[0] for finding in violations] == [
        "_WidgetCreate.colour",
        "_WidgetCreate.name",
        "_WidgetCreate.nickname",
    ]
    assert compared == 5


def test_an_email_is_held_to_254_characters() -> None:
    violations, _ = width_violations([_WidgetCreate], {"_Widget": {"email": 200}})

    assert violations == ["_WidgetCreate.email admits 254, the column holds 200"]


def test_every_request_string_has_a_length_bound() -> None:
    found, read = unbounded_strings(request_models())
    offenders = [name for name in found if name not in UNBOUNDED_STRINGS_ALLOWED]

    assert read > 2000, "the rule read too few properties to be checking the API"
    assert not offenders, (
        "Bound these request strings - max_length (MAX_NOTES_LENGTH, MAX_LINE_NOTES_LENGTH "
        "and MAX_DESCRIPTION_LENGTH in src/core/constants.py for free text), an enum, or an "
        "anchored pattern - or, if a validator bounds one, add it to UNBOUNDED_STRINGS_ALLOWED "
        f"with the reason: {offenders}"
    )


def test_every_string_allowance_is_still_needed() -> None:
    found, _ = unbounded_strings(request_models())
    stale = sorted(set(UNBOUNDED_STRINGS_ALLOWED) - set(found))
    assert not stale, (
        f"remove these UNBOUNDED_STRINGS_ALLOWED entries, they are bounded now: {stale}"
    )


class _Notes(BaseModel):
    notes: str | None = None
    labels: list[str] = []
    tags: dict[str, str] = {}
    title: str = Field(max_length=160)
    kind: Literal["A", "B"] = "A"
    email: EmailStr | None = None
    otp: str = Field(default="000000", pattern=r"^[0-9]{6}$")
    code: str = Field(default="A", pattern=r"^[A-Z][A-Z0-9_]*$")
    amount: Decimal = Field(default=Decimal("0"), max_digits=18, decimal_places=4)
    lines: list[Annotated[str, Field(max_length=10)]] = []


def test_the_bound_rule_reports_a_string_with_no_limit() -> None:
    found, read = unbounded_strings([_Notes])

    assert [name.split(".", 2)[2] for name in found] == ["code", "labels.[]", "notes", "tags.{}"]
    assert read == 10


@pytest.mark.parametrize(
    ("pattern", "width"),
    [
        (r"^[0-9]{6}$", 6),
        (r"\A[a-z]{2,4}\Z", 4),
        (r"[0-9]{6}", None),
        (r"^[A-Z][A-Z0-9_]*$", None),
        (r"^\S(?:.*\S)?$", None),
    ],
)
def test_only_an_anchored_finite_pattern_bounds_a_string(pattern: str, width: int | None) -> None:
    assert pattern_width(pattern) == width
