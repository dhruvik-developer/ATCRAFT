"""Shared column types and numeric conventions.

Master rules section 84: monetary values never touch binary floating point.
Every amount is ``Numeric`` in PostgreSQL and ``decimal.Decimal`` in Python,
with precision and scale fixed here so no module invents its own.

Scale choices
-------------
``Money``      18,4 - four places absorb per-unit rounding before a 2-place
               presentation rounding is applied at document level.
``UnitPrice``  18,6 - hardware components are priced per-thousand often enough
               that two places lose real money on a BOM roll-up.
``Quantity``   18,4 - supports grams/millimetres without float drift.
``Percentage`` 9,4  - tax rates, yields, scrap percentages.
``ConversionFactor`` 18,8 - a unit ratio (1 BOX = 12 NOS, so NOS -> BOX is
               0.08333333). Neither money nor a price: rounding it to the
               unit-price scale made ``input * factor`` disagree with the
               converted quantity by a whole unit at large quantities.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator
from sqlalchemy import CHAR, Numeric, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID


# --- Numeric aliases used by every model -------------------------------------
def _render_ip(value: Any) -> Any:
    """Render whatever asyncpg hands back for an ``INET`` column as a string.

    asyncpg returns :class:`ipaddress.IPv4Address` / ``IPv6Address`` for an
    ``INET`` column, never a string, even though the SQLAlchemy column is
    declared ``Mapped[str | None]``. A Pydantic model that declares the field as
    ``str`` and is built with ``model_validate`` therefore raises - and it raises
    at *read* time, on a row that was written perfectly well.

    That is not hypothetical: ``GET /audit/logs/{id}`` answered 500 for every
    entry recorded with an IP address, which is every entry written through the
    API, so the audit trail could be listed and never opened. The auth module had
    already met the same thing and rendered it by hand at one call site; doing it
    in the type means the next ``INET`` field cannot repeat it.
    """
    if value is None or isinstance(value, str):
        return value
    return str(value)


#: An ``INET`` column, as a string, however the driver returns it.
IpAddress = Annotated[str | None, BeforeValidator(_render_ip)]

Money = Numeric(18, 4)
UnitPrice = Numeric(18, 6)
Quantity = Numeric(18, 4)
Percentage = Numeric(9, 4)
ConversionFactor = Numeric(18, 8)

MONEY_QUANTUM = Decimal("0.01")
QUANTITY_QUANTUM = Decimal("0.0001")
UNIT_PRICE_QUANTUM = Decimal("0.000001")
FACTOR_QUANTUM = Decimal("0.00000001")

ZERO = Decimal("0")


def to_money(value: Decimal | int | str) -> Decimal:
    """Round a value to presentation money scale using banker-free HALF_UP.

    HALF_UP is the convention Indian GST invoicing expects; ROUND_HALF_EVEN
    would make invoice totals disagree with customer expectations.
    """
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def to_quantity(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_UP)


def to_unit_price(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(UNIT_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def to_factor(value: Decimal | int | str) -> Decimal:
    """Round a unit-conversion ratio to the scale its column stores."""
    return Decimal(value).quantize(FACTOR_QUANTUM, rounding=ROUND_HALF_UP)


class GUID(TypeDecorator[uuid.UUID]):
    """UUID column that stays a real ``uuid.UUID`` in Python.

    PostgreSQL native ``uuid`` is used everywhere; the CHAR(32) fallback exists
    only so unit tests can run the same models on SQLite without a container.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return value.hex

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class CIText(TypeDecorator[str]):
    """Case-insensitive text stored lower-cased.

    Used for e-mail and login handles so ``Foo@x.com`` and ``foo@x.com`` cannot
    become two accounts. Normalising on write keeps the unique index simple.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


# JSONB is used for audit payloads, QC parameter captures and MRP result blobs.
JSONBType = JSONB(astext_type=String())

__all__ = [
    "FACTOR_QUANTUM",
    "GUID",
    "MONEY_QUANTUM",
    "QUANTITY_QUANTUM",
    "UNIT_PRICE_QUANTUM",
    "ZERO",
    "CIText",
    "ConversionFactor",
    "JSONBType",
    "Money",
    "Percentage",
    "Quantity",
    "UnitPrice",
    "to_factor",
    "to_money",
    "to_quantity",
    "to_unit_price",
]
