"""Bank identifiers: one IFSC rule, and one way of masking an account number.

Two masters carry bank details: a vendor's, which is where the company pays, and
the company's own, which is where its customers pay and which is printed on
every sales invoice. A validator copied into each of them drifts - the lesson
:mod:`src.common.tax_ids` records for GSTIN, where three hand-copied validators
had already disagreed - so both read the rule from here.

The masking exists for the audit trail. Redirecting a payee account is how
invoice-redirection fraud starts, so every edit to bank details is audited; the
audit row has to prove *that* the account changed without becoming a second,
more widely readable copy of the number itself.
"""

from __future__ import annotations

import re

#: 4-letter bank code, a reserved '0', 6-character branch code.
IFSC_PATTERN = r"^[A-Z]{4}0[A-Z0-9]{6}$"

IFSC_SHAPE_MESSAGE = "IFSC must be 4 letters, a '0', then 6 alphanumeric characters."

#: How many trailing characters of a bank account number stay visible.
ACCOUNT_NUMBER_VISIBLE_DIGITS = 4


def _normalise(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = value.strip().upper().replace(" ", "")
    return collapsed or None


def validate_optional_ifsc(value: str | None) -> str | None:
    """Upper-case an IFSC, drop its spaces, and refuse one of the wrong shape.

    Blank becomes NULL. Raises :class:`ValueError` with a user-facing sentence,
    so it binds directly as a pydantic field validator. Only the shape is
    checked: whether the branch exists is a question for the RBI's register.
    """
    normalised = _normalise(value)
    if normalised is not None and not re.match(IFSC_PATTERN, normalised):
        raise ValueError(IFSC_SHAPE_MESSAGE)
    return normalised


def mask_account_number(value: str | None) -> str | None:
    """Reduce a bank account number to its last four characters.

    Used for audit snapshots and for responses read by people who must not see
    the full number. Four or fewer characters are masked entirely: showing all
    of a short number would mask nothing.
    """
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) <= ACCOUNT_NUMBER_VISIBLE_DIGITS:
        return "*" * len(trimmed)
    return (
        "*" * (len(trimmed) - ACCOUNT_NUMBER_VISIBLE_DIGITS)
        + trimmed[-ACCOUNT_NUMBER_VISIBLE_DIGITS:]
    )


__all__ = [
    "ACCOUNT_NUMBER_VISIBLE_DIGITS",
    "IFSC_PATTERN",
    "IFSC_SHAPE_MESSAGE",
    "mask_account_number",
    "validate_optional_ifsc",
]
