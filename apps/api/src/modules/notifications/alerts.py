"""Pure registry and reconciliation planning for timed notification alerts.

Owning modules provide tenant-scoped scanners; this module owns only the
cross-module vocabulary and the deterministic episode comparison.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class AlertRuleKey(StrEnum):
    PO_NOT_ACKNOWLEDGED = "PO_NOT_ACKNOWLEDGED"
    PO_DELIVERY_MISSED = "PO_DELIVERY_MISSED"
    PO_DISPATCH_NOT_UPDATED = "PO_DISPATCH_NOT_UPDATED"
    PO_TRACKING_MISSING = "PO_TRACKING_MISSING"
    GRN_NOT_CREATED = "GRN_NOT_CREATED"
    QC_PENDING = "QC_PENDING"
    VENDOR_INVOICE_PENDING = "VENDOR_INVOICE_PENDING"
    VENDOR_PAYMENT_OVERDUE = "VENDOR_PAYMENT_OVERDUE"
    JOB_WORK_OVERDUE = "JOB_WORK_OVERDUE"
    DISPATCH_DELAYED = "DISPATCH_DELAYED"
    PRODUCTION_ORDER_DELAYED = "PRODUCTION_ORDER_DELAYED"
    SALES_ORDER_AT_RISK = "SALES_ORDER_AT_RISK"
    STOCK_BELOW_REORDER = "STOCK_BELOW_REORDER"


class AlertGranularity(StrEnum):
    ENTITY = "ENTITY"
    DIGEST = "DIGEST"


@dataclass(frozen=True, slots=True)
class AlertRule:
    key: AlertRuleKey
    notification_type: str
    name: str
    description: str
    severity: str
    default_threshold_minutes: int
    default_remind_every_minutes: int
    granularity: AlertGranularity
    internal_permission: str | None = None
    internal_roles: tuple[str, ...] = ()
    vendor_permission: str | None = None
    notify_creator: bool = False


@dataclass(frozen=True, slots=True)
class AlertStateSnapshot:
    entity_id: uuid.UUID
    last_notified_at: datetime | None
    notified_count: int


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    """One finding produced by an owning module's bounded scanner."""

    entity_type: str
    entity_id: uuid.UUID
    title: str
    body: str
    action_url: str
    creator_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    assigned_user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AlertReconcilePlan:
    new_ids: frozenset[uuid.UUID]
    still_ids: frozenset[uuid.UUID]
    gone_ids: frozenset[uuid.UUID]
    notify_ids: frozenset[uuid.UUID]


# ASSUMPTION: alert times are UTC and the defaults below are conservative
# operational starting points; each company's sparse rule row can override them.
ALERT_RULES: Mapping[AlertRuleKey, AlertRule] = {
    AlertRuleKey.PO_NOT_ACKNOWLEDGED: AlertRule(
        AlertRuleKey.PO_NOT_ACKNOWLEDGED,
        "PURCHASE_ORDER_NOT_ACKNOWLEDGED",
        "Purchase order not acknowledged",
        "A sent purchase order has not been acknowledged by its vendor.",
        "WARNING",
        24 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="purchase_order.approve",
        vendor_permission="vendor_portal.respond",
        notify_creator=True,
    ),
    AlertRuleKey.PO_DELIVERY_MISSED: AlertRule(
        AlertRuleKey.PO_DELIVERY_MISSED,
        "PURCHASE_ORDER_DELIVERY_MISSED",
        "Purchase order delivery missed",
        "An open purchase order is past its expected delivery date.",
        "WARNING",
        0,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="purchase_order.approve",
        vendor_permission="vendor_portal.respond",
        notify_creator=True,
    ),
    AlertRuleKey.PO_DISPATCH_NOT_UPDATED: AlertRule(
        AlertRuleKey.PO_DISPATCH_NOT_UPDATED,
        "PURCHASE_ORDER_DISPATCH_NOT_UPDATED",
        "Vendor dispatch not updated",
        "An acknowledged purchase order has no shipment recorded near its due date.",
        "WARNING",
        24 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="purchase_order.approve",
        vendor_permission="vendor_portal.ship",
        notify_creator=True,
    ),
    AlertRuleKey.PO_TRACKING_MISSING: AlertRule(
        AlertRuleKey.PO_TRACKING_MISSING,
        "PURCHASE_ORDER_TRACKING_MISSING",
        "Shipment tracking missing",
        "A vendor shipment has no tracking reference or lorry receipt number.",
        "WARNING",
        4 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="purchase_order.approve",
        vendor_permission="vendor_portal.ship",
        notify_creator=True,
    ),
    AlertRuleKey.GRN_NOT_CREATED: AlertRule(
        AlertRuleKey.GRN_NOT_CREATED,
        "GOODS_RECEIPT_NOT_CREATED",
        "Goods receipt not created",
        "A shipment is overdue and has no posted goods receipt.",
        "WARNING",
        2 * 24 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="grn.create",
        notify_creator=True,
    ),
    AlertRuleKey.QC_PENDING: AlertRule(
        AlertRuleKey.QC_PENDING,
        "QC_INSPECTION_PENDING",
        "Quality inspection pending",
        "A quality inspection has remained open beyond its threshold.",
        "WARNING",
        24 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="qc.inspect",
    ),
    AlertRuleKey.VENDOR_INVOICE_PENDING: AlertRule(
        AlertRuleKey.VENDOR_INVOICE_PENDING,
        "VENDOR_INVOICE_PENDING",
        "Vendor invoice pending",
        "A posted goods receipt has no non-cancelled vendor invoice.",
        "WARNING",
        7 * 24 * 60,
        7 * 24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="vendor_invoice.create",
        vendor_permission="vendor_portal.invoice",
        notify_creator=True,
    ),
    AlertRuleKey.VENDOR_PAYMENT_OVERDUE: AlertRule(
        AlertRuleKey.VENDOR_PAYMENT_OVERDUE,
        "VENDOR_PAYMENT_OVERDUE",
        "Vendor payment overdue",
        "A vendor invoice is past due and has an unpaid balance.",
        "WARNING",
        0,
        3 * 24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="payment.approve",
    ),
    AlertRuleKey.JOB_WORK_OVERDUE: AlertRule(
        AlertRuleKey.JOB_WORK_OVERDUE,
        "JOB_WORK_OVERDUE",
        "Job work overdue",
        "A job-work order with material at the vendor is past its expected return date.",
        "WARNING",
        0,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="job_work.issue",
        notify_creator=True,
    ),
    AlertRuleKey.DISPATCH_DELAYED: AlertRule(
        AlertRuleKey.DISPATCH_DELAYED,
        "DISPATCH_DELAYED",
        "Dispatch delayed",
        "A dispatch has passed its promised delivery date.",
        "WARNING",
        0,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="dispatch.view",
    ),
    AlertRuleKey.PRODUCTION_ORDER_DELAYED: AlertRule(
        AlertRuleKey.PRODUCTION_ORDER_DELAYED,
        "PRODUCTION_ORDER_DELAYED",
        "Production order delayed",
        "An open production order is past its planned end date.",
        "WARNING",
        0,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="production_order.view",
        notify_creator=True,
    ),
    AlertRuleKey.SALES_ORDER_AT_RISK: AlertRule(
        AlertRuleKey.SALES_ORDER_AT_RISK,
        "SALES_ORDER_AT_RISK",
        "Sales order at risk",
        "An open sales order is near its required date and remains short.",
        "WARNING",
        7 * 24 * 60,
        24 * 60,
        AlertGranularity.ENTITY,
        internal_permission="sales_order.view",
        notify_creator=True,
    ),
    AlertRuleKey.STOCK_BELOW_REORDER: AlertRule(
        AlertRuleKey.STOCK_BELOW_REORDER,
        "STOCK_BELOW_REORDER",
        "Stock below reorder level",
        "Available stock is at or below its configured reorder level.",
        "WARNING",
        0,
        24 * 60,
        AlertGranularity.DIGEST,
        internal_permission="inventory.view",
    ),
}


def plan_reconciliation(
    *,
    open_states: Mapping[uuid.UUID, AlertStateSnapshot],
    current_entity_ids: set[uuid.UUID],
    as_of: datetime,
    remind_every: timedelta,
) -> AlertReconcilePlan:
    """Compare one rule's current findings with its open episodes."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware.")
    if remind_every <= timedelta(0):
        raise ValueError("remind_every must be positive.")

    existing_ids = set(open_states)
    new_ids = current_entity_ids - existing_ids
    still_ids = current_entity_ids & existing_ids
    gone_ids = existing_ids - current_entity_ids

    def is_reminder_due(entity_id: uuid.UUID) -> bool:
        last_notified_at = open_states[entity_id].last_notified_at
        return last_notified_at is None or as_of - last_notified_at >= remind_every

    notify_ids = new_ids | {entity_id for entity_id in still_ids if is_reminder_due(entity_id)}

    return AlertReconcilePlan(
        new_ids=frozenset(new_ids),
        still_ids=frozenset(still_ids),
        gone_ids=frozenset(gone_ids),
        notify_ids=frozenset(notify_ids),
    )


__all__ = [
    "ALERT_RULES",
    "AlertCandidate",
    "AlertGranularity",
    "AlertReconcilePlan",
    "AlertRule",
    "AlertRuleKey",
    "AlertStateSnapshot",
    "plan_reconciliation",
]
