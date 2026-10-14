"""PATCH omission preserves identity; explicit null is a validation error."""

import pytest
from pydantic import ValidationError

from src.modules.customers.schemas import ContactUpdate, CustomerUpdate
from src.modules.users.schemas import RoleUpdate, UserUpdate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "schema,field",
    [
        (CustomerUpdate, "name"),
        (ContactUpdate, "name"),
        (RoleUpdate, "name"),
        (UserUpdate, "full_name"),
        (UserUpdate, "email"),
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
