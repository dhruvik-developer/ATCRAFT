"""Request and response shapes for reporting.

Two decisions shape this file.

**Every report has its own row schema.** A single ``dict[str, Any]`` row would
have been a third of the code, and it would also have made the OpenAPI document
useless: a client could not tell what a stock-availability row contains without
running one. Each report is a named contract instead, and because the repository
labels its SELECT columns with exactly these field names, a row is validated
straight out of the result mapping with no hand-written mapper in between - so a
column renamed in a query fails loudly at the first request rather than silently
serialising a null.

**The period is a validated object, not two loose query parameters.** Master
rules section 25 requires a bounded date range on every period report, and a
range is only bounded if something refuses the unbounded case. That refusal lives
here, once, in :class:`ReportPeriod` - not repeated in nineteen route handlers,
where the nineteenth would eventually forget.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.errors import ValidationError
from src.modules.reports.models import ReportJobStatus, ReportKey

#: Longest period a report may cover. A year plus a day so "the whole financial
#: year" is expressible inclusive of both endpoints; beyond that the honest
#: answer is a scheduled extract, not a synchronous scan of several years of
#: ledger (master rules section 25).
REPORT_MAX_RANGE_DAYS = 366


class ReportRowBase(BaseModel):
    """Base for every report row.

    ``from_attributes`` so a row can be built from a SQLAlchemy ``Row`` mapping
    as well as from a plain dict, which is what lets the same statement feed both
    the JSON response and the spreadsheet writer.
    """

    model_config = ConfigDict(from_attributes=True)


# -----------------------------------------------------------------------------
# Request parameters
# -----------------------------------------------------------------------------


class ReportPeriod(BaseModel):
    """A closed, bounded date range - the only kind a period report accepts.

    Both ends are inclusive, and both are required. An open-ended range is not
    "convenient default behaviour": on a five-year-old ledger it is a full scan
    that times out after holding a connection for a minute, which is precisely
    what section 25 forbids. Rejecting it with a message that says what to do
    instead is the useful behaviour.
    """

    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _bounded(self) -> ReportPeriod:
        if self.date_to < self.date_from:
            raise ValidationError(
                "The report period ends before it starts.",
                details={"date_from": str(self.date_from), "date_to": str(self.date_to)},
            )
        span = (self.date_to - self.date_from).days + 1
        if span > REPORT_MAX_RANGE_DAYS:
            raise ValidationError(
                f"A report period may cover at most {REPORT_MAX_RANGE_DAYS} days; "
                f"this one covers {span}. Narrow the range and run it again.",
                details={"requested_days": span, "maximum_days": REPORT_MAX_RANGE_DAYS},
            )
        return self

    @property
    def days(self) -> int:
        """Calendar days in the period, both ends included.

        The denominator of every per-day figure in this module, so it is defined
        once: an off-by-one here would quietly bias machine utilisation and
        average daily usage in every report that uses them.
        """
        return (self.date_to - self.date_from).days + 1


class ReportFilters(BaseModel):
    """The optional narrowing every report shares.

    One filter object rather than nineteen. Each report reads the fields that
    mean something to it and ignores the rest - a stock report has no customer,
    a vendor report has no warehouse - which keeps the query-parameter vocabulary
    identical across the whole reporting surface. A filter a report does not use
    is not silently dropped: it is simply not part of that report's parameter
    hash, so it cannot split the snapshot cache either.
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    work_center_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    status: Annotated[str | None, Field(max_length=32)] = None
    #: Age threshold in days: dead stock older than this, delays longer than this.
    min_days: Annotated[int | None, Field(ge=0, le=3650)] = None
    search: Annotated[str | None, Field(max_length=120)] = None


# -----------------------------------------------------------------------------
# Report rows
# -----------------------------------------------------------------------------


class SalesOrderStatusRow(ReportRowBase):
    sales_order_id: uuid.UUID
    code: str
    order_date: date
    required_date: date
    status: str
    customer_code: str
    customer_name: str
    total_amount: Decimal
    ordered_quantity: Decimal
    allocated_quantity: Decimal
    dispatched_quantity: Decimal
    #: Derived after the query, not selected - see ``service._derive_*``.
    pending_quantity: Decimal = Decimal("0")
    fulfilment_percent: Decimal = Decimal("0")


class StockAvailabilityRow(ReportRowBase):
    item_id: uuid.UUID
    item_code: str
    item_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    warehouse_name: str
    on_hand: Decimal
    reserved: Decimal
    available: Decimal
    valuation_amount: Decimal
    reorder_level: Decimal
    last_movement_at: datetime | None = None
    refreshed_at: datetime
    #: Derived: is this position at or below its reorder level?
    below_reorder_level: bool = False


class MrpShortageRow(ReportRowBase):
    item_id: uuid.UUID
    item_code: str
    run_code: str
    run_completed_at: datetime | None = None
    gross_requirement: Decimal
    available_quantity: Decimal
    incoming_quantity: Decimal
    shortage_quantity: Decimal
    earliest_required_date: date
    #: Derived: days between today and the date the shortage bites.
    days_to_required: int = 0
    urgency_bucket: str = ""


class PurchasePendingRow(ReportRowBase):
    purchase_order_id: uuid.UUID
    code: str
    po_date: date
    expected_delivery_date: date | None = None
    status: str
    vendor_code: str
    vendor_name: str
    total_amount: Decimal
    ordered_quantity: Decimal
    received_quantity: Decimal
    pending_quantity: Decimal
    days_overdue: int = 0
    ageing_bucket: str = ""


class ProductionPendingRow(ReportRowBase):
    production_order_id: uuid.UUID
    code: str
    item_code: str
    item_name: str
    status: str
    priority: int
    planned_quantity: Decimal
    produced_quantity: Decimal
    pending_quantity: Decimal
    planned_start_date: date
    planned_end_date: date
    days_overdue: int = 0
    ageing_bucket: str = ""


class JobWorkPendingRow(ReportRowBase):
    job_work_order_id: uuid.UUID
    code: str
    vendor_code: str
    vendor_name: str
    process_type: str
    status: str
    expected_return_date: date
    issued_quantity: Decimal
    received_quantity: Decimal
    pending_quantity: Decimal
    #: Output the vendor produced badly, still traceable to the order. Shown
    #: rather than folded into pending: rejected stock is accounted for, not
    #: outstanding (docs/assumptions.md §E7b-1).
    rejected_quantity: Decimal = Decimal("0")
    #: Input written off as process loss. Included in ``received_quantity``
    #: upstream; repeated here so the report reads without a second lookup.
    scrap_quantity: Decimal = Decimal("0")
    total_charges: Decimal
    days_overdue: int = 0
    ageing_bucket: str = ""


class JobWorkCostRow(ReportRowBase):
    """What one job-work order line's output really cost (E7b/P13.M5).

    ``input_cost`` values the input the line's good output consumed at the
    frozen challan issue cost - the ledger's FIFO draw when the material left -
    and ``cost_per_unit`` adds the processing charges the receipts accrued.
    Null while the line has returned no good output: there is nothing to divide
    by yet.
    """

    job_work_order_id: uuid.UUID
    code: str
    line_number: int
    vendor_code: str
    vendor_name: str
    process_type: str
    status: str
    output_item_code: str
    input_cost: Decimal
    charges: Decimal
    debits: Decimal
    output_quantity: Decimal
    cost_per_unit: Decimal | None = None


class QcRejectionRow(ReportRowBase):
    inspection_id: uuid.UUID
    code: str
    qc_stage: str
    result: str
    item_code: str
    item_name: str
    vendor_name: str | None = None
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    scrap_quantity: Decimal
    rejection_reason: str | None = None
    inspected_at: datetime | None = None
    rejection_rate_percent: Decimal = Decimal("0")


class ReworkRateRow(ReportRowBase):
    """Rework quantity and rate for one item and vendor over the period."""

    item_id: uuid.UUID
    item_code: str
    item_name: str
    vendor_id: uuid.UUID | None = None
    vendor_name: str | None = None
    inspection_count: int
    inspected_quantity: Decimal
    rework_quantity: Decimal
    rework_rate_percent: Decimal = Decimal("0")


class VendorPerformanceRow(ReportRowBase):
    vendor_id: uuid.UUID
    vendor_code: str
    vendor_name: str
    receipt_count: int
    on_time_receipt_count: int
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    on_time_percent: Decimal = Decimal("0")
    rejection_rate_percent: Decimal = Decimal("0")
    performance_score: Decimal = Decimal("0")


class ProductProfitabilityRow(ReportRowBase):
    item_id: uuid.UUID
    item_code: str
    item_name: str
    quantity_sold: Decimal
    revenue_amount: Decimal
    cost_amount: Decimal
    margin_amount: Decimal = Decimal("0")
    margin_percent: Decimal = Decimal("0")


class ItemMovementRow(ReportRowBase):
    """Shared by the fast-moving and slow-moving reports.

    One shape for both because they are the same question read from opposite
    ends of the same ordering; giving them two schemas would let the two drift
    apart and make them incomparable.
    """

    item_id: uuid.UUID
    item_code: str
    item_name: str
    on_hand: Decimal
    issued_quantity: Decimal
    movement_count: int
    last_movement_at: datetime | None = None
    average_daily_usage: Decimal = Decimal("0")
    days_of_cover: Decimal | None = None


class DeadStockRow(ReportRowBase):
    item_id: uuid.UUID
    item_code: str
    item_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    on_hand: Decimal
    valuation_amount: Decimal
    last_movement_at: datetime | None = None
    #: Null when the position has never moved at all, which is not the same as
    #: "moved zero days ago" - the ageing bucket reads it as the oldest state.
    days_since_movement: int | None = None
    ageing_bucket: str = ""


class OrderDelayRow(ReportRowBase):
    sales_order_id: uuid.UUID
    code: str
    customer_code: str
    customer_name: str
    status: str
    order_date: date
    required_date: date
    promised_date: date | None = None
    total_amount: Decimal
    ordered_quantity: Decimal
    dispatched_quantity: Decimal
    pending_quantity: Decimal = Decimal("0")
    days_overdue: int = 0
    ageing_bucket: str = ""


class ProductionEfficiencyRow(ReportRowBase):
    production_order_id: uuid.UUID
    code: str
    item_code: str
    item_name: str
    planned_quantity: Decimal
    produced_quantity: Decimal
    rejected_quantity: Decimal
    scrap_quantity: Decimal
    planned_minutes: Decimal
    actual_minutes: Decimal
    planned_end_date: date
    actual_end_date: date | None = None
    yield_percent: Decimal = Decimal("0")
    schedule_efficiency_percent: Decimal = Decimal("0")
    completed_on_time: bool = False


class ProductionCostRow(ReportRowBase):
    production_order_id: uuid.UUID
    code: str
    closed_at: datetime
    item_id: uuid.UUID
    item_code: str
    item_name: str
    cost_centre_id: uuid.UUID | None
    cost_centre_code: str | None
    cost_centre_name: str | None
    produced_quantity: Decimal
    material_cost: Decimal
    operation_cost: Decimal
    subcontract_cost: Decimal | None
    total_cost: Decimal
    actual_unit_cost: Decimal | None
    standard_unit_cost: Decimal | None
    material_variance: Decimal | None
    operation_variance: Decimal | None
    subcontract_variance: Decimal | None
    total_variance: Decimal | None


class ShiftProductionRow(ReportRowBase):
    """One date, shift, cell, machine and operator: what was booked.

    ``shift`` is the code snapshot on the bookings - legacy typed text included -
    and any of the cell, machine and operator may be empty on a booking that did
    not name one.
    """

    entry_date: date
    shift: str | None
    work_center_id: uuid.UUID | None
    work_center_code: str | None
    machine_id: uuid.UUID | None
    machine_code: str | None
    operator_user_id: uuid.UUID | None
    operator_name: str | None
    employee_code: str | None
    produced_quantity: Decimal
    rejected_quantity: Decimal
    downtime_minutes: Decimal
    bookings: int
    #: Good output over good plus rejected, as a percentage.
    yield_percent: Decimal = Decimal("0")


class MachineUtilisationRow(ReportRowBase):
    machine_id: uuid.UUID
    machine_code: str
    machine_name: str
    work_center_code: str
    working_hours_per_day: Decimal
    work_order_count: int
    busy_minutes: Decimal
    downtime_minutes: Decimal
    completed_quantity: Decimal
    rejected_quantity: Decimal
    #: Net minutes the roster planned on this machine in the period - its
    #: PLANNED shift windows less their breaks.
    rostered_minutes: Decimal = Decimal("0")
    #: Busy minutes over rostered minutes. Zero when nothing was rostered.
    rostered_utilisation_percent: Decimal = Decimal("0")
    available_minutes: Decimal = Decimal("0")
    utilisation_percent: Decimal = Decimal("0")
    downtime_percent: Decimal = Decimal("0")


class InventoryValuationRow(ReportRowBase):
    item_id: uuid.UUID
    item_code: str
    item_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    on_hand: Decimal
    valuation_amount: Decimal
    unit_value: Decimal = Decimal("0")


class DispatchRow(ReportRowBase):
    dispatch_id: uuid.UUID
    code: str
    dispatch_type: str
    status: str
    dispatch_date: date
    expected_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    customer_name: str | None = None
    vendor_name: str | None = None
    transporter_name: str | None = None
    total_boxes: int
    total_weight_kg: Decimal
    freight_amount: Decimal
    tracking_id: str | None = None
    lr_number: str | None = None
    delay_days: int = 0


class CourierTrackingRow(ReportRowBase):
    dispatch_id: uuid.UUID
    code: str
    status: str
    dispatch_date: date
    expected_delivery_date: date | None = None
    courier_name: str | None = None
    transport_name: str | None = None
    transporter_name: str | None = None
    tracking_id: str | None = None
    lr_number: str | None = None
    vehicle_number: str | None = None
    last_event_status: str | None = None
    last_event_at: datetime | None = None
    last_event_location: str | None = None
    days_in_transit: int = 0


class VendorDispatchHistoryRow(ReportRowBase):
    dispatch_id: uuid.UUID
    code: str
    vendor_code: str
    vendor_name: str
    dispatch_type: str
    status: str
    dispatch_date: date
    expected_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    total_boxes: int
    total_weight_kg: Decimal
    freight_amount: Decimal
    tracking_id: str | None = None
    delay_days: int = 0


class CostCentreExpenseRow(ReportRowBase):
    """One (cost centre, account) pair's postings over the period.

    ``net_amount`` is debit minus credit for every row, whatever the account
    class: positive is cost, negative is income. One sign throughout is what
    lets a reader add the column down a cost centre and get its net cost; a
    sign flipped per account class would make that sum meaningless.
    """

    cost_centre_id: uuid.UUID
    cost_centre_code: str
    cost_centre_name: str
    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    debit_amount: Decimal
    credit_amount: Decimal
    net_amount: Decimal


class ScrapRegisterRow(ReportRowBase):
    """One movement into or out of the scrap pool.

    Exactly one of the two quantities is non-zero. ``source_type`` is the
    document that moved it (``qc_inspection``, ``job_work_order``,
    ``scrap_disposal``, ...) and ``source_code`` its number; the disposal
    columns are filled on the way out only.
    """

    transaction_id: uuid.UUID
    occurred_at: datetime
    item_id: uuid.UUID
    item_code: str
    item_name: str
    warehouse_code: str
    transaction_type: str
    source_type: str
    source_code: str | None
    disposal_kind: str | None
    buyer_reference: str | None
    scrap_in_quantity: Decimal
    scrap_out_quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal


class ConsumptionByDepartmentRow(ReportRowBase):
    """What one department and/or cost centre consumed of one item over the period."""

    department_id: uuid.UUID | None
    department_code: str | None
    department_name: str | None
    cost_centre_id: uuid.UUID | None
    cost_centre_code: str | None
    cost_centre_name: str | None
    item_id: uuid.UUID
    item_code: str
    item_name: str
    issue_count: int
    quantity: Decimal
    total_cost: Decimal


# -----------------------------------------------------------------------------
# Dashboard and jobs
# -----------------------------------------------------------------------------


class DashboardRead(BaseModel):
    """The management tile set (master rules section 25).

    Assembled from summary tables and indexed counts in one round trip and
    cached briefly. Every figure here is a *headline*: the tile links to the
    report that can be trusted for a decision, and none of these numbers is used
    to promise stock (section 34).
    """

    model_config = ConfigDict(from_attributes=True)

    open_sales_orders: int
    overdue_sales_orders: int
    stock_value: Decimal
    pending_purchase_orders: int
    pending_purchase_value: Decimal
    production_in_progress: int
    qc_pending: int
    dispatches_in_transit: int
    overdue_deliveries: int
    #: When the underlying summary table was last refreshed. A tile drawn from a
    #: stale summary must be able to say so rather than looking live.
    stock_refreshed_at: datetime | None = None
    generated_at: datetime


class ReportJobRead(BaseModel):
    """Status and result of a background report (``GET /reports/jobs/{id}``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_key: ReportKey
    status: ReportJobStatus
    parameters: dict[str, Any] | None = None
    row_count: int
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    document_id: uuid.UUID | None = None
    #: Where to fetch the artefact. Null until the job completes.
    download_url: str | None = None
    error_message: str | None = None
    created_at: datetime


class ReportColumnRead(BaseModel):
    """One column of a report, for a client that renders the grid generically."""

    name: str
    label: str
    kind: str


class ReportDescriptorRead(BaseModel):
    """What a report is and what it needs - drives the report picker."""

    key: ReportKey
    title: str
    description: str
    requires_period: bool
    columns: list[ReportColumnRead]


__all__ = [
    "REPORT_MAX_RANGE_DAYS",
    "CostCentreExpenseRow",
    "CourierTrackingRow",
    "DashboardRead",
    "DeadStockRow",
    "DispatchRow",
    "InventoryValuationRow",
    "ItemMovementRow",
    "JobWorkCostRow",
    "JobWorkPendingRow",
    "MachineUtilisationRow",
    "MrpShortageRow",
    "OrderDelayRow",
    "ProductProfitabilityRow",
    "ProductionCostRow",
    "ProductionEfficiencyRow",
    "ProductionPendingRow",
    "PurchasePendingRow",
    "QcRejectionRow",
    "ReportColumnRead",
    "ReportDescriptorRead",
    "ReportFilters",
    "ReportJobRead",
    "ReportPeriod",
    "ReportRowBase",
    "SalesOrderStatusRow",
    "StockAvailabilityRow",
    "VendorDispatchHistoryRow",
    "VendorPerformanceRow",
]
