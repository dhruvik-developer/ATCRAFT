"""A password-policy refusal lands on the control the password was typed into.

The self-service and reset flows send ``new_password``; an administrator's
create-user form sends ``password``. ``details.fields`` has to name whichever
one the request used, or the frontend's ``applyFieldErrors`` puts the message
on a field the form does not have and the user sees a submit that did nothing.
``details.requirements`` stays as the bare list for callers that render the
policy as a checklist.
"""

from __future__ import annotations

import pytest

from src.core.errors import ErrorCode, ValidationError
from src.core.security import validate_password_policy

pytestmark = pytest.mark.unit

WEAK = "short"


def test_the_default_field_is_new_password() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_password_policy(WEAK)
    details = exc.value.details
    assert exc.value.code == ErrorCode.PASSWORD_POLICY
    assert list(details["fields"]) == ["new_password"]


def test_the_caller_names_the_field_that_carried_the_password() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_password_policy(WEAK, field="password")
    assert list(exc.value.details["fields"]) == ["password"]


def test_the_field_messages_are_the_requirements_as_sentences() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_password_policy(WEAK, field="password")
    details = exc.value.details
    requirements = details["requirements"]
    assert requirements, "a five-character password must fail at least one rule"
    assert details["fields"]["password"] == [f"Password {p}." for p in requirements]
    assert exc.value.message == "Password " + "; ".join(requirements) + "."


def test_a_password_containing_the_email_is_named_against_the_same_field() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_password_policy("Person.Example#2026x", email="person@example.com")
    assert "must not contain your email address" in exc.value.details["requirements"]
    assert list(exc.value.details["fields"]) == ["new_password"]
