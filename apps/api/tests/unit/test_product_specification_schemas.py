"""Typed descriptive values reject ambiguity before they reach PostgreSQL."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.constants import (
    MAX_SPECIFICATION_FILTERS,
    MAX_SPECIFICATION_OPTIONS,
    MAX_SPECIFICATIONS_PER_ITEM,
)
from src.core.errors import ValidationError as BusinessValidationError
from src.modules.organization.service import MasterLifecycleState
from src.modules.products.models import (
    AccessoryRelation,
    SpecificationAttribute,
    SpecificationValueType,
)
from src.modules.products.specification_schemas import (
    CategorySpecificationIn,
    CategorySpecificationsReplace,
    ItemAccessoryCreate,
    ItemAccessoryUpdate,
    ItemSpecificationsReplace,
    ItemSpecificationValueIn,
    SpecificationAttributeCreate,
    SpecificationAttributeUpdate,
    SpecificationFilterFields,
)
from src.modules.products.specification_service import (
    SPECIFICATION_LIFECYCLE,
    validate_specification_value,
)

pytestmark = pytest.mark.unit


def attribute(**changes: object) -> SpecificationAttributeCreate:
    return SpecificationAttributeCreate.model_validate(
        {"code": " fire-rated ", "name": " Fire rated ", "value_type": "BOOLEAN", **changes}
    )


def test_attribute_code_and_display_text_are_normalized() -> None:
    row = attribute()
    assert row.code == "FIRE-RATED"
    assert row.name == "Fire rated"
    assert row.options == []


@pytest.mark.parametrize("value_type", ["TEXT", "NUMBER", "BOOLEAN"])
def test_non_option_attributes_cannot_carry_options(value_type: str) -> None:
    with pytest.raises(ValidationError, match="Options must be nonempty"):
        attribute(value_type=value_type, options=["Steel"])


@pytest.mark.parametrize("options", [[], ["Steel", "Steel"], [" Steel ", "Steel"], [""]])
def test_option_master_requires_distinct_nonblank_choices(options: list[str]) -> None:
    with pytest.raises(ValidationError):
        attribute(value_type="OPTION", options=options)


def test_option_master_accepts_the_full_bounded_vocabulary() -> None:
    options = [f"Choice {index}" for index in range(MAX_SPECIFICATION_OPTIONS)]
    assert attribute(value_type="OPTION", options=options).options == options
    with pytest.raises(ValidationError):
        attribute(value_type="OPTION", options=[*options, "One too many"])


@pytest.mark.parametrize(
    "changes",
    [
        {"code": ""},
        {"code": "A" * 33},
        {"name": ""},
        {"name": "A" * 121},
        {"unit": ""},
        {"unit": "A" * 17},
        {"sort_order": -1},
        {"sort_order": 10000},
        {"value_type": "DATE"},
        {"company_id": str(uuid.uuid4())},
    ],
)
def test_master_rejects_invalid_or_client_owned_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        attribute(**changes)


@pytest.mark.parametrize(
    "field", ["name", "value_type", "options", "is_filterable", "sort_order", "is_active"]
)
def test_master_patch_cannot_clear_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        SpecificationAttributeUpdate.model_validate({field: None})


def test_master_patch_distinguishes_omission_from_clearing_the_unit() -> None:
    assert SpecificationAttributeUpdate().model_dump(exclude_unset=True) == {}
    assert SpecificationAttributeUpdate(unit=None).model_dump(exclude_unset=True) == {"unit": None}
    with pytest.raises(ValidationError):
        SpecificationAttributeUpdate(options=["Steel", "Steel"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value_text", " Brass "),
        ("value_number", "0"),
        ("value_number", "-12345678901234.5678"),
        ("value_bool", False),
        ("value_bool", True),
        ("value_option", " Mortise "),
    ],
)
def test_one_typed_value_preserves_zero_and_false(field: str, value: object) -> None:
    result = ItemSpecificationValueIn.model_validate(
        {"specification_attribute_id": uuid.uuid4(), field: value}
    )
    expected = value.strip() if isinstance(value, str) else value
    if field == "value_number":
        expected = Decimal(str(value))
    assert getattr(result, field) == expected


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"value_text": None, "value_number": None, "value_bool": None, "value_option": None},
        {"value_bool": False, "value_number": "0"},
        {"value_text": "Steel", "value_option": "Steel"},
        {"value_text": ""},
        {"value_text": "   "},
        {"value_text": "A" * 2001},
        {"value_option": " "},
        {"value_option": "A" * 65},
        {"value_number": "NaN"},
        {"value_number": "Infinity"},
        {"value_number": "-Infinity"},
        {"value_number": "100000000000000"},
        {"value_number": "1.00001"},
        {"value_bool": "false"},
        {"value_bool": 0},
        {"value_number": True},
        {"value_bool": False, "value_type": "BOOLEAN"},
        {"value_bool": False, "company_id": str(uuid.uuid4())},
    ],
)
def test_values_reject_ambiguous_invalid_or_forged_inputs(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ItemSpecificationValueIn.model_validate(
            {"specification_attribute_id": uuid.uuid4(), **values}
        )


def test_replace_sets_can_clear_values_and_templates() -> None:
    assert ItemSpecificationsReplace(values=[]).values == []
    assert CategorySpecificationsReplace(specifications=[]).specifications == []


def test_replace_sets_reject_duplicate_attribute_ids() -> None:
    attribute_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="more than once"):
        ItemSpecificationsReplace(
            values=[
                ItemSpecificationValueIn(specification_attribute_id=attribute_id, value_bool=False),
                ItemSpecificationValueIn(specification_attribute_id=attribute_id, value_bool=True),
            ]
        )
    with pytest.raises(ValidationError, match="more than once"):
        CategorySpecificationsReplace(
            specifications=[
                CategorySpecificationIn(specification_attribute_id=attribute_id),
                CategorySpecificationIn(specification_attribute_id=attribute_id, is_required=True),
            ]
        )


def test_replace_sets_bound_document_size() -> None:
    values = [
        ItemSpecificationValueIn(specification_attribute_id=uuid.uuid4(), value_bool=False)
        for _ in range(MAX_SPECIFICATIONS_PER_ITEM + 1)
    ]
    with pytest.raises(ValidationError):
        ItemSpecificationsReplace(values=values)
    with pytest.raises(ValidationError):
        CategorySpecificationsReplace(
            specifications=[
                CategorySpecificationIn(specification_attribute_id=value.specification_attribute_id)
                for value in values
            ]
        )


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity", "0.00001", "1e14"])
def test_accessory_quantity_is_positive_finite_and_representable(quantity: str) -> None:
    with pytest.raises(ValidationError):
        ItemAccessoryCreate(accessory_item_id=uuid.uuid4(), quantity=quantity)
    with pytest.raises(ValidationError):
        ItemAccessoryUpdate(quantity=quantity)


def test_accessory_defaults_and_patch_contract() -> None:
    row = ItemAccessoryCreate(accessory_item_id=uuid.uuid4())
    assert row.quantity == Decimal("1")
    assert row.relation is AccessoryRelation.OPTIONAL
    assert ItemAccessoryUpdate(notes=None).model_dump(exclude_unset=True) == {"notes": None}
    assert ItemAccessoryUpdate().model_dump(exclude_unset=True) == {}
    for field in ("quantity", "relation", "sort_order"):
        with pytest.raises(ValidationError):
            ItemAccessoryUpdate.model_validate({field: None})


@pytest.mark.parametrize(
    "changes",
    [
        {"relation": "BOM"},
        {"notes": "A" * 2001},
        {"sort_order": -1},
        {"sort_order": 10000},
        {"company_id": str(uuid.uuid4())},
    ],
)
def test_accessory_rejects_unsupported_relations_and_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ItemAccessoryCreate.model_validate({"accessory_item_id": uuid.uuid4(), **changes})


@pytest.mark.parametrize("value_type", list(SpecificationValueType))
@pytest.mark.parametrize("field", ["value_text", "value_number", "value_bool", "value_option"])
def test_the_service_matches_the_value_field_to_the_master_type(
    value_type: SpecificationValueType, field: str
) -> None:
    master = SpecificationAttribute(
        id=uuid.uuid4(),
        name="Material",
        value_type=value_type,
        options=["Steel"] if value_type is SpecificationValueType.OPTION else [],
    )
    candidate = ItemSpecificationValueIn.model_validate(
        {
            "specification_attribute_id": master.id,
            field: {
                "value_text": "Steel",
                "value_number": "0",
                "value_bool": False,
                "value_option": "Steel",
            }[field],
        }
    )
    expected = {
        SpecificationValueType.TEXT: "value_text",
        SpecificationValueType.NUMBER: "value_number",
        SpecificationValueType.BOOLEAN: "value_bool",
        SpecificationValueType.OPTION: "value_option",
    }[value_type]
    if field == expected:
        validate_specification_value(master, candidate)
    else:
        with pytest.raises(BusinessValidationError):
            validate_specification_value(master, candidate)


def test_a_well_typed_option_must_still_belong_to_the_master() -> None:
    master = SpecificationAttribute(
        id=uuid.uuid4(),
        name="Material",
        value_type=SpecificationValueType.OPTION,
        options=["Brass", "Steel"],
    )
    with pytest.raises(BusinessValidationError, match="configured option"):
        validate_specification_value(
            master,
            ItemSpecificationValueIn(specification_attribute_id=master.id, value_option="Wood"),
        )


def test_the_specification_lifecycle_is_valid_and_retirement_is_terminal() -> None:
    SPECIFICATION_LIFECYCLE.validate()
    assert SPECIFICATION_LIFECYCLE.is_terminal(MasterLifecycleState.ARCHIVED)
    assert set(SPECIFICATION_LIFECYCLE.allowed_targets(MasterLifecycleState.ACTIVE)) == {
        MasterLifecycleState.INACTIVE,
        MasterLifecycleState.ARCHIVED,
    }


def test_specification_filters_normalize_codes_but_preserve_option_case_and_empty_values() -> None:
    result = SpecificationFilterFields(
        spec=[" d5-material = Brass ", "D5-FIRE=false", "D5-FIRE=", " "]
    )
    assert result.spec == ["D5-MATERIAL=Brass", "D5-FIRE=false", "D5-FIRE=", "="]
    assert SpecificationFilterFields().spec == []


@pytest.mark.parametrize(
    "expression", ["MATERIAL", "=Brass", "A" * 33 + "=Brass", "MATERIAL=" + "A" * 65]
)
def test_malformed_or_oversized_filter_expressions_are_refused(expression: str) -> None:
    with pytest.raises(ValidationError):
        SpecificationFilterFields(spec=[expression])


def test_the_number_of_specification_filters_is_bounded() -> None:
    assert (
        len(SpecificationFilterFields(spec=["MATERIAL=Brass"] * MAX_SPECIFICATION_FILTERS).spec)
        == MAX_SPECIFICATION_FILTERS
    )
    with pytest.raises(ValidationError):
        SpecificationFilterFields(spec=["MATERIAL=Brass"] * (MAX_SPECIFICATION_FILTERS + 1))
