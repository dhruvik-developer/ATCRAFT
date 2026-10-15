"""Sales order business rules.

The rules that matter, and why each one lives here rather than in the caller:

1. **Confirming an order reserves what exists and reports what does not.**
   Partial reservation is the correct outcome, not a failure: the shortfall per
   line is exactly the finished-goods shortage MRP exists to cover (master rules
   section 72). Refusing the whole order because one line is short would make
   the shortage invisible until somebody tried to pick it.
2. **Price comes from the price list, not from the request.** ``unit_price`` on
   a payload is an *override*, allowed only to ``price.manage``; below the
   item's cost it also needs a second person's approval before the order can be
   confirmed.
3. **GST treatment is decided once, from two state codes, and then frozen.**
   Intra-state supply is CGST+SGST, inter-state is IGST. Both halves of an
   intra-state split are rounded independently, because that is what the
   invoice has to print.
4. **The credit check happens inside the confirming transaction**, against a
   locked customer row. Checking outside it and writing inside re-opens the
   double-spend the limit exists to prevent.
5. **Every write is one transaction with its audit row**, and every status
   change goes through :data:`SALES_ORDER_LIFECYCLE`.

Allocation is reached through :class:`AllocationPort`. The default
implementation delegates to ``src.modules.allocation.service`` exactly as
database.md section 4.5 specifies; the seam exists so this module's rules -
"reserve the minimum of demand and availability, report the rest" - can be
exercised without standing up the whole reservation engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.currency import assert_customer_currency
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AppError,
    ApprovalRequiredError,
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity, to_unit_price
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.customers.models import Customer
from src.modules.customers.service import assert_customer_can_order, get_customer_for_order
from src.modules.products.models import ItemPrice, PriceType
from src.modules.products.service import ITEM_MASTER_FALLBACK_FIELD, select_price_row
from src.modules.sales.models import (
    RESERVING_STATUSES,
    SalesOrder,
    SalesOrderLine,
    SalesOrderLineStatus,
    SalesOrderStatus,
)
from src.modules.sales.repository import (
    SALES_ORDER_ENTITY,
    AvailabilityRepository,
    ItemLookupRepository,
    OrderPartyRepository,
    PriceListRepository,
    SalesOrderLineRepository,
    SalesOrderRepository,
    SellableItem,
    VariantInfo,
)
from src.modules.sales.schemas import (
    ConfirmResult,
    DemandSummaryFilters,
    DemandSummaryRow,
    LineAllocationRead,
    OrderAllocationRead,
    SalesOrderCreate,
    SalesOrderDetail,
    SalesOrderFilters,
    SalesOrderLineIn,
    SalesOrderLineRead,
    SalesOrderSummary,
    SalesOrderUpdate,
)

log = get_logger(__name__)

AUDIT_SALES_ORDER = "sales_order"

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
ORDER_AUDITED_FIELDS = [
    "code",
    "customer_id",
    "customer_po_number",
    "source",
    "order_date",
    "required_date",
    "promised_date",
    "branch_id",
    "warehouse_id",
    "status",
    "priority",
    "is_urgent",
    "currency",
    "subtotal",
    "discount_amount",
    "tax_amount",
    "total_amount",
    "is_interstate",
    "place_of_supply_state_code",
    "billing_address_id",
    "shipping_address_id",
    "transporter_id",
    "requires_approval",
    "notes",
]

#: Header fields a confirmed order will not accept a change to. Each one either
#: moves the tax basis (addresses, branch) or invalidates every reservation the
#: order holds (warehouse), so changing it after confirmation would silently
#: leave the document describing something other than what was reserved.
FROZEN_AFTER_CONFIRMATION = (
    "warehouse_id",
    "branch_id",
    "billing_address_id",
    "shipping_address_id",
)

#: ASSUMPTION: the approver of a sales order may not be the person who raised
#: it, unless they hold ``SUPER_ADMIN`` (or are a platform superuser). Approval
#: is evidence of a second person's judgement (master rules section 42), and a
#: below-cost price signed off by the salesperson who granted it is not
#: evidence of anything. One predicate, in one place, if a small team wants it
#: relaxed.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"


def is_below_cost(unit_price: Decimal, standard_cost: Decimal) -> bool:
    """Whether an overridden price sits under the item's costing floor.

    ASSUMPTION: "cost" here is ``items.standard_cost``, the item master's own
    costing figure. Moving-average cost lives per warehouse on
    ``inventory_balances`` and would make the same price acceptable from one
    store and not from another, which is not a commercial rule anybody would
    recognise.

    ASSUMPTION: an item whose standard cost is zero - never costed - is not
    treated as "everything is below cost". A zero floor would demand an
    approval for every single line on a freshly loaded item master, and an
    approval everyone clicks through protects nothing.
    """
    return standard_cost > ZERO and unit_price < standard_cost


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move sales_orders.status
# -----------------------------------------------------------------------------

SALES_ORDER_LIFECYCLE: StateMachine[SalesOrderStatus] = StateMachine(
    entity="Sales order",
    initial=SalesOrderStatus.DRAFT,
    transitions=(
        Transition(
            SalesOrderStatus.DRAFT,
            SalesOrderStatus.CONFIRMED,
            permission=Permission.SALES_ORDER_UPDATE,
            label="Confirm",
            # Confirmation runs the credit check and takes stock reservations
            # away from every other order competing for the same items.
            is_irreversible=True,
        ),
        Transition(
            SalesOrderStatus.CONFIRMED,
            SalesOrderStatus.ALLOCATED,
            permission=Permission.SALES_ORDER_UPDATE,
            label="Fully allocated",
        ),
        # A partly reserved order may ship what is reserved (K08): the reserved
        # part of a line leaves now, and the balance waits for stock and the
        # top-up. Each consigned line is capped at its live reservation
        # (``dispatchable_quantity``), so this edge never ships unpromised stock.
        Transition(
            SalesOrderStatus.CONFIRMED,
            SalesOrderStatus.PARTIALLY_DISPATCHED,
            permission=Permission.DISPATCH_CREATE,
            label="Dispatch",
            is_irreversible=True,
        ),
        # ALLOCATED means every open line is fully reserved. A quantity increase
        # the free stock cannot cover takes that away again (docs/assumptions.md
        # section E1-3); an under-reserved order must not keep the label.
        Transition(
            SalesOrderStatus.ALLOCATED,
            SalesOrderStatus.CONFIRMED,
            permission=Permission.SALES_ORDER_UPDATE,
            label="Allocation shortfall",
        ),
        Transition(
            SalesOrderStatus.ALLOCATED,
            SalesOrderStatus.PARTIALLY_DISPATCHED,
            permission=Permission.DISPATCH_CREATE,
            label="First dispatch",
            is_irreversible=True,
        ),
        Transition(
            SalesOrderStatus.PARTIALLY_DISPATCHED,
            SalesOrderStatus.DISPATCHED,
            permission=Permission.DISPATCH_CREATE,
            label="Final dispatch",
            is_irreversible=True,
        ),
        Transition(
            SalesOrderStatus.DISPATCHED,
            SalesOrderStatus.INVOICED,
            permission=Permission.SALES_INVOICE_CREATE,
            label="Invoice",
            is_irreversible=True,
        ),
        Transition(
            SalesOrderStatus.INVOICED,
            SalesOrderStatus.CLOSED,
            permission=Permission.SALES_ORDER_UPDATE,
            label="Close",
        ),
        Transition(
            SalesOrderStatus.INVOICED,
            SalesOrderStatus.DISPATCHED,
            permission=Permission.SALES_INVOICE_CANCEL,
            label="Invoice cancelled",
        ),
        # Cancellation is reachable only before anything has shipped
        # (database.md section 7): a document that moved stock cannot claim
        # never to have existed.
        Transition(
            SalesOrderStatus.DRAFT,
            SalesOrderStatus.CANCELLED,
            permission=Permission.SALES_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            SalesOrderStatus.CONFIRMED,
            SalesOrderStatus.CANCELLED,
            permission=Permission.SALES_ORDER_CANCEL,
            label="Cancel",
            is_irreversible=True,
        ),
        Transition(
            SalesOrderStatus.ALLOCATED,
            SalesOrderStatus.CANCELLED,
            permission=Permission.SALES_ORDER_CANCEL,
            label="Cancel",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({SalesOrderStatus.CLOSED, SalesOrderStatus.CANCELLED}),
)

#: Statuses the allocation top-up (:func:`allocate_order`) works on: every
#: status that still holds reservations and may still ship. An ALLOCATED order
#: is included because a reservation released or expired elsewhere leaves it
#: short, and the top-up is what re-reserves it.
TOP_UP_STATUSES: tuple[SalesOrderStatus, ...] = (
    SalesOrderStatus.CONFIRMED,
    SalesOrderStatus.ALLOCATED,
    SalesOrderStatus.PARTIALLY_DISPATCHED,
)


# -----------------------------------------------------------------------------
# Pure rules - unit tested without a database
# -----------------------------------------------------------------------------


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Segregation of duties on approval. See ``SUPER_ADMIN_ROLE_CODE``."""
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


def resolve_tax_treatment(seller_state_code: str | None, place_of_supply: str | None) -> bool:
    """True when IGST applies, i.e. the supply crosses a state boundary.

    ASSUMPTION: the supply is treated as intra-state only when both state codes
    are known and equal. An export or an address with no state code has no
    Indian place of supply, and IGST is the treatment for a supply whose place
    of supply is outside the seller's state - including outside India. Guessing
    "same state" from a missing value would understate the tax on every export.
    """
    if seller_state_code is None or place_of_supply is None:
        return True
    return seller_state_code != place_of_supply


@dataclass(frozen=True, slots=True)
class LineAmounts:
    """Everything money-shaped about one line, all at money scale."""

    gross_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


def compute_line_amounts(
    *,
    quantity: Decimal,
    unit_price: Decimal,
    tax_rate: Decimal,
    is_interstate: bool,
    discount_percentage: Decimal | None = None,
    discount_amount: Decimal | None = None,
) -> LineAmounts:
    """Price one line: gross, discount, taxable, GST split, total.

    Rounding is applied at each stored value rather than once at the end,
    because every one of these numbers is written to a ``NUMERIC`` column and
    printed on an invoice - a total that cannot be reproduced from the columns
    beside it is a reconciliation problem, not a rounding detail.

    The intra-state split rounds CGST and SGST **independently and identically**
    and takes their sum as the tax. Halving a rounded total instead would, at a
    5% rate on 1234.55, produce two halves that do not add back to it. Indian
    invoicing rounds each component, so the two components are what is computed
    and the total is what falls out.
    """
    if quantity <= 0:
        raise ValidationError("Line quantity must be greater than zero.")
    if unit_price < 0:
        raise ValidationError("Line unit price cannot be negative.")
    if tax_rate < 0:
        raise ValidationError("Tax rate cannot be negative.")

    gross = to_money(Decimal(quantity) * Decimal(unit_price))

    if discount_percentage is not None and discount_percentage > 0:
        discount = to_money(gross * Decimal(discount_percentage) / Decimal(100))
    elif discount_amount is not None:
        discount = to_money(discount_amount)
    else:
        discount = to_money(ZERO)

    if discount > gross:
        raise ValidationError(
            "Line discount is larger than the line value.",
            details={"gross_amount": str(gross), "discount_amount": str(discount)},
        )

    taxable = gross - discount
    rate = Decimal(tax_rate)

    if is_interstate:
        igst = to_money(taxable * rate / Decimal(100))
        cgst = sgst = to_money(ZERO)
        tax = igst
    else:
        half = to_money(taxable * (rate / Decimal(2)) / Decimal(100))
        cgst = sgst = half
        igst = to_money(ZERO)
        tax = cgst + sgst

    return LineAmounts(
        gross_amount=gross,
        discount_amount=discount,
        taxable_amount=taxable,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
        tax_amount=tax,
        line_total=taxable + tax,
    )


@dataclass(frozen=True, slots=True)
class OrderTotals:
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal


def sum_order_totals(amounts: Sequence[LineAmounts]) -> OrderTotals:
    """Roll line amounts up to the header.

    ``subtotal`` is the gross value before discount, so the header reads
    ``subtotal - discount + tax = total`` - the arithmetic a customer checks by
    hand on the acknowledgement.
    """
    subtotal = to_money(sum((a.gross_amount for a in amounts), ZERO))
    discount = to_money(sum((a.discount_amount for a in amounts), ZERO))
    tax = to_money(sum((a.tax_amount for a in amounts), ZERO))
    return OrderTotals(
        subtotal=subtotal,
        discount_amount=discount,
        tax_amount=tax,
        total_amount=subtotal - discount + tax,
    )


def line_status_for(
    *,
    quantity: Decimal,
    allocated_quantity: Decimal,
    dispatched_quantity: Decimal,
    cancelled: bool = False,
) -> SalesOrderLineStatus:
    """Derive a line's status from its quantities.

    Derived rather than assigned so a line can never claim ALLOCATED while its
    reserved quantity is short - the status and the numbers are one fact, and
    two writers of the same fact eventually disagree.
    """
    if cancelled:
        return SalesOrderLineStatus.CANCELLED
    if dispatched_quantity >= quantity:
        return SalesOrderLineStatus.DISPATCHED
    if dispatched_quantity > 0:
        return SalesOrderLineStatus.PARTIALLY_DISPATCHED
    if allocated_quantity >= quantity:
        return SalesOrderLineStatus.ALLOCATED
    if allocated_quantity > 0:
        return SalesOrderLineStatus.PARTIALLY_ALLOCATED
    return SalesOrderLineStatus.PENDING


def reservable_quantity(demand: Decimal, available: Decimal) -> Decimal:
    """How much of ``demand`` can be promised from ``available``.

    The whole of master rules section 72 in one line: reserve what exists, never
    more, never refuse the order because it is not all there.
    """
    if available <= 0:
        return to_quantity(ZERO)
    return to_quantity(min(Decimal(demand), Decimal(available)))


def dispatchable_quantity(
    *, ordered: Decimal, dispatched: Decimal, reserved: Decimal, planned: Decimal = ZERO
) -> Decimal:
    """How much of one order line a new consignment may carry right now.

    ``min(live reservation remaining, ordered - dispatched - already on other
    open consignments)``, never below zero. ``reserved`` is what the line's
    reservation still holds *live*: a released, expired or consumed reservation
    counts as zero, so a line with no live promise is simply not dispatchable
    yet. ``planned`` is what consignments raised but not yet sent already carry
    for the line - counting it is what stops two plans shipping one balance.

    ASSUMPTION: the live reservation is the promise, and free stock may not
    bypass it on an order-linked line (docs/assumptions.md section E1-1).
    Anything beyond it has to be reserved first - by the top-up - so a
    consignment can never ship stock another order was promised.
    """
    outstanding = Decimal(ordered) - Decimal(dispatched) - Decimal(planned)
    return to_quantity(max(min(Decimal(reserved), outstanding), ZERO))


def line_shortfall(line: SalesOrderLine, reserved: Decimal) -> Decimal:
    """What a line still needs reserved: open demand minus its live reservation."""
    if SalesOrderLineStatus(line.status) is SalesOrderLineStatus.CANCELLED:
        return to_quantity(ZERO)
    outstanding = Decimal(line.quantity) - Decimal(line.dispatched_quantity)
    return to_quantity(max(outstanding - Decimal(reserved), ZERO))


# -----------------------------------------------------------------------------
# Allocation seam
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReservationRef:
    """The part of a ``StockReservation`` this module needs to keep."""

    id: uuid.UUID
    quantity: Decimal


class AllocationPort(Protocol):
    """The allocation contract sales depends on (database.md section 4.5)."""

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        quantity: Decimal,
        demand_id: uuid.UUID,
        demand_line_id: uuid.UUID,
        demand_code: str,
        priority: int,
        required_date: date,
        reason: str,
    ) -> ReservationRef: ...

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> None: ...


class AllocationService:
    """Default port implementation: calls ``src.modules.allocation.service``.

    The import is deferred to call time rather than done at module import. The
    allocation module is deployed independently of this one, and a top-level
    import would make the whole sales module - list screens, pricing, the demand
    summary - unimportable in an environment where allocation is not present.
    Failing at the one operation that genuinely needs it, with an error that
    says so, is the useful behaviour.
    """

    @staticmethod
    def _module() -> Any:
        try:
            import src.modules.allocation.service as allocation_service
        except ModuleNotFoundError as exc:  # pragma: no cover - deployment shape
            raise AppError(
                "Stock allocation is unavailable, so this order cannot be reserved.",
                code=ErrorCode.SERVICE_UNAVAILABLE,
                status_code=503,
            ) from exc
        return allocation_service

    @staticmethod
    def _vocabulary() -> Any:
        """The allocation module's own enums, imported with its service.

        The demand and reservation vocabularies are owned there and validated
        against a database CHECK; passing bare strings would work until somebody
        compared one with ``is``.
        """
        try:
            import src.modules.allocation.models as allocation_models
        except ModuleNotFoundError as exc:  # pragma: no cover - deployment shape
            raise AppError(
                "Stock allocation is unavailable, so this order cannot be reserved.",
                code=ErrorCode.SERVICE_UNAVAILABLE,
                status_code=503,
            ) from exc
        return allocation_models

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        quantity: Decimal,
        demand_id: uuid.UUID,
        demand_line_id: uuid.UUID,
        demand_code: str,
        priority: int,
        required_date: date,
        reason: str,
    ) -> ReservationRef:
        vocabulary = self._vocabulary()
        reservation = await self._module().reserve(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            demand_type=vocabulary.DemandType.SALES_ORDER,
            demand_id=demand_id,
            demand_line_id=demand_line_id,
            # Carried so an allocation screen or a reallocation approval can name
            # the order without joining a table the approver may not read.
            demand_code=demand_code,
            # HARD: a customer commitment is not something reallocation may take
            # without an approved reallocation request (database.md section 4.5).
            reservation_type=vocabulary.ReservationType.HARD,
            priority=priority,
            required_date=required_date,
            reason=reason,
        )
        return ReservationRef(id=reservation.id, quantity=Decimal(reservation.quantity))

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> None:
        """Give a line's reservation back - a no-op when it is already closed.

        The allocation engine refuses to release a RELEASED, EXPIRED or CONSUMED
        reservation, because nothing of it is outstanding. A sales path that
        gives a line's stock back - cancelling the order, re-reserving a changed
        line, topping one up - must not fail because the promise was already
        returned elsewhere (from the allocation screen, by expiry), so a closed
        reservation is treated as "no live reservation" and skipped. Keeping the
        order line in step when allocation releases on its own is WP E2's seam.
        """
        live = await SalesOrderLineRepository(session, tenant).live_reservation_quantities(
            [reservation_id]
        )
        if reservation_id not in live:
            return
        await self._module().release(
            session, tenant, reservation_id=reservation_id, reason=reason, quantity=quantity
        )


def _allocation(port: AllocationPort | None) -> AllocationPort:
    return port if port is not None else AllocationService()


async def live_reservations(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[SalesOrderLine]
) -> dict[uuid.UUID, Decimal]:
    """What each line's reservation still holds, keyed by reservation id.

    One statement for the whole line set. Only open reservations are present,
    so :func:`live_reserved` reads a closed one as zero.
    """
    return await SalesOrderLineRepository(session, tenant).live_reservation_quantities(
        [line.reservation_id for line in lines if line.reservation_id is not None]
    )


def live_reserved(line: SalesOrderLine, live: Mapping[uuid.UUID, Decimal]) -> Decimal:
    """One line's live reservation: zero when it holds none, or only a closed one."""
    if line.reservation_id is None:
        return ZERO
    return Decimal(live.get(line.reservation_id, ZERO))


# -----------------------------------------------------------------------------
# Dispatch seam - what open consignments already carry
# -----------------------------------------------------------------------------


class DispatchPlanPort(Protocol):
    """The dispatch module's count of what unsent consignments carry per order line."""

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        sales_order_id: uuid.UUID,
        *,
        exclude_dispatch_id: uuid.UUID | None = None,
    ) -> dict[uuid.UUID, Decimal]: ...


def resolve_dispatch_plan_port() -> DispatchPlanPort | None:
    """``dispatch.service.open_dispatch_quantities``, if dispatch is deployed.

    Dispatch sits downstream of sales and imports it, so sales reaches it at
    call time through this guarded import - the way procurement reaches quality
    - rather than at module import. Only the module's *absence* is tolerated:
    without dispatch there are no open consignments, which is exactly what an
    empty answer says.
    """
    try:
        from src.modules.dispatch import service as dispatch_service
    except ImportError:  # pragma: no cover - deployment shape
        return None
    reader = getattr(dispatch_service, "open_dispatch_quantities", None)
    return reader if callable(reader) else None


async def planned_dispatch_quantities(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Quantity per order line already on consignments raised but not yet sent."""
    reader = resolve_dispatch_plan_port()
    if reader is None:  # pragma: no cover - deployment shape
        return {}
    return await reader(session, tenant, order_id)


# -----------------------------------------------------------------------------
# Pricing
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PricingContext:
    """Everything needed to price a whole order, already loaded."""

    price_type: PriceType
    on_date: date
    is_interstate: bool
    items: dict[uuid.UUID, SellableItem]
    variants: dict[uuid.UUID, VariantInfo]
    price_rows: dict[uuid.UUID, list[ItemPrice]]
    #: Dealer schemes that may discount a line. Empty for a caller that priced
    #: without them - and with no offers configured nothing changes for
    #: existing ERP orders (docs/assumptions.md §C3-3).
    offers: tuple = ()
    #: Category id to itself plus its ancestors, for CATEGORY-scoped offers.
    offer_ancestors: dict[uuid.UUID, frozenset[uuid.UUID]] = dc_field(default_factory=dict)
    #: The customer the offers are evaluated for (negotiated schemes).
    offer_customer_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PricedLine:
    """One fully costed line, ready to be written to a row."""

    payload: SalesOrderLineIn
    item: SellableItem
    unit_price: Decimal
    price_source: str
    uom_id: uuid.UUID
    description: str
    tax_rate: Decimal
    amounts: LineAmounts
    is_price_override: bool
    is_below_cost: bool
    #: The effective discount percentage: the payload's own, or the best
    #: dealer scheme when the caller gave none (docs/assumptions.md §C3-3).
    discount_percentage: Decimal = Decimal("0")
    #: The scheme the discount came from, if any - stored on the line for
    #: traceability. Explicit discounts carry none.
    offer_id: uuid.UUID | None = None


def _dealer_offer_functions() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """The dealer scheme matcher, imported late.

    ``dealer_portal`` sits downstream of sales and imports this module's
    service for checkout, so a top-level import would cycle. The same deferred
    seam ``AllocationService._module`` uses for the same reason.
    """
    from src.modules.dealer_portal.pricing import load_pricing_offers, select_best_offer

    return load_pricing_offers, select_best_offer


def resolve_unit_price(
    *,
    item: SellableItem,
    variant: VariantInfo | None,
    rows: Sequence[ItemPrice],
    quantity: Decimal,
    price_type: PriceType,
) -> tuple[Decimal, str]:
    """The list price for this item, channel and quantity.

    Slab ranking is ``products.service.select_price_row`` rather than a copy:
    sales and the price-list screen must never disagree about what an item
    costs. When no row applies, the item master's channel column is the
    documented fallback - and the distributor channel has no such column, so the
    resolver refuses rather than quietly invoicing at the dealer rate.
    """
    chosen = select_price_row(rows, quantity, variant_id=variant.id if variant else None)
    if chosen is not None:
        # A variant-specific row is already the variant's price; the delta
        # applies only when an item-wide row was matched for a variant.
        delta = variant.price_delta if variant is not None and chosen.variant_id is None else ZERO
        return to_unit_price(Decimal(chosen.price) + delta), "PRICE_LIST"

    fallback_field = ITEM_MASTER_FALLBACK_FIELD[price_type]
    if fallback_field is None:
        raise BusinessRuleError(
            f"No {price_type} price is configured for item {item.code}. "
            "Add a price list entry for this channel.",
            details={"item_code": item.code, "price_type": str(price_type)},
        )
    base = Decimal(getattr(item, fallback_field))
    delta = variant.price_delta if variant is not None else ZERO
    return to_unit_price(base + delta), "ITEM_MASTER"


def price_line(
    payload: SalesOrderLineIn,
    context: PricingContext,
    principal: Principal,
    *,
    enforce_override_authority: bool = True,
) -> PricedLine:
    """Resolve price and tax for one line, enforcing the override rules.

    A ``unit_price`` on the payload is an override and nothing else: it is
    refused outright without ``price.manage``, and when it lands below the
    item's cost the order is flagged as needing approval rather than silently
    accepted (master rules section 42).

    ``enforce_override_authority=False`` is for the one caller that did not get
    the price from a request: :func:`_update_draft` rebuilds payloads from the
    order's own stored lines when a header edit changes the tax treatment, and
    those prices were authorised when they were written. Re-demanding
    ``price.manage`` there would mean nobody could edit a delivery date on an
    order that once carried an approved discount.

    When the payload carries no explicit discount - neither a percentage nor a
    flat amount - the line takes the best active dealer scheme as its default
    discount, and the scheme's id is stored on the line for traceability. An
    explicit discount always wins and carries no scheme
    (docs/assumptions.md §C3-3).
    """
    item = context.items.get(payload.item_id)
    if item is None:
        raise NotFoundError("Item", payload.item_id)

    variant: VariantInfo | None = None
    if payload.item_variant_id is not None:
        variant = context.variants.get(payload.item_variant_id)
        if variant is None or variant.item_id != item.id:
            raise NotFoundError("Item variant", payload.item_variant_id)
        if not variant.is_active:
            raise BusinessRuleError(
                f"Line {payload.line_number}: variant {variant.code} is inactive.",
                details={"line_number": payload.line_number, "variant_code": variant.code},
            )

    is_override = payload.unit_price is not None
    below_cost = False

    if is_override:
        if enforce_override_authority and not principal.has(Permission.PRICE_MANAGE):
            raise PermissionDeniedError(str(Permission.PRICE_MANAGE))
        unit_price = to_unit_price(payload.unit_price or ZERO)
        source = "OVERRIDE"
        below_cost = is_below_cost(unit_price, item.standard_cost)
    else:
        unit_price, source = resolve_unit_price(
            item=item,
            variant=variant,
            rows=context.price_rows.get(item.id, ()),
            quantity=payload.quantity,
            price_type=context.price_type,
        )

    discount_percentage = payload.discount_percentage
    offer_id: uuid.UUID | None = None
    if discount_percentage is None and payload.discount_amount is None and context.offers:
        _, select_best_offer = _dealer_offer_functions()
        if item.category_id is not None:
            category_ids = context.offer_ancestors.get(
                item.category_id, frozenset({item.category_id})
            )
        else:
            category_ids = frozenset()
        best = select_best_offer(
            context.offers,
            item_id=item.id,
            category_ids=category_ids,
            brand_id=item.brand_id,
            quantity=payload.quantity,
            price_type=context.price_type,
            customer_id=context.offer_customer_id,
            on_date=context.on_date,
        )
        if best is not None:
            discount_percentage = Decimal(best.discount_percentage)
            offer_id = best.id

    amounts = compute_line_amounts(
        quantity=payload.quantity,
        unit_price=unit_price,
        tax_rate=item.gst_rate,
        is_interstate=context.is_interstate,
        discount_percentage=discount_percentage,
        discount_amount=payload.discount_amount,
    )

    return PricedLine(
        payload=payload,
        item=item,
        unit_price=unit_price,
        price_source=source,
        uom_id=payload.uom_id or item.order_uom_id,
        description=payload.description or item.name,
        tax_rate=Decimal(item.gst_rate),
        amounts=amounts,
        is_price_override=is_override,
        is_below_cost=below_cost,
        discount_percentage=discount_percentage or ZERO,
        offer_id=offer_id,
    )


# -----------------------------------------------------------------------------
# Loading and validation
# -----------------------------------------------------------------------------


async def _build_pricing_context(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    customer: Customer,
    lines: Sequence[SalesOrderLineIn],
    is_interstate: bool,
    on_date: date,
) -> PricingContext:
    """Load every fact the whole line set needs, in six statements.

    The last two are the dealer schemes: the active offers for this customer's
    channel plus the category tree they match against. Batched once per order,
    never per line (docs/assumptions.md §C3-3).
    """
    items_repo = ItemLookupRepository(session, tenant)
    item_ids = [line.item_id for line in lines]
    variant_ids = [line.item_variant_id for line in lines if line.item_variant_id is not None]
    items_repo = ItemLookupRepository(session, tenant)
    item_ids = [line.item_id for line in lines]
    variant_ids = [line.item_variant_id for line in lines if line.item_variant_id is not None]

    items = await items_repo.load(item_ids)
    missing = sorted({str(i) for i in item_ids if i not in items})
    if missing:
        raise NotFoundError("Item", ", ".join(missing))

    for line in lines:
        item = items[line.item_id]
        # Master rules section 1: read the capability flag, never item_group.
        if not item.can_sell:
            raise BusinessRuleError(
                f"Line {line.line_number}: {item.code} is not flagged as sellable.",
                details={"line_number": line.line_number, "item_code": item.code},
            )
        if not item.is_active:
            raise BusinessRuleError(
                f"Line {line.line_number}: {item.code} is inactive and cannot be sold.",
                details={"line_number": line.line_number, "item_code": item.code},
            )

    supplied_uoms = [line.uom_id for line in lines if line.uom_id is not None]
    if supplied_uoms:
        known = await items_repo.uom_ids_in_tenant(supplied_uoms)
        unknown = sorted({str(u) for u in supplied_uoms if u not in known})
        if unknown:
            raise NotFoundError("Unit of measure", ", ".join(unknown))

    variants = await items_repo.variants(variant_ids)
    price_rows = await PriceListRepository(session, tenant).candidates_for_items(
        item_ids,
        price_type=PriceType(customer.price_list_type),
        variant_ids=variant_ids,
        on_date=on_date,
    )

    load_pricing_offers, _ = _dealer_offer_functions()
    offers, ancestors = await load_pricing_offers(
        session,
        tenant,
        on_date=on_date,
        price_type=PriceType(customer.price_list_type),
        customer_id=customer.id,
    )

    return PricingContext(
        price_type=PriceType(customer.price_list_type),
        on_date=on_date,
        is_interstate=is_interstate,
        items=items,
        variants=variants,
        price_rows=price_rows,
        offers=offers,
        offer_ancestors=ancestors,
        offer_customer_id=customer.id,
    )


async def _resolve_shipping_context(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    customer: Customer,
    branch_id: uuid.UUID | None,
    billing_address_id: uuid.UUID | None,
    shipping_address_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, str | None, str | None]:
    """Resolve the two addresses and the two state codes GST depends on.

    Returns ``(billing_id, shipping_id, seller_state_code, place_of_supply)``.
    Addresses are loaded scoped to the customer, so an id belonging to another
    account cannot be attached to this order.
    """
    parties = OrderPartyRepository(session, tenant)

    requested = [i for i in (billing_address_id, shipping_address_id) if i is not None]
    found = await parties.addresses_of(customer.id, requested)
    for address_id in requested:
        if address_id not in found:
            raise NotFoundError("Customer address", address_id)

    shipping = found.get(shipping_address_id) if shipping_address_id else None
    if shipping is None:
        shipping = await parties.default_shipping_address(customer.id)

    seller_state = await parties.seller_state_code(branch_id)
    place_of_supply = shipping.state_code if shipping is not None else None

    return (
        billing_address_id,
        shipping.id if shipping is not None else None,
        seller_state,
        place_of_supply,
    )


def _assert_seller_state_known(seller_state_code: str | None, priced: Sequence[PricedLine]) -> None:
    """A taxed order cannot be raised from an unknown state.

    ASSUMPTION: the check is conditional on the order actually carrying tax. A
    wholly zero-rated order does not depend on the treatment, and refusing it
    would block a legitimate document over a master-data field it never reads.
    """
    if seller_state_code is not None:
        return
    if not any(line.tax_rate > 0 for line in priced):
        return
    raise BusinessRuleError(
        "GST cannot be charged because the selling company (or branch) has no state "
        "code configured, so intra-state and inter-state supply cannot be told apart.",
        details={"missing": "company.state_code"},
    )


# -----------------------------------------------------------------------------
# Projection
# -----------------------------------------------------------------------------


def order_detail(order: SalesOrder, lines: Sequence[SalesOrderLine]) -> SalesOrderDetail:
    payload = {
        **{field: getattr(order, field) for field in SalesOrderSummary.model_fields},
        "billing_address_id": order.billing_address_id,
        "shipping_address_id": order.shipping_address_id,
        "transporter_id": order.transporter_id,
        "cancellation_reason": order.cancellation_reason,
        "notes": order.notes,
        "lines": [SalesOrderLineRead.model_validate(line) for line in lines],
    }
    return SalesOrderDetail(**payload)


def allocation_view(
    order: SalesOrder,
    lines: Sequence[SalesOrderLine],
    *,
    item_codes: dict[uuid.UUID, str] | None = None,
    available: dict[uuid.UUID, Decimal] | None = None,
    live: Mapping[uuid.UUID, Decimal] | None = None,
    planned: Mapping[uuid.UUID, Decimal] | None = None,
) -> OrderAllocationRead:
    """Per-line reserved versus shortfall.

    ``shortfall`` is what is neither shipped nor reserved: the quantity MRP has
    to cover for this order (master rules section 72). Cancelled lines are
    reported with a zero shortfall - they are not demand any more.

    ``live`` is each reservation's live remaining quantity (from
    :func:`live_reservations`). Without it the line's own ``allocated_quantity``
    stands in, which is exact only straight after this module reserved - the
    confirm and top-up responses. ``planned`` is what open consignments already
    carry per line, which ``dispatchable_quantity`` nets off.
    """
    rows: list[LineAllocationRead] = []
    short_lines = 0

    for line in lines:
        cancelled = SalesOrderLineStatus(line.status) is SalesOrderLineStatus.CANCELLED
        reserved = (
            to_quantity(Decimal(line.allocated_quantity))
            if live is None
            else to_quantity(live_reserved(line, live))
        )
        shortfall = line_shortfall(line, reserved)
        if shortfall > 0:
            short_lines += 1
        rows.append(
            LineAllocationRead(
                line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                item_code=(item_codes or {}).get(line.item_id),
                ordered_quantity=Decimal(line.quantity),
                allocated_quantity=Decimal(line.allocated_quantity),
                reserved_quantity=reserved,
                dispatchable_quantity=(
                    to_quantity(ZERO)
                    if cancelled
                    else dispatchable_quantity(
                        ordered=Decimal(line.quantity),
                        dispatched=Decimal(line.dispatched_quantity),
                        reserved=reserved,
                        planned=(planned or {}).get(line.id, ZERO),
                    )
                ),
                dispatched_quantity=Decimal(line.dispatched_quantity),
                shortfall_quantity=shortfall,
                status=SalesOrderLineStatus(line.status),
                reservation_id=line.reservation_id,
                available_quantity=None if available is None else available.get(line.item_id),
                required_date=line.required_date or order.required_date,
            )
        )

    return OrderAllocationRead(
        order_id=order.id,
        code=order.code,
        status=SalesOrderStatus(order.status),
        warehouse_id=order.warehouse_id,
        fully_allocated=short_lines == 0,
        total_shortfall_lines=short_lines,
        lines=rows,
    )


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: SalesOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[SalesOrderSummary], OffsetPagination]:
    rows, pagination = await SalesOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [SalesOrderSummary.model_validate(row) for row in rows], pagination


async def get_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> SalesOrderDetail:
    """One order with its lines, in two queries."""
    order = await SalesOrderRepository(session, tenant).get_or_404(order_id)
    lines = await SalesOrderLineRepository(session, tenant).list_for_order(order.id)
    return order_detail(order, lines)


async def allocation_status(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> OrderAllocationRead:
    """Reserved, dispatchable-now and short per line, with live free stock beside it.

    Six statements regardless of line count: the order, its lines, the item
    codes, one grouped availability read, one read of the lines' live
    reservations and one grouped read of what open consignments carry. Asking
    ``inventory.get_available`` per line would be the N+1 section 32 forbids.
    """
    order = await SalesOrderRepository(session, tenant).get_or_404(order_id)
    lines = await SalesOrderLineRepository(session, tenant).list_for_order(order.id)
    return await _allocation_read(session, tenant, order, lines)


async def _allocation_read(
    session: AsyncSession,
    tenant: TenantContext,
    order: SalesOrder,
    lines: Sequence[SalesOrderLine],
) -> OrderAllocationRead:
    """The full allocation read of an order whose lines are already loaded."""
    item_ids = [line.item_id for line in lines]
    items = await ItemLookupRepository(session, tenant).load(item_ids)
    available = await AvailabilityRepository(session, tenant).available_by_item(
        item_ids, order.warehouse_id
    )
    return allocation_view(
        order,
        lines,
        item_codes={item_id: info.code for item_id, info in items.items()},
        available=available,
        live=await live_reservations(session, tenant, lines),
        planned=await planned_dispatch_quantities(session, tenant, order.id),
    )


async def demand_summary(
    session: AsyncSession,
    tenant: TenantContext,
    filters: DemandSummaryFilters,
    params: PageParams,
) -> tuple[list[DemandSummaryRow], OffsetPagination]:
    """Open sales demand per item - the feed MRP and the dashboards consume.

    One aggregate query, paginated. Never a loop over orders: this is read on
    every planning run and on every dashboard load.
    """
    return await SalesOrderRepository(session, tenant).demand_summary(filters, params)


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------


def _apply_priced_line(line: SalesOrderLine, priced: PricedLine) -> SalesOrderLine:
    payload = priced.payload
    line.line_number = payload.line_number
    line.item_id = payload.item_id
    line.item_variant_id = payload.item_variant_id
    line.description = priced.description
    line.quantity = to_quantity(payload.quantity)
    line.uom_id = priced.uom_id
    line.unit_price = priced.unit_price
    line.discount_percentage = priced.discount_percentage
    line.offer_id = priced.offer_id
    line.discount_amount = priced.amounts.discount_amount
    line.tax_rate = priced.tax_rate
    line.cgst_amount = priced.amounts.cgst_amount
    line.sgst_amount = priced.amounts.sgst_amount
    line.igst_amount = priced.amounts.igst_amount
    line.tax_amount = priced.amounts.tax_amount
    line.line_total = priced.amounts.line_total
    line.required_date = payload.required_date
    line.notes = payload.notes
    return line


def prospective_totals(
    lines: Sequence[SalesOrderLine],
    *,
    changed: Sequence[tuple[SalesOrderLine, Decimal]],
    is_interstate: bool,
) -> OrderTotals:
    """What the header would total if the changed lines took their new quantities.

    Pure: nothing is written, so the credit decision can be made *before* any
    reservation moves. Unchanged lines keep their stored quantity; every line
    keeps its agreed price and discount terms, exactly as the later re-pricing
    in ``_update_confirmed`` does.
    """
    new_quantities = {id(line): quantity for line, quantity in changed}
    amounts = [
        compute_line_amounts(
            quantity=new_quantities.get(id(line), Decimal(line.quantity)),
            unit_price=Decimal(line.unit_price),
            tax_rate=Decimal(line.tax_rate),
            is_interstate=is_interstate,
            discount_percentage=Decimal(line.discount_percentage) or None,
            discount_amount=Decimal(line.discount_amount),
        )
        for line in lines
    ]
    return sum_order_totals(amounts)


def _apply_totals(order: SalesOrder, totals: OrderTotals) -> None:
    order.subtotal = totals.subtotal
    order.discount_amount = totals.discount_amount
    order.tax_amount = totals.tax_amount
    order.total_amount = totals.total_amount


async def create_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payload: SalesOrderCreate,
    *,
    source: str = "ERP",
) -> tuple[SalesOrder, list[SalesOrderLine]]:
    """Create a DRAFT order, priced and taxed.

    No stock is reserved and no credit is consumed: a draft is a quotation the
    customer has not yet committed to, and holding stock against it would let an
    unconfirmed order starve a confirmed one.

    ``source`` records who raised the order and is set by the server, never by
    a payload: the dealer-portal checkout passes ``DEALER_PORTAL``
    (docs/assumptions.md §C3-6).
    """
    if source not in ("ERP", "DEALER_PORTAL"):
        raise ValidationError(
            f"Unknown sales order source: {source}.",
            details={"source": source},
        )
    repo = SalesOrderRepository(session, tenant)
    line_repo = SalesOrderLineRepository(session, tenant)
    parties = OrderPartyRepository(session, tenant)

    async with transaction(session):
        customer = await get_customer_for_order(session, tenant, payload.customer_id)
        home_currency = await parties.company_currency()
        denomination = payload.currency or home_currency
        assert_customer_currency(denomination, home_currency)
        if customer.deleted_at is not None or not customer.is_active:
            raise BusinessRuleError(
                f"Customer {customer.code} is not active and cannot be sold to.",
                details={"customer_id": str(customer.id)},
            )

        if not await parties.warehouse_in_tenant(payload.warehouse_id):
            raise NotFoundError("Warehouse", payload.warehouse_id)
        if payload.branch_id is not None and not await parties.branch_in_tenant(payload.branch_id):
            raise NotFoundError("Branch", payload.branch_id)

        billing_id, shipping_id, seller_state, place_of_supply = await _resolve_shipping_context(
            session,
            tenant,
            customer=customer,
            branch_id=payload.branch_id,
            billing_address_id=payload.billing_address_id,
            shipping_address_id=payload.shipping_address_id,
        )
        is_interstate = resolve_tax_treatment(seller_state, place_of_supply)

        order_date = payload.order_date or today()
        if payload.required_date < order_date:
            raise ValidationError("The required date cannot be before the order date.")

        context = await _build_pricing_context(
            session,
            tenant,
            customer=customer,
            lines=payload.lines,
            is_interstate=is_interstate,
            on_date=order_date,
        )
        priced = [price_line(line, context, principal) for line in payload.lines]
        _assert_seller_state_known(seller_state, priced)

        totals = sum_order_totals([p.amounts for p in priced])
        code = await next_document_number(
            session, tenant.company_id, DocTypes.SALES_ORDER, on=order_date
        )

        order = SalesOrder(
            code=code,
            customer_id=customer.id,
            customer_po_number=payload.customer_po_number,
            source=source,
            order_date=order_date,
            required_date=payload.required_date,
            promised_date=payload.promised_date,
            branch_id=payload.branch_id,
            warehouse_id=payload.warehouse_id,
            status=SalesOrderStatus.DRAFT,
            priority=payload.priority,
            is_urgent=payload.is_urgent,
            currency=denomination,
            place_of_supply_state_code=place_of_supply,
            is_interstate=is_interstate,
            billing_address_id=billing_id,
            shipping_address_id=shipping_id,
            transporter_id=payload.transporter_id,
            requires_approval=any(p.is_below_cost for p in priced),
            notes=payload.notes,
        )
        _apply_totals(order, totals)
        repo.add(order)
        await session.flush()

        lines = [
            _apply_priced_line(
                line_repo.add(SalesOrderLine(sales_order_id=order.id)),
                priced_line,
            )
            for priced_line in priced
        ]
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Created {order.code} for {customer.code}: {len(lines)} line(s), "
                f"{order.currency} {order.total_amount}"
            ),
            user_id=tenant.user_id,
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "line_count": len(lines),
                "price_overrides": [p.payload.line_number for p in priced if p.is_price_override],
                "below_cost_lines": [p.payload.line_number for p in priced if p.is_below_cost],
            },
        )

    return order, lines


async def update_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    payload: SalesOrderUpdate,
    *,
    allocation: AllocationPort | None = None,
) -> tuple[SalesOrder, list[SalesOrderLine]]:
    """Edit an order.

    A draft is re-priced from scratch: lines are replaced and every amount is
    recomputed. A CONFIRMED or ALLOCATED order accepts a narrower change -
    quantities and scheduling only - and each quantity change is pushed straight
    through to the reservation the line holds, because an order that promises
    more than it has reserved is exactly the discrepancy the picking list will
    find at the worst possible moment.

    ASSUMPTION: changing the quantity of a confirmed line does **not** re-run
    price resolution, even when the new quantity crosses a slab boundary. The
    unit price on a confirmed order is an agreed number; silently restating it
    because a quantity moved would change what the customer owes without anybody
    deciding to.
    """
    port = _allocation(allocation)
    repo = SalesOrderRepository(session, tenant)
    line_repo = SalesOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)

        status = SalesOrderStatus(order.status)
        assert_customer_currency(
            order.currency, await OrderPartyRepository(session, tenant).company_currency()
        )
        before = snapshot(order, ORDER_AUDITED_FIELDS)

        if status is SalesOrderStatus.DRAFT:
            lines = await _update_draft(
                session, tenant, principal, order=order, payload=payload, line_repo=line_repo
            )
        elif status in RESERVING_STATUSES:
            lines = await _update_confirmed(
                session, tenant, order=order, payload=payload, line_repo=line_repo, port=port
            )
            if payload.lines is not None:
                await _sync_allocation_status(session, tenant, order, lines, repo=repo)
        else:
            raise BusinessRuleError(
                f"{order.code} is {order.status} and can no longer be edited.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated {order.code}",
            user_id=tenant.user_id,
            old_values={**before, "line_count": len(lines)},
            new_values={**snapshot(order, ORDER_AUDITED_FIELDS), "line_count": len(lines)},
        )

    return order, lines


async def _update_draft(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    *,
    order: SalesOrder,
    payload: SalesOrderUpdate,
    line_repo: SalesOrderLineRepository,
) -> list[SalesOrderLine]:
    """Re-price a draft in place. Lines are replaced wholesale when sent."""
    parties = OrderPartyRepository(session, tenant)
    customer = await get_customer_for_order(session, tenant, order.customer_id)

    sent = payload.model_dump(exclude_unset=True, exclude={"lines"})
    for field, value in sent.items():
        if value is None and field in {"required_date", "warehouse_id", "priority", "is_urgent"}:
            # A PATCH that explicitly nulls a NOT NULL column is dropped rather
            # than turned into an IntegrityError at flush time.
            continue
        setattr(order, field, value)

    if payload.warehouse_id is not None and not await parties.warehouse_in_tenant(
        payload.warehouse_id
    ):
        raise NotFoundError("Warehouse", payload.warehouse_id)
    if payload.branch_id is not None and not await parties.branch_in_tenant(payload.branch_id):
        raise NotFoundError("Branch", payload.branch_id)
    if order.required_date < order.order_date:
        raise ValidationError("The required date cannot be before the order date.")

    billing_id, shipping_id, seller_state, place_of_supply = await _resolve_shipping_context(
        session,
        tenant,
        customer=customer,
        branch_id=order.branch_id,
        billing_address_id=order.billing_address_id,
        shipping_address_id=order.shipping_address_id,
    )
    order.billing_address_id = billing_id
    order.shipping_address_id = shipping_id
    order.place_of_supply_state_code = place_of_supply
    order.is_interstate = resolve_tax_treatment(seller_state, place_of_supply)

    existing = await line_repo.list_for_order(order.id)
    # Snapshotted now, as plain values: the rows below are deleted and rebuilt,
    # and reading attributes off a deleted ORM object afterwards is the expired
    # identity-map trap. Used to keep a carried scheme citation across the
    # rewrite when the discount is unchanged.
    previous_offer = {
        line.line_number: (Decimal(line.discount_percentage), line.offer_id) for line in existing
    }
    # A header-only edit still has to re-price, because the tax treatment may
    # have moved with the shipping address - but the prices it re-prices are the
    # order's own, not the caller's, so the override authority check does not
    # apply to them.
    carried_forward = payload.lines is None
    line_payloads = payload.lines if payload.lines is not None else _payloads_from(existing)

    context = await _build_pricing_context(
        session,
        tenant,
        customer=customer,
        lines=line_payloads,
        is_interstate=order.is_interstate,
        on_date=order.order_date,
    )
    priced = [
        price_line(line, context, principal, enforce_override_authority=not carried_forward)
        for line in line_payloads
    ]
    _assert_seller_state_known(seller_state, priced)

    # Replaced wholesale even when only the tax treatment moved: every stored
    # amount on a line depends on the treatment, so leaving old rows in place
    # would produce a header total its own lines do not add up to.
    await line_repo.delete_for_order(order.id)
    lines = [
        _apply_priced_line(line_repo.add(SalesOrderLine(sales_order_id=order.id)), priced_line)
        for priced_line in priced
    ]
    # A header-only edit re-prices the order's own stored lines, whose discount
    # arrived as an explicit percentage - so the scheme lookup above does not
    # run and a carried scheme reference would be dropped. When the discount is
    # unchanged, the citation survives the rewrite.
    for line in lines:
        if line.offer_id is None:
            previous = previous_offer.get(line.line_number)
            if (
                previous is not None
                and previous[1] is not None
                and Decimal(line.discount_percentage) == previous[0]
            ):
                line.offer_id = previous[1]
    _apply_totals(order, sum_order_totals([p.amounts for p in priced]))
    order.requires_approval = any(p.is_below_cost for p in priced)
    if not order.requires_approval:
        # A re-price that removed the below-cost line also removes the reason
        # the approval existed; a stale approval on a document nobody has looked
        # at since is not evidence of anything.
        order.approved_by_id = None
        order.approved_at = None

    await session.flush()
    return lines


def _payloads_from(lines: Sequence[SalesOrderLine]) -> list[SalesOrderLineIn]:
    """Rebuild input payloads from stored lines, for a header-only edit.

    The header edit may have changed the tax treatment, so the lines have to be
    re-priced even though the client did not send them. The stored unit price is
    carried through as an explicit override so a price-list change between the
    two edits cannot silently restate a quotation the customer has already seen.
    """
    return [
        SalesOrderLineIn(
            line_number=line.line_number,
            item_id=line.item_id,
            item_variant_id=line.item_variant_id,
            description=line.description,
            quantity=Decimal(line.quantity),
            uom_id=line.uom_id,
            unit_price=Decimal(line.unit_price),
            discount_percentage=Decimal(line.discount_percentage) or None,
            discount_amount=(
                Decimal(line.discount_amount) if not line.discount_percentage else None
            ),
            required_date=line.required_date,
            notes=line.notes,
        )
        for line in lines
    ]


async def _update_confirmed(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    payload: SalesOrderUpdate,
    line_repo: SalesOrderLineRepository,
    port: AllocationPort,
) -> list[SalesOrderLine]:
    """Adjust quantities on a live order and move its reservations with them."""
    frozen = [f for f in FROZEN_AFTER_CONFIRMATION if getattr(payload, f, None) is not None]
    if frozen:
        raise BusinessRuleError(
            f"{order.code} is confirmed: {', '.join(frozen)} cannot be changed because the "
            "order already holds stock reservations and a fixed tax treatment. Cancel it "
            "and raise a replacement instead.",
            details={"order_id": str(order.id), "frozen_fields": frozen},
        )

    for field in (
        "customer_po_number",
        "required_date",
        "promised_date",
        "priority",
        "is_urgent",
        "transporter_id",
        "notes",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(order, field, value)
    if order.required_date < order.order_date:
        raise ValidationError("The required date cannot be before the order date.")

    existing = await line_repo.list_for_order(order.id)
    if payload.lines is None:
        return existing

    by_number = {line.line_number: line for line in existing}
    sent_numbers = {line.line_number for line in payload.lines}
    if sent_numbers != set(by_number):
        raise BusinessRuleError(
            f"{order.code} is confirmed: lines cannot be added or removed, only their "
            "quantities changed.",
            details={
                "existing_lines": sorted(by_number),
                "submitted_lines": sorted(sent_numbers),
            },
        )

    changed: list[tuple[SalesOrderLine, Decimal]] = []
    for sent in payload.lines:
        line = by_number[sent.line_number]
        if sent.item_id != line.item_id or sent.item_variant_id != line.item_variant_id:
            raise BusinessRuleError(
                f"Line {sent.line_number}: the item on a confirmed order cannot be changed.",
                details={"line_number": sent.line_number},
            )
        if sent.unit_price is not None and to_unit_price(sent.unit_price) != Decimal(
            line.unit_price
        ):
            raise BusinessRuleError(
                f"Line {sent.line_number}: the price on a confirmed order cannot be changed. "
                "Cancel the order and raise a replacement.",
                details={"line_number": sent.line_number},
            )
        new_quantity = to_quantity(sent.quantity)
        if new_quantity < Decimal(line.invoiced_quantity):
            raise BusinessRuleError(
                f"Line {sent.line_number}: {line.invoiced_quantity} has already been invoiced, "
                "so the quantity cannot be reduced below it."
            )
        if new_quantity < Decimal(line.dispatched_quantity):
            raise BusinessRuleError(
                f"Line {sent.line_number}: {line.dispatched_quantity} has already been "
                "dispatched, so the quantity cannot be reduced below it.",
                details={
                    "line_number": sent.line_number,
                    "dispatched_quantity": str(line.dispatched_quantity),
                },
            )
        if new_quantity != Decimal(line.quantity):
            changed.append((line, new_quantity))
        line.required_date = sent.required_date
        line.notes = sent.notes

    if changed:
        # Rule 4 (module docstring): the credit check ran inside the confirming
        # transaction for the total *at that moment*. Growing the order afterwards
        # is new exposure the check never saw, so it is re-run here, against the
        # locked customer row and before the item advisory locks - the same
        # order ``confirm_order`` takes. The full prospective total is checked,
        # not the delta, because ``outstanding_amount`` never includes open
        # orders.
        # ASSUMPTION: only an *increase* is re-checked. A reduction stays
        # unchecked so a customer now on hold can still shrink its exposure
        # (docs/assumptions.md, credit limit semantics).
        prospective = prospective_totals(
            existing, changed=changed, is_interstate=order.is_interstate
        )
        if prospective.total_amount > Decimal(order.total_amount):
            await assert_customer_can_order(
                session, tenant, order.customer_id, prospective.total_amount, lock=True
            )
        await _rereserve_changed_lines(session, tenant, order=order, changed=changed, port=port)

    # Amounts follow the quantity: the agreed unit price and discount terms stay,
    # everything derived from them is recomputed.
    amounts: list[LineAmounts] = []
    for line in existing:
        computed = compute_line_amounts(
            quantity=Decimal(line.quantity),
            unit_price=Decimal(line.unit_price),
            tax_rate=Decimal(line.tax_rate),
            is_interstate=order.is_interstate,
            discount_percentage=Decimal(line.discount_percentage) or None,
            discount_amount=Decimal(line.discount_amount),
        )
        line.discount_amount = computed.discount_amount
        line.cgst_amount = computed.cgst_amount
        line.sgst_amount = computed.sgst_amount
        line.igst_amount = computed.igst_amount
        line.tax_amount = computed.tax_amount
        line.line_total = computed.line_total
        amounts.append(computed)

    _apply_totals(order, sum_order_totals(amounts))
    await session.flush()
    return existing


async def _rereserve_changed_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    changed: Sequence[tuple[SalesOrderLine, Decimal]],
    port: AllocationPort,
) -> None:
    """Release and retake the reservations of the lines whose quantity moved.

    Release-then-reserve rather than an incremental adjustment, so a line always
    holds exactly one reservation whose quantity is the whole truth about it.
    The release happens first and is flushed before availability is read, so the
    stock this order was already holding is counted as available to itself -
    otherwise shrinking a line by 10 and growing it by 2 could fail for want of
    stock the order had all along.
    """
    item_ids = sorted({line.item_id for line, _ in changed})
    for item_id in item_ids:
        # Sorted, so two orders touching the same items cannot deadlock.
        await advisory_xact_lock(session, "sales_allocation", tenant.company_id, item_id)

    for line, _ in changed:
        if line.reservation_id is not None:
            await port.release(
                session,
                tenant,
                reservation_id=line.reservation_id,
                reason=f"Quantity changed on {order.code} line {line.line_number}",
            )
            line.reservation_id = None
        line.allocated_quantity = to_quantity(ZERO)
    await session.flush()

    available = await AvailabilityRepository(session, tenant).available_by_item(
        item_ids, order.warehouse_id
    )
    pool = {item_id: Decimal(available.get(item_id, ZERO)) for item_id in item_ids}

    for line, new_quantity in sorted(
        changed, key=lambda pair: (pair[0].item_id, pair[0].line_number)
    ):
        line.quantity = new_quantity
        demand = new_quantity - Decimal(line.dispatched_quantity)
        take = reservable_quantity(demand, pool[line.item_id])
        if take > 0:
            reservation = await port.reserve(
                session,
                tenant,
                item_id=line.item_id,
                warehouse_id=order.warehouse_id,
                quantity=take,
                demand_id=order.id,
                demand_line_id=line.id,
                demand_code=order.code,
                priority=order.priority,
                required_date=line.required_date or order.required_date,
                reason=f"Quantity changed on {order.code} line {line.line_number}",
            )
            line.reservation_id = reservation.id
            line.allocated_quantity = take
            pool[line.item_id] -= take
        line.status = line_status_for(
            quantity=Decimal(line.quantity),
            allocated_quantity=Decimal(line.allocated_quantity),
            dispatched_quantity=Decimal(line.dispatched_quantity),
        )
    await session.flush()


async def _sync_allocation_status(
    session: AsyncSession,
    tenant: TenantContext,
    order: SalesOrder,
    lines: Sequence[SalesOrderLine],
    *,
    repo: SalesOrderRepository,
) -> None:
    """Keep ALLOCATED meaning "every open line fully reserved" after a line edit.

    A quantity raised past what free stock could cover leaves demand the
    reservations do not hold, and an ALLOCATED order in that state would read
    as covered to everyone planning around it - so it goes back to CONFIRMED
    (docs/assumptions.md section E1-3). The reverse holds as well: an edit that
    leaves every line covered makes a CONFIRMED order ALLOCATED, as confirming
    it would have. Coverage is read from the live reservations, so one returned
    elsewhere counts as nothing. A PARTIALLY_DISPATCHED order keeps its status:
    its next step is a consignment, not an allocation.
    """
    status = SalesOrderStatus(order.status)
    if status not in (SalesOrderStatus.CONFIRMED, SalesOrderStatus.ALLOCATED):
        return
    live = await live_reservations(session, tenant, lines)
    covered = all(line_shortfall(line, live_reserved(line, live)) == 0 for line in lines)
    if status is SalesOrderStatus.ALLOCATED and not covered:
        await _transition(
            session,
            tenant,
            order,
            SalesOrderStatus.CONFIRMED,
            reason="A quantity change left a line short of its reservation",
            repo=repo,
        )
    elif status is SalesOrderStatus.CONFIRMED and covered:
        await _transition(
            session,
            tenant,
            order,
            SalesOrderStatus.ALLOCATED,
            reason="Every line fully reserved after the quantity change",
            repo=repo,
        )


async def delete_draft_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a draft that was never committed.

    A hard delete, and only from DRAFT: nothing references an unconfirmed order,
    no stock was reserved against it and no number was reported to the customer.
    Anything past DRAFT is cancelled instead, so the trail survives.

    A portal-sourced draft is refused outright: the dealer's own cancel route
    is the way back, because a delete would silently drop an order the sales
    desk may already be reviewing (docs/assumptions.md §C3-6).
    """
    repo = SalesOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.get_or_404(order_id)
        if SalesOrderStatus(order.status) is not SalesOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status} and must be cancelled rather than deleted, "
                "so the record of it survives.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )
        if str(order.source) == "DEALER_PORTAL":
            raise BusinessRuleError(
                f"{order.code} was raised through the dealer portal and must be "
                "cancelled rather than deleted, so the dealer sees what happened "
                "to it.",
                details={"order_id": str(order.id), "source": str(order.source)},
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Deleted draft {order.code}",
            user_id=tenant.user_id,
            old_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={"reason": reason},
        )
        # Lines go with it through ON DELETE CASCADE.
        await repo.hard_delete(order)


async def approve_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    reason: str,
) -> SalesOrder:
    """Sign off an order priced below cost so it can be confirmed."""
    repo = SalesOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)

        if SalesOrderStatus(order.status) is not SalesOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status}: approval gates confirmation, so it has "
                "no effect once the order has been confirmed.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )
        if order.approved_at is not None:
            raise BusinessRuleError(
                f"{order.code} has already been approved.",
                details={"approved_by_id": str(order.approved_by_id)},
            )
        if not can_approve(principal, order.created_by_id):
            raise AuthorizationError(
                "A sales order must be approved by someone other than the person who raised it.",
                details={"order_id": str(order.id)},
            )

        order.approved_by_id = principal.user_id
        order.approved_at = utcnow()
        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.APPROVE,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Approved {order.code} for confirmation",
            user_id=tenant.user_id,
            new_values={"approved_by_id": order.approved_by_id, "approved_at": order.approved_at},
            context={"reason": reason},
        )

    return order


@dataclass(frozen=True, slots=True)
class ReservationPass:
    """What one reserving pass over an order did, keyed by line number."""

    #: Quantity each line is still short of after the pass.
    shortfalls: dict[int, str]
    #: Quantity this pass newly reserved per line.
    reserved_now: dict[int, str]


async def _reserve_available_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    lines: Sequence[SalesOrderLine],
    port: AllocationPort,
    purpose: str,
) -> ReservationPass:
    """Reserve free stock for every line still short of its open demand.

    The one reservation path confirmation and the top-up share. Every item is
    advisory-locked in sorted order, so two orders reserving the same item
    queue rather than both promising it. Free stock is read once, as one pool
    per item drawn down line by line, so two lines for the same item are never
    promised the same units, and lines are served in line order.

    A line keeps exactly one reservation. When it already holds a live one and
    more stock is free, the old promise is released and one reservation for the
    whole covered quantity is taken in its place - release-then-reserve, the
    shape a quantity change takes too - so ``reservation_id`` stays the whole
    truth about the line. A line whose reservation was released, expired or
    consumed elsewhere holds nothing (E1a section 4): it is reserved afresh and
    the closed one is never released again.
    """
    item_ids = sorted({line.item_id for line in lines})
    for item_id in item_ids:
        await advisory_xact_lock(session, "sales_allocation", tenant.company_id, item_id)

    live = await live_reservations(session, tenant, lines)
    available = await AvailabilityRepository(session, tenant).available_by_item(
        item_ids, order.warehouse_id
    )
    pool = {item_id: Decimal(available.get(item_id, ZERO)) for item_id in item_ids}

    shortfalls: dict[int, str] = {}
    reserved_now: dict[int, str] = {}
    for line in sorted(lines, key=lambda row: row.line_number):
        if SalesOrderLineStatus(line.status) is SalesOrderLineStatus.CANCELLED:
            continue
        held = live_reserved(line, live)
        demand = Decimal(line.quantity) - Decimal(line.dispatched_quantity)
        take = reservable_quantity(max(demand - held, ZERO), pool[line.item_id])
        if take > 0:
            line_reason = f"{purpose} of {order.code} line {line.line_number}"
            if held > 0 and line.reservation_id is not None:
                await port.release(
                    session, tenant, reservation_id=line.reservation_id, reason=line_reason
                )
            reservation = await port.reserve(
                session,
                tenant,
                item_id=line.item_id,
                warehouse_id=order.warehouse_id,
                quantity=to_quantity(held + take),
                demand_id=order.id,
                demand_line_id=line.id,
                demand_code=order.code,
                priority=order.priority,
                required_date=line.required_date or order.required_date,
                reason=line_reason,
            )
            line.reservation_id = reservation.id
            held += take
            pool[line.item_id] -= take
            reserved_now[line.line_number] = str(to_quantity(take))
        elif held == 0:
            # A closed reservation is no reservation: forget it, so neither a
            # consignment nor a later release ever reaches for it again.
            line.reservation_id = None
        line.allocated_quantity = to_quantity(held)
        line.status = line_status_for(
            quantity=Decimal(line.quantity),
            allocated_quantity=held,
            dispatched_quantity=Decimal(line.dispatched_quantity),
        )
        short = demand - held
        if short > 0:
            shortfalls[line.line_number] = str(to_quantity(short))
    await session.flush()
    return ReservationPass(shortfalls=shortfalls, reserved_now=reserved_now)


async def allocate_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> OrderAllocationRead:
    """Top a short order up from stock that has become free since it was confirmed.

    The allocation top-up behind ``POST /sales/{id}/allocate`` and the scheduled
    ``maintenance.allocate_short_orders`` sweep. In one transaction: lock the
    order header, then its lines in primary-key order (the order a consignment
    locks them in), take the same per-item advisory locks confirmation takes,
    and reserve newly free stock for each short line by line order through the
    path confirmation uses.

    The status follows the result. A CONFIRMED order that ends fully covered
    moves to ALLOCATED. An ALLOCATED order can be short too, when one of its
    reservations was released or expired elsewhere: the top-up re-reserves the
    line (E1a section 4), and if free stock cannot cover it the order goes
    back to CONFIRMED rather than keep a label it no longer earns. A
    PARTIALLY_DISPATCHED order keeps its status - its next step is the
    consignment that ships the rest.

    The audit row is written only when a reservation actually changed: the
    sweep calls this for every short order on every tick, and a row per no-op
    would bury the ones that matter. A status change is audited by
    ``_transition`` either way.
    """
    port = _allocation(allocation)
    repo = SalesOrderRepository(session, tenant)
    line_repo = SalesOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)
        status = SalesOrderStatus(order.status)
        if status not in TOP_UP_STATUSES:
            raise BusinessRuleError(
                f"{order.code} is {status}: only a confirmed, allocated or partially "
                "dispatched order can have newly available stock allocated to it.",
                details={"order_id": str(order.id), "status": str(status)},
            )

        lines = await line_repo.lock_for_order(order.id)
        topped = await _reserve_available_lines(
            session, tenant, order=order, lines=lines, port=port, purpose="Allocation top-up"
        )
        if not topped.shortfalls and status is SalesOrderStatus.CONFIRMED:
            await _transition(
                session,
                tenant,
                order,
                SalesOrderStatus.ALLOCATED,
                reason="Every line fully reserved by the allocation top-up",
                repo=repo,
            )
        elif topped.shortfalls and status is SalesOrderStatus.ALLOCATED:
            await _transition(
                session,
                tenant,
                order,
                SalesOrderStatus.CONFIRMED,
                reason="A reservation was closed elsewhere and free stock cannot replace it",
                repo=repo,
            )
        if topped.reserved_now:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.RESERVATION_CHANGE,
                entity_type=AUDIT_SALES_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=(
                    f"Allocated newly available stock to {order.code}: "
                    f"{len(topped.reserved_now)} line(s) topped up, "
                    f"{len(topped.shortfalls)} still short"
                ),
                user_id=tenant.user_id,
                new_values={
                    "reserved_now_by_line": topped.reserved_now,
                    "shortfall_by_line": topped.shortfalls or None,
                },
                context={"warehouse_id": order.warehouse_id, "reason": reason},
            )
        view = await _allocation_read(session, tenant, order, lines)

    return view


async def confirm_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> ConfirmResult:
    """Commit the order: check credit, reserve what exists, report what does not.

    The whole operation is one transaction. The customer row is locked before
    its headroom is read, every item touched is locked in sorted order by an
    advisory lock so two confirmations cannot both promise the same stock, and
    the reservations, the status change and the audit row commit together or not
    at all.

    Partial reservation is a success, not a failure (master rules section 72).
    The response carries the per-line shortfall so the desk sees immediately
    what MRP will have to cover, instead of finding out at picking time.
    """
    port = _allocation(allocation)
    repo = SalesOrderRepository(session, tenant)
    line_repo = SalesOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)

        current = SalesOrderStatus(order.status)
        # Validated before any work is done, so a wrong-state request costs one
        # comparison rather than a credit check and a round of reservations.
        SALES_ORDER_LIFECYCLE.assert_transition(current, SalesOrderStatus.CONFIRMED)
        assert_customer_currency(
            order.currency, await OrderPartyRepository(session, tenant).company_currency()
        )

        if order.requires_approval and order.approved_at is None:
            raise ApprovalRequiredError(
                f"{order.code} contains a below-cost price and needs approval before it "
                "can be confirmed.",
                details={"order_id": str(order.id)},
            )

        lines = await line_repo.list_for_order(order.id)
        if not lines:
            raise BusinessRuleError(
                f"{order.code} has no lines, so confirming it would promise nothing.",
                details={"order_id": str(order.id)},
            )

        # Locks the customer row: the headroom read here is consumed by this
        # order, and reading it unlocked is the classic credit double-spend.
        assessment = await assert_customer_can_order(
            session, tenant, order.customer_id, Decimal(order.total_amount), lock=True
        )

        shortfalls = (
            await _reserve_available_lines(
                session, tenant, order=order, lines=lines, port=port, purpose="Confirmation"
            )
        ).shortfalls

        await _transition(
            session, tenant, order, SalesOrderStatus.CONFIRMED, reason=reason, repo=repo
        )

        fully_allocated = not shortfalls
        if fully_allocated:
            # Two edges, both legal, both audited: the order was confirmed and
            # then found to be completely covered.
            await _transition(
                session,
                tenant,
                order,
                SalesOrderStatus.ALLOCATED,
                reason="Every line fully reserved on confirmation",
                repo=repo,
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.RESERVATION_CHANGE,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Reserved stock for {order.code}: {len(lines) - len(shortfalls)} of "
                f"{len(lines)} line(s) fully covered"
            ),
            user_id=tenant.user_id,
            new_values={
                "allocated_lines": len(lines) - len(shortfalls),
                "short_lines": len(shortfalls),
                "shortfall_by_line": shortfalls or None,
            },
            context={"warehouse_id": order.warehouse_id, "reason": reason},
        )

        if shortfalls:
            log.info(
                "sales.confirm.partial_allocation",
                order_code=order.code,
                short_lines=len(shortfalls),
                warehouse_id=str(order.warehouse_id),
            )

        detail = order_detail(order, lines)
        view = allocation_view(order, lines)

    return ConfirmResult(order=detail, allocation=view, credit_headroom=assessment.headroom)


async def cancel_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> SalesOrder:
    """Cancel an order and give every reserved unit back.

    Releasing inside the same transaction as the status change is the point: a
    cancelled order that still holds reservations makes stock invisible to every
    other customer while belonging to nobody.
    """
    port = _allocation(allocation)
    repo = SalesOrderRepository(session, tenant)
    line_repo = SalesOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)

        if await repo.has_live_invoice(order.id):
            raise BusinessRuleError(
                "This order has a live sales invoice. Cancel the invoice first."
            )
        SALES_ORDER_LIFECYCLE.assert_transition(
            SalesOrderStatus(order.status), SalesOrderStatus.CANCELLED
        )

        lines = await line_repo.list_for_order(order.id)
        released = 0
        for line in sorted(lines, key=lambda row: (row.item_id, row.line_number)):
            if line.reservation_id is not None:
                await port.release(
                    session,
                    tenant,
                    reservation_id=line.reservation_id,
                    reason=f"{order.code} cancelled: {reason}",
                )
                line.reservation_id = None
                released += 1
            line.allocated_quantity = to_quantity(ZERO)
            line.status = SalesOrderLineStatus.CANCELLED

        order.cancellation_reason = reason
        await _transition(
            session,
            tenant,
            order,
            SalesOrderStatus.CANCELLED,
            reason=reason,
            repo=repo,
            action=AuditAction.CANCEL,
        )

        if released:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.RESERVATION_CHANGE,
                entity_type=AUDIT_SALES_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Released {released} reservation(s) held by {order.code}",
                user_id=tenant.user_id,
                new_values={"released_reservations": released},
                context={"reason": reason},
            )

    return order


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    order: SalesOrder,
    target: SalesOrderStatus,
    *,
    reason: str,
    repo: SalesOrderRepository,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> SalesOrder:
    """Move an order's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``sales_orders.status`` and exactly one place that writes the matching
    audit row (master rules section 73).
    """
    current = SalesOrderStatus(order.status)
    SALES_ORDER_LIFECYCLE.assert_transition(current, target)
    order.status = target
    repo.touch(order)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_SALES_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return order


__all__ = [
    "AUDIT_SALES_ORDER",
    "SALES_ORDER_LIFECYCLE",
    "TOP_UP_STATUSES",
    "AllocationPort",
    "AllocationService",
    "DispatchPlanPort",
    "LineAmounts",
    "OrderTotals",
    "PricedLine",
    "PricingContext",
    "ReservationPass",
    "ReservationRef",
    "allocate_order",
    "allocation_status",
    "allocation_view",
    "approve_order",
    "can_approve",
    "cancel_order",
    "compute_line_amounts",
    "confirm_order",
    "create_order",
    "delete_draft_order",
    "demand_summary",
    "dispatchable_quantity",
    "get_order_detail",
    "is_below_cost",
    "line_shortfall",
    "line_status_for",
    "list_orders",
    "live_reservations",
    "live_reserved",
    "order_detail",
    "planned_dispatch_quantities",
    "price_line",
    "prospective_totals",
    "reservable_quantity",
    "resolve_dispatch_plan_port",
    "resolve_tax_treatment",
    "resolve_unit_price",
    "sum_order_totals",
    "update_order",
]


async def close_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> SalesOrder:
    repo = SalesOrderRepository(session, tenant)
    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError(SALES_ORDER_ENTITY, order_id)
        return await _transition(
            session, tenant, order, SalesOrderStatus.CLOSED, reason=reason, repo=repo
        )
