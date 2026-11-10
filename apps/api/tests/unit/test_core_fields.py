"""The request field types every module shares, and the guards that keep them shared.

``src/core/fields.py`` states each rule once. The currency code had drifted
eight ways before it did - finance refused ``inr`` that products stored, and
products and vendors stored ``1A$`` - and ``Reason`` had been declared in
fourteen schema files. These tests pin what each shared type admits and
refuses, prove every request body that names a currency reads it through the
shared rule, and refuse a module that declares one of the shared names for
itself. Each guard has a self-test that feeds it the violation it exists for.
"""

from __future__ import annotations

import ast
import typing
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter, field_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.fields import FieldInfo

from src.core import fields
from src.core.config import API_ROOT
from src.core.constants import REASON_MIN_LENGTH
from src.core.errors import ValidationError as AppValidationError
from src.core.fields import (
    CurrencyCode,
    LongReason,
    NonNegativeMoney,
    NonNegativeQuantity,
    PositiveQuantity,
    Reason,
    currency_code,
)
from src.core.messages import CURRENCY_CODE_INVALID
from src.modules.finance.service import normalise_currency
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

CURRENCY = TypeAdapter(CurrencyCode)


# -----------------------------------------------------------------------------
# The currency code
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("INR", "INR"), ("inr", "INR"), ("Usd", "USD"), (" eur ", "EUR"), ("abc", "ABC")],
)
def test_a_currency_is_kept_as_its_upper_case_code(raw: str, stored: str) -> None:
    assert CURRENCY.validate_python(raw) == stored


@pytest.mark.parametrize("raw", ["", "IN", "INRR", "1A$", "IN1", "I N", "ÄÖÜ", "₹"])
def test_anything_but_three_letters_is_refused_in_the_shared_sentence(raw: str) -> None:
    with pytest.raises(PydanticValidationError) as refused:
        CURRENCY.validate_python(raw)

    (error,) = refused.value.errors()
    assert error["type"] == "value_error"
    assert CURRENCY_CODE_INVALID in error["msg"]


def test_a_currency_that_is_not_text_is_a_type_error() -> None:
    with pytest.raises(PydanticValidationError) as refused:
        CURRENCY.validate_python(356)

    assert [error["type"] for error in refused.value.errors()] == ["string_type"]


def test_an_omitted_optional_currency_stays_none() -> None:
    assert TypeAdapter(CurrencyCode | None).validate_python(None) is None


def test_the_contract_states_the_shape_of_a_code() -> None:
    assert CURRENCY.json_schema() == {
        "type": "string",
        "minLength": 3,
        "maxLength": 3,
        "pattern": "^[A-Z]{3}$",
    }


def test_the_order_of_the_annotations_is_what_keeps_the_pattern_published() -> None:
    """Pins the reason the comment on ``CurrencyCode`` gives for its order.

    If pydantic ever publishes the pattern either way round, this fails and the
    comment can go - the order then no longer matters.
    """
    reversed_order = Annotated[
        str,
        BeforeValidator(currency_code),
        Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
    ]

    assert "pattern" not in TypeAdapter(reversed_order).json_schema()


@pytest.mark.parametrize(("raw", "stored"), [("inr", "INR"), (" usd ", "USD"), (None, None)])
def test_a_report_filter_reads_a_currency_as_a_field_does(raw: str | None, stored: str) -> None:
    assert normalise_currency(raw) == stored


def test_a_report_filter_refuses_a_non_code_in_the_shared_sentence() -> None:
    with pytest.raises(AppValidationError) as refused:
        normalise_currency("1A$")

    assert refused.value.message == CURRENCY_CODE_INVALID
    assert refused.value.details == {"currency": "1A$"}


# -----------------------------------------------------------------------------
# Reasons, quantities and money
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(("alias", "longest"), [(Reason, 255), (LongReason, 500)])
def test_a_reason_is_bounded_at_both_ends(alias: Any, longest: int) -> None:
    adapter = TypeAdapter(alias)

    for kept in ("a" * REASON_MIN_LENGTH, "a" * longest):
        assert adapter.validate_python(kept) == kept
    for refused in ("a" * (REASON_MIN_LENGTH - 1), "a" * (longest + 1)):
        with pytest.raises(PydanticValidationError):
            adapter.validate_python(refused)


@pytest.mark.parametrize(
    ("alias", "kept", "refused"),
    [
        (
            PositiveQuantity,
            ["0.0001", "1.2345", "99999999999999.9999"],
            ["0", "-1", "0.00004", "1.23456", "100000000000000"],
        ),
        (
            NonNegativeQuantity,
            ["0", "1.2345", "99999999999999.9999"],
            ["-0.0001", "0.00001", "100000000000000"],
        ),
        (
            NonNegativeMoney,
            ["0", "1.2345", "99999999999999.9999"],
            ["-0.01", "0.00001", "100000000000000"],
        ),
    ],
    ids=["positive-quantity", "non-negative-quantity", "non-negative-money"],
)
def test_a_figure_finer_or_larger_than_its_column_is_refused_not_rounded(
    alias: Any, kept: list[str], refused: list[str]
) -> None:
    adapter = TypeAdapter(alias)

    for value in kept:
        assert adapter.validate_python(value) == Decimal(value)
    for value in refused:
        with pytest.raises(PydanticValidationError):
            adapter.validate_python(value)


# -----------------------------------------------------------------------------
# Guard: every request body reads a currency through the shared rule
# -----------------------------------------------------------------------------


def _models_in(annotation: Any, seen: set[type[BaseModel]]) -> Iterator[type[BaseModel]]:
    """Every model an annotation reaches: the model itself, its lines, their lines."""
    if typing.get_origin(annotation) is Annotated:
        yield from _models_in(typing.get_args(annotation)[0], seen)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation not in seen:
            seen.add(annotation)
            yield annotation
            for info in annotation.model_fields.values():
                yield from _models_in(info.annotation, seen)
        return
    for argument in typing.get_args(annotation):
        yield from _models_in(argument, seen)


def request_currency_fields() -> list[tuple[type[BaseModel], str, FieldInfo]]:
    """Each field named ``currency`` in a model some route accepts as its body."""
    seen: set[type[BaseModel]] = set()
    found: list[tuple[type[BaseModel], str, FieldInfo]] = []
    for mounted in module_routes():
        body = mounted.route.body_field
        if body is None:
            continue
        for model in _models_in(body.field_info.annotation, seen):
            found.extend(
                (model, name, info)
                for name, info in model.model_fields.items()
                if name == "currency"
            )
    return found


def _field_adapter(info: FieldInfo) -> TypeAdapter[Any]:
    """The field's own type and constraints, without the model around it."""
    if not info.metadata:
        return TypeAdapter(info.annotation)
    return TypeAdapter(Annotated[(info.annotation, *info.metadata)])  # type: ignore[valid-type]


def currency_faults(model: type[BaseModel], name: str, info: FieldInfo) -> list[str]:
    """How a currency field departs from ``CurrencyCode``, if it does."""
    label = f"{model.__module__}.{model.__name__}.{name}"
    adapter = _field_adapter(info)
    faults: list[str] = []
    try:
        kept = adapter.validate_python(" inr ")
    except PydanticValidationError:
        kept = None
    if kept != "INR":
        faults.append(f"{label} does not keep ' inr ' as 'INR'")
    try:
        adapter.validate_python("1A$")
    except PydanticValidationError:
        pass
    else:
        faults.append(f"{label} admits '1A$'")
    own = sorted(
        decorator.cls_var_name
        for decorator in model.__pydantic_decorators__.field_validators.values()
        if name in decorator.info.fields
    )
    if own:
        faults.append(f"{label} is also validated by the model's own {own}")
    return faults


def test_every_request_body_reads_a_currency_through_the_shared_rule() -> None:
    found = request_currency_fields()

    # 16 on 10 November 2026: an empty walk must not pass as a clean one.
    assert len(found) >= 16, [f"{model.__name__}.{name}" for model, name, _ in found]
    assert not [fault for entry in found for fault in currency_faults(*entry)]


def test_the_currency_check_reports_a_length_only_field_and_a_private_validator() -> None:
    class LengthOnly(BaseModel):
        currency: str = Field(min_length=3, max_length=3)

    class PrivatelyCased(BaseModel):
        currency: CurrencyCode

        @field_validator("currency")
        @classmethod
        def _lower(cls, value: str) -> str:
            return value.lower()

    class Shared(BaseModel):
        currency: CurrencyCode | None = None

    length_only = currency_faults(LengthOnly, "currency", LengthOnly.model_fields["currency"])
    privately_cased = currency_faults(
        PrivatelyCased, "currency", PrivatelyCased.model_fields["currency"]
    )

    assert [fault.split(" ", 1)[1] for fault in length_only] == [
        "does not keep ' inr ' as 'INR'",
        "admits '1A$'",
    ]
    assert [fault.split(" ", 1)[1] for fault in privately_cased] == [
        "is also validated by the model's own ['_lower']"
    ]
    assert currency_faults(Shared, "currency", Shared.model_fields["currency"]) == []


# -----------------------------------------------------------------------------
# Guard: no module declares a shared name for itself
# -----------------------------------------------------------------------------

FIELDS_MODULE = API_ROOT / "src" / "core" / "fields.py"

#: The shared type names, and ``Currency`` - the name finance gave its copy.
SHARED_NAMES = frozenset(name for name in fields.__all__ if name[0].isupper()) | {"Currency"}


def shared_name_declarations(source: str) -> list[str]:
    """Module-level assignments to a shared name; importing one is not declaring it."""
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        found.extend(
            f"line {node.lineno}: {target.id}"
            for target in targets
            if isinstance(target, ast.Name) and target.id in SHARED_NAMES
        )
    return found


def test_no_module_declares_a_shared_field_type_for_itself() -> None:
    offenders = {
        path.relative_to(API_ROOT).as_posix(): found
        for path in sorted((API_ROOT / "src").rglob("*.py"))
        if path != FIELDS_MODULE
        and (found := shared_name_declarations(path.read_text(encoding="utf-8")))
    }

    assert not offenders, (
        "Import the type from src/core/fields.py, or give a rule that really "
        f"differs a name of its own: {offenders}"
    )


def test_the_declaration_check_finds_a_copy_and_lets_an_import_or_a_new_name_pass() -> None:
    source = "\n".join(
        [
            "from src.core.fields import Reason",
            "PositiveQuantity = Annotated[Decimal, Field(gt=0)]",
            "Currency: TypeAlias = Annotated[str, Field(min_length=3)]",
            "UnscaledPositiveQuantity = Annotated[Decimal, Field(gt=0)]",
            "class Line(BaseModel):",
            "    Reason = 'a class attribute is not a module type'",
        ]
    )

    assert shared_name_declarations(source) == ["line 2: PositiveQuantity", "line 3: Currency"]


def test_the_shared_names_are_the_ones_the_module_exports() -> None:
    assert {
        "CurrencyCode",
        "LongReason",
        "NonNegativeMoney",
        "NonNegativeQuantity",
        "PositiveQuantity",
        "Reason",
    } <= SHARED_NAMES
