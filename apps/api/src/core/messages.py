"""Shared user-facing success, validation and failure messages.

The error classes in :mod:`src.core.errors` carry a default message each, and a
service composes its own sentence when the situation is specific to it - that
is right, because the service knows why it refused. What does *not* belong in
a service is a general-purpose sentence copied nineteen times: when the wording
changes, or is translated, every copy has to be found.

Rules for adding shared copy here:

* it is used by at least two modules or defines a common response outcome;
* success titles name the subject and a past-tense outcome;
* validation and failure descriptions read as clear sentences a user can act on;
* it names no internals (tables, columns, exception classes).
"""

from __future__ import annotations

from typing import Literal

# --- successes ----------------------------------------------------------------

OPERATION_COMPLETED = "Operation completed successfully"
JOB_QUEUED = "Job queued for background processing"

SuccessAction = Literal[
    "created",
    "updated",
    "saved",
    "deleted",
    "retired",
    "restored",
    "submitted",
    "approved",
    "rejected",
    "confirmed",
    "cancelled",
    "closed",
    "reopened",
    "opened",
    "released",
    "completed",
    "posted",
    "uploaded",
    "sent",
    "raised",
    "activated",
    "deactivated",
    "reset",
    "changed",
    "revoked",
    "recorded",
    "added",
    "removed",
    "received",
    "dispatched",
    "drafted",
    "quarantined",
    "expired",
    "issued",
    "allocated",
    "disputed",
    "resolved",
    "blocked",
    "reserved",
    "requested",
    "linked",
    "started",
    "enabled",
    "disabled",
    "refreshed",
    "cleared",
    "suspended",
    "reactivated",
]


def success_message(subject: str, action: SuccessAction) -> str:
    """A short outcome, matching the frontend's standard success titles.

    Name the record or operation, optionally with its document code, and use a
    past-tense verb. Workflow-specific explanations remain with their module.
    """
    return f"{subject} {action}"


# --- validation ---------------------------------------------------------------

#: The headline of a 422 that carries per-field messages under
#: ``details.fields`` - the request validator's and a router's own field-level
#: refusals read the same way.
FIELDS_NEED_ATTENTION = "Some fields need attention."
INVALID_VALUE = "Enter a valid value."
MALFORMED_JSON = "The request body must contain valid JSON."

# --- request failures ----------------------------------------------------------

RATE_LIMITED = "Too many requests. Please slow down."
SERVICE_UNAVAILABLE = "The service is temporarily unavailable. Please try again."
UNEXPECTED_ERROR = "An unexpected error occurred. Quote the request id when reporting this."

# --- updates ------------------------------------------------------------------

#: A PATCH/PUT whose body, after ``exclude_unset``, changes nothing.
NO_FIELDS_TO_UPDATE = "No fields were supplied to update."

# --- authentication -----------------------------------------------------------

CURRENT_PASSWORD_INCORRECT = "The current password is incorrect."  # noqa: S105 - a sentence
INVALID_MFA_CHALLENGE = "Invalid multi-factor challenge."
SESSION_ENDED_FOR_SECURITY = (
    "This session has been ended for security reasons. Please sign in again."
)

# --- the shift roster ---------------------------------------------------------

#: A second PLANNED window for one operator or one machine. Raised by the roster
#: service's own check, with every clash in ``details``, and by the integrity
#: handler when a race reaches the EXCLUDE constraint first.
SHIFT_ASSIGNMENT_OVERLAP = "That assignment overlaps another for the same operator or machine."

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


# --- quality ------------------------------------------------------------------


def held_for_inspection(document: str, inspections: str) -> str:
    """Refuse an owner-side verdict while the output is under a quality inspection.

    Raised by assembly and job work alike: the inspection is the one release path
    for a hold it covers, so the document's own verdict endpoint points at it
    instead of releasing the same stock a second time.
    """
    return (
        f"{document} is held for inspection {inspections}. Record the verdict on that "
        "inspection in Quality: deciding it releases the stock and completes this order, "
        "once."
    )


def verdict_differs_from_inspection(document: str, inspections: str) -> str:
    """Refuse a verdict that disagrees with the one an inspection already recorded."""
    return (
        f"Inspection {inspections} already decided {document}'s held output, and the "
        "stock has moved accordingly. A different verdict cannot be recorded over it; "
        "repeat the inspection's own figures to complete the order from it."
    )


__all__ = [
    "CURRENT_PASSWORD_INCORRECT",
    "FIELDS_NEED_ATTENTION",
    "INVALID_MFA_CHALLENGE",
    "INVALID_VALUE",
    "JOB_QUEUED",
    "MALFORMED_JSON",
    "NO_FIELDS_TO_UPDATE",
    "OPERATION_COMPLETED",
    "RATE_LIMITED",
    "SERVICE_UNAVAILABLE",
    "SESSION_ENDED_FOR_SECURITY",
    "SHIFT_ASSIGNMENT_OVERLAP",
    "UNEXPECTED_ERROR",
    "SuccessAction",
    "date_before",
    "held_for_inspection",
    "inactive_record",
    "success_message",
    "verdict_differs_from_inspection",
]
