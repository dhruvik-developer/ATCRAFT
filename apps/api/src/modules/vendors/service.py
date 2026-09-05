"""Business rules for vendor and transporter master data (sections 15, 16).

Rules enforced here, and why each one exists:

* **A code is never reused.** It is quoted on purchase orders and in the vendor
  portal, so a second vendor answering to ``VEND-001`` makes the archive
  ambiguous.
* **A GSTIN identifies one legal entity.** Two live vendors sharing one is a
  duplicate master record, and it produces two payment trails for one supplier.
* **Approval is a separate, permissioned act**, recorded with who and when, and
  by default it cannot be performed by the person who created the record.
* **Retirement is soft and conditional.** A vendor with open purchase orders,
  live item sourcing links, or staff who can still sign in to the portal cannot
  be retired - each of those would leave a live reference to a record the
  business believes is closed.
* **Every status change goes through a state machine** and is written to the
  audit log in the same transaction as the change itself.
* **Bank details are audited with the account number masked.** A diverted
  payment starts with a quiet edit to those fields; the audit row must prove the
  change happened without becoming a second copy of the account number.

The rating engine is deliberately split in two: a pure function over a typed
input, which is where the arithmetic lives and where the unit tests point, and
an adapter that assembles that input from goods receipts and QC inspections.
Quality is still being built and procurement may be absent from a database built
one module at a time, so the adapter probes for every source with ``to_regclass``
and degrades to "insufficient data" instead of raising.
"""

from __future__ import annotations

import re
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.database import affected_rows, transaction
from src.core.deps import TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.messages import NO_FIELDS_TO_UPDATE
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.vendors.models import (
    DEFAULT_RATING_WINDOW_DAYS,
    MAX_VENDOR_RATING,
    Transporter,
    TransporterType,
    Vendor,
    VendorCategory,
    VendorContact,
    VendorType,
)
from src.modules.vendors.repository import (
    TransporterRepository,
    VendorCategoryRepository,
    VendorContactRepository,
    VendorRepository,
    columns_present,
    table_exists,
)
from src.modules.vendors.schemas import (
    TRACKING_PLACEHOLDER,
    TrackingLinkRead,
    TransporterCreate,
    TransporterFilters,
    TransporterOption,
    TransporterRead,
    TransporterUpdate,
    VendorCategoryAssign,
    VendorCategoryFilters,
    VendorCategoryRead,
    VendorCategoryUpdate,
    VendorContactCreate,
    VendorContactFilters,
    VendorContactRead,
    VendorContactUpdate,
    VendorCreate,
    VendorFilters,
    VendorOption,
    VendorRatingRead,
    VendorRead,
    VendorUpdate,
    assert_gstin_pan_agree,
    assert_gstin_state_agrees,
)

log = get_logger(__name__)

AUDIT_ENTITY_VENDOR = "vendor"
AUDIT_ENTITY_CONTACT = "vendor_contact"
AUDIT_ENTITY_CATEGORY = "vendor_category"
AUDIT_ENTITY_TRANSPORTER = "transporter"

# ASSUMPTION: a vendor may not be approved by the person who created the record.
# Vendor approval is what unlocks purchase orders and payments to a bank account
# the same person entered, so maker-checker is the conservative default. It is a
# constant rather than a hard-coded condition because a very small company may
# legitimately have one buyer, and turning it off must be a deliberate,
# reviewable edit rather than a code change scattered through the service.
REQUIRE_SEPARATE_APPROVER = True

# --- rating engine tuning ----------------------------------------------------

# ``DEFAULT_RATING_WINDOW_DAYS`` and the window bounds live in ``models`` beside
# ``MAX_VENDOR_RATING``, where the request schema and the router read them too;
# the service re-exports the default for the callers that import it from here.

# ASSUMPTION: fewer than this many datable receipts in the window is not enough
# evidence to publish a score. A vendor rated 1.0 because their single delivery
# was a day late would be excluded from sourcing on noise.
MIN_RECEIPTS_FOR_RATING = 5

# ASSUMPTION: on-time delivery is weighted 60% and quality 40%. Both matter, and
# late-but-perfect is a schedule problem the plant can absorb, while
# on-time-but-rejected stops a line. Named constants so the balance can be
# retuned in one place once real data exists to justify a different split.
DELIVERY_WEIGHT = Decimal("0.60")
QUALITY_WEIGHT = Decimal("0.40")

_PERCENT_QUANTUM = Decimal("0.01")
_RATING_QUANTUM = Decimal("0.01")
_HUNDRED = Decimal("100")

# Tables the rating adapter reads once the owning modules exist, with the
# columns it needs from each. Probed at runtime (see :func:`load_performance`)
# so a schema that has not been created yet - or has moved on - degrades to
# "insufficient data" rather than raising UndefinedTable/UndefinedColumn.
#
# The procurement names below are the ones that module actually shipped. Quality
# is still being built, so its names remain an ASSUMPTION - which is why the
# adapter verifies every column before relying on it and logs a warning when the
# assumption does not hold, rather than rating nobody in silence.
_GOODS_RECEIPTS_TABLE = "goods_receipts"
_GOODS_RECEIPTS_COLUMNS = ("company_id", "vendor_id", "purchase_order_id", "receipt_date", "status")
#: The date the vendor promised is recorded once, on the order; a receipt only
#: says when the goods turned up. Delivery performance is therefore the two read
#: together, and is unmeasurable while either side is missing.
_PURCHASE_ORDERS_TABLE = "purchase_orders"
_PURCHASE_ORDERS_COLUMNS = ("id", "expected_delivery_date")
_QC_INSPECTIONS_TABLE = "qc_inspections"
_QC_INSPECTIONS_COLUMNS = (
    "company_id",
    "vendor_id",
    "inspected_at",
    "inspected_quantity",
    "rejected_quantity",
)

#: Fields captured before and after an edit so the audit entry records exactly
#: what changed. Explicit rather than "every column": ``created_at`` and the
#: actor columns are not business changes.
_VENDOR_AUDITED_FIELDS = [
    "code",
    "name",
    "legal_name",
    "vendor_types",
    "gstin",
    "pan",
    "msme_number",
    "payment_terms_days",
    "credit_limit",
    "lead_time_days",
    "currency",
    "bank_name",
    "bank_branch",
    "bank_account_name",
    "bank_account_number",
    "bank_ifsc",
    "bank_swift",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "contact_person",
    "email",
    "phone",
    "mobile",
    "website",
    "is_active",
    "is_approved",
    "notes",
]

_CONTACT_AUDITED_FIELDS = [
    "vendor_id",
    "name",
    "designation",
    "contact_role",
    "email",
    "phone",
    "mobile",
    "is_primary",
    "is_active",
    "notes",
]

_CATEGORY_AUDITED_FIELDS = ["vendor_id", "item_category_id", "is_preferred", "notes"]

_TRANSPORTER_AUDITED_FIELDS = [
    "code",
    "name",
    "transporter_type",
    "gstin",
    "pan",
    "contact_person",
    "email",
    "phone",
    "mobile",
    "website",
    "address_line1",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "tracking_url_template",
    "is_active",
    "notes",
]

#: How many trailing digits of a bank account number stay visible.
ACCOUNT_NUMBER_VISIBLE_DIGITS = 4


# -----------------------------------------------------------------------------
# Pure helpers - unit tested without a database
# -----------------------------------------------------------------------------


def mask_account_number(value: str | None) -> str | None:
    """Reduce a bank account number to its last four characters.

    Used for both the API response and the audit snapshot. The audit trail must
    prove *that* the payee account changed - the detection signal for a diverted
    payment - without becoming a second, more widely readable copy of the number
    itself.
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


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot remove this, something still points at it" refusal.

    One constructor so every such refusal carries the same machine-readable code
    and the frontend can react to all of them uniformly.
    """
    return BusinessRuleError(
        message, code=ErrorCode.REFERENCED_RECORD_IN_USE, details=details or None
    )


def build_tracking_url(template: str | None, tracking_id: str) -> str | None:
    """Resolve a carrier's tracking template against one consignment number.

    The tracking id is percent-encoded before substitution. Carrier consignment
    numbers arrive by e-mail and phone and regularly contain slashes and spaces;
    pasting one into a URL unencoded produces either a broken link or, with a
    hostile value, an injected parameter.
    """
    if not template:
        return None
    cleaned = tracking_id.strip()
    if not cleaned:
        return None
    return template.replace(TRACKING_PLACEHOLDER, urllib.parse.quote(cleaned, safe=""))


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class VendorState(StrEnum):
    """Lifecycle of a vendor master record.

    There is no ``status`` column: the state is derived from ``is_approved``,
    ``is_active`` and ``deleted_at``, which are the three flags procurement
    already reads. A fourth, redundant column could disagree with them. Routing
    the transitions through a :class:`StateMachine` still gives one declared,
    testable place for the legal moves (section 18).
    """

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    #: Deactivated but not retired: existing orders may still be received and
    #: paid, no new order may be raised.
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


class TransporterState(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


VENDOR_LIFECYCLE: StateMachine[VendorState] = StateMachine(
    entity="Vendor",
    initial=VendorState.PENDING_APPROVAL,
    transitions=(
        Transition(
            VendorState.PENDING_APPROVAL,
            VendorState.APPROVED,
            permission=Permission.VENDOR_UPDATE,
            label="Approve",
        ),
        # Withdrawing approval is not a deletion: history stays, but no new
        # purchase order can be raised until someone approves again.
        Transition(
            VendorState.APPROVED,
            VendorState.PENDING_APPROVAL,
            permission=Permission.VENDOR_UPDATE,
            label="Withdraw approval",
        ),
        Transition(
            VendorState.PENDING_APPROVAL,
            VendorState.SUSPENDED,
            permission=Permission.VENDOR_UPDATE,
            label="Deactivate",
        ),
        Transition(
            VendorState.APPROVED,
            VendorState.SUSPENDED,
            permission=Permission.VENDOR_UPDATE,
            label="Deactivate",
        ),
        # Reactivation returns the vendor to whichever approval state it held,
        # so switching a vendor off and on again cannot silently approve it.
        Transition(
            VendorState.SUSPENDED,
            VendorState.PENDING_APPROVAL,
            permission=Permission.VENDOR_UPDATE,
            label="Reactivate",
        ),
        Transition(
            VendorState.SUSPENDED,
            VendorState.APPROVED,
            permission=Permission.VENDOR_UPDATE,
            label="Reactivate",
        ),
        Transition(
            VendorState.PENDING_APPROVAL,
            VendorState.ARCHIVED,
            permission=Permission.VENDOR_DELETE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            VendorState.APPROVED,
            VendorState.ARCHIVED,
            permission=Permission.VENDOR_DELETE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            VendorState.SUSPENDED,
            VendorState.ARCHIVED,
            permission=Permission.VENDOR_DELETE,
            label="Retire",
            is_irreversible=True,
        ),
        # A restored vendor comes back suspended and unapproved-by-default:
        # somebody must look at it and approve it again before it can take
        # orders. Restoring straight to APPROVED would let a retirement be used
        # to launder an approval.
        Transition(
            VendorState.ARCHIVED,
            VendorState.SUSPENDED,
            permission=Permission.VENDOR_UPDATE,
            label="Restore",
        ),
    ),
)

TRANSPORTER_LIFECYCLE: StateMachine[TransporterState] = StateMachine(
    entity="Transporter",
    initial=TransporterState.ACTIVE,
    transitions=(
        Transition(
            TransporterState.ACTIVE,
            TransporterState.INACTIVE,
            permission=Permission.TRANSPORTER_MANAGE,
            label="Deactivate",
        ),
        Transition(
            TransporterState.INACTIVE,
            TransporterState.ACTIVE,
            permission=Permission.TRANSPORTER_MANAGE,
            label="Activate",
        ),
        Transition(
            TransporterState.ACTIVE,
            TransporterState.ARCHIVED,
            permission=Permission.TRANSPORTER_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            TransporterState.INACTIVE,
            TransporterState.ARCHIVED,
            permission=Permission.TRANSPORTER_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            TransporterState.ARCHIVED,
            TransporterState.INACTIVE,
            permission=Permission.TRANSPORTER_MANAGE,
            label="Restore",
        ),
    ),
)


def vendor_state(vendor: Vendor) -> VendorState:
    """Derive the lifecycle state of a vendor from its flags.

    Order matters: retirement wins over deactivation, which wins over approval,
    so a retired row that still carries ``is_approved=True`` from before the
    retirement never reads as usable.
    """
    if vendor.deleted_at is not None:
        return VendorState.ARCHIVED
    if not vendor.is_active:
        return VendorState.SUSPENDED
    return VendorState.APPROVED if vendor.is_approved else VendorState.PENDING_APPROVAL


def transporter_state(transporter: Transporter) -> TransporterState:
    if transporter.deleted_at is not None:
        return TransporterState.ARCHIVED
    return TransporterState.ACTIVE if transporter.is_active else TransporterState.INACTIVE


def can_receive_purchase_orders(vendor: Vendor) -> bool:
    """The single predicate procurement should ask before raising an order.

    Exported so no other module re-derives "approved and not switched off" and
    gets one of the three flags wrong.
    """
    return vendor_state(vendor) is VendorState.APPROVED


# -----------------------------------------------------------------------------
# Rating engine - pure calculation
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VendorPerformanceWindow:
    """Measured vendor performance over one window.

    A typed input rather than loose keyword arguments so the arithmetic can be
    unit tested against hand-computed numbers with no database, and so the
    adapter that eventually reads goods receipts and QC inspections has one
    obvious shape to produce.

    ``receipt_count`` counts only receipts against a *promised* date. A delivery
    with no expected date cannot be late, and including it would inflate every
    vendor's on-time percentage with receipts nobody measured.
    """

    vendor_id: uuid.UUID
    window_days: int
    receipt_count: int
    on_time_receipt_count: int
    inspected_quantity: Decimal
    rejected_quantity: Decimal

    def __post_init__(self) -> None:
        if self.window_days <= 0:
            raise ValueError("window_days must be positive")
        if self.receipt_count < 0 or self.on_time_receipt_count < 0:
            raise ValueError("receipt counts cannot be negative")
        if self.on_time_receipt_count > self.receipt_count:
            raise ValueError("on-time receipts cannot exceed total receipts")
        if self.inspected_quantity < 0 or self.rejected_quantity < 0:
            raise ValueError("inspected and rejected quantities cannot be negative")
        if self.rejected_quantity > self.inspected_quantity:
            raise ValueError("rejected quantity cannot exceed inspected quantity")


@dataclass(frozen=True, slots=True)
class VendorRatingResult:
    """Outcome of a rating computation.

    ``rating`` is ``None`` when the window held too little evidence. That is a
    different statement from a low score, and callers must not collapse the two:
    an unrated vendor is unknown, not bad.
    """

    vendor_id: uuid.UUID
    rating: Decimal | None
    on_time_percentage: Decimal | None
    rejection_percentage: Decimal | None
    receipt_count: int
    window_days: int
    has_quality_evidence: bool
    is_sufficient: bool
    reason: str


def _percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator * _HUNDRED).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def compute_vendor_rating(window: VendorPerformanceWindow) -> VendorRatingResult:
    """Score a vendor from 0 to 5 over one measurement window.

    ``rating = (delivery_share x on_time% + quality_share x good%) / 100 x 5``

    where ``good% = 100 - rejection%``. Both components are already percentages,
    so the weighted sum is a percentage too and the scale conversion is a single
    multiplication - no magic normalising constants.

    ASSUMPTION: when the window contains no inspected quantity at all, the
    quality term is dropped and delivery takes the full weight, rather than the
    vendor being credited with a perfect quality score they have not earned.
    Many purchased items are never QC-inspected, so requiring quality evidence
    would leave most vendors permanently unrated. ``has_quality_evidence`` on the
    result says which of the two formulas was used, so a report can distinguish
    "3.9 on delivery and quality" from "3.9 on delivery alone".

    All arithmetic is :class:`~decimal.Decimal`; a float here would make two
    identical windows occasionally produce different stored ratings (§84).
    """
    if window.receipt_count < MIN_RECEIPTS_FOR_RATING:
        return VendorRatingResult(
            vendor_id=window.vendor_id,
            rating=None,
            on_time_percentage=None,
            rejection_percentage=None,
            receipt_count=window.receipt_count,
            window_days=window.window_days,
            has_quality_evidence=window.inspected_quantity > 0,
            is_sufficient=False,
            reason=(
                f"Only {window.receipt_count} measurable receipt(s) in the last "
                f"{window.window_days} days; at least {MIN_RECEIPTS_FOR_RATING} are needed."
            ),
        )

    on_time_percentage = _percentage(
        Decimal(window.on_time_receipt_count), Decimal(window.receipt_count)
    )

    has_quality_evidence = window.inspected_quantity > 0
    if has_quality_evidence:
        rejection_percentage = _percentage(window.rejected_quantity, window.inspected_quantity)
        weighted = DELIVERY_WEIGHT * on_time_percentage + QUALITY_WEIGHT * (
            _HUNDRED - rejection_percentage
        )
    else:
        rejection_percentage = None
        weighted = on_time_percentage

    rating = (weighted / _HUNDRED * Decimal(MAX_VENDOR_RATING)).quantize(
        _RATING_QUANTUM, rounding=ROUND_HALF_UP
    )
    # Clamp defensively: the inputs are validated, but a future re-weighting
    # that does not sum to 1 must not be able to store a 5.4 the check
    # constraint would then reject at flush time.
    rating = min(max(rating, Decimal("0")), Decimal(MAX_VENDOR_RATING))

    return VendorRatingResult(
        vendor_id=window.vendor_id,
        rating=rating,
        on_time_percentage=on_time_percentage,
        rejection_percentage=rejection_percentage,
        receipt_count=window.receipt_count,
        window_days=window.window_days,
        has_quality_evidence=has_quality_evidence,
        is_sufficient=True,
        reason=(
            "Rated on delivery and quality"
            if has_quality_evidence
            else "Rated on delivery only; no QC inspections in the window"
        ),
    )


def insufficient_result(vendor_id: uuid.UUID, window_days: int, reason: str) -> VendorRatingResult:
    """A rating result meaning "we could not measure this"."""
    return VendorRatingResult(
        vendor_id=vendor_id,
        rating=None,
        on_time_percentage=None,
        rejection_percentage=None,
        receipt_count=0,
        window_days=window_days,
        has_quality_evidence=False,
        is_sufficient=False,
        reason=reason,
    )


# -----------------------------------------------------------------------------
# Rating engine - adapter over goods receipts and QC inspections
# -----------------------------------------------------------------------------


async def load_performance(
    session: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    window_days: int = DEFAULT_RATING_WINDOW_DAYS,
) -> VendorPerformanceWindow | None:
    """Assemble a performance window from goods receipts and QC inspections.

    This is the seam between the vendors module and procurement/quality. Quality
    does not exist yet, so every source is probed with ``to_regclass`` *and* for
    the columns this function reads: a missing table returns ``None``, which the
    caller renders as "insufficient data", instead of an UndefinedTable error
    reaching the request. A schema that exists but has moved on is logged, so
    the mismatch is noticed rather than silently producing unrated vendors
    forever.

    Deliberately raw SQL rather than an import of the procurement models: master
    data sits *below* procurement in the dependency order, and inverting that
    would make the two modules impossible to build or test independently.

    The delivery half joins receipts to their orders because that is where the
    promised date lives; a receipt whose order never promised a date is counted
    in neither figure, since a delivery nobody promised cannot be late.
    """
    since = utcnow() - timedelta(days=window_days)

    for table, columns in (
        (_GOODS_RECEIPTS_TABLE, _GOODS_RECEIPTS_COLUMNS),
        (_PURCHASE_ORDERS_TABLE, _PURCHASE_ORDERS_COLUMNS),
    ):
        if not await table_exists(session, table):
            return None
        if not await columns_present(session, table, columns):
            log.warning(
                "vendors.rating_source_schema_mismatch",
                table=table,
                expected_columns=list(columns),
            )
            return None

    delivery = (
        await session.execute(
            text(
                # count(*) FILTER is one pass over the same rows; two separate
                # aggregate queries would read the index twice for no benefit.
                "SELECT "  # noqa: S608 - table and columns are module constants
                "  count(*) FILTER (WHERE po.expected_delivery_date IS NOT NULL) AS measurable, "
                "  count(*) FILTER (WHERE po.expected_delivery_date IS NOT NULL "
                "                     AND gr.receipt_date <= po.expected_delivery_date) "
                "    AS on_time "
                f"FROM {_GOODS_RECEIPTS_TABLE} AS gr "
                # LEFT JOIN, not INNER: a receipt whose order has gone is still a
                # delivery, just an unmeasurable one, and dropping it silently
                # would flatter the vendor's on-time percentage.
                f"LEFT JOIN {_PURCHASE_ORDERS_TABLE} AS po ON po.id = gr.purchase_order_id "
                "WHERE gr.company_id = :company_id AND gr.vendor_id = :vendor_id "
                "  AND gr.receipt_date >= :since AND gr.status <> 'CANCELLED'"
            ),
            {
                "company_id": tenant.company_id,
                "vendor_id": vendor_id,
                "since": since.date(),
            },
        )
    ).one()

    inspected = Decimal("0")
    rejected = Decimal("0")
    if await table_exists(session, _QC_INSPECTIONS_TABLE) and await columns_present(
        session, _QC_INSPECTIONS_TABLE, _QC_INSPECTIONS_COLUMNS
    ):
        quality = (
            await session.execute(
                text(
                    "SELECT "  # noqa: S608 - table and columns are module constants
                    "  COALESCE(sum(inspected_quantity), 0) AS inspected, "
                    "  COALESCE(sum(rejected_quantity), 0) AS rejected "
                    f"FROM {_QC_INSPECTIONS_TABLE} "
                    "WHERE company_id = :company_id AND vendor_id = :vendor_id "
                    "  AND inspected_at >= :since"
                ),
                {"company_id": tenant.company_id, "vendor_id": vendor_id, "since": since},
            )
        ).one()
        inspected = Decimal(str(quality.inspected))
        rejected = Decimal(str(quality.rejected))
        # Defend the invariant the dataclass asserts: a quality module that has
        # recorded a rejection larger than the inspected quantity is telling us
        # something is wrong there, and a 500 from this endpoint is the least
        # useful way to surface it.
        rejected = min(rejected, inspected)

    return VendorPerformanceWindow(
        vendor_id=vendor_id,
        window_days=window_days,
        receipt_count=int(delivery.measurable),
        on_time_receipt_count=int(delivery.on_time),
        inspected_quantity=inspected,
        rejected_quantity=rejected,
    )


async def apply_vendor_rating(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    window: VendorPerformanceWindow,
) -> VendorRatingResult:
    """Compute a rating from a supplied window and store it, with an audit row.

    The cross-module entry point: quality and procurement can call this after a
    receipt or an inspection without going through the HTTP surface. It takes a
    :class:`VendorPerformanceWindow` rather than reading the tables itself so a
    caller that already has the numbers does not pay for them twice.

    An insufficient window leaves the stored rating untouched rather than
    nulling it. Discarding a measured score because the *latest* window happens
    to be empty would throw away information; the caller can see
    ``is_sufficient`` and decide how to present staleness.
    """
    result = compute_vendor_rating(window)

    async with transaction(session):
        repo = VendorRepository(session, tenant)
        vendor = await repo.get_or_404(window.vendor_id)

        if result.rating is None:
            return result

        previous = vendor.rating
        vendor.rating = result.rating
        vendor.rating_updated_at = utcnow()
        vendor.rating_sample_size = result.receipt_count
        repo.touch(vendor)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ENTITY_VENDOR,
            entity_id=vendor.id,
            entity_code=vendor.code,
            summary=f"Vendor {vendor.code} rated {result.rating}/{MAX_VENDOR_RATING}",
            user_id=tenant.user_id,
            old_values={"rating": previous},
            new_values={"rating": result.rating},
            context={
                "window_days": result.window_days,
                "receipt_count": result.receipt_count,
                "on_time_percentage": result.on_time_percentage,
                "rejection_percentage": result.rejection_percentage,
                "has_quality_evidence": result.has_quality_evidence,
            },
        )

    return result


# -----------------------------------------------------------------------------
# Vendor service
# -----------------------------------------------------------------------------


class VendorService:
    """Vendor master-data operations. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = VendorRepository(session, tenant)
        self.contacts = VendorContactRepository(session, tenant)
        self.categories = VendorCategoryRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_vendors(
        self, filters: VendorFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[VendorRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)

        # Two extra queries for the whole page rather than two per row.
        ids = [row.id for row in rows]
        contact_counts = await self.repo.contact_counts(ids)
        category_counts = await self.repo.category_counts(ids)

        items = [
            self._to_read(row, contact_counts.get(row.id, 0), category_counts.get(row.id, 0))
            for row in rows
        ]
        return items, pagination

    async def get_vendor(self, vendor_id: uuid.UUID) -> VendorRead:
        vendor = await self.repo.get_or_404(vendor_id)
        contact_counts = await self.repo.contact_counts([vendor.id])
        category_counts = await self.repo.category_counts([vendor.id])
        return self._to_read(
            vendor, contact_counts.get(vendor.id, 0), category_counts.get(vendor.id, 0)
        )

    async def list_options(
        self, vendor_type: VendorType | None, *, approved_only: bool = True
    ) -> list[VendorOption]:
        rows = await self.repo.list_options(
            vendor_type=str(vendor_type) if vendor_type else None,
            approved_only=approved_only,
        )
        return [VendorOption.model_validate(row) for row in rows]

    # --- writes -------------------------------------------------------------

    async def create_vendor(self, payload: VendorCreate) -> VendorRead:
        async with transaction(self.session):
            await self._assert_code_free(payload.code)
            if payload.gstin and await self.repo.gstin_taken(payload.gstin):
                raise DuplicateError(
                    f"GSTIN {payload.gstin} is already registered to another vendor.",
                    details={"gstin": payload.gstin},
                )

            vendor = Vendor(
                **payload.model_dump(exclude={"vendor_types"}),
                vendor_types=[str(t) for t in payload.vendor_types],
            )
            self.repo.add(vendor)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                summary=f"Created vendor {vendor.code} ({vendor.name})",
                user_id=self.tenant.user_id,
                new_values=self._audit_snapshot(vendor),
            )

        return self._to_read(vendor, 0, 0)

    async def update_vendor(self, vendor_id: uuid.UUID, payload: VendorUpdate) -> VendorRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            vendor = await self.repo.get_or_404(vendor_id)
            before = self._audit_snapshot(vendor)

            # The cross-field statutory checks have to be re-run against the
            # *merged* record: a PATCH that changes only the PAN is internally
            # consistent on its own and still contradicts the stored GSTIN.
            merged_gstin = changes.get("gstin", vendor.gstin)
            merged_pan = changes.get("pan", vendor.pan)
            merged_state_code = changes.get("state_code", vendor.state_code)
            try:
                assert_gstin_pan_agree(merged_gstin, merged_pan)
                assert_gstin_state_agrees(merged_gstin, merged_state_code)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            if (
                merged_gstin
                and merged_gstin != vendor.gstin
                and await self.repo.gstin_taken(merged_gstin, exclude_id=vendor.id)
            ):
                raise DuplicateError(
                    f"GSTIN {merged_gstin} is already registered to another vendor.",
                    details={"gstin": merged_gstin},
                )

            if "vendor_types" in changes and changes["vendor_types"] is not None:
                changes["vendor_types"] = [str(t) for t in changes["vendor_types"]]

            bank_changed = self._bank_fields_changed(vendor, changes)

            for field, value in changes.items():
                setattr(vendor, field, value)
            self.repo.touch(vendor)
            await self.session.flush()

            if bank_changed:
                self._log_bank_details_changed(vendor)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                summary=f"Updated vendor {vendor.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=self._audit_snapshot(vendor),
            )

        return await self.get_vendor(vendor.id)

    async def approve_vendor(self, vendor_id: uuid.UUID, *, reason: str) -> VendorRead:
        """Approve a vendor so purchase orders may be raised on it."""
        async with transaction(self.session):
            vendor = await self.repo.get_or_404(vendor_id)
            current = vendor_state(vendor)
            VENDOR_LIFECYCLE.assert_transition(current, VendorState.APPROVED)

            if REQUIRE_SEPARATE_APPROVER and vendor.created_by_id == self.tenant.user_id:
                raise AuthorizationError(
                    "A vendor cannot be approved by the person who created it. "
                    "Ask a second approver to review the record.",
                    code=ErrorCode.APPROVAL_REQUIRED,
                )

            # A vendor with no way to be paid or invoiced correctly should not
            # be approved: the gap surfaces later as a blocked payment run.
            self._assert_approvable(vendor)

            vendor.is_approved = True
            vendor.approved_by_id = self.tenant.user_id
            vendor.approved_at = utcnow()
            self.repo.touch(vendor)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                from_status=str(current),
                to_status=str(VendorState.APPROVED),
                user_id=self.tenant.user_id,
                action=AuditAction.APPROVE,
                reason=reason,
            )

        return await self.get_vendor(vendor.id)

    async def withdraw_approval(self, vendor_id: uuid.UUID, *, reason: str) -> VendorRead:
        """Withdraw approval. Existing orders stand; no new one may be raised."""
        async with transaction(self.session):
            vendor = await self.repo.get_or_404(vendor_id)
            current = vendor_state(vendor)
            VENDOR_LIFECYCLE.assert_transition(current, VendorState.PENDING_APPROVAL)

            vendor.is_approved = False
            # approved_by_id/approved_at are cleared together: leaving a stale
            # approver on an unapproved record makes the audit read as though
            # the approval is still standing.
            vendor.approved_by_id = None
            vendor.approved_at = None
            self.repo.touch(vendor)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                from_status=str(current),
                to_status=str(VendorState.PENDING_APPROVAL),
                user_id=self.tenant.user_id,
                action=AuditAction.REJECT,
                reason=reason,
            )

        return await self.get_vendor(vendor.id)

    async def set_active(self, vendor_id: uuid.UUID, *, active: bool, reason: str) -> VendorRead:
        """Activate or suspend a vendor, validated by the state machine."""
        async with transaction(self.session):
            vendor = await self.repo.get_or_404(vendor_id)
            current = vendor_state(vendor)
            # Reactivation restores the approval state the vendor already holds
            # rather than a fixed target, so switching a vendor off and on again
            # cannot promote an unapproved one.
            if active:
                target = (
                    VendorState.APPROVED if vendor.is_approved else VendorState.PENDING_APPROVAL
                )
            else:
                target = VendorState.SUSPENDED
            VENDOR_LIFECYCLE.assert_transition(current, target)

            vendor.is_active = active
            self.repo.touch(vendor)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return await self.get_vendor(vendor.id)

    async def delete_vendor(self, vendor_id: uuid.UUID, *, reason: str) -> None:
        """Retire a vendor (soft delete), refusing while anything still needs it."""
        async with transaction(self.session):
            vendor = await self.repo.get_or_404(vendor_id)
            current = vendor_state(vendor)
            VENDOR_LIFECYCLE.assert_transition(current, VendorState.ARCHIVED)

            if await self.repo.has_open_purchase_orders(vendor.id):
                raise referenced_in_use(
                    f"Vendor {vendor.code} still has open purchase orders. "
                    "Close or cancel them before retiring the vendor.",
                    vendor_code=vendor.code,
                )
            if await self.repo.has_item_links(vendor.id):
                raise referenced_in_use(
                    f"Vendor {vendor.code} is still named as a source on one or more items. "
                    "Remove those item-vendor links first.",
                    vendor_code=vendor.code,
                )
            if await self.repo.has_active_portal_users(vendor.id):
                raise referenced_in_use(
                    f"Vendor {vendor.code} still has active portal users. "
                    "Deactivate their accounts first.",
                    vendor_code=vendor.code,
                )

            # Contacts and category qualifications cannot outlive the vendor:
            # a qualification left behind would keep the vendor appearing in
            # category-based sourcing after it was retired.
            retired_contacts = await self._soft_delete_contacts(vendor.id)
            removed_categories = await self.categories.delete_for_vendor(vendor.id)

            vendor.is_active = False
            vendor.is_approved = False
            await self.repo.soft_delete(vendor)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                summary=f"Retired vendor {vendor.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(VendorState.ARCHIVED)},
                context={
                    "reason": reason,
                    "retired_contacts": retired_contacts,
                    "removed_categories": removed_categories,
                },
            )

    async def restore_vendor(self, vendor_id: uuid.UUID) -> VendorRead:
        """Bring a retired vendor back, suspended and unapproved.

        Its contacts and category qualifications are *not* restored: they were
        removed as a consequence, and which of them should come back is a
        decision for whoever is reopening the relationship.
        """
        async with transaction(self.session):
            vendor = await self.repo.get(vendor_id, include_deleted=True)
            if vendor is None:
                raise NotFoundError("Vendor", vendor_id)

            current = vendor_state(vendor)
            # Assert the *source* as well as the edge: the machine also allows
            # APPROVED -> SUSPENDED, so checking the edge alone would let
            # "restore" quietly suspend a vendor that is still live.
            if current is not VendorState.ARCHIVED:
                raise InvalidStateTransitionError(
                    entity="Vendor",
                    current=str(current),
                    requested="RESTORED",
                    allowed=[str(t) for t in VENDOR_LIFECYCLE.allowed_targets(current)],
                )
            VENDOR_LIFECYCLE.assert_transition(current, VendorState.SUSPENDED)

            # Another vendor may have taken the code, or re-registered the
            # GSTIN, while this one was retired.
            if await self.repo.code_taken(vendor.code, exclude_id=vendor.id):
                raise DuplicateError(
                    f"Code '{vendor.code}' is now used by another vendor.",
                    details={"code": vendor.code},
                )
            if vendor.gstin and await self.repo.gstin_taken(vendor.gstin, exclude_id=vendor.id):
                raise DuplicateError(
                    f"GSTIN {vendor.gstin} is now registered to another vendor.",
                    details={"gstin": vendor.gstin},
                )

            vendor.deleted_at = None
            vendor.is_active = False
            vendor.is_approved = False
            vendor.approved_by_id = None
            vendor.approved_at = None
            self.repo.touch(vendor)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_VENDOR,
                entity_id=vendor.id,
                entity_code=vendor.code,
                from_status=str(current),
                to_status=str(VendorState.SUSPENDED),
                user_id=self.tenant.user_id,
                reason="Vendor restored",
            )

        return self._to_read(vendor, 0, 0)

    # --- rating -------------------------------------------------------------

    async def preview_rating(
        self, vendor_id: uuid.UUID, *, window_days: int = DEFAULT_RATING_WINDOW_DAYS
    ) -> VendorRatingRead:
        """Compute a rating without storing it."""
        vendor = await self.repo.get_or_404(vendor_id)
        window = await load_performance(
            self.session, self.tenant, vendor.id, window_days=window_days
        )
        if window is None:
            return self._rating_read(
                insufficient_result(
                    vendor.id,
                    window_days,
                    "Goods receipt history is not available yet, so no rating can be computed.",
                ),
                applied=False,
            )
        return self._rating_read(compute_vendor_rating(window), applied=False)

    async def recompute_rating(
        self, vendor_id: uuid.UUID, *, window_days: int = DEFAULT_RATING_WINDOW_DAYS
    ) -> VendorRatingRead:
        """Recompute and store a vendor's rating from measured performance."""
        vendor = await self.repo.get_or_404(vendor_id)
        window = await load_performance(
            self.session, self.tenant, vendor.id, window_days=window_days
        )
        if window is None:
            return self._rating_read(
                insufficient_result(
                    vendor.id,
                    window_days,
                    "Goods receipt history is not available yet, so no rating can be computed.",
                ),
                applied=False,
            )

        result = await apply_vendor_rating(self.session, self.tenant, window=window)
        return self._rating_read(result, applied=result.rating is not None)

    # --- internals ----------------------------------------------------------

    async def _assert_code_free(self, code: str) -> None:
        if await self.repo.code_taken(code):
            raise DuplicateError(
                f"Vendor code '{code}' is already used in this company.",
                details={"code": code},
            )

    def _assert_approvable(self, vendor: Vendor) -> None:
        """Refuse to approve a record that cannot actually be transacted with.

        ASSUMPTION: an approved vendor must have either a GSTIN or an explicit
        MSME/unregistered marker, and must have a payee name and account number
        on file. Approving without them defers the problem to the first payment
        run, where it blocks a payment rather than a form.
        """
        missing: list[str] = []
        if not vendor.gstin and not vendor.msme_number:
            missing.append("gstin or msme_number")
        if not vendor.bank_account_number:
            missing.append("bank_account_number")
        if not vendor.bank_account_name:
            missing.append("bank_account_name")
        if missing:
            raise BusinessRuleError(
                "Vendor cannot be approved until these details are recorded: " + ", ".join(missing),
                details={"missing_fields": missing, "vendor_code": vendor.code},
            )

    @staticmethod
    def _bank_fields_changed(vendor: Vendor, changes: dict[str, Any]) -> bool:
        bank_fields = (
            "bank_name",
            "bank_branch",
            "bank_account_name",
            "bank_account_number",
            "bank_ifsc",
            "bank_swift",
        )
        return any(
            field in changes and changes[field] != getattr(vendor, field) for field in bank_fields
        )

    def _log_bank_details_changed(self, vendor: Vendor) -> None:
        """Surface a payee change in the operational log as well as the audit trail.

        The audit row records who changed what; this line makes the event
        visible to whoever is investigating a payment that went to the wrong
        account. The account number itself is never logged.
        """
        log.warning(
            "vendors.bank_details_changed",
            company_id=str(self.tenant.company_id),
            vendor_id=str(vendor.id),
            vendor_code=vendor.code,
            actor_id=str(self.tenant.user_id),
        )

    async def _soft_delete_contacts(self, vendor_id: uuid.UUID) -> int:
        from sqlalchemy import update

        stmt = (
            update(VendorContact)
            .where(
                VendorContact.company_id == self.tenant.company_id,
                VendorContact.vendor_id == vendor_id,
                VendorContact.deleted_at.is_(None),
            )
            .values(
                deleted_at=utcnow(),
                is_active=False,
                is_primary=False,
                updated_by_id=self.tenant.user_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    @staticmethod
    def _audit_snapshot(vendor: Vendor) -> dict[str, Any]:
        """Audited field values, with the bank account number masked."""
        captured = snapshot(vendor, _VENDOR_AUDITED_FIELDS)
        captured["bank_account_number"] = mask_account_number(captured["bank_account_number"])
        return captured

    @staticmethod
    def _to_read(vendor: Vendor, contact_count: int, category_count: int) -> VendorRead:
        read = VendorRead.model_validate(vendor)
        read.bank_account_number_masked = mask_account_number(vendor.bank_account_number)
        read.contact_count = contact_count
        read.category_count = category_count
        return read

    @staticmethod
    def _rating_read(result: VendorRatingResult, *, applied: bool) -> VendorRatingRead:
        return VendorRatingRead(
            vendor_id=result.vendor_id,
            rating=result.rating,
            on_time_percentage=result.on_time_percentage,
            rejection_percentage=result.rejection_percentage,
            receipt_count=result.receipt_count,
            window_days=result.window_days,
            has_quality_evidence=result.has_quality_evidence,
            is_sufficient=result.is_sufficient,
            reason=result.reason,
            applied=applied,
        )


# -----------------------------------------------------------------------------
# Vendor contact service
# -----------------------------------------------------------------------------


class VendorContactService:
    """Contacts of a vendor, always scoped to that vendor."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = VendorContactRepository(session, tenant)
        self.vendors = VendorRepository(session, tenant)

    async def list_contacts(
        self, filters: VendorContactFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[VendorContactRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [VendorContactRead.model_validate(row) for row in rows], pagination

    async def get_contact(self, contact_id: uuid.UUID) -> VendorContactRead:
        return VendorContactRead.model_validate(await self.repo.get_or_404(contact_id))

    async def create_contact(
        self, vendor_id: uuid.UUID, payload: VendorContactCreate
    ) -> VendorContactRead:
        async with transaction(self.session):
            vendor = await self.vendors.get_or_404(vendor_id)

            if await self.repo.role_taken(vendor_id, payload.name, str(payload.contact_role)):
                raise DuplicateError(
                    f"{payload.name} is already recorded as {payload.contact_role} "
                    f"for vendor {vendor.code}.",
                    details={"name": payload.name, "contact_role": str(payload.contact_role)},
                )

            # Clearing runs before the insert so the partial unique index never
            # sees two primaries, even momentarily.
            if payload.is_primary:
                await self.repo.clear_primary(vendor_id)

            contact = VendorContact(vendor_id=vendor_id, **payload.model_dump())
            self.repo.add(contact)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_CONTACT,
                entity_id=contact.id,
                entity_code=vendor.code,
                summary=f"Added contact {contact.name} ({contact.contact_role}) to {vendor.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(contact, _CONTACT_AUDITED_FIELDS),
            )

        return VendorContactRead.model_validate(contact)

    async def update_contact(
        self, contact_id: uuid.UUID, payload: VendorContactUpdate
    ) -> VendorContactRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            contact = await self.repo.get_or_404(contact_id)
            before = snapshot(contact, _CONTACT_AUDITED_FIELDS)

            for field, value in changes.items():
                setattr(contact, field, value)

            # Re-check reachability against the merged row: a PATCH clearing the
            # only phone number leaves a contact nobody can reach, and the check
            # constraint would reject it with an opaque IntegrityError.
            if not (contact.email or contact.phone or contact.mobile):
                raise ValidationError(
                    "A contact needs at least an e-mail, a phone or a mobile number."
                )

            self.repo.touch(contact)
            await self.session.flush()
            await self.session.refresh(contact)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_CONTACT,
                entity_id=contact.id,
                entity_code=contact.name,
                summary=f"Updated contact {contact.name}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(contact, _CONTACT_AUDITED_FIELDS),
            )

        return VendorContactRead.model_validate(contact)

    async def set_primary(self, contact_id: uuid.UUID) -> VendorContactRead:
        """Promote a contact to primary, demoting the previous one."""
        async with transaction(self.session):
            contact = await self.repo.get_or_404(contact_id)
            if not contact.is_active:
                raise BusinessRuleError(
                    "An inactive contact cannot be the primary contact.",
                    details={"contact_name": contact.name},
                )

            # Idempotent: re-nominating the current primary is a no-op rather
            # than an error, so a retried request does not fail.
            if not contact.is_primary:
                await self.repo.clear_primary(contact.vendor_id, keep_id=contact.id)
                contact.is_primary = True
                self.repo.touch(contact)
                await self.session.flush()
                await self.session.refresh(contact)

                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CONFIG_CHANGE,
                    entity_type=AUDIT_ENTITY_CONTACT,
                    entity_id=contact.id,
                    entity_code=contact.name,
                    summary=f"{contact.name} set as the primary contact",
                    user_id=self.tenant.user_id,
                    old_values={"is_primary": False},
                    new_values={"is_primary": True},
                )

        return VendorContactRead.model_validate(contact)

    async def delete_contact(self, contact_id: uuid.UUID, *, reason: str) -> None:
        """Retire a contact. Soft, because a PO may quote the person it went to."""
        async with transaction(self.session):
            contact = await self.repo.get_or_404(contact_id)
            if contact.is_primary:
                raise referenced_in_use(
                    "The primary contact cannot be removed. Nominate another primary first.",
                    contact_name=contact.name,
                )

            contact.is_active = False
            await self.repo.soft_delete(contact)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_CONTACT,
                entity_id=contact.id,
                entity_code=contact.name,
                summary=f"Removed contact {contact.name}",
                user_id=self.tenant.user_id,
                context={"reason": reason},
            )


# -----------------------------------------------------------------------------
# Vendor category service
# -----------------------------------------------------------------------------


class VendorCategoryService:
    """Which item categories a vendor is qualified for (section 16)."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = VendorCategoryRepository(session, tenant)
        self.vendors = VendorRepository(session, tenant)

    async def list_categories(
        self, filters: VendorCategoryFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[VendorCategoryRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)

        # One lookup for the whole page: the alternative is a category query per
        # qualification, which is the N+1 section 32 forbids.
        labels = await self.repo.category_labels([row.item_category_id for row in rows])
        return [self._to_read(row, labels) for row in rows], pagination

    async def assign(
        self, vendor_id: uuid.UUID, payload: VendorCategoryAssign
    ) -> VendorCategoryRead:
        async with transaction(self.session):
            vendor = await self.vendors.get_or_404(vendor_id)
            if not await self.vendors.category_exists(payload.item_category_id):
                raise NotFoundError("Item category", payload.item_category_id)

            existing = await self.repo.get_link(vendor_id, payload.item_category_id)
            if existing is not None:
                raise DuplicateError(
                    f"Vendor {vendor.code} is already qualified for that category.",
                    details={"item_category_id": str(payload.item_category_id)},
                )

            if payload.is_preferred:
                await self.repo.clear_preferred(payload.item_category_id)

            link = VendorCategory(vendor_id=vendor_id, **payload.model_dump())
            self.repo.add(link)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_CATEGORY,
                entity_id=link.id,
                entity_code=vendor.code,
                summary=f"Qualified vendor {vendor.code} for a category",
                user_id=self.tenant.user_id,
                new_values=snapshot(link, _CATEGORY_AUDITED_FIELDS),
            )

        labels = await self.repo.category_labels([link.item_category_id])
        return self._to_read(link, labels)

    async def update(self, link_id: uuid.UUID, payload: VendorCategoryUpdate) -> VendorCategoryRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            link = await self.repo.get_or_404(link_id)
            before = snapshot(link, _CATEGORY_AUDITED_FIELDS)

            if changes.get("is_preferred") and not link.is_preferred:
                await self.repo.clear_preferred(link.item_category_id, keep_id=link.id)

            for field, value in changes.items():
                setattr(link, field, value)
            self.repo.touch(link)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_CATEGORY,
                entity_id=link.id,
                entity_code=str(link.vendor_id),
                summary="Updated a vendor category qualification",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(link, _CATEGORY_AUDITED_FIELDS),
            )

        labels = await self.repo.category_labels([link.item_category_id])
        return self._to_read(link, labels)

    async def remove(self, link_id: uuid.UUID, *, reason: str) -> None:
        """Revoke a qualification.

        A hard delete: the association row is not quoted by any document, and
        the revocation itself is preserved in the audit log, which is where the
        history of "who was allowed to supply what, when" belongs.
        """
        async with transaction(self.session):
            link = await self.repo.get_or_404(link_id)
            captured = snapshot(link, _CATEGORY_AUDITED_FIELDS)
            vendor_id = link.vendor_id

            await self.repo.hard_delete(link)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_CATEGORY,
                entity_id=link_id,
                entity_code=str(vendor_id),
                summary="Revoked a vendor category qualification",
                user_id=self.tenant.user_id,
                old_values=captured,
                context={"reason": reason},
            )

    @staticmethod
    def _to_read(
        link: VendorCategory, labels: dict[uuid.UUID, tuple[str, str]]
    ) -> VendorCategoryRead:
        read = VendorCategoryRead.model_validate(link)
        label = labels.get(link.item_category_id)
        if label is not None:
            read.item_category_code, read.item_category_name = label
        return read


# -----------------------------------------------------------------------------
# Transporter service
# -----------------------------------------------------------------------------


class TransporterService:
    """Carrier and courier master data."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = TransporterRepository(session, tenant)

    async def list_transporters(
        self, filters: TransporterFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[TransporterRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [TransporterRead.model_validate(row) for row in rows], pagination

    async def get_transporter(self, transporter_id: uuid.UUID) -> TransporterRead:
        return TransporterRead.model_validate(await self.repo.get_or_404(transporter_id))

    async def list_options(
        self, transporter_type: TransporterType | None
    ) -> list[TransporterOption]:
        rows = await self.repo.list_options(
            transporter_type=str(transporter_type) if transporter_type else None
        )
        options = []
        for row in rows:
            option = TransporterOption.model_validate(row)
            option.has_tracking = bool(row.tracking_url_template)
            options.append(option)
        return options

    async def tracking_link(self, transporter_id: uuid.UUID, tracking_id: str) -> TrackingLinkRead:
        """Resolve a consignment number against the carrier's tracking template."""
        transporter = await self.repo.get_or_404(transporter_id)
        url = build_tracking_url(transporter.tracking_url_template, tracking_id)
        return TrackingLinkRead(
            transporter_id=transporter.id,
            tracking_id=tracking_id.strip(),
            url=url,
            is_resolvable=url is not None,
        )

    async def create_transporter(self, payload: TransporterCreate) -> TransporterRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Transporter code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )

            transporter = Transporter(**payload.model_dump())
            self.repo.add(transporter)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_TRANSPORTER,
                entity_id=transporter.id,
                entity_code=transporter.code,
                summary=f"Created transporter {transporter.code} ({transporter.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(transporter, _TRANSPORTER_AUDITED_FIELDS),
            )

        return TransporterRead.model_validate(transporter)

    async def update_transporter(
        self, transporter_id: uuid.UUID, payload: TransporterUpdate
    ) -> TransporterRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            transporter = await self.repo.get_or_404(transporter_id)
            before = snapshot(transporter, _TRANSPORTER_AUDITED_FIELDS)

            merged_gstin = changes.get("gstin", transporter.gstin)
            try:
                assert_gstin_pan_agree(merged_gstin, changes.get("pan", transporter.pan))
                assert_gstin_state_agrees(
                    merged_gstin, changes.get("state_code", transporter.state_code)
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            for field, value in changes.items():
                setattr(transporter, field, value)
            self.repo.touch(transporter)
            await self.session.flush()
            await self.session.refresh(transporter)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_TRANSPORTER,
                entity_id=transporter.id,
                entity_code=transporter.code,
                summary=f"Updated transporter {transporter.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(transporter, _TRANSPORTER_AUDITED_FIELDS),
            )

        return TransporterRead.model_validate(transporter)

    async def set_active(
        self, transporter_id: uuid.UUID, *, active: bool, reason: str
    ) -> TransporterRead:
        target = TransporterState.ACTIVE if active else TransporterState.INACTIVE

        async with transaction(self.session):
            transporter = await self.repo.get_or_404(transporter_id)
            current = transporter_state(transporter)
            TRANSPORTER_LIFECYCLE.assert_transition(current, target)

            transporter.is_active = active
            self.repo.touch(transporter)
            await self.session.flush()
            await self.session.refresh(transporter)

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_TRANSPORTER,
                entity_id=transporter.id,
                entity_code=transporter.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return TransporterRead.model_validate(transporter)

    async def delete_transporter(self, transporter_id: uuid.UUID, *, reason: str) -> None:
        """Retire a carrier, refusing while a consignment is still with them."""
        async with transaction(self.session):
            transporter = await self.repo.get_or_404(transporter_id)
            current = transporter_state(transporter)
            TRANSPORTER_LIFECYCLE.assert_transition(current, TransporterState.ARCHIVED)

            if await self.repo.has_open_dispatches(transporter.id):
                raise referenced_in_use(
                    f"Transporter {transporter.code} is still carrying undelivered "
                    "consignments and cannot be retired.",
                    transporter_code=transporter.code,
                )

            transporter.is_active = False
            await self.repo.soft_delete(transporter)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_TRANSPORTER,
                entity_id=transporter.id,
                entity_code=transporter.code,
                summary=f"Retired transporter {transporter.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(TransporterState.ARCHIVED)},
                context={"reason": reason},
            )

    async def restore_transporter(self, transporter_id: uuid.UUID) -> TransporterRead:
        """Bring a retired carrier back, inactive."""
        async with transaction(self.session):
            transporter = await self.repo.get(transporter_id, include_deleted=True)
            if transporter is None:
                raise NotFoundError("Transporter", transporter_id)

            current = transporter_state(transporter)
            if current is not TransporterState.ARCHIVED:
                raise InvalidStateTransitionError(
                    entity="Transporter",
                    current=str(current),
                    requested="RESTORED",
                    allowed=[str(t) for t in TRANSPORTER_LIFECYCLE.allowed_targets(current)],
                )
            TRANSPORTER_LIFECYCLE.assert_transition(current, TransporterState.INACTIVE)

            if await self.repo.code_taken(transporter.code, exclude_id=transporter.id):
                raise DuplicateError(
                    f"Code '{transporter.code}' is now used by another transporter.",
                    details={"code": transporter.code},
                )

            transporter.deleted_at = None
            transporter.is_active = False
            self.repo.touch(transporter)
            await self.session.flush()
            await self.session.refresh(transporter)

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_TRANSPORTER,
                entity_id=transporter.id,
                entity_code=transporter.code,
                from_status=str(current),
                to_status=str(TransporterState.INACTIVE),
                user_id=self.tenant.user_id,
                reason="Transporter restored",
            )

        return TransporterRead.model_validate(transporter)


# Kept for callers that want to validate a template outside a request; the same
# expression the model's check constraint enforces.
_TEMPLATE_PLACEHOLDER_RE = re.compile(re.escape(TRACKING_PLACEHOLDER))


def template_has_placeholder(template: str) -> bool:
    return _TEMPLATE_PLACEHOLDER_RE.search(template) is not None


__all__ = [
    "DEFAULT_RATING_WINDOW_DAYS",
    "DELIVERY_WEIGHT",
    "MIN_RECEIPTS_FOR_RATING",
    "QUALITY_WEIGHT",
    "REQUIRE_SEPARATE_APPROVER",
    "TRANSPORTER_LIFECYCLE",
    "VENDOR_LIFECYCLE",
    "TransporterService",
    "TransporterState",
    "VendorCategoryService",
    "VendorContactService",
    "VendorPerformanceWindow",
    "VendorRatingResult",
    "VendorService",
    "VendorState",
    "apply_vendor_rating",
    "build_tracking_url",
    "can_receive_purchase_orders",
    "compute_vendor_rating",
    "insufficient_result",
    "load_performance",
    "mask_account_number",
    "referenced_in_use",
    "template_has_placeholder",
    "transporter_state",
    "vendor_state",
]
