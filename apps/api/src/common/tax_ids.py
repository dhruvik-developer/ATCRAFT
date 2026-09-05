"""Indian statutory identifiers: GSTIN and PAN.

One implementation, because the same number is keyed on three masters. A GSTIN
is validated on a vendor, on a customer and on the company's own branches, and
three hand-copied validators had already drifted: one refused the entity digit
``0`` (correctly), the other two accepted it, and only one verified the check
digit - so a mistyped GSTIN was rejected as a vendor and stored as a customer.
A regex fix in one file never reached the other two.

The check digit matters more than the shape. A transposed pair or a single
wrong character produces a structurally valid, non-existent GSTIN, which is
discovered only when a GST return is rejected - the most expensive place to
find a typo. Nothing here proves the number is *registered*; only the GST
portal can say that.
"""

from __future__ import annotations

import re

#: 2-digit state code, 10-character PAN, entity number, a literal Z, check digit.
#: The entity number is ``1``-``9`` then ``A``-``Z``; ``0`` is not issued.
GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"

GSTIN_LENGTH = 15
PAN_LENGTH = 10

#: Alphabet the GSTIN check digit is computed over: 0-9 then A-Z, value = index.
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Where the PAN sits inside a GSTIN. A GSTIN whose embedded PAN disagrees with
#: the PAN recorded separately means one of the two was mistyped.
_GSTIN_PAN_SLICE = slice(2, 12)

GSTIN_SHAPE_MESSAGE = (
    "GSTIN must be 15 characters: 2-digit state code, 10-character PAN, "
    "entity number, 'Z' and a check digit."
)
GSTIN_CHECK_DIGIT_MESSAGE = "GSTIN check digit is wrong; the number was mistyped."
PAN_SHAPE_MESSAGE = "PAN must be 5 letters, 4 digits and 1 letter, e.g. AAPFU0939F."


def _normalise(value: str) -> str:
    return value.strip().upper().replace(" ", "")


def gstin_check_digit(first_fourteen: str) -> str:
    """Compute the 15th character of a GSTIN from the first fourteen.

    The published algorithm: each character's alphabet index is multiplied by an
    alternating factor of 1 and 2, the quotient and remainder of that product
    over 36 are both added to a running sum, and the check digit is the
    character whose index is ``(36 - sum % 36) % 36``.
    """
    total = 0
    for position, char in enumerate(first_fourteen):
        value = _GSTIN_ALPHABET.index(char)
        product = value * (2 if position % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_ALPHABET[(36 - total % 36) % 36]


def validate_gstin(value: str) -> str:
    """Normalise and fully validate a GSTIN, check digit included.

    Raises :class:`ValueError` with a user-facing sentence, so it can be bound
    directly as a pydantic field validator.
    """
    candidate = _normalise(value)
    if not re.match(GSTIN_PATTERN, candidate):
        raise ValueError(GSTIN_SHAPE_MESSAGE)
    if gstin_check_digit(candidate[:14]) != candidate[14]:
        raise ValueError(GSTIN_CHECK_DIGIT_MESSAGE)
    return candidate


def validate_pan(value: str) -> str:
    """Normalise and shape-check a PAN. A PAN carries no check digit."""
    candidate = _normalise(value)
    if not re.match(PAN_PATTERN, candidate):
        raise ValueError(PAN_SHAPE_MESSAGE)
    return candidate


def validate_optional_gstin(value: str | None) -> str | None:
    """:func:`validate_gstin` for a nullable field.

    Blank collapses to ``None``: an empty string and NULL must not both mean
    "unregistered", or a report filtering on ``gstin IS NULL`` misses half of
    them.
    """
    if value is None:
        return None
    stripped = value.strip()
    return validate_gstin(stripped) if stripped else None


def validate_optional_pan(value: str | None) -> str | None:
    """:func:`validate_pan` for a nullable field; blank collapses to ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return validate_pan(stripped) if stripped else None


def pan_from_gstin(gstin: str) -> str:
    """The PAN embedded in a GSTIN, positions 3-12."""
    return gstin[_GSTIN_PAN_SLICE]


def state_code_from_gstin(gstin: str) -> str:
    """The GST state code a GSTIN begins with."""
    return gstin[:2]


__all__ = [
    "GSTIN_CHECK_DIGIT_MESSAGE",
    "GSTIN_LENGTH",
    "GSTIN_PATTERN",
    "GSTIN_SHAPE_MESSAGE",
    "PAN_LENGTH",
    "PAN_PATTERN",
    "PAN_SHAPE_MESSAGE",
    "gstin_check_digit",
    "pan_from_gstin",
    "state_code_from_gstin",
    "validate_gstin",
    "validate_optional_gstin",
    "validate_optional_pan",
    "validate_pan",
]
