"""An audited reason's floor is one constant, and the refusal states it from there.

Two refusals spelled the floor out - "at least three characters" - while the
fields they guard were bound by a literal ``min_length=3`` beside them. Raise the
floor and both sentences would keep promising the old one while the API refused
on the new. ``REASON_MIN_LENGTH`` now bounds the fields and ``REASON_TOO_SHORT``
interpolates it: the frontend's ``errors.reasonTooShort``, word for word.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.constants import REASON_MIN_LENGTH
from src.core.messages import REASON_TOO_SHORT
from src.modules.dealer_portal.promotion_schemas import PromotionRetire
from src.modules.inventory.schemas import RejectionRequest

pytestmark = pytest.mark.unit


def test_the_sentence_states_the_floor_from_the_constant() -> None:
    assert f"Record at least {REASON_MIN_LENGTH} characters of reason." == REASON_TOO_SHORT
    assert "three" not in REASON_TOO_SHORT


@pytest.mark.parametrize(
    "schema,field",
    [(PromotionRetire, "reason"), (RejectionRequest, "note")],
)
def test_a_reason_that_is_padding_is_refused_in_the_shared_sentence(schema, field) -> None:
    # Long enough for the field's own bound, one character once the padding is gone.
    padded = "a".ljust(REASON_MIN_LENGTH)
    with pytest.raises(ValidationError) as error:
        schema.model_validate({field: padded})

    [problem] = error.value.errors()
    assert problem["loc"] == (field,)
    assert str(problem["ctx"]["error"]) == REASON_TOO_SHORT


@pytest.mark.parametrize(
    "schema,field",
    [(PromotionRetire, "reason"), (RejectionRequest, "note")],
)
def test_a_reason_at_the_floor_is_kept(schema, field) -> None:
    accepted = schema.model_validate({field: "x" * REASON_MIN_LENGTH})

    assert getattr(accepted, field) == "x" * REASON_MIN_LENGTH
