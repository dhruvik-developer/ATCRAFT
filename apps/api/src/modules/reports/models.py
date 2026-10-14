"""Read models for reporting (master rules section 25).

This module owns **no business tables**. Every number it reports is produced
somewhere else - the inventory ledger, the order book, the QC register - and
duplicating any of them here would create a second version of the truth. What it
owns is two tables that exist purely so a dashboard never has to aggregate a hot
transactional table on a request path:

``inventory_summary``
    One row per (company, item, warehouse), refreshed by
    ``maintenance.refresh_summary_views``. Stock value, on-hand and last movement
    for a whole company are a single indexed range scan here instead of a group
    over ``inventory_balances``.

    **It is not authoritative.** ``available`` is a *copy*, minutes old, and no
    allocation decision may read it - master rules section 34 requires that
    availability come from ``inventory.get_available`` every time. The column
    exists so a stock-availability listing can be paginated and sorted; a promise
    made from it would be a promise made from stale data.

``report_snapshots``
    The record of one generated report: which report, against which parameters,
    how many rows it produced, and where the artefact went. It doubles as the job
    row for a report too large to serve inline, which is what
    ``GET /reports/jobs/{id}`` reads. Keeping the two in one table is deliberate -
    a snapshot and "the job that produced it" are the same fact at two points in
    time, and splitting them would mean writing the parameters twice and
    reconciling them afterwards.

Neither table is a source of truth, so neither is soft-deleted: they are derived
state, rebuilt on their own schedule from rows that are themselves audited.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity, sql_in_list
from src.core.state_machine import StateMachine, Transition
from src.core.types import GUID, Money, Quantity
from src.modules.inventory.models import StatusEnum


class ReportKey(StrEnum):
    """The closed catalogue of reports (master rules section 25).

    Closed rather than free text because it is a stored column: it keys the
    snapshot lookup, the cache entry and the artefact, so an ad-hoc string would
    produce files nothing can ever find again. Adding a member is a normal code
    change - a definition in ``service.REPORT_CATALOGUE`` and a route - and the
    catalogue asserts at import that every member has exactly one definition.
    """

    SALES_ORDER_STATUS = "SALES_ORDER_STATUS"
    STOCK_AVAILABILITY = "STOCK_AVAILABILITY"
    MRP_SHORTAGE = "MRP_SHORTAGE"
    PURCHASE_PENDING = "PURCHASE_PENDING"
    PRODUCTION_PENDING = "PRODUCTION_PENDING"
    JOB_WORK_PENDING = "JOB_WORK_PENDING"
    QC_REJECTION = "QC_REJECTION"
    VENDOR_PERFORMANCE = "VENDOR_PERFORMANCE"
    PRODUCT_PROFITABILITY = "PRODUCT_PROFITABILITY"
    FAST_MOVING = "FAST_MOVING"
    SLOW_MOVING = "SLOW_MOVING"
    DEAD_STOCK = "DEAD_STOCK"
    ORDER_DELAY = "ORDER_DELAY"
    PRODUCTION_EFFICIENCY = "PRODUCTION_EFFICIENCY"
    MACHINE_UTILISATION = "MACHINE_UTILISATION"
    SHIFT_PRODUCTION = "SHIFT_PRODUCTION"
    INVENTORY_VALUATION = "INVENTORY_VALUATION"
    DISPATCH = "DISPATCH"
    COURIER_TRACKING = "COURIER_TRACKING"
    VENDOR_DISPATCH_HISTORY = "VENDOR_DISPATCH_HISTORY"
    #: Income and expense per cost centre and account over a period, read from
    #: the posted ledger - the "cost centre statement".
    COST_CENTRE_EXPENSE = "COST_CENTRE_EXPENSE"
    #: Scrap in by source and scrap out by disposal over a period, read from the
    #: stock ledger and valued at what the scrap cost.
    SCRAP_REGISTER = "SCRAP_REGISTER"
    #: Posted internal issues per department / cost centre and item over a period.
    CONSUMPTION_BY_DEPARTMENT = "CONSUMPTION_BY_DEPARTMENT"
    #: A rendered document rather than a tabular report: the packing-list PDF
    #: for one packing order. It has a job row here - ``GET /reports/jobs/{id}``
    #: is what the client polls - but no ``ReportDefinition``, because there are
    #: no rows to page, count or cache. See :data:`DOCUMENT_ONLY_REPORT_KEYS`.
    PACKING_LIST = "PACKING_LIST"


#: Members of :class:`ReportKey` whose artefact is produced by a dedicated task
#: rather than by the catalogue runner. They exist so a module that renders a
#: document in the background (the packing list) can use the same job row, the
#: same status route and the same download link as every other background
#: report - without pretending to be a query with columns. None of them has a
#: definition, a route of its own under ``/reports`` or a catalogue descriptor,
#: and ``service`` asserts both halves at import.
DOCUMENT_ONLY_REPORT_KEYS: frozenset[ReportKey] = frozenset({ReportKey.PACKING_LIST})


class ReportJobStatus(StrEnum):
    """Lifecycle of a generated report artefact.

    ``QUEUED -> FAILED`` is a real edge, not a defensive one: the broker can
    refuse the task, and a job left in QUEUED forever would be polled by a client
    that never gets an answer.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: The only thing permitted to move ``report_snapshots.status`` (section 73).
REPORT_JOB_LIFECYCLE: StateMachine[ReportJobStatus] = StateMachine(
    entity="Report job",
    initial=ReportJobStatus.QUEUED,
    transitions=(
        Transition(ReportJobStatus.QUEUED, ReportJobStatus.RUNNING, label="Start"),
        Transition(ReportJobStatus.QUEUED, ReportJobStatus.FAILED, label="Could not be queued"),
        Transition(ReportJobStatus.RUNNING, ReportJobStatus.COMPLETED, label="Complete"),
        Transition(ReportJobStatus.RUNNING, ReportJobStatus.FAILED, label="Fail"),
    ),
    terminal=frozenset({ReportJobStatus.COMPLETED, ReportJobStatus.FAILED}),
)


class ReportSnapshot(TenantEntity):
    """One generated report: what was asked for, and what came out.

    ``parameters_hash`` is what makes a repeated request cheap. Two people asking
    for the same report over the same period within the retention window get the
    artefact that already exists rather than two Celery jobs writing two
    identical spreadsheets - which is also why the hash is taken over the
    *normalised* parameters (see ``service.parameters_hash``) rather than over
    the raw query string, where ``?warehouse_id=&page=1`` and ``?page=1`` would
    hash apart.

    A snapshot holds either an inline ``payload`` or a ``document_id``, never
    both: a small result is worth keeping in the row, and a 40 000-row export
    belongs in the document store where it can be streamed and expired like any
    other file (section 43).
    """

    __tablename__ = "report_snapshots"

    report_key: Mapped[ReportKey] = mapped_column(StatusEnum(ReportKey, 48), nullable=False)

    #: SHA-256 of the normalised parameter set. Hex, so 64 characters exactly.
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The parameters themselves, kept so a stored artefact can say what it is a
    #: report *of* - a hash alone is unreadable in a support conversation.
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[ReportJobStatus] = mapped_column(
        StatusEnum(ReportJobStatus),
        nullable=False,
        default=ReportJobStatus.QUEUED,
        server_default=ReportJobStatus.QUEUED.value,
        index=True,
    )

    #: When the rows were actually read. Null until the run completes - a queued
    #: job has no result, and stamping it at creation would date the artefact
    #: from before it existed.
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Inline result for a small report. Mutually exclusive with ``document_id``.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    #: After this the snapshot is no longer reused. Every cached artefact expires
    #: (section 33); the document it points at carries its own retention.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The reuse lookup: "is there a live artefact for exactly this request?"
        # Named explicitly because the generated name for four columns would
        # exceed PostgreSQL's 63-character identifier limit.
        Index(
            "ix_report_snapshots_lookup",
            "company_id",
            "report_key",
            "parameters_hash",
            text("created_at DESC"),
        ),
        CheckConstraint("row_count >= 0", name="row_count_is_not_negative"),
        CheckConstraint(f"status IN ({sql_in_list(ReportJobStatus)})", name="status_is_known"),
        CheckConstraint(f"report_key IN ({sql_in_list(ReportKey)})", name="report_key_is_known"),
        # A finished job has a result and a time it was produced; an unfinished
        # one has neither. Without this a client polling the job endpoint could
        # be told COMPLETED and handed nothing.
        CheckConstraint(
            "(status = 'COMPLETED') = (generated_at IS NOT NULL)",
            name="completed_has_generated_at",
        ),
        CheckConstraint(
            "payload IS NULL OR document_id IS NULL",
            name="result_is_inline_or_a_document",
        ),
        CheckConstraint(
            "status = 'FAILED' OR error_message IS NULL",
            name="only_a_failure_carries_an_error",
        ),
    )


class InventorySummary(TenantEntity):
    """Pre-aggregated stock position, one row per item and warehouse.

    Refreshed on a schedule from ``inventory_balances``, which is itself the
    derived-but-authoritative current state. Two derivations deep is deliberate:
    the balance table is written under row locks by every stock movement, and a
    dashboard scanning it would contend with the operations that keep it correct.

    The grain is (item, warehouse) - deliberately coarser than
    ``inventory_balances``, which is per location and per batch. Reporting
    questions are asked per warehouse; keeping the batch grain here would make
    this table as large as the one it exists to avoid reading.
    """

    __tablename__ = "inventory_summary"

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False
    )

    on_hand: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=0, server_default="0"
    )
    reserved: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=0, server_default="0"
    )
    #: A snapshot of promisable stock, NOT a promise. Section 34: an allocation
    #: decision calls ``inventory.get_available``; this column is for display and
    #: for sorting a report page.
    available: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=0, server_default="0"
    )
    valuation_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=0, server_default="0"
    )

    #: Newest movement against this item and warehouse. Null means "never moved",
    #: which for a row holding stock is the oldest possible dead stock - the
    #: ageing report has to treat it as such rather than skipping it.
    last_movement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "item_id",
            "warehouse_id",
            name="uq_inventory_summary_company_id_item_id_warehouse_id",
        ),
        # Dead-stock and ageing scans read exactly this: one company, ordered by
        # how long the stock has sat. NULLS FIRST because a row that has never
        # moved is the oldest, not the newest.
        Index(
            "ix_inventory_summary_ageing",
            "company_id",
            text("last_movement_at ASC NULLS FIRST"),
        ),
        # Valuation tiles and the stock-value report order by money descending.
        Index("ix_inventory_summary_value", "company_id", text("valuation_amount DESC")),
        CheckConstraint("on_hand >= 0", name="on_hand_is_not_negative"),
        CheckConstraint("reserved >= 0", name="reserved_is_not_negative"),
    )


__all__ = [
    "DOCUMENT_ONLY_REPORT_KEYS",
    "REPORT_JOB_LIFECYCLE",
    "InventorySummary",
    "ReportJobStatus",
    "ReportKey",
    "ReportSnapshot",
]
