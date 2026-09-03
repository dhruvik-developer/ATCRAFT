"""Business rules for customer, dealer and distributor master data.

What this module is responsible for, and why each rule exists:

* **The credit decision.** :func:`assert_within_credit_limit` is the one place
  that decides whether an account may take on more exposure. Sales order
  creation calls it; nothing re-implements the comparison. It is a pure
  function over an already-loaded customer, so it can be unit tested exhaustively
  and called inside a transaction that has the customer row locked.
* **Blocking is a state change, not a flag edit.** A credit hold carries a
  mandatory reason, a timestamp and the person who imposed it, goes through
  :data:`CREDIT_STANDING`, and is written to the audit log in the same
  transaction. The same is true of activation and retirement, which go through
  :data:`CUSTOMER_LIFECYCLE`.
* **Exactly one default address per (customer, address type).** The database
  enforces "at most one" with a partial unique index; this module enforces the
  other half - the first address of a type becomes the default, and deleting the
  default promotes the next one - so a customer with addresses always has one
  selected.
* **GST correctness is checked at entry.** A domestic address without a state
  code cannot be taxed, and a GSTIN whose state prefix contradicts the address
  state code is one of the two data errors that make an e-invoice unfileable.
  Both are refused when the address is saved rather than discovered at
  invoicing.
* **The cached outstanding balance is finance-owned.**
  :func:`apply_outstanding_delta` locks the customer row, applies a signed
  delta and audits it. No other path writes that column, and no CRUD payload
  can reach it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_money
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.customers.models import (
    AddressType,
    Customer,
    CustomerAddress,
    CustomerContact,
    CustomerType,
)
from src.modules.customers.repository import (
    CustomerAddressRepository,
    CustomerContactRepository,
    CustomerRepository,
)
from src.modules.customers.schemas import (
    AddressCreate,
    AddressRead,
    AddressUpdate,
    ContactCreate,
    ContactRead,
    ContactUpdate,
    CreditStatus,
    CustomerCreate,
    CustomerDetail,
    CustomerFilters,
    CustomerSummary,
    CustomerUpdate,
    OutstandingAdjustment,
)

log = get_logger(__name__)

AUDIT_CUSTOMER = "customer"
AUDIT_ADDRESS = "customer_address"
AUDIT_CONTACT = "customer_contact"

# -----------------------------------------------------------------------------
# Decisions this module makes explicitly
# -----------------------------------------------------------------------------

# ASSUMPTION (the one the brief asks to be explicit about): a NULL credit_limit
# means *unlimited* credit - no limit has been agreed with this account - while a
# credit_limit of 0 means *strictly no credit*: every order of non-zero value is
# refused and the customer pays before dispatch. The alternative convention,
# "0 means unlimited", is rejected because it makes the safest-looking value the
# most permissive one, so a mistyped or defaulted limit silently grants infinite
# credit. With this convention a mistake fails closed.
CREDIT_LIMIT_NULL_MEANS_UNLIMITED = True

# ASSUMPTION: an inactive customer may not take on new exposure. The credit
# check therefore refuses an order for a deactivated or retired account, rather
# than leaving that to each calling module to remember.
INACTIVE_CUSTOMER_MAY_NOT_ORDER = True

# ASSUMPTION: dealer and distributor accounts are managed by the channel team.
# Writing one therefore requires Permission.DEALER_MANAGE *in addition* to the
# customer permission the route already gates on. The alternative - letting any
# holder of customer.update change a dealer's price list or credit limit -
# would put the entire dealer-margin structure behind the most widely granted
# permission in the sales department.
DEALER_CHANNELS: frozenset[CustomerType] = frozenset(
    {CustomerType.DEALER, CustomerType.DISTRIBUTOR}
)

# ASSUMPTION: when both a GSTIN and a state code are present on the same
# address, the first two characters of the GSTIN (which *are* the state code by
# construction of the number) must agree with it. A mismatch is always a
# data-entry error and it produces an invoice that the GST portal rejects, so it
# is refused on save. Set to False only if a jurisdiction stops encoding the
# state in the GSTIN.
ENFORCE_GSTIN_STATE_CODE_MATCH = True

# ASSUMPTION: a customer with a non-zero outstanding balance may not be retired.
# Retiring one would hide a receivable behind a soft-deleted master row, which
# is precisely the "never use soft delete to hide a financial obligation" case
# in section 28. Settle or write off the balance first.
BLOCK_RETIREMENT_WITH_OUTSTANDING = True

#: Upper bound on the address and contact lists embedded in a customer detail
#: response. The full lists are available on their own paginated sub-resources;
#: this keeps a single record's payload bounded no matter what the data does.
MAX_EMBEDDED_CHILDREN = 50

_CUSTOMER_AUDITED_FIELDS = [
    "code",
    "name",
    "legal_name",
    "customer_type",
    "gstin",
    "pan",
    "price_list_type",
    "credit_limit",
    "credit_days",
    "assigned_sales_user_id",
    "default_transporter_id",
    "is_active",
    "is_blocked",
    "notes",
]

_ADDRESS_AUDITED_FIELDS = [
    "address_type",
    "label",
    "address_line1",
    "address_line2",
    "landmark",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "gstin",
    "contact_person",
    "phone",
    "email",
    "is_default",
    "is_active",
]

_CONTACT_AUDITED_FIELDS = [
    "name",
    "designation",
    "email",
    "phone",
    "is_primary",
    "is_active",
]

#: Columns the detail response reads off the customer row. Listed explicitly
#: because the response is assembled from a dict - see ``_to_detail`` - and an
#: attribute-mode validation would touch the lazy="raise" relationships.
_CUSTOMER_DETAIL_FIELDS = (
    "id",
    "code",
    "name",
    "legal_name",
    "customer_type",
    "price_list_type",
    "gstin",
    "pan",
    "credit_limit",
    "credit_days",
    "outstanding_amount",
    "is_active",
    "is_blocked",
    "block_reason",
    "blocked_at",
    "blocked_by_id",
    "assigned_sales_user_id",
    "default_transporter_id",
    "notes",
    "created_at",
    "updated_at",
)


# -----------------------------------------------------------------------------
# Lifecycle and credit standing
# -----------------------------------------------------------------------------


class MasterLifecycleState(StrEnum):
    """Lifecycle of a customer master row.

    There is no ``status`` column: the state is derived from ``is_active`` and
    ``deleted_at``, the two flags the rest of the system already reads. A third,
    redundant column could disagree with them. Routing the transitions through a
    :class:`StateMachine` still gives one place where the legal moves are
    declared, tested and rendered for the UI (section 18).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


class CreditStanding(StrEnum):
    """Whether the account may take on new exposure.

    Orthogonal to the lifecycle on purpose: a customer on credit hold is still
    an active account with open orders and open invoices; it simply may not add
    to what it already owes.
    """

    GOOD_STANDING = "GOOD_STANDING"
    ON_HOLD = "ON_HOLD"


CUSTOMER_LIFECYCLE: StateMachine[MasterLifecycleState] = StateMachine(
    entity="Customer",
    initial=MasterLifecycleState.ACTIVE,
    transitions=(
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.INACTIVE,
            permission=Permission.CUSTOMER_UPDATE,
            label="Deactivate",
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ACTIVE,
            permission=Permission.CUSTOMER_UPDATE,
            label="Activate",
        ),
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.CUSTOMER_DELETE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.CUSTOMER_DELETE,
            label="Retire",
            is_irreversible=True,
        ),
        # A restored account comes back INACTIVE, never straight to ACTIVE:
        # someone has to look at its credit terms before it can order again.
        Transition(
            MasterLifecycleState.ARCHIVED,
            MasterLifecycleState.INACTIVE,
            permission=Permission.CUSTOMER_UPDATE,
            label="Restore",
        ),
    ),
)

CREDIT_STANDING: StateMachine[CreditStanding] = StateMachine(
    entity="Customer credit standing",
    initial=CreditStanding.GOOD_STANDING,
    transitions=(
        Transition(
            CreditStanding.GOOD_STANDING,
            CreditStanding.ON_HOLD,
            permission=Permission.CUSTOMER_UPDATE,
            label="Block",
            is_irreversible=True,
        ),
        Transition(
            CreditStanding.ON_HOLD,
            CreditStanding.GOOD_STANDING,
            permission=Permission.CUSTOMER_UPDATE,
            label="Release hold",
        ),
    ),
)


def lifecycle_state(record: Any) -> MasterLifecycleState:
    """Derive the lifecycle state of a master row from its flags."""
    if getattr(record, "deleted_at", None) is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if record.is_active else MasterLifecycleState.INACTIVE


def credit_standing(record: Any) -> CreditStanding:
    return CreditStanding.ON_HOLD if record.is_blocked else CreditStanding.GOOD_STANDING


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot remove this, something still depends on it" refusal.

    One constructor so every such refusal carries the same machine-readable code
    and the frontend can react to all of them uniformly.
    """
    return BusinessRuleError(
        message, code=ErrorCode.REFERENCED_RECORD_IN_USE, details=details or None
    )


# -----------------------------------------------------------------------------
# Credit check - pure, and the contract other modules call
# -----------------------------------------------------------------------------


class CreditSubject(Protocol):
    """The shape :func:`evaluate_credit` needs.

    A protocol rather than the ORM class so the rule can be exercised - and
    called - without a database session or a mapped instance.
    """

    id: uuid.UUID
    code: str
    is_active: bool
    is_blocked: bool
    block_reason: str | None
    credit_limit: Decimal | None
    credit_days: int
    outstanding_amount: Decimal


@dataclass(frozen=True, slots=True)
class CreditAssessment:
    """The full result of a credit check, whether or not it passed.

    Returned even on success so a caller can show the remaining headroom, and
    populated on failure so the error message can state the numbers rather than
    just "credit limit exceeded".
    """

    customer_id: uuid.UUID
    customer_code: str
    is_active: bool
    is_blocked: bool
    block_reason: str | None
    credit_limit: Decimal | None
    credit_days: int
    outstanding_amount: Decimal
    order_value: Decimal
    exposure: Decimal
    headroom: Decimal | None
    is_unlimited: bool
    within_limit: bool
    #: Why the check would fail, or ``None`` when the account may order.
    failure_reason: str | None


def evaluate_credit(customer: CreditSubject, order_value: Decimal) -> CreditAssessment:
    """Compute the credit position of ``customer`` for an order of ``order_value``.

    Pure and non-raising (except for a nonsensical input), so a UI can render
    "you have 12,340 of headroom left" from exactly the calculation the backend
    will enforce a moment later.

    All arithmetic is on money-scaled Decimals: comparing unrounded values would
    let a limit be exceeded by a fraction of a paisa, or - worse - reject an
    order that is exactly on the limit because of a trailing rounding artefact.
    """
    if order_value < 0:
        raise ValidationError(
            "Order value for a credit check cannot be negative.",
            details={"order_value": str(order_value)},
        )

    outstanding = to_money(customer.outstanding_amount or Decimal("0"))
    value = to_money(order_value)
    exposure = outstanding + value

    limit = customer.credit_limit
    is_unlimited = limit is None
    headroom: Decimal | None = None
    within_limit = True

    if limit is not None:
        limit = to_money(limit)
        headroom = limit - exposure
        within_limit = exposure <= limit

    failure: str | None = None
    if customer.is_blocked:
        failure = f"Customer {customer.code} is on credit hold" + (
            f": {customer.block_reason}" if customer.block_reason else "."
        )
    elif INACTIVE_CUSTOMER_MAY_NOT_ORDER and not customer.is_active:
        failure = f"Customer {customer.code} is not active and cannot take new orders."
    elif not within_limit:
        # ``within_limit`` is only False when a limit exists, so ``limit`` is
        # never None in this branch.
        failure = (
            f"Order would take customer {customer.code} past its credit limit: "
            f"outstanding {outstanding} + order {value} = {exposure}, limit {limit}."
        )

    return CreditAssessment(
        customer_id=customer.id,
        customer_code=customer.code,
        is_active=customer.is_active,
        is_blocked=customer.is_blocked,
        block_reason=customer.block_reason,
        credit_limit=limit,
        credit_days=customer.credit_days,
        outstanding_amount=outstanding,
        order_value=value,
        exposure=exposure,
        headroom=headroom,
        is_unlimited=is_unlimited,
        within_limit=within_limit,
        failure_reason=failure,
    )


def assert_within_credit_limit(customer: CreditSubject, order_value: Decimal) -> CreditAssessment:
    """Refuse an order the customer may not take on.

    **This is the contract sales order creation calls.** It raises
    :class:`~src.core.errors.BusinessRuleError` when the customer is blocked,
    when the account is not active, or when the order would push total exposure
    past the agreed credit limit; otherwise it returns the assessment so the
    caller can record the headroom it consumed.

    Call it inside the same transaction that creates the order, with the
    customer row already read (and, for a hot account, locked). Checking outside
    the transaction and writing inside it re-opens exactly the race the limit
    exists to prevent.
    """
    assessment = evaluate_credit(customer, order_value)
    if assessment.failure_reason is None:
        return assessment

    raise BusinessRuleError(
        assessment.failure_reason,
        details={
            "customer_id": str(assessment.customer_id),
            "customer_code": assessment.customer_code,
            "is_blocked": assessment.is_blocked,
            "block_reason": assessment.block_reason,
            "is_active": assessment.is_active,
            "credit_limit": None if assessment.is_unlimited else str(assessment.credit_limit),
            "outstanding_amount": str(assessment.outstanding_amount),
            "order_value": str(assessment.order_value),
            "exposure": str(assessment.exposure),
            "headroom": None if assessment.headroom is None else str(assessment.headroom),
        },
    )


def credit_status_of(assessment: CreditAssessment) -> CreditStatus:
    """Render an assessment as the API response model."""
    return CreditStatus(
        customer_id=assessment.customer_id,
        customer_code=assessment.customer_code,
        is_active=assessment.is_active,
        is_blocked=assessment.is_blocked,
        block_reason=assessment.block_reason,
        credit_limit=assessment.credit_limit,
        credit_days=assessment.credit_days,
        outstanding_amount=assessment.outstanding_amount,
        order_value=assessment.order_value,
        exposure=assessment.exposure,
        headroom=assessment.headroom,
        is_unlimited=assessment.is_unlimited,
        within_limit=assessment.within_limit,
    )


# -----------------------------------------------------------------------------
# GST helpers - pure
# -----------------------------------------------------------------------------


def gstin_state_code(gstin: str | None) -> str | None:
    """The state code encoded in the first two characters of a GSTIN."""
    if not gstin or len(gstin) < 2:
        return None
    return gstin[:2]


def assert_gstin_matches_state(gstin: str | None, state_code: str | None) -> None:
    """Refuse a GSTIN whose state prefix contradicts the address state code.

    Only checked when both are present: an unregistered customer has no GSTIN,
    and an export address has no Indian state code.
    """
    if not ENFORCE_GSTIN_STATE_CODE_MATCH:
        return
    encoded = gstin_state_code(gstin)
    if encoded is None or state_code is None:
        return
    if encoded != state_code:
        raise BusinessRuleError(
            f"GSTIN {gstin} belongs to state {encoded}, but the address state code is "
            f"{state_code}. One of the two is wrong, and the invoice would be rejected.",
            details={"gstin_state_code": encoded, "address_state_code": state_code},
        )


def requires_dealer_permission(customer_type: CustomerType) -> bool:
    """Is this account part of the dealer channel? See ``DEALER_CHANNELS``."""
    return customer_type in DEALER_CHANNELS


def assert_channel_write_allowed(
    principal: Principal, *customer_types: CustomerType | None
) -> None:
    """Require ``dealer.manage`` before writing a dealer or distributor account.

    Both the current and the proposed channel are passed on an edit, so neither
    moving an account *into* the dealer channel nor moving one *out of* it can
    be done by someone who may not manage dealers.
    """
    if not any(t is not None and requires_dealer_permission(t) for t in customer_types):
        return
    if not principal.has(Permission.DEALER_MANAGE):
        raise PermissionDeniedError(str(Permission.DEALER_MANAGE))


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------


class CustomerService:
    """Customer master-data operations. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = CustomerRepository(session, tenant)
        self.addresses = CustomerAddressRepository(session, tenant)
        self.contacts = CustomerContactRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_customers(
        self, filters: CustomerFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[CustomerSummary], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [CustomerSummary.model_validate(row) for row in rows], pagination

    async def list_dealers(
        self, filters: CustomerFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[CustomerSummary], OffsetPagination]:
        """The same list, restricted to the dealer channel.

        A ``customer_type`` filter outside :data:`DEALER_CHANNELS` is refused
        rather than quietly returning an empty page - an empty result reads as
        "there are no such dealers", which would be a lie.
        """
        if filters.customer_type is not None and not requires_dealer_permission(
            filters.customer_type
        ):
            raise BusinessRuleError(
                f"{filters.customer_type} is not a dealer channel. Use the customers list for it.",
                details={"dealer_channels": sorted(str(c) for c in DEALER_CHANNELS)},
            )

        channels = (
            (filters.customer_type,)
            if filters.customer_type is not None
            else tuple(sorted(DEALER_CHANNELS))
        )
        stmt = self.repo.build_list_stmt(filters, sort, channels=channels)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [CustomerSummary.model_validate(row) for row in rows], pagination

    async def get_customer(self, customer_id: uuid.UUID) -> CustomerDetail:
        """One customer with its addresses and contacts.

        Three statements, never one per child row: the customer, then its
        addresses, then its contacts. The relationships are ``lazy="raise"``
        precisely so that a future edit cannot turn this into an N+1 by
        accident.
        """
        customer = await self.repo.get_or_404(customer_id)
        addresses = await self.addresses.list_for_customer(customer.id, limit=MAX_EMBEDDED_CHILDREN)
        contacts = await self.contacts.list_for_customer(customer.id, limit=MAX_EMBEDDED_CHILDREN)
        return self._to_detail(customer, addresses, contacts)

    async def get_credit_status(
        self, customer_id: uuid.UUID, order_value: Decimal | None = None
    ) -> CreditStatus:
        """The credit position, optionally against a prospective order value."""
        customer = await self.repo.get_or_404(customer_id)
        return credit_status_of(evaluate_credit(customer, order_value or Decimal("0")))

    # --- writes -------------------------------------------------------------

    async def create_customer(
        self, principal: Principal, payload: CustomerCreate
    ) -> CustomerDetail:
        assert_channel_write_allowed(principal, payload.customer_type)

        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Customer code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            if payload.gstin and await self.repo.gstin_taken(payload.gstin):
                raise DuplicateError(
                    f"Another customer is already registered under GSTIN {payload.gstin}.",
                    details={"gstin": payload.gstin},
                )

            await self._assert_sales_user(payload.assigned_sales_user_id)
            await self._assert_transporter(payload.default_transporter_id)

            customer = Customer(**payload.model_dump())
            self.repo.add(customer)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                summary=f"Created customer {customer.code} ({customer.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(customer, _CUSTOMER_AUDITED_FIELDS),
            )

        return self._to_detail(customer, [], [])

    async def update_customer(
        self, principal: Principal, customer_id: uuid.UUID, payload: CustomerUpdate
    ) -> CustomerDetail:
        changes = payload.model_dump(exclude_unset=True)
        clear_credit_limit = bool(changes.pop("clear_credit_limit", False))
        clear_sales_user = bool(changes.pop("clear_assigned_sales_user", False))
        clear_transporter = bool(changes.pop("clear_default_transporter", False))

        if not changes and not (clear_credit_limit or clear_sales_user or clear_transporter):
            raise BusinessRuleError("No fields were supplied to update.")

        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            # Both the existing and the proposed channel are checked, so an
            # account cannot be moved into or out of the dealer channel without
            # the dealer permission.
            assert_channel_write_allowed(
                principal, customer.customer_type, changes.get("customer_type")
            )

            before = snapshot(customer, _CUSTOMER_AUDITED_FIELDS)

            new_gstin = changes.get("gstin")
            if (
                new_gstin
                and new_gstin != customer.gstin
                and await self.repo.gstin_taken(new_gstin, exclude_id=customer.id)
            ):
                raise DuplicateError(
                    f"Another customer is already registered under GSTIN {new_gstin}.",
                    details={"gstin": new_gstin},
                )

            if changes.get("assigned_sales_user_id") is not None:
                await self._assert_sales_user(changes["assigned_sales_user_id"])
            if changes.get("default_transporter_id") is not None:
                await self._assert_transporter(changes["default_transporter_id"])

            for field, value in changes.items():
                setattr(customer, field, value)

            # Explicit clears, because "field absent" and "field null" both
            # arrive as None and must not mean the same thing.
            if clear_credit_limit:
                customer.credit_limit = None
            if clear_sales_user:
                customer.assigned_sales_user_id = None
            if clear_transporter:
                customer.default_transporter_id = None

            self.repo.touch(customer)
            await self.session.flush()

            after = snapshot(customer, _CUSTOMER_AUDITED_FIELDS)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                summary=f"Updated customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=after,
            )

        return await self.get_customer(customer.id)

    async def set_active(
        self, principal: Principal, customer_id: uuid.UUID, *, active: bool, reason: str
    ) -> CustomerDetail:
        """Activate or deactivate, validated by the lifecycle state machine."""
        target = MasterLifecycleState.ACTIVE if active else MasterLifecycleState.INACTIVE

        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            assert_channel_write_allowed(principal, customer.customer_type)

            current = lifecycle_state(customer)
            CUSTOMER_LIFECYCLE.assert_transition(current, target)

            customer.is_active = active
            self.repo.touch(customer)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return await self.get_customer(customer_id)

    async def block(
        self, principal: Principal, customer_id: uuid.UUID, *, reason: str
    ) -> CustomerDetail:
        """Put an account on credit hold.

        Not idempotent by design: blocking an already-blocked account is refused
        by the state machine rather than silently overwriting the original
        reason and timestamp, which is the evidence of why the hold exists.
        """
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            assert_channel_write_allowed(principal, customer.customer_type)

            current = credit_standing(customer)
            CREDIT_STANDING.assert_transition(current, CreditStanding.ON_HOLD)

            customer.is_blocked = True
            customer.block_reason = reason
            customer.blocked_at = utcnow()
            customer.blocked_by_id = self.tenant.user_id
            self.repo.touch(customer)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                from_status=str(current),
                to_status=str(CreditStanding.ON_HOLD),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return await self.get_customer(customer_id)

    async def unblock(
        self, principal: Principal, customer_id: uuid.UUID, *, reason: str
    ) -> CustomerDetail:
        """Release a credit hold.

        ``block_reason`` is cleared together with the flag: the check constraint
        pairs the two, and the history of why the hold existed lives in the
        audit log, which is where it cannot be overwritten by the next hold.
        """
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            assert_channel_write_allowed(principal, customer.customer_type)

            current = credit_standing(customer)
            CREDIT_STANDING.assert_transition(current, CreditStanding.GOOD_STANDING)

            previous_reason = customer.block_reason
            customer.is_blocked = False
            customer.block_reason = None
            customer.blocked_at = None
            customer.blocked_by_id = None
            self.repo.touch(customer)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                from_status=str(current),
                to_status=str(CreditStanding.GOOD_STANDING),
                user_id=self.tenant.user_id,
                reason=f"{reason} (hold was: {previous_reason})" if previous_reason else reason,
            )

        return await self.get_customer(customer_id)

    async def retire_customer(
        self, principal: Principal, customer_id: uuid.UUID, *, reason: str
    ) -> None:
        """Soft-delete a customer, refusing while it still owes money."""
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            assert_channel_write_allowed(principal, customer.customer_type)

            current = lifecycle_state(customer)
            CUSTOMER_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if BLOCK_RETIREMENT_WITH_OUTSTANDING and to_money(
                customer.outstanding_amount or Decimal("0")
            ) != Decimal("0.00"):
                raise referenced_in_use(
                    f"Customer {customer.code} has an outstanding balance of "
                    f"{customer.outstanding_amount} and cannot be retired. Settle or write "
                    "off the balance first.",
                    customer_code=customer.code,
                    outstanding_amount=str(customer.outstanding_amount),
                )

            # Addresses and contacts have no life of their own; retiring the
            # account retires them in the same transaction.
            retired_addresses = await self.addresses.soft_delete_for_customer(customer.id)
            retired_contacts = await self.contacts.soft_delete_for_customer(customer.id)

            customer.is_active = False
            await self.repo.soft_delete(customer)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                summary=f"Retired customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={
                    "reason": reason,
                    "retired_addresses": retired_addresses,
                    "retired_contacts": retired_contacts,
                },
            )

    async def restore_customer(
        self, principal: Principal, customer_id: uuid.UUID
    ) -> CustomerDetail:
        """Bring a retired customer back, inactive.

        Its addresses and contacts are not restored automatically: they were
        retired as a consequence, and which of them are still valid is a
        judgement for whoever is reopening the account.
        """
        async with transaction(self.session):
            customer = await self.repo.get(customer_id, include_deleted=True)
            if customer is None:
                raise NotFoundError("Customer", customer_id)

            assert_channel_write_allowed(principal, customer.customer_type)

            current = lifecycle_state(customer)
            # The source state is asserted, not just the edge: the machine also
            # allows ACTIVE -> INACTIVE, so checking the edge alone would let
            # "restore" quietly deactivate a live account.
            if current is not MasterLifecycleState.ARCHIVED:
                raise InvalidStateTransitionError(
                    entity="Customer",
                    current=str(current),
                    requested="RESTORED",
                    allowed=[str(t) for t in CUSTOMER_LIFECYCLE.allowed_targets(current)],
                )

            customer.deleted_at = None
            customer.is_active = False
            self.repo.touch(customer)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_CUSTOMER,
                entity_id=customer.id,
                entity_code=customer.code,
                from_status=str(MasterLifecycleState.ARCHIVED),
                to_status=str(MasterLifecycleState.INACTIVE),
                user_id=self.tenant.user_id,
                reason="Customer restored",
            )

        return await self.get_customer(customer_id)

    # --- addresses ----------------------------------------------------------

    async def list_addresses(
        self,
        customer_id: uuid.UUID,
        params: PageParams,
        *,
        address_type: AddressType | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[AddressRead], OffsetPagination]:
        await self.repo.get_or_404(customer_id)
        stmt = self.addresses.build_list_stmt(
            customer_id, address_type=address_type, is_active=is_active
        )
        rows, pagination = await self.addresses.list_page(params, stmt=stmt, order_by=None)
        return [AddressRead.model_validate(row) for row in rows], pagination

    async def create_address(self, customer_id: uuid.UUID, payload: AddressCreate) -> AddressRead:
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            self._assert_address_gst(payload.country, payload.state_code, payload.gstin)

            existing_default = await self.addresses.get_default(customer.id, payload.address_type)
            # The first address of a type is always the default: a customer with
            # addresses but no selected one makes every document ask a question
            # it should not have to.
            make_default = payload.is_default or existing_default is None

            if make_default and existing_default is not None:
                # Cleared before the insert so the partial unique index never
                # sees two flagged rows, even for an instant.
                await self.addresses.clear_default(customer.id, payload.address_type)

            address = CustomerAddress(
                customer_id=customer.id,
                **{**payload.model_dump(), "is_default": make_default},
            )
            self.addresses.add(address)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ADDRESS,
                entity_id=address.id,
                entity_code=customer.code,
                summary=(
                    f"Added {address.address_type} address for customer {customer.code} "
                    f"({address.city})"
                ),
                user_id=self.tenant.user_id,
                new_values=snapshot(address, _ADDRESS_AUDITED_FIELDS),
                context={"customer_id": str(customer.id)},
            )

        return AddressRead.model_validate(address)

    async def update_address(
        self, customer_id: uuid.UUID, address_id: uuid.UUID, payload: AddressUpdate
    ) -> AddressRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError("No fields were supplied to update.")

        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            address = await self._address_of(customer_id, address_id)
            before = snapshot(address, _ADDRESS_AUDITED_FIELDS)

            # Validate the *resulting* address, not the submitted fragment: a
            # PATCH that changes only the state code must still be checked
            # against the GSTIN already on the row.
            self._assert_address_gst(
                changes.get("country", address.country),
                changes.get("state_code", address.state_code),
                changes.get("gstin", address.gstin),
            )

            if changes.get("is_active") is False and address.is_default:
                raise BusinessRuleError(
                    "The default address cannot be deactivated. Nominate another default "
                    "for this address type first.",
                    details={"address_id": str(address.id)},
                )

            for field, value in changes.items():
                setattr(address, field, value)
            self.addresses.touch(address)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ADDRESS,
                entity_id=address.id,
                entity_code=customer.code,
                summary=f"Updated {address.address_type} address for customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(address, _ADDRESS_AUDITED_FIELDS),
                context={"customer_id": str(customer.id)},
            )

        return AddressRead.model_validate(address)

    async def set_default_address(
        self, customer_id: uuid.UUID, address_id: uuid.UUID
    ) -> AddressRead:
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            address = await self._address_of(customer_id, address_id)

            if not address.is_active:
                raise BusinessRuleError(
                    "An inactive address cannot be made the default.",
                    details={"address_id": str(address.id)},
                )

            # Idempotent: re-nominating the current default is a no-op rather
            # than an error, so a retried request does not fail.
            if not address.is_default:
                await self.addresses.clear_default(
                    customer.id, address.address_type, keep_id=address.id
                )
                address.is_default = True
                self.addresses.touch(address)
                await self.session.flush()

                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CONFIG_CHANGE,
                    entity_type=AUDIT_ADDRESS,
                    entity_id=address.id,
                    entity_code=customer.code,
                    summary=(
                        f"Address {address.id} set as the default "
                        f"{address.address_type} address for {customer.code}"
                    ),
                    user_id=self.tenant.user_id,
                    old_values={"is_default": False},
                    new_values={"is_default": True},
                    context={"customer_id": str(customer.id)},
                )

        return AddressRead.model_validate(address)

    async def delete_address(
        self, customer_id: uuid.UUID, address_id: uuid.UUID, *, reason: str
    ) -> None:
        """Retire an address, promoting a successor if it was the default."""
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            address = await self._address_of(customer_id, address_id)
            before = snapshot(address, _ADDRESS_AUDITED_FIELDS)

            promoted: CustomerAddress | None = None
            if address.is_default:
                promoted = await self.addresses.next_default_candidate(
                    customer.id, address.address_type, exclude_id=address.id
                )
                # Cleared first: the row keeps its flag until the delete lands,
                # and two rows must never carry it at once.
                address.is_default = False
                if promoted is not None:
                    promoted.is_default = True
                    self.addresses.touch(promoted)

            await self.addresses.soft_delete(address)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ADDRESS,
                entity_id=address.id,
                entity_code=customer.code,
                summary=f"Removed {address.address_type} address for customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                context={
                    "reason": reason,
                    "customer_id": str(customer.id),
                    "promoted_default_id": str(promoted.id) if promoted else None,
                },
            )

    # --- contacts -----------------------------------------------------------

    async def list_contacts(
        self,
        customer_id: uuid.UUID,
        params: PageParams,
        *,
        is_active: bool | None = None,
    ) -> tuple[list[ContactRead], OffsetPagination]:
        await self.repo.get_or_404(customer_id)
        stmt = self.contacts.build_list_stmt(customer_id, is_active=is_active)
        rows, pagination = await self.contacts.list_page(params, stmt=stmt, order_by=None)
        return [ContactRead.model_validate(row) for row in rows], pagination

    async def create_contact(self, customer_id: uuid.UUID, payload: ContactCreate) -> ContactRead:
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            self._assert_contact_reachable(payload.email, payload.phone)

            # The first contact of a customer becomes the primary one: an
            # account with contacts but nobody nominated leaves every
            # notification asking who to write to.
            has_primary = await self.contacts.has_primary(customer.id)
            make_primary = payload.is_primary or not has_primary

            if make_primary and has_primary:
                await self.contacts.clear_primary(customer.id)

            contact = CustomerContact(
                customer_id=customer.id,
                **{**payload.model_dump(), "is_primary": make_primary},
            )
            self.contacts.add(contact)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_CONTACT,
                entity_id=contact.id,
                entity_code=customer.code,
                summary=f"Added contact {contact.name} for customer {customer.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(contact, _CONTACT_AUDITED_FIELDS),
                context={"customer_id": str(customer.id)},
            )

        return ContactRead.model_validate(contact)

    async def update_contact(
        self, customer_id: uuid.UUID, contact_id: uuid.UUID, payload: ContactUpdate
    ) -> ContactRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError("No fields were supplied to update.")

        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            contact = await self._contact_of(customer_id, contact_id)
            before = snapshot(contact, _CONTACT_AUDITED_FIELDS)

            self._assert_contact_reachable(
                changes.get("email", contact.email), changes.get("phone", contact.phone)
            )

            if changes.get("is_active") is False and contact.is_primary:
                raise BusinessRuleError(
                    "The primary contact cannot be deactivated. Nominate another primary "
                    "contact first.",
                    details={"contact_id": str(contact.id)},
                )

            for field, value in changes.items():
                setattr(contact, field, value)
            self.contacts.touch(contact)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_CONTACT,
                entity_id=contact.id,
                entity_code=customer.code,
                summary=f"Updated contact {contact.name} for customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(contact, _CONTACT_AUDITED_FIELDS),
                context={"customer_id": str(customer.id)},
            )

        return ContactRead.model_validate(contact)

    async def set_primary_contact(
        self, customer_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ContactRead:
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            contact = await self._contact_of(customer_id, contact_id)

            if not contact.is_active:
                raise BusinessRuleError(
                    "An inactive contact cannot be made the primary contact.",
                    details={"contact_id": str(contact.id)},
                )

            if not contact.is_primary:
                await self.contacts.clear_primary(customer.id, keep_id=contact.id)
                contact.is_primary = True
                self.contacts.touch(contact)
                await self.session.flush()

                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CONFIG_CHANGE,
                    entity_type=AUDIT_CONTACT,
                    entity_id=contact.id,
                    entity_code=customer.code,
                    summary=f"{contact.name} set as primary contact for {customer.code}",
                    user_id=self.tenant.user_id,
                    old_values={"is_primary": False},
                    new_values={"is_primary": True},
                    context={"customer_id": str(customer.id)},
                )

        return ContactRead.model_validate(contact)

    async def delete_contact(
        self, customer_id: uuid.UUID, contact_id: uuid.UUID, *, reason: str
    ) -> None:
        async with transaction(self.session):
            customer = await self.repo.get_or_404(customer_id)
            contact = await self._contact_of(customer_id, contact_id)
            before = snapshot(contact, _CONTACT_AUDITED_FIELDS)

            promoted: CustomerContact | None = None
            if contact.is_primary:
                promoted = await self.contacts.next_primary_candidate(
                    customer.id, exclude_id=contact.id
                )
                contact.is_primary = False
                if promoted is not None:
                    promoted.is_primary = True
                    self.contacts.touch(promoted)

            await self.contacts.soft_delete(contact)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_CONTACT,
                entity_id=contact.id,
                entity_code=customer.code,
                summary=f"Removed contact {contact.name} for customer {customer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                context={
                    "reason": reason,
                    "customer_id": str(customer.id),
                    "promoted_primary_id": str(promoted.id) if promoted else None,
                },
            )

    # --- internals ----------------------------------------------------------

    async def _assert_sales_user(self, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        if not await self.repo.sales_user_exists(user_id):
            raise NotFoundError("User", user_id)

    async def _assert_transporter(self, transporter_id: uuid.UUID | None) -> None:
        """Validate the default carrier without a foreign key.

        ``None`` from the probe means the transporters table is not present in
        this database (the vendors module has not been migrated here). The
        reference is then stored unverified rather than blocking an unrelated
        edit - which is the trade accepted when the FK was deliberately left
        off; see ``Customer.default_transporter_id``.
        """
        if transporter_id is None:
            return
        exists = await self.repo.transporter_exists(transporter_id)
        if exists is None:
            log.info(
                "customers.transporter_unverified",
                transporter_id=str(transporter_id),
                reason="transporters table not present",
            )
            return
        if not exists:
            raise NotFoundError("Transporter", transporter_id)

    def _assert_address_gst(
        self, country: str | None, state_code: str | None, gstin: str | None
    ) -> None:
        if (country or "India") == "India" and not state_code:
            raise BusinessRuleError(
                "A state code is required for an Indian address: GST cannot be charged "
                "correctly without knowing the place of supply.",
                details={"field": "state_code"},
            )
        assert_gstin_matches_state(gstin, state_code)

    def _assert_contact_reachable(self, email: str | None, phone: str | None) -> None:
        if not email and not phone:
            raise BusinessRuleError(
                "A contact needs at least an e-mail address or a phone number.",
                details={"fields": ["email", "phone"]},
            )

    async def _address_of(self, customer_id: uuid.UUID, address_id: uuid.UUID) -> CustomerAddress:
        """Load an address and confirm it belongs to the customer in the URL.

        Without this check a caller could edit any address in the tenant through
        any customer's path, which would let them repoint another account's
        ship-to.
        """
        address = await self.addresses.get_or_404(address_id)
        if address.customer_id != customer_id:
            raise NotFoundError("Customer address", address_id)
        return address

    async def _contact_of(self, customer_id: uuid.UUID, contact_id: uuid.UUID) -> CustomerContact:
        contact = await self.contacts.get_or_404(contact_id)
        if contact.customer_id != customer_id:
            raise NotFoundError("Customer contact", contact_id)
        return contact

    def _to_detail(
        self,
        customer: Customer,
        addresses: list[CustomerAddress],
        contacts: list[CustomerContact],
    ) -> CustomerDetail:
        """Assemble the detail response from an explicit field list.

        Built from a dict rather than by validating the ORM instance directly:
        ``Customer.addresses`` and ``Customer.contacts`` are ``lazy="raise"``,
        so attribute-mode validation would try to read them and raise. The
        children are supplied by the caller, which has already fetched them in
        two bounded queries.
        """
        assessment = evaluate_credit(customer, Decimal("0"))
        data: dict[str, Any] = {
            field: getattr(customer, field) for field in _CUSTOMER_DETAIL_FIELDS
        }
        data["addresses"] = [AddressRead.model_validate(row) for row in addresses]
        data["contacts"] = [ContactRead.model_validate(row) for row in contacts]
        data["credit_headroom"] = assessment.headroom
        return CustomerDetail.model_validate(data)


# -----------------------------------------------------------------------------
# Cross-module entry points
# -----------------------------------------------------------------------------


async def get_customer_for_order(
    session: AsyncSession, tenant: TenantContext, customer_id: uuid.UUID, *, lock: bool = False
) -> Customer:
    """Load a customer for a sales document.

    ``lock=True`` takes a row lock, which a caller should use when it is about
    to consume credit headroom in the same transaction: reading the balance
    without the lock and writing the order after it is the classic double-spend
    of a credit limit.
    """
    repo = CustomerRepository(session, tenant)
    customer = await (repo.lock_for_update(customer_id) if lock else repo.get(customer_id))
    if customer is None:
        raise NotFoundError("Customer", customer_id)
    return customer


async def assert_customer_can_order(
    session: AsyncSession,
    tenant: TenantContext,
    customer_id: uuid.UUID,
    order_value: Decimal,
    *,
    lock: bool = True,
) -> CreditAssessment:
    """Load the customer and run the credit check in one call.

    The convenience form of :func:`assert_within_credit_limit` for callers that
    hold only a customer id. Locks by default, because the caller is by
    definition about to add exposure.
    """
    customer = await get_customer_for_order(session, tenant, customer_id, lock=lock)
    return assert_within_credit_limit(customer, order_value)


async def apply_outstanding_delta(
    session: AsyncSession,
    tenant: TenantContext,
    customer_id: uuid.UUID,
    adjustment: OutstandingAdjustment,
) -> Customer:
    """Apply a finance-owned change to the cached outstanding balance.

    **The only supported way that column changes.** Must be called inside the
    transaction that posts the invoice, receipt or credit note it describes, so
    the cached balance and the ledger commit or roll back together.

    The row is locked before it is read: two invoices posting for the same
    customer concurrently would otherwise both read the old balance and one
    increment would be lost, quietly restoring credit that was already consumed.
    """
    repo = CustomerRepository(session, tenant)

    async with transaction(session):
        customer = await repo.lock_for_update(customer_id)
        if customer is None:
            raise NotFoundError("Customer", customer_id)

        before = to_money(customer.outstanding_amount or Decimal("0"))
        after = before + to_money(adjustment.delta)
        customer.outstanding_amount = after
        repo.touch(customer)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_CUSTOMER,
            entity_id=customer.id,
            entity_code=customer.code,
            summary=(
                f"Outstanding balance for {customer.code} changed by {adjustment.delta} "
                f"({before} -> {after})"
            ),
            user_id=tenant.user_id,
            old_values={"outstanding_amount": before},
            new_values={"outstanding_amount": after},
            context={
                "reason": adjustment.reason,
                "delta": str(adjustment.delta),
                "reference_type": adjustment.reference_type,
                "reference_id": str(adjustment.reference_id) if adjustment.reference_id else None,
                "reference_code": adjustment.reference_code,
            },
        )

    return customer


__all__ = [
    "AUDIT_ADDRESS",
    "AUDIT_CONTACT",
    "AUDIT_CUSTOMER",
    "BLOCK_RETIREMENT_WITH_OUTSTANDING",
    "CREDIT_LIMIT_NULL_MEANS_UNLIMITED",
    "CREDIT_STANDING",
    "CUSTOMER_LIFECYCLE",
    "DEALER_CHANNELS",
    "ENFORCE_GSTIN_STATE_CODE_MATCH",
    "INACTIVE_CUSTOMER_MAY_NOT_ORDER",
    "MAX_EMBEDDED_CHILDREN",
    "CreditAssessment",
    "CreditStanding",
    "CreditSubject",
    "CustomerService",
    "MasterLifecycleState",
    "apply_outstanding_delta",
    "assert_channel_write_allowed",
    "assert_customer_can_order",
    "assert_gstin_matches_state",
    "assert_within_credit_limit",
    "credit_standing",
    "credit_status_of",
    "evaluate_credit",
    "get_customer_for_order",
    "gstin_state_code",
    "lifecycle_state",
    "requires_dealer_permission",
]
