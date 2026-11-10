"""Request field types every module's schemas share.

Each of these was declared module by module - ``Reason`` in fourteen schema
files, ``PositiveQuantity`` in thirteen - and a rule changed in one copy had to
be found in the others. The currency code had already drifted eight ways:
procurement, sales and finance matched ``^[A-Z]{3}$`` and refused ``inr``,
organisation upper-cased it, products and vendors checked only its length and
stored ``1A$`` on a price list or a vendor master, and collections refused a bad
code as a business rule where finance answered a field error.

A module whose rule genuinely differs keeps its own type under its own name;
one that means the shared rule imports it from here.
``tests/unit/test_core_fields.py`` refuses a module that declares one of these
names for itself.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field

from src.core.constants import CURRENCY_CODE_LENGTH, REASON_MIN_LENGTH
from src.core.messages import CURRENCY_CODE_INVALID


def currency_code(value: object) -> object:
    """``" inr "`` -> ``"INR"``; anything but three letters is refused.

    ASSUMPTION: a currency is a code, not a spelling - case and surrounding
    space are not part of it, so ``inr`` and ``INR`` cannot become two books.
    Anything that is not three ASCII letters is refused outright: a code that
    matches no row reports a party with open items as owing nothing. A value
    that is not a string passes through for pydantic's own type error.
    docs/assumptions.md §INPUT-2.
    """
    if not isinstance(value, str):
        return value
    code = value.strip().upper()
    if len(code) != CURRENCY_CODE_LENGTH or not (code.isascii() and code.isalpha()):
        raise ValueError(CURRENCY_CODE_INVALID)
    return code


#: An ISO 4217 alphabetic code, normalised to upper case on the way in.
#:
#: The ``Field`` is listed before the ``BeforeValidator`` on purpose. In this
#: order the validator wraps the constrained string, so ``inr`` is normalised
#: before the pattern is checked and the pattern stays in the published schema.
#: Listed the other way round, pydantic drops ``pattern`` from the JSON schema,
#: and the OpenAPI contract stops stating the rule the request is held to.
CurrencyCode = Annotated[
    str,
    Field(
        min_length=CURRENCY_CODE_LENGTH,
        max_length=CURRENCY_CODE_LENGTH,
        pattern=r"^[A-Z]{3}$",
    ),
    BeforeValidator(currency_code),
]

#: An audited reason - a cancellation's, a rejection's, a retirement's.
Reason = Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=255)]
#: A reason that may run longer: finance's vendor-invoice and payment decisions
#: and a dealer's request to cancel an order. Their columns are ``TEXT``; 500 is
#: the request's own bound.
LongReason = Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=500)]

#: A quantity at the ``Numeric(18,4)`` scale every quantity column has. The
#: scale is bounded here so a request for 0.00004 is a 422, not a value
#: ``to_quantity`` rounds to zero for a CHECK constraint to refuse.
PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]

#: A money amount at the ``Numeric(18,4)`` scale of every money column.
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]


__all__ = [
    "CurrencyCode",
    "LongReason",
    "NonNegativeMoney",
    "NonNegativeQuantity",
    "PositiveQuantity",
    "Reason",
    "currency_code",
]
