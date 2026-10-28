"""Sales orders: the demand the whole plant is planned from.

A sales order is where a customer promise becomes an inventory reservation and,
for whatever stock does not exist, an MRP requirement. Five modelling decisions
in this file are load-bearing.

**The tax split is stored, not recomputed.** ``is_interstate`` and the per-line
CGST/SGST/IGST amounts are frozen onto the document when it is priced. GST is
intra-state (CGST+SGST) when the seller's state equals the place of supply and
inter-state (IGST) otherwise; the place of supply comes from the shipping
address, and an address may legitimately be corrected next month. Recomputing on
read would silently restate a confirmed order's tax, so the treatment that
applied when the order was raised is written down (master rules section 84).

**``allocated_quantity`` is the reserved quantity, and it is bounded by the
ordered quantity in the database.** Partial reservation is the normal outcome of
confirming an order - the shortfall is what MRP exists to cover - so the column
is not a flag but a running number, and ``allocated_quantity <= quantity`` plus
``dispatched_quantity <= quantity`` are CHECK constraints rather than promises
made by application code.

**``reservation_id`` is a bare UUID, not a foreign key.** ``stock_reservations``
is owned by the ``allocation`` module, which is built independently of this one;
a hard reference would make this table uncreatable until that module lands, and
a reservation released and retaken during an edit is a lifecycle this table does
not arbitrate. Same reasoning as the inventory ledger's ``reference_id``
(database.md section 4.2) and ``customers.default_transporter_id``.

**``warehouse_id`` is mandatory.** Confirming an order reserves stock, and a
reservation has to name the position it is taken against. An order with no
warehouse could be confirmed but never allocated, which is a document that looks
finished and promises nothing.

**Optimistic locking is on the header only.** Several people edit a live order -
the salesperson changes quantities while planning changes the promised date -
and the last writer must not silently win (database.md section 1). Lines are
always rewritten through the header, so the header's version covers them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.core.models import OptimisticLockMixin, TenantEntity, sql_in_list
from src.core.types import GUID, Money, Percentage, Quantity, UnitPrice

#: Planning priority. Lower is more urgent, so the natural ascending sort is
#: also the order the plant should work in, and the value passes straight
#: through to ``allocation.reserve``.
#:
#: ASSUMPTION: the scale is 1-9 with 5 as the default. The contract fixes the
#: column as an integer but not its range; an unbounded integer would let two
#: sites invent incompatible conventions, and a bounded named range is one
#: constant to change if a customer wants a different scale.
MIN_PRIORITY = 1
MAX_PRIORITY = 9
DEFAULT_PRIORITY = 5

#: Currency codes are ISO-4217 alphabetic.


class SalesOrderStatus(StrEnum):
    """Lifecycle of a sales order (database.md section 7).

    ``CANCELLED`` is reachable only before anything has physically shipped;
    after a dispatch the order is closed off through the dispatch and invoice
    path instead, because cancelling a document that moved stock would leave the
    ledger describing a movement against a document claiming never to have
    existed.
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    ALLOCATED = "ALLOCATED"
    PARTIALLY_DISPATCHED = "PARTIALLY_DISPATCHED"
    DISPATCHED = "DISPATCHED"
    INVOICED = "INVOICED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SalesOrderLineStatus(StrEnum):
    """Fulfilment state of one order line.

    Derived from the quantities rather than set independently - see
    ``service.line_status_for`` - so a line can never claim ALLOCATED while
    ``allocated_quantity`` is short of ``quantity``.
    """

    PENDING = "PENDING"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    ALLOCATED = "ALLOCATED"
    PARTIALLY_DISPATCHED = "PARTIALLY_DISPATCHED"
    DISPATCHED = "DISPATCHED"
    CANCELLED = "CANCELLED"


#: Statuses in which an order still represents unmet demand. MRP, the demand
#: summary and the dashboards all read exactly this set, and the partial index
#: on ``sales_orders`` below is built for it - so the tuple and the index have
#: to change together.
OPEN_DEMAND_STATUSES: tuple[SalesOrderStatus, ...] = (
    SalesOrderStatus.CONFIRMED,
    SalesOrderStatus.ALLOCATED,
    SalesOrderStatus.PARTIALLY_DISPATCHED,
)

#: Statuses in which the order holds live reservations and its quantities may
#: still be adjusted.
RESERVING_STATUSES: tuple[SalesOrderStatus, ...] = (
    SalesOrderStatus.CONFIRMED,
    SalesOrderStatus.ALLOCATED,
)


class SalesOrder(TenantEntity, OptimisticLockMixin):
    """A customer's committed demand for one delivery."""

    __tablename__ = "sales_orders"

    # --- identity -----------------------------------------------------------
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The customer's own reference. Printed on every document sent back to them
    #: and often the only key their accounts-payable team can match on.
    customer_po_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Who raised the order. Set by the server, never by a payload: the dealer
    #: portal checkout passes DEALER_PORTAL, everything else stays ERP
    #: (docs/assumptions.md §C3-6).
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ERP", server_default="ERP"
    )

    # --- dates --------------------------------------------------------------
    # order_date is the commercial date of the document; required_date is what
    # the customer asked for; promised_date is what sales committed to after
    # checking capacity. Keeping all three means "are we late?" is answered
    # against the commitment rather than against the request.
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    required_date: Mapped[date] = mapped_column(Date, nullable=False)
    promised_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- where it ships from ------------------------------------------------
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: NOT NULL: confirmation reserves stock, and a reservation must name the
    #: warehouse it is taken against (see the module docstring).
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- lifecycle ----------------------------------------------------------
    status: Mapped[SalesOrderStatus] = mapped_column(
        String(24),
        nullable=False,
        default=SalesOrderStatus.DRAFT,
        server_default=SalesOrderStatus.DRAFT.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PRIORITY, server_default=str(DEFAULT_PRIORITY)
    )
    #: Distinct from ``priority``: urgency is a commercial flag the customer
    #: service desk sets, priority is the number allocation and scheduling sort
    #: on. A rush order left at the default priority is a real situation.
    is_urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- money --------------------------------------------------------------
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
        server_default=DEFAULT_CURRENCY,
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- GST treatment, frozen at pricing time ------------------------------
    #: Place of supply: the state code of the shipping address the order was
    #: priced against. Stored because the address row may be corrected later and
    #: a confirmed order's tax must not move with it.
    place_of_supply_state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: True when the place of supply differs from the company's state, i.e. IGST
    #: applies. Decided once, in ``service.resolve_tax_treatment``.
    is_interstate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- parties and logistics ----------------------------------------------
    billing_address_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("customer_addresses.id", ondelete="RESTRICT"), nullable=True
    )
    shipping_address_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("customer_addresses.id", ondelete="RESTRICT"), nullable=True
    )
    #: Deliberately a bare UUID with no foreign key: ``transporters`` belongs to
    #: the vendors module, which is deployed independently of this one. The same
    #: decision, for the same reason, as ``customers.default_transporter_id``.
    transporter_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    # --- approval evidence (master rules section 42) ------------------------
    #: Set when a line was priced below cost by an authorised override. The
    #: order then cannot be confirmed until somebody holding sales_order.approve
    #: has signed it off - see ``service.confirm_order``.
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_sales_orders_company_id_code"),
        # The order desk's default list: open orders by delivery date.
        Index("ix_sales_orders_company_status_required", "company_id", "status", "required_date"),
        # "What has this customer got open?" - the account screen.
        Index("ix_sales_orders_customer", "company_id", "customer_id", "order_date"),
        # Allocation and dispatch both work a warehouse at a time.
        Index("ix_sales_orders_warehouse_status", "company_id", "warehouse_id", "status"),
        # THE index behind the demand summary and MRP's demand pull. Partial,
        # because a mature company's table is mostly closed orders and planning
        # never looks at them.
        Index(
            "ix_sales_orders_open_demand",
            "company_id",
            "required_date",
            "warehouse_id",
            postgresql_where=text(f"status IN ({sql_in_list(OPEN_DEMAND_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(SalesOrderStatus)})", name="status_known"),
        CheckConstraint(
            "source IN ('ERP', 'DEALER_PORTAL')",
            name="source_known",
        ),
        CheckConstraint(
            f"priority BETWEEN {MIN_PRIORITY} AND {MAX_PRIORITY}", name="priority_in_range"
        ),
        CheckConstraint("required_date >= order_date", name="required_date_not_before_order"),
        CheckConstraint(
            "promised_date IS NULL OR promised_date >= order_date",
            name="promised_date_not_before_order",
        ),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        CheckConstraint("length(currency) = 3", name="currency_is_iso_code"),
        # Approval evidence is all-or-nothing: an approver with no timestamp
        # cannot be audited, and a timestamp with no approver names nobody.
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name="approval_evidence_complete",
        ),
        # "Why was this cancelled?" is the first question asked and the last
        # thing anyone remembers, so the row cannot exist without the answer.
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_orders_carry_a_reason",
        ),
    )


class SalesOrderLine(TenantEntity):
    """One item on a sales order, with its own fulfilment progress."""

    __tablename__ = "sales_order_lines"

    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Ordering and identity within the order. Printed on the acknowledgement and
    #: the packing list, so it is stable and unique per order rather than an
    #: array index that shifts when a line is removed.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    item_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_variants.id", ondelete="RESTRICT"), nullable=True
    )
    #: Snapshot of what was sold, not a pointer to the item name. The item
    #: master may be renamed; last year's order must still print what the
    #: customer agreed to buy.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    # --- pricing, resolved once and then frozen -----------------------------
    unit_price: Mapped[Decimal] = mapped_column(UnitPrice, nullable=False)
    discount_percentage: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The money the percentage worked out to, or the flat discount the
    #: salesperson typed. Stored rather than recomputed so a later rounding-rule
    #: change cannot restate an accepted order.
    discount_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- GST ----------------------------------------------------------------
    tax_rate: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The split is stored beside the total because an invoice prints CGST and
    #: SGST as separate lines, and halving a rounded total afterwards does not
    #: reliably reproduce the two halves.
    cgst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    sgst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    igst_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Per-line delivery date, for an order shipped in tranches. NULL means the
    #: header's ``required_date`` applies; the demand summary coalesces the two.
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- fulfilment progress ------------------------------------------------
    allocated_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    dispatched_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    invoiced_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    status: Mapped[SalesOrderLineStatus] = mapped_column(
        String(24),
        nullable=False,
        default=SalesOrderLineStatus.PENDING,
        server_default=SalesOrderLineStatus.PENDING.value,
    )

    #: A ``stock_reservations`` row this line currently holds. Bare UUID, no
    #: foreign key - see the module docstring. The line's whole promise is every
    #: open SALES_ORDER reservation whose ``demand_line_id`` is this line: stock
    #: sitting in two bins or two lots is held by one reservation per position
    #: (docs/assumptions.md §E2-2), and ``allocated_quantity`` is their sum. A
    #: quantity change releases them all and reserves afresh.
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: The dealer offer this line's discount came from, if any. Bare UUID, no
    #: foreign key: the sales table must not depend on the dealer portal's
    #: schema, and the offer may be retired while the order still cites it
    #: (docs/assumptions.md §C3-3). Set by the server only.
    offer_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "sales_order_id", "line_number", name="uq_sales_order_lines_order_line_number"
        ),
        Index("ix_sales_order_lines_order", "company_id", "sales_order_id", "line_number"),
        # The demand summary groups by item over open lines only. Partial index
        # keyed on the same predicate the aggregate uses.
        Index(
            "ix_sales_order_lines_open_item",
            "company_id",
            "item_id",
            postgresql_where=text("status <> 'CANCELLED' AND dispatched_quantity < quantity"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(SalesOrderLineStatus)})", name="status_known"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name="discount_percentage_bounded",
        ),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_bounded"),
        CheckConstraint("cgst_amount >= 0", name="cgst_non_negative"),
        CheckConstraint("sgst_amount >= 0", name="sgst_non_negative"),
        CheckConstraint("igst_amount >= 0", name="igst_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        # The correctness floor for fulfilment. Reserving or shipping more than
        # was ordered is the defect these prevent, and application code is not
        # trusted with it.
        CheckConstraint("allocated_quantity >= 0", name="allocated_non_negative"),
        CheckConstraint("dispatched_quantity >= 0", name="dispatched_non_negative"),
        CheckConstraint("invoiced_quantity >= 0", name="invoiced_non_negative"),
        CheckConstraint("allocated_quantity <= quantity", name="allocated_within_ordered"),
        CheckConstraint("dispatched_quantity <= quantity", name="dispatched_within_ordered"),
        CheckConstraint("invoiced_quantity <= quantity", name="invoiced_within_ordered"),
        # A tax split that does not add up to the stored total would make the
        # order and its invoice disagree by construction.
        CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount", name="tax_split_sums_to_total"
        ),
        # CGST+SGST and IGST are mutually exclusive: a supply is either
        # intra-state or inter-state, never both.
        CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name="gst_treatment_is_exclusive",
        ),
    )


#: Scalar defaults for :class:`SalesSettings`, resolved when the row is absent
#: or its column is NULL. A stored decision wins; an unset column means "not
#: decided", so changing a default here moves every company that never made
#: the decision and restates nobody's.
DEFAULT_AUTO_PLAN_ON_CONFIRM = True


class SalesSettings(TenantEntity):
    """Sparse per-company sales settings (E1b section 1).

    One row per company, created on the first change. Every column is
    nullable, and NULL reads as the code default above: a company that has
    never opened the settings screen behaves exactly as the defaults document,
    and a change to a default is not silently undone by rows written before it.
    """

    __tablename__ = "sales_settings"

    #: Plan the confirmed order's shortfall automatically: draft production
    #: orders and purchase requisitions, raised in the confirming transaction
    #: (WP E1b). False means the desk plans on demand with "Plan shortfall".
    auto_plan_on_confirm: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        # One row per company is the whole shape of the table, and the
        # uniqueness is what makes "sparse row or default" unambiguous.
        UniqueConstraint("company_id", name="uq_sales_settings_company"),
    )


__all__ = [
    "CURRENCY_CODE_LENGTH",
    "DEFAULT_AUTO_PLAN_ON_CONFIRM",
    "DEFAULT_PRIORITY",
    "MAX_PRIORITY",
    "MIN_PRIORITY",
    "OPEN_DEMAND_STATUSES",
    "RESERVING_STATUSES",
    "SalesOrder",
    "SalesOrderLine",
    "SalesOrderLineStatus",
    "SalesOrderStatus",
    "SalesSettings",
]
