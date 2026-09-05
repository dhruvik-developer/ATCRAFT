"""User-facing sentences that more than one module raises.

The error classes in :mod:`src.core.errors` carry a default message each, and a
service composes its own sentence when the situation is specific to it - that
is right, because the service knows why it refused. What does *not* belong in
a service is a general-purpose sentence copied nineteen times: when the wording
changes, or is translated, every copy has to be found.

Rules for adding one here:

* it is raised, verbatim, from at least two modules;
* it reads as a complete sentence a user can act on;
* it names no internals (tables, columns, exception classes).
"""

from __future__ import annotations

# --- validation ---------------------------------------------------------------

#: The headline of a 422 that carries per-field messages under
#: ``details.fields`` - the request validator's and a router's own field-level
#: refusals read the same way.
FIELDS_NEED_ATTENTION = "Some fields need attention."

# --- updates ------------------------------------------------------------------

#: A PATCH/PUT whose body, after ``exclude_unset``, changes nothing.
NO_FIELDS_TO_UPDATE = "No fields were supplied to update."

# --- authentication -----------------------------------------------------------

CURRENT_PASSWORD_INCORRECT = "The current password is incorrect."  # noqa: S105 - a sentence
INVALID_MFA_CHALLENGE = "Invalid multi-factor challenge."
SESSION_ENDED_FOR_SECURITY = (
    "This session has been ended for security reasons. Please sign in again."
)

# --- masters ------------------------------------------------------------------


def inactive_record(entity: str, code: str, consequence: str) -> str:
    """``"Warehouse WH-01 is inactive and cannot receive stock."``

    ``consequence`` is the verb phrase the caller was about to perform, so the
    sentence says what is refused rather than merely that the row is inactive.
    """
    return f"{entity} {code} is inactive and {consequence}."


def date_before(later: str, earlier: str) -> str:
    """``"The required date cannot be before the order date."``"""
    return f"The {later} cannot be before the {earlier}."


__all__ = [
    "CURRENT_PASSWORD_INCORRECT",
    "FIELDS_NEED_ATTENTION",
    "INVALID_MFA_CHALLENGE",
    "NO_FIELDS_TO_UPDATE",
    "SESSION_ENDED_FOR_SECURITY",
    "date_before",
    "inactive_record",
]
