"""PATCH omission preserves identity; explicit null is a validation error."""

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from src.core.messages import cannot_be_null
from src.core.validation import reject_null
from src.modules.customers.schemas import ContactUpdate, CustomerUpdate
from src.modules.inventory.schemas import StockTransferUpdate
from src.modules.organization.schemas import CostCentreUpdate, FinancialYearUpdate
from src.modules.production.schemas import ProductionOrderUpdate
from src.modules.users.schemas import RoleUpdate, UserUpdate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "schema,field",
    [
        (CustomerUpdate, "name"),
        (ContactUpdate, "name"),
        (RoleUpdate, "name"),
        (RoleUpdate, "is_active"),
        (RoleUpdate, "permissions"),
        (UserUpdate, "full_name"),
        (UserUpdate, "email"),
        (ProductionOrderUpdate, "planned_quantity"),
        (ProductionOrderUpdate, "warehouse_id"),
        (ProductionOrderUpdate, "planned_start_date"),
        (ProductionOrderUpdate, "planned_end_date"),
        (ProductionOrderUpdate, "priority"),
        (StockTransferUpdate, "reason"),
        (CostCentreUpdate, "cost_centre_type"),
        (FinancialYearUpdate, "start_date"),
    ],
)
def test_required_identity_rejects_explicit_null_but_allows_omission(schema, field):
    with pytest.raises(ValidationError) as error:
        schema.model_validate({field: None})
    assert error.value.errors()[0]["loc"] == (field,)
    assert "cannot be null" in error.value.errors()[0]["msg"]
    assert field not in schema().model_fields_set


@pytest.mark.parametrize(
    "schema,field",
    [
        (CustomerUpdate, "notes"),
        (ContactUpdate, "phone"),
        (UserUpdate, "phone"),
        (RoleUpdate, "description"),
        (ProductionOrderUpdate, "notes"),
        (ProductionOrderUpdate, "plant_id"),
        (ProductionOrderUpdate, "cost_centre_id"),
        # Null on the hold flag asks for the item/routing default again.
        (ProductionOrderUpdate, "requires_qc"),
        (StockTransferUpdate, "expected_date"),
        (StockTransferUpdate, "notes"),
    ],
)
def test_nullable_profile_fields_remain_clearable(schema, field):
    parsed = schema.model_validate({field: None})
    assert getattr(parsed, field) is None
    assert field in parsed.model_fields_set


def test_normal_name_and_email_normalization_is_preserved():
    user = UserUpdate(full_name="  Sam   Kumar  ", email=" SAM@EXAMPLE.COM ")
    assert user.full_name == "Sam Kumar"
    assert str(user.email) == "sam@example.com"


class _Probe(BaseModel):
    kept: int | None = None
    clearable: int | None = None

    _kept = field_validator("kept")(reject_null)


def test_reject_null_names_the_field_it_refuses():
    with pytest.raises(ValidationError) as error:
        _Probe.model_validate({"kept": None})

    [problem] = error.value.errors()
    assert problem["loc"] == ("kept",)
    assert str(problem["ctx"]["error"]) == "kept cannot be null. Omit it to leave it unchanged."


@pytest.mark.parametrize("field", ["is_active", "permissions"])
def test_a_role_update_refuses_a_null_in_the_shared_sentence(field):
    with pytest.raises(ValidationError) as error:
        RoleUpdate.model_validate({field: None})

    [problem] = error.value.errors()
    assert problem["loc"] == (field,)
    assert str(problem["ctx"]["error"]) == cannot_be_null(field)


def test_reject_null_passes_a_value_an_omission_and_other_fields_through():
    assert _Probe.model_validate({"kept": 0}).kept == 0
    assert _Probe().model_fields_set == set()
    cleared = _Probe.model_validate({"clearable": None})
    assert (cleared.clearable, cleared.model_fields_set) == (None, {"clearable"})
