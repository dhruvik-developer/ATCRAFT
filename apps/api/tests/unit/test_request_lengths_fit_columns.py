"""A request string is never longer than the VARCHAR column that stores it.

A ``max_length`` above its column's width passes validation and then fails in
PostgreSQL (SQLSTATE 22001) - a 500 that names no field. ``RoleCreate.name``
carried the person-sized ``Name`` (255) into ``roles.name`` VARCHAR(120), so an
administrator typing a 121-character role name was told to quote a request id.

Request models are matched to the ORM class of the same name less its verb
(``RoleCreate`` -> ``Role``), and every field sharing a name with one of that
class's bounded string columns must admit nothing longer than the column holds.
The longest admitted value is read from the request's JSON schema: ``maxLength``,
the longest ``enum`` or ``const`` value, or the fixed size of a format
(``EmailStr`` is held to 254 characters by email-validator). The self-tests feed
the rule an over-wide field, an unbounded one and an over-long enum value.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal

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
        else:
            return None
        longest = max(longest, length)
    return longest


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
