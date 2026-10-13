"""The permission registry and baseline role definitions.

Master rules section 39: authorisation is ``User -> Role -> Permission ->
Resource -> Action``, enforced on the server. The frontend uses the same strings
purely to hide controls a user cannot use - it is never the enforcement point.

Permission strings are ``resource.action`` and are part of the API contract.
Adding one is routine; renaming one is a breaking change that must be migrated.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class Permission(StrEnum):
    """Every action the backend can gate on."""

    # --- platform / organisation -------------------------------------------
    COMPANY_VIEW = "company.view"
    COMPANY_MANAGE = "company.manage"
    BRANCH_VIEW = "branch.view"
    BRANCH_MANAGE = "branch.manage"
    PLANT_VIEW = "plant.view"
    PLANT_MANAGE = "plant.manage"
    DEPARTMENT_VIEW = "department.view"
    DEPARTMENT_MANAGE = "department.manage"
    COST_CENTRE_VIEW = "cost_centre.view"
    COST_CENTRE_MANAGE = "cost_centre.manage"
    SETTINGS_VIEW = "settings.view"
    SETTINGS_MANAGE = "settings.manage"

    # --- identity -----------------------------------------------------------
    USER_VIEW = "user.view"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DEACTIVATE = "user.deactivate"
    USER_RESET_PASSWORD = "user.reset_password"
    ROLE_VIEW = "role.view"
    ROLE_MANAGE = "role.manage"
    PERMISSION_ASSIGN = "permission.assign"

    # --- business partners --------------------------------------------------
    CUSTOMER_VIEW = "customer.view"
    CUSTOMER_CREATE = "customer.create"
    CUSTOMER_UPDATE = "customer.update"
    CUSTOMER_DELETE = "customer.delete"
    DEALER_VIEW = "dealer.view"
    DEALER_MANAGE = "dealer.manage"
    VENDOR_VIEW = "vendor.view"
    VENDOR_CREATE = "vendor.create"
    VENDOR_UPDATE = "vendor.update"
    VENDOR_DELETE = "vendor.delete"
    VENDOR_RATE = "vendor.rate"
    TRANSPORTER_VIEW = "transporter.view"
    TRANSPORTER_MANAGE = "transporter.manage"

    # --- item / product master ---------------------------------------------
    ITEM_VIEW = "item.view"
    ITEM_CREATE = "item.create"
    ITEM_UPDATE = "item.update"
    ITEM_DELETE = "item.delete"
    ITEM_COST_VIEW = "item.cost_view"
    CATEGORY_VIEW = "category.view"
    CATEGORY_MANAGE = "category.manage"
    UOM_VIEW = "uom.view"
    UOM_MANAGE = "uom.manage"
    ITEM_ATTRIBUTE_VIEW = "item_attribute.view"
    ITEM_ATTRIBUTE_MANAGE = "item_attribute.manage"
    BRAND_VIEW = "brand.view"
    BRAND_MANAGE = "brand.manage"
    MANUFACTURER_VIEW = "manufacturer.view"
    MANUFACTURER_MANAGE = "manufacturer.manage"
    PRICE_VIEW = "price.view"
    PRICE_MANAGE = "price.manage"

    # --- warehouses ---------------------------------------------------------
    WAREHOUSE_VIEW = "warehouse.view"
    WAREHOUSE_MANAGE = "warehouse.manage"
    LOCATION_VIEW = "location.view"
    LOCATION_MANAGE = "location.manage"

    # --- inventory ----------------------------------------------------------
    INVENTORY_VIEW = "inventory.view"
    INVENTORY_LEDGER_VIEW = "inventory.ledger_view"
    INVENTORY_TRANSFER = "inventory.transfer"
    INVENTORY_ADJUST = "inventory.adjust"
    INVENTORY_ADJUST_APPROVE = "inventory.adjust_approve"
    INVENTORY_RECONCILE = "inventory.reconcile"
    INVENTORY_PHYSICAL_COUNT = "inventory.physical_count"
    INVENTORY_VALUATION_VIEW = "inventory.valuation_view"
    INVENTORY_SCRAP_DISPOSE = "inventory.scrap_dispose"
    INTERNAL_ISSUE_CREATE = "internal_issue.create"
    INTERNAL_ISSUE_APPROVE = "internal_issue.approve"

    # --- reservation / allocation ------------------------------------------
    RESERVATION_VIEW = "reservation.view"
    RESERVATION_CREATE = "reservation.create"
    RESERVATION_RELEASE = "reservation.release"
    ALLOCATION_VIEW = "allocation.view"
    ALLOCATION_CREATE = "allocation.create"
    ALLOCATION_REALLOCATE = "allocation.reallocate"
    ALLOCATION_APPROVE = "allocation.approve"

    # --- product engineering / BOM -----------------------------------------
    BOM_VIEW = "bom.view"
    BOM_CREATE = "bom.create"
    BOM_UPDATE = "bom.update"
    BOM_APPROVE = "bom.approve"
    BOM_OBSOLETE = "bom.obsolete"
    BOM_COST_VIEW = "bom.cost_view"
    ROUTING_VIEW = "routing.view"
    ROUTING_MANAGE = "routing.manage"
    WORK_CENTER_VIEW = "work_center.view"
    WORK_CENTER_MANAGE = "work_center.manage"
    MACHINE_VIEW = "machine.view"
    MACHINE_MANAGE = "machine.manage"

    # --- sales --------------------------------------------------------------
    SALES_ORDER_VIEW = "sales_order.view"
    SALES_ORDER_CREATE = "sales_order.create"
    SALES_ORDER_UPDATE = "sales_order.update"
    SALES_ORDER_APPROVE = "sales_order.approve"
    SALES_ORDER_CANCEL = "sales_order.cancel"
    SALES_INVOICE_VIEW = "sales_invoice.view"
    SALES_INVOICE_CREATE = "sales_invoice.create"

    # --- MRP ----------------------------------------------------------------
    MRP_VIEW = "mrp.view"
    MRP_RUN = "mrp.run"
    MRP_CONVERT_SUGGESTION = "mrp.convert_suggestion"

    # --- procurement --------------------------------------------------------
    PURCHASE_REQUISITION_VIEW = "purchase_requisition.view"
    PURCHASE_REQUISITION_CREATE = "purchase_requisition.create"
    PURCHASE_REQUISITION_APPROVE = "purchase_requisition.approve"
    RFQ_VIEW = "rfq.view"
    RFQ_MANAGE = "rfq.manage"
    QUOTATION_VIEW = "quotation.view"
    QUOTATION_MANAGE = "quotation.manage"
    PURCHASE_ORDER_VIEW = "purchase_order.view"
    PURCHASE_ORDER_CREATE = "purchase_order.create"
    PURCHASE_ORDER_UPDATE = "purchase_order.update"
    PURCHASE_ORDER_APPROVE = "purchase_order.approve"
    PURCHASE_ORDER_REJECT = "purchase_order.reject"
    PURCHASE_ORDER_CANCEL = "purchase_order.cancel"
    PURCHASE_ORDER_SEND = "purchase_order.send"
    GRN_VIEW = "grn.view"
    GRN_CREATE = "grn.create"
    GRN_POST = "grn.post"
    PURCHASE_RETURN_VIEW = "purchase_return.view"
    PURCHASE_RETURN_CREATE = "purchase_return.create"

    # --- production ---------------------------------------------------------
    PRODUCTION_ORDER_VIEW = "production_order.view"
    PRODUCTION_ORDER_CREATE = "production_order.create"
    PRODUCTION_ORDER_APPROVE = "production_order.approve"
    PRODUCTION_ORDER_RELEASE = "production_order.release"
    PRODUCTION_ORDER_CANCEL = "production_order.cancel"
    PRODUCTION_ORDER_CLOSE = "production_order.close"
    WORK_ORDER_VIEW = "work_order.view"
    WORK_ORDER_MANAGE = "work_order.manage"
    MATERIAL_ISSUE_CREATE = "material_issue.create"
    MATERIAL_RETURN_CREATE = "material_return.create"
    PRODUCTION_ENTRY_CREATE = "production_entry.create"
    PRODUCTION_COST_VIEW = "production_cost.view"

    # --- assembly -----------------------------------------------------------
    ASSEMBLY_ORDER_VIEW = "assembly_order.view"
    ASSEMBLY_ORDER_MANAGE = "assembly_order.manage"
    ASSEMBLY_COMPLETE = "assembly.complete"

    # --- job work -----------------------------------------------------------
    JOB_WORK_VIEW = "job_work.view"
    JOB_WORK_CREATE = "job_work.create"
    JOB_WORK_ISSUE = "job_work.issue"
    JOB_WORK_RECEIVE = "job_work.receive"
    JOB_WORK_CLOSE = "job_work.close"

    # --- quality ------------------------------------------------------------
    QC_VIEW = "qc.view"
    QC_INSPECT = "qc.inspect"
    QC_DECIDE = "qc.decide"
    QC_TEMPLATE_MANAGE = "qc.template_manage"

    # --- packing / dispatch -------------------------------------------------
    PACKING_VIEW = "packing.view"
    PACKING_MANAGE = "packing.manage"
    DISPATCH_VIEW = "dispatch.view"
    DISPATCH_CREATE = "dispatch.create"
    DISPATCH_UPDATE = "dispatch.update"
    DISPATCH_CONFIRM_DELIVERY = "dispatch.confirm_delivery"

    # --- finance ------------------------------------------------------------
    FINANCE_VIEW = "finance.view"
    LEDGER_VIEW = "ledger.view"
    VENDOR_INVOICE_VIEW = "vendor_invoice.view"
    VENDOR_INVOICE_CREATE = "vendor_invoice.create"
    VENDOR_INVOICE_APPROVE = "vendor_invoice.approve"
    PAYMENT_VIEW = "payment.view"
    PAYMENT_RECORD = "payment.record"
    PAYMENT_APPROVE = "payment.approve"
    GST_REPORT_VIEW = "gst_report.view"

    # --- cross-cutting ------------------------------------------------------
    REPORT_VIEW = "report.view"
    REPORT_EXPORT = "report.export"
    DASHBOARD_VIEW = "dashboard.view"
    DOCUMENT_VIEW = "document.view"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DELETE = "document.delete"
    NOTIFICATION_VIEW = "notification.view"
    AUDIT_LOG_VIEW = "audit_log.view"
    JOB_VIEW = "job.view"


# --- Groupings used by the seed and by the permission-matrix UI ---------------

PERMISSION_GROUPS: dict[str, tuple[str, ...]] = {
    "Organisation": ("company", "branch", "plant", "department", "cost_centre", "settings"),
    "Identity & Access": ("user", "role", "permission"),
    "Business Partners": ("customer", "dealer", "vendor", "transporter"),
    "Item Master": (
        "item",
        "category",
        "uom",
        "item_attribute",
        "brand",
        "manufacturer",
        "price",
    ),
    "Warehouses": ("warehouse", "location"),
    "Inventory": ("inventory", "reservation", "allocation", "internal_issue"),
    "Engineering": ("bom", "routing", "work_center", "machine"),
    "Sales": ("sales_order", "sales_invoice"),
    "Planning": ("mrp",),
    "Procurement": (
        "purchase_requisition",
        "rfq",
        "quotation",
        "purchase_order",
        "grn",
        "purchase_return",
    ),
    "Production": (
        "production_order",
        "work_order",
        "material_issue",
        "material_return",
        "production_entry",
        "production_cost",
    ),
    "Assembly": ("assembly_order", "assembly"),
    "Job Work": ("job_work",),
    "Quality": ("qc",),
    "Packing & Dispatch": ("packing", "dispatch"),
    "Finance": ("finance", "ledger", "vendor_invoice", "payment", "gst_report"),
    "Reporting & Audit": ("report", "dashboard", "document", "notification", "audit_log", "job"),
}


def group_for(permission: Permission) -> str:
    resource = str(permission).split(".", 1)[0]
    for group, resources in PERMISSION_GROUPS.items():
        if resource in resources:
            return group
    return "Other"


# --- Resolution: one definition of "what may this account do" ------------------


class GrantingRole(Protocol):
    """What resolution reads off a role: an ORM ``Role`` or a query projection.

    A protocol so the request path (``core.deps``), the administration screen
    (``users.service``) and any batch reader - procurement folding the rights of
    a vendor's staff to choose who an order goes to - share this one fold
    instead of each spelling it out and drifting apart.
    """

    @property
    def code(self) -> str: ...

    @property
    def permissions(self) -> Sequence[str] | None: ...

    @property
    def is_active(self) -> bool: ...

    @property
    def deleted_at(self) -> datetime | None: ...


def resolve_role_grants(
    roles: Iterable[GrantingRole],
    extra_permissions: Sequence[str] | None,
    denied_permissions: Sequence[str] | None,
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Union of usable role permissions plus direct grants, minus direct denials.

    Returns the permission set and the codes of the roles that contributed to
    it, sorted. A suspended or retired role grants nothing. Denials are applied
    last and unconditionally: an explicit "this person may not approve payments"
    must not be overridable by adding another role. The superuser flag is not
    folded in - it is authority applied at the check (``Principal.has``), not a
    stored permission.
    """
    granted: set[str] = set()
    role_codes: list[str] = []
    for role in roles:
        if not role.is_active or role.deleted_at is not None:
            continue
        role_codes.append(role.code)
        granted.update(role.permissions or [])
    granted.update(extra_permissions or [])
    granted.difference_update(denied_permissions or [])
    return frozenset(granted), tuple(sorted(role_codes))


# --- Baseline roles -----------------------------------------------------------
# Seed data, not a hard-coded policy: an administrator may edit any of these, and
# may create new roles. Vendor roles mirror master rules section 16.

VIEW_ONLY: frozenset[Permission] = frozenset(
    p
    for p in Permission
    if str(p).endswith((".view", ".ledger_view", ".cost_view", ".valuation_view"))
)

_STORE = frozenset(
    {
        Permission.INVENTORY_VIEW,
        Permission.INVENTORY_LEDGER_VIEW,
        Permission.INVENTORY_TRANSFER,
        Permission.INVENTORY_SCRAP_DISPOSE,
        Permission.INTERNAL_ISSUE_CREATE,
        Permission.DEPARTMENT_VIEW,
        Permission.INVENTORY_ADJUST,
        Permission.INVENTORY_PHYSICAL_COUNT,
        Permission.INVENTORY_RECONCILE,
        Permission.RESERVATION_VIEW,
        Permission.RESERVATION_CREATE,
        Permission.RESERVATION_RELEASE,
        Permission.ALLOCATION_VIEW,
        Permission.WAREHOUSE_VIEW,
        Permission.LOCATION_VIEW,
        Permission.LOCATION_MANAGE,
        Permission.ITEM_VIEW,
        Permission.MATERIAL_ISSUE_CREATE,
        Permission.MATERIAL_RETURN_CREATE,
        Permission.GRN_VIEW,
        Permission.GRN_CREATE,
        Permission.DOCUMENT_VIEW,
        Permission.DOCUMENT_UPLOAD,
        Permission.REPORT_VIEW,
        Permission.DASHBOARD_VIEW,
        Permission.NOTIFICATION_VIEW,
    }
)

_PURCHASE = frozenset(
    {
        Permission.VENDOR_VIEW,
        Permission.VENDOR_CREATE,
        Permission.VENDOR_UPDATE,
        Permission.VENDOR_RATE,
        Permission.ITEM_VIEW,
        Permission.ITEM_COST_VIEW,
        # A buyer sources by maker and brand: "that maker's cylinder, from
        # whichever distributor has it" is a normal purchasing question.
        Permission.BRAND_VIEW,
        Permission.MANUFACTURER_VIEW,
        Permission.PURCHASE_REQUISITION_VIEW,
        Permission.PURCHASE_REQUISITION_CREATE,
        Permission.RFQ_VIEW,
        Permission.RFQ_MANAGE,
        Permission.QUOTATION_VIEW,
        Permission.QUOTATION_MANAGE,
        Permission.PURCHASE_ORDER_VIEW,
        Permission.PURCHASE_ORDER_CREATE,
        Permission.PURCHASE_ORDER_UPDATE,
        Permission.PURCHASE_ORDER_SEND,
        Permission.GRN_VIEW,
        Permission.PURCHASE_RETURN_VIEW,
        Permission.PURCHASE_RETURN_CREATE,
        Permission.MRP_VIEW,
        Permission.MRP_CONVERT_SUGGESTION,
        Permission.INVENTORY_VIEW,
        Permission.DISPATCH_VIEW,
        Permission.REPORT_VIEW,
        Permission.REPORT_EXPORT,
        Permission.DASHBOARD_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.DOCUMENT_UPLOAD,
        Permission.NOTIFICATION_VIEW,
        Permission.JOB_VIEW,
        # Requisitions and orders name the cost centre they are charged to.
        Permission.COST_CENTRE_VIEW,
    }
)

_PRODUCTION = frozenset(
    {
        Permission.PRODUCTION_ORDER_VIEW,
        Permission.PRODUCTION_ORDER_CREATE,
        Permission.PRODUCTION_ORDER_RELEASE,
        Permission.PRODUCTION_ORDER_CLOSE,
        Permission.WORK_ORDER_VIEW,
        Permission.WORK_ORDER_MANAGE,
        Permission.MATERIAL_ISSUE_CREATE,
        Permission.MATERIAL_RETURN_CREATE,
        Permission.PRODUCTION_ENTRY_CREATE,
        Permission.PRODUCTION_COST_VIEW,
        Permission.ASSEMBLY_ORDER_VIEW,
        Permission.ASSEMBLY_ORDER_MANAGE,
        Permission.ASSEMBLY_COMPLETE,
        Permission.JOB_WORK_VIEW,
        Permission.JOB_WORK_CREATE,
        Permission.JOB_WORK_ISSUE,
        Permission.JOB_WORK_RECEIVE,
        Permission.BOM_VIEW,
        Permission.ROUTING_VIEW,
        Permission.WORK_CENTER_VIEW,
        Permission.MACHINE_VIEW,
        Permission.INVENTORY_VIEW,
        Permission.INVENTORY_LEDGER_VIEW,
        Permission.RESERVATION_VIEW,
        Permission.ALLOCATION_VIEW,
        Permission.MRP_VIEW,
        Permission.ITEM_VIEW,
        Permission.REPORT_VIEW,
        Permission.DASHBOARD_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.NOTIFICATION_VIEW,
        # Production and job-work orders name the cost centre they are charged to.
        Permission.COST_CENTRE_VIEW,
    }
)

_QC = frozenset(
    {
        Permission.QC_VIEW,
        Permission.QC_INSPECT,
        Permission.QC_DECIDE,
        Permission.ITEM_VIEW,
        Permission.INVENTORY_VIEW,
        Permission.GRN_VIEW,
        Permission.JOB_WORK_VIEW,
        Permission.PRODUCTION_ORDER_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.DOCUMENT_UPLOAD,
        Permission.REPORT_VIEW,
        Permission.DASHBOARD_VIEW,
        Permission.NOTIFICATION_VIEW,
    }
)

_DISPATCH = frozenset(
    {
        Permission.PACKING_VIEW,
        Permission.PACKING_MANAGE,
        Permission.DISPATCH_VIEW,
        Permission.DISPATCH_CREATE,
        Permission.DISPATCH_UPDATE,
        Permission.DISPATCH_CONFIRM_DELIVERY,
        Permission.TRANSPORTER_VIEW,
        Permission.SALES_ORDER_VIEW,
        Permission.INVENTORY_VIEW,
        Permission.ITEM_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.DOCUMENT_UPLOAD,
        Permission.REPORT_VIEW,
        Permission.DASHBOARD_VIEW,
        Permission.NOTIFICATION_VIEW,
    }
)

_SALES = frozenset(
    {
        Permission.CUSTOMER_VIEW,
        Permission.CUSTOMER_CREATE,
        Permission.CUSTOMER_UPDATE,
        Permission.DEALER_VIEW,
        Permission.SALES_ORDER_VIEW,
        Permission.SALES_ORDER_CREATE,
        Permission.SALES_ORDER_UPDATE,
        Permission.SALES_INVOICE_VIEW,
        Permission.ITEM_VIEW,
        # Dealers order by brand; a salesperson has to be able to read it.
        Permission.BRAND_VIEW,
        Permission.PRICE_VIEW,
        Permission.INVENTORY_VIEW,
        Permission.RESERVATION_VIEW,
        Permission.ALLOCATION_VIEW,
        Permission.DISPATCH_VIEW,
        Permission.REPORT_VIEW,
        Permission.DASHBOARD_VIEW,
        Permission.NOTIFICATION_VIEW,
    }
)

_ACCOUNTS = frozenset(
    {
        Permission.FINANCE_VIEW,
        Permission.LEDGER_VIEW,
        Permission.VENDOR_INVOICE_VIEW,
        Permission.VENDOR_INVOICE_CREATE,
        Permission.PAYMENT_VIEW,
        Permission.PAYMENT_RECORD,
        Permission.GST_REPORT_VIEW,
        Permission.INVENTORY_VALUATION_VIEW,
        Permission.SALES_INVOICE_VIEW,
        Permission.SALES_INVOICE_CREATE,
        Permission.PURCHASE_ORDER_VIEW,
        Permission.GRN_VIEW,
        Permission.CUSTOMER_VIEW,
        Permission.VENDOR_VIEW,
        Permission.ITEM_VIEW,
        Permission.ITEM_COST_VIEW,
        Permission.REPORT_VIEW,
        Permission.REPORT_EXPORT,
        Permission.DASHBOARD_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.NOTIFICATION_VIEW,
        # ASSUMPTION: Accounts owns the cost-centre master - it is the analytic
        # dimension of the ledger, and the cost-centre statement is theirs to
        # answer for. Purchase, production and PLANNER only view it.
        # docs/assumptions.md §48.
        Permission.COST_CENTRE_VIEW,
        Permission.COST_CENTRE_MANAGE,
    }
)

# Vendor-portal roles. A vendor principal is additionally constrained to its own
# vendor_id and assigned item categories - enforced server-side, never by the
# permission string alone (section 16/39).
_VENDOR_BASE = frozenset(
    {
        Permission.PURCHASE_ORDER_VIEW,
        Permission.DISPATCH_VIEW,
        Permission.DOCUMENT_VIEW,
        Permission.NOTIFICATION_VIEW,
    }
)

ROLE_PRESETS: dict[str, dict[str, object]] = {
    "SUPER_ADMIN": {
        "name": "Super Administrator",
        "description": "Unrestricted access. Reserved for platform owners.",
        "is_system": True,
        "permissions": frozenset(Permission),
    },
    "ADMIN": {
        "name": "Administrator",
        "description": "Full operational access excluding platform-level company management.",
        "is_system": True,
        "permissions": frozenset(Permission) - {Permission.COMPANY_MANAGE},
    },
    "PLANT_MANAGER": {
        "name": "Plant Manager",
        "description": "Oversees production, assembly, job work and quality.",
        "is_system": False,
        "permissions": _PRODUCTION
        | _QC
        | {
            Permission.PRODUCTION_ORDER_APPROVE,
            Permission.INTERNAL_ISSUE_APPROVE,
            Permission.PRODUCTION_ORDER_CANCEL,
            Permission.ALLOCATION_APPROVE,
            Permission.ALLOCATION_REALLOCATE,
            Permission.JOB_WORK_CLOSE,
            Permission.BOM_APPROVE,
        },
    },
    "STORE_KEEPER": {
        "name": "Store Keeper",
        "description": "Warehouse operations: receipts, issues, transfers, counts.",
        "is_system": False,
        "permissions": _STORE,
    },
    "PURCHASE_MANAGER": {
        "name": "Purchase Manager",
        "description": "Procurement lifecycle including PO approval.",
        "is_system": False,
        "permissions": _PURCHASE
        | {
            Permission.PURCHASE_REQUISITION_APPROVE,
            Permission.PURCHASE_ORDER_APPROVE,
            Permission.PURCHASE_ORDER_REJECT,
            Permission.PURCHASE_ORDER_CANCEL,
            Permission.MRP_RUN,
        },
    },
    "PURCHASE_EXECUTIVE": {
        "name": "Purchase Executive",
        "description": "Raises requisitions and purchase orders; cannot approve them.",
        "is_system": False,
        "permissions": _PURCHASE,
    },
    "PRODUCTION_SUPERVISOR": {
        "name": "Production Supervisor",
        "description": "Runs the shop floor against released orders.",
        "is_system": False,
        "permissions": _PRODUCTION,
    },
    "QC_INSPECTOR": {
        "name": "QC Inspector",
        "description": "Performs inspections and records accept/reject decisions.",
        "is_system": False,
        "permissions": _QC,
    },
    "DISPATCH_MANAGER": {
        "name": "Dispatch Manager",
        "description": "Packing, dispatch planning and delivery confirmation.",
        "is_system": False,
        "permissions": _DISPATCH,
    },
    "SALES_EXECUTIVE": {
        "name": "Sales Executive",
        "description": "Customer and sales-order management.",
        "is_system": False,
        "permissions": _SALES,
    },
    "ACCOUNTS": {
        "name": "Accounts",
        "description": "Ledgers, invoices, payments and statutory reporting.",
        "is_system": False,
        "permissions": _ACCOUNTS,
    },
    "PLANNER": {
        "name": "Production Planner",
        "description": "Runs MRP and converts suggestions into orders.",
        "is_system": False,
        "permissions": frozenset(
            {
                Permission.MRP_VIEW,
                Permission.MRP_RUN,
                Permission.MRP_CONVERT_SUGGESTION,
                Permission.BOM_VIEW,
                Permission.BOM_COST_VIEW,
                Permission.ROUTING_VIEW,
                Permission.INVENTORY_VIEW,
                Permission.INVENTORY_LEDGER_VIEW,
                Permission.RESERVATION_VIEW,
                Permission.ALLOCATION_VIEW,
                Permission.ALLOCATION_CREATE,
                Permission.ALLOCATION_REALLOCATE,
                Permission.SALES_ORDER_VIEW,
                Permission.PRODUCTION_ORDER_VIEW,
                Permission.PRODUCTION_ORDER_CREATE,
                Permission.PURCHASE_REQUISITION_VIEW,
                Permission.PURCHASE_REQUISITION_CREATE,
                Permission.ITEM_VIEW,
                Permission.BRAND_VIEW,
                Permission.WAREHOUSE_VIEW,
                Permission.REPORT_VIEW,
                Permission.DASHBOARD_VIEW,
                Permission.JOB_VIEW,
                Permission.NOTIFICATION_VIEW,
                Permission.COST_CENTRE_VIEW,
            }
        ),
    },
    "DESIGN_ENGINEER": {
        "name": "Design Engineer",
        "description": "Owns item engineering data, BOMs and routings.",
        "is_system": False,
        "permissions": frozenset(
            {
                Permission.ITEM_VIEW,
                Permission.ITEM_CREATE,
                Permission.ITEM_UPDATE,
                Permission.CATEGORY_VIEW,
                Permission.CATEGORY_MANAGE,
                Permission.UOM_VIEW,
                Permission.ITEM_ATTRIBUTE_VIEW,
                Permission.ITEM_ATTRIBUTE_MANAGE,
                Permission.BRAND_VIEW,
                Permission.BRAND_MANAGE,
                Permission.MANUFACTURER_VIEW,
                Permission.MANUFACTURER_MANAGE,
                Permission.BOM_VIEW,
                Permission.BOM_CREATE,
                Permission.BOM_UPDATE,
                Permission.BOM_OBSOLETE,
                Permission.BOM_COST_VIEW,
                Permission.ROUTING_VIEW,
                Permission.ROUTING_MANAGE,
                Permission.WORK_CENTER_VIEW,
                Permission.MACHINE_VIEW,
                Permission.DOCUMENT_VIEW,
                Permission.DOCUMENT_UPLOAD,
                Permission.REPORT_VIEW,
                Permission.DASHBOARD_VIEW,
            }
        ),
    },
    "AUDITOR": {
        "name": "Auditor",
        "description": "Read-only across the system, including the audit trail.",
        "is_system": False,
        "permissions": VIEW_ONLY | {Permission.AUDIT_LOG_VIEW, Permission.REPORT_EXPORT},
    },
    # --- vendor portal ------------------------------------------------------
    "VENDOR_OWNER": {
        "name": "Vendor Owner",
        "description": "Vendor principal with full access to that vendor's records.",
        "is_system": True,
        "is_vendor_role": True,
        "permissions": _VENDOR_BASE
        | {
            Permission.PURCHASE_ORDER_UPDATE,
            Permission.DISPATCH_CREATE,
            Permission.DISPATCH_UPDATE,
            Permission.VENDOR_INVOICE_VIEW,
            Permission.VENDOR_INVOICE_CREATE,
            Permission.PAYMENT_VIEW,
            Permission.JOB_WORK_VIEW,
            Permission.QC_VIEW,
            Permission.DOCUMENT_UPLOAD,
            Permission.USER_VIEW,
        },
    },
    "VENDOR_MANAGER": {
        "name": "Vendor Manager",
        "description": "Vendor staff limited to assigned item categories.",
        "is_system": True,
        "is_vendor_role": True,
        "permissions": _VENDOR_BASE
        | {
            Permission.PURCHASE_ORDER_UPDATE,
            Permission.DISPATCH_VIEW,
            Permission.JOB_WORK_VIEW,
            Permission.DOCUMENT_UPLOAD,
        },
    },
    "VENDOR_DISPATCH_MANAGER": {
        "name": "Vendor Dispatch Manager",
        "description": "Vendor staff who record dispatch and courier details.",
        "is_system": True,
        "is_vendor_role": True,
        "permissions": _VENDOR_BASE
        | {
            Permission.DISPATCH_CREATE,
            Permission.DISPATCH_UPDATE,
            Permission.DOCUMENT_UPLOAD,
        },
    },
    "VENDOR_QC_CONTACT": {
        "name": "Vendor QC Contact",
        "description": "Vendor quality contact; sees inspection outcomes on own supplies.",
        "is_system": True,
        "is_vendor_role": True,
        "permissions": _VENDOR_BASE | {Permission.QC_VIEW, Permission.DOCUMENT_UPLOAD},
    },
    "VENDOR_ACCOUNT_MANAGER": {
        "name": "Vendor Account Manager",
        "description": "Vendor finance contact: invoices, payment status, ledger.",
        "is_system": True,
        "is_vendor_role": True,
        "permissions": _VENDOR_BASE
        | {
            Permission.VENDOR_INVOICE_VIEW,
            Permission.VENDOR_INVOICE_CREATE,
            Permission.PAYMENT_VIEW,
            Permission.LEDGER_VIEW,
            Permission.DOCUMENT_UPLOAD,
        },
    },
}

VENDOR_ROLE_CODES: frozenset[str] = frozenset(
    code for code, spec in ROLE_PRESETS.items() if spec.get("is_vendor_role")
)

ALL_PERMISSIONS: tuple[Permission, ...] = tuple(Permission)

__all__ = [
    "ALL_PERMISSIONS",
    "PERMISSION_GROUPS",
    "ROLE_PRESETS",
    "VENDOR_ROLE_CODES",
    "VIEW_ONLY",
    "GrantingRole",
    "Permission",
    "group_for",
    "resolve_role_grants",
]
