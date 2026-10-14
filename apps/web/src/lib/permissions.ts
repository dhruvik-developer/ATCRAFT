/**
 * Permission string constants, mirroring
 * `apps/api/src/core/permissions.py`.
 *
 * Duplicated deliberately rather than fetched at runtime: these strings are used
 * in menu definitions and route guards that must be resolvable at build time,
 * and a typo becoming a silently-hidden button is far worse than the small cost
 * of keeping two lists in step.
 *
 * Reminder: these gate UI affordances only. The API authorises every request
 * independently (master rules 38/39).
 */

export const P = {
  // organisation
  COMPANY_VIEW: 'company.view',
  COMPANY_MANAGE: 'company.manage',
  BRANCH_MANAGE: 'branch.manage',
  BRANCH_VIEW: 'branch.view',
  PLANT_MANAGE: 'plant.manage',
  PLANT_VIEW: 'plant.view',
  DEPARTMENT_MANAGE: 'department.manage',
  DEPARTMENT_VIEW: 'department.view',
  COST_CENTRE_MANAGE: 'cost_centre.manage',
  COST_CENTRE_VIEW: 'cost_centre.view',
  SETTINGS_MANAGE: 'settings.manage',
  SETTINGS_VIEW: 'settings.view',

  // identity
  USER_VIEW: 'user.view',
  USER_CREATE: 'user.create',
  USER_UPDATE: 'user.update',
  USER_DEACTIVATE: 'user.deactivate',
  USER_RESET_PASSWORD: 'user.reset_password',
  ROLE_VIEW: 'role.view',
  ROLE_MANAGE: 'role.manage',
  PERMISSION_ASSIGN: 'permission.assign',

  // partners
  CUSTOMER_VIEW: 'customer.view',
  CUSTOMER_CREATE: 'customer.create',
  CUSTOMER_UPDATE: 'customer.update',
  CUSTOMER_DELETE: 'customer.delete',
  VENDOR_VIEW: 'vendor.view',
  VENDOR_CREATE: 'vendor.create',
  VENDOR_UPDATE: 'vendor.update',
  VENDOR_RATE: 'vendor.rate',
  VENDOR_DELETE: 'vendor.delete',
  TRANSPORTER_VIEW: 'transporter.view',
  TRANSPORTER_MANAGE: 'transporter.manage',

  // items
  ITEM_VIEW: 'item.view',
  ITEM_CREATE: 'item.create',
  ITEM_UPDATE: 'item.update',
  ITEM_COST_VIEW: 'item.cost_view',
  ITEM_DELETE: 'item.delete',
  CATEGORY_VIEW: 'category.view',
  CATEGORY_MANAGE: 'category.manage',
  UOM_VIEW: 'uom.view',
  UOM_MANAGE: 'uom.manage',
  ITEM_ATTRIBUTE_VIEW: 'item_attribute.view',
  ITEM_ATTRIBUTE_MANAGE: 'item_attribute.manage',
  BRAND_VIEW: 'brand.view',
  BRAND_MANAGE: 'brand.manage',
  MANUFACTURER_VIEW: 'manufacturer.view',
  MANUFACTURER_MANAGE: 'manufacturer.manage',
  PRICE_VIEW: 'price.view',
  PRICE_MANAGE: 'price.manage',

  // warehouses
  WAREHOUSE_VIEW: 'warehouse.view',
  WAREHOUSE_MANAGE: 'warehouse.manage',
  LOCATION_MANAGE: 'location.manage',
  LOCATION_VIEW: 'location.view',

  // inventory
  INVENTORY_VIEW: 'inventory.view',
  INVENTORY_LEDGER_VIEW: 'inventory.ledger_view',
  INVENTORY_TRANSFER: 'inventory.transfer',
  INVENTORY_ADJUST: 'inventory.adjust',
  INVENTORY_ADJUST_APPROVE: 'inventory.adjust_approve',
  INVENTORY_PHYSICAL_COUNT: 'inventory.physical_count',
  INVENTORY_VALUATION_VIEW: 'inventory.valuation_view',
  INVENTORY_RECONCILE: 'inventory.reconcile',

  // allocation
  RESERVATION_VIEW: 'reservation.view',
  RESERVATION_CREATE: 'reservation.create',
  RESERVATION_RELEASE: 'reservation.release',
  ALLOCATION_VIEW: 'allocation.view',
  ALLOCATION_REALLOCATE: 'allocation.reallocate',
  ALLOCATION_APPROVE: 'allocation.approve',
  ALLOCATION_CREATE: 'allocation.create',

  // engineering
  BOM_VIEW: 'bom.view',
  BOM_CREATE: 'bom.create',
  BOM_UPDATE: 'bom.update',
  BOM_APPROVE: 'bom.approve',
  BOM_COST_VIEW: 'bom.cost_view',
  BOM_OBSOLETE: 'bom.obsolete',
  ROUTING_VIEW: 'routing.view',
  ROUTING_MANAGE: 'routing.manage',
  WORK_CENTER_VIEW: 'work_center.view',
  WORK_CENTER_MANAGE: 'work_center.manage',
  MACHINE_VIEW: 'machine.view',
  MACHINE_MANAGE: 'machine.manage',

  // sales
  SALES_ORDER_VIEW: 'sales_order.view',
  SALES_ORDER_CREATE: 'sales_order.create',
  SALES_ORDER_UPDATE: 'sales_order.update',
  SALES_ORDER_APPROVE: 'sales_order.approve',
  SALES_ORDER_CANCEL: 'sales_order.cancel',

  // planning
  MRP_VIEW: 'mrp.view',
  MRP_RUN: 'mrp.run',
  MRP_CONVERT_SUGGESTION: 'mrp.convert_suggestion',

  // procurement
  PURCHASE_REQUISITION_VIEW: 'purchase_requisition.view',
  PURCHASE_REQUISITION_CREATE: 'purchase_requisition.create',
  PURCHASE_REQUISITION_APPROVE: 'purchase_requisition.approve',
  RFQ_VIEW: 'rfq.view',
  RFQ_MANAGE: 'rfq.manage',
  QUOTATION_VIEW: 'quotation.view',
  QUOTATION_MANAGE: 'quotation.manage',
  PURCHASE_ORDER_VIEW: 'purchase_order.view',
  PURCHASE_ORDER_CREATE: 'purchase_order.create',
  PURCHASE_ORDER_UPDATE: 'purchase_order.update',
  PURCHASE_ORDER_APPROVE: 'purchase_order.approve',
  PURCHASE_ORDER_REJECT: 'purchase_order.reject',
  PURCHASE_ORDER_CANCEL: 'purchase_order.cancel',
  PURCHASE_ORDER_SEND: 'purchase_order.send',
  VENDOR_PORTAL_ACCESS: 'vendor_portal.access',
  VENDOR_PORTAL_RESPOND: 'vendor_portal.respond',
  VENDOR_PORTAL_SHIP: 'vendor_portal.ship',
  VENDOR_PORTAL_INVOICE: 'vendor_portal.invoice',
  VENDOR_PORTAL_QUALITY: 'vendor_portal.quality',
  DEALER_PORTAL_BROWSE: 'dealer_portal.browse',
  DEALER_PORTAL_ORDER: 'dealer_portal.order',
  DEALER_PORTAL_ACCOUNT: 'dealer_portal.account',
  DEALER_PORTAL_RETURN: 'dealer_portal.return',
  GRN_VIEW: 'grn.view',
  GRN_CREATE: 'grn.create',
  GRN_POST: 'grn.post',

  // production
  PRODUCTION_ORDER_VIEW: 'production_order.view',
  PRODUCTION_ORDER_CREATE: 'production_order.create',
  PRODUCTION_ORDER_APPROVE: 'production_order.approve',
  PRODUCTION_ORDER_RELEASE: 'production_order.release',
  PRODUCTION_ORDER_CLOSE: 'production_order.close',
  PRODUCTION_ORDER_CANCEL: 'production_order.cancel',
  MATERIAL_ISSUE_CREATE: 'material_issue.create',
  PRODUCTION_ENTRY_CREATE: 'production_entry.create',
  PRODUCTION_COST_VIEW: 'production_cost.view',

  // assembly & job work
  ASSEMBLY_ORDER_VIEW: 'assembly_order.view',
  ASSEMBLY_ORDER_MANAGE: 'assembly_order.manage',
  JOB_WORK_VIEW: 'job_work.view',
  JOB_WORK_CREATE: 'job_work.create',
  JOB_WORK_ISSUE: 'job_work.issue',
  JOB_WORK_RECEIVE: 'job_work.receive',
  JOB_WORK_CLOSE: 'job_work.close',

  // quality
  QC_VIEW: 'qc.view',
  QC_INSPECT: 'qc.inspect',
  QC_DECIDE: 'qc.decide',
  QC_TEMPLATE_MANAGE: 'qc.template_manage',

  // packing & dispatch
  PACKING_VIEW: 'packing.view',
  PACKING_MANAGE: 'packing.manage',
  DISPATCH_VIEW: 'dispatch.view',
  DISPATCH_CREATE: 'dispatch.create',
  DISPATCH_UPDATE: 'dispatch.update',
  DISPATCH_CONFIRM_DELIVERY: 'dispatch.confirm_delivery',

  // finance
  FINANCE_VIEW: 'finance.view',
  LEDGER_VIEW: 'ledger.view',
  VENDOR_INVOICE_VIEW: 'vendor_invoice.view',
  VENDOR_INVOICE_CREATE: 'vendor_invoice.create',
  VENDOR_INVOICE_APPROVE: 'vendor_invoice.approve',
  PAYMENT_VIEW: 'payment.view',
  PAYMENT_RECORD: 'payment.record',
  PAYMENT_APPROVE: 'payment.approve',
  GST_REPORT_VIEW: 'gst_report.view',

  // cross-cutting
  REPORT_VIEW: 'report.view',
  REPORT_EXPORT: 'report.export',
  DASHBOARD_VIEW: 'dashboard.view',
  DOCUMENT_VIEW: 'document.view',
  DOCUMENT_UPLOAD: 'document.upload',
  DOCUMENT_DELETE: 'document.delete',
  NOTIFICATION_VIEW: 'notification.view',
  AUDIT_LOG_VIEW: 'audit_log.view',
  JOB_VIEW: 'job.view',

  // Rights with no existing domain group above.
  DEALER_VIEW: 'dealer.view',
  DEALER_MANAGE: 'dealer.manage',
  SALES_INVOICE_VIEW: 'sales_invoice.view',
  SALES_INVOICE_CREATE: 'sales_invoice.create',
  PURCHASE_RETURN_VIEW: 'purchase_return.view',
  PURCHASE_RETURN_CREATE: 'purchase_return.create',
  WORK_ORDER_VIEW: 'work_order.view',
  WORK_ORDER_MANAGE: 'work_order.manage',
  MATERIAL_RETURN_CREATE: 'material_return.create',
  ASSEMBLY_COMPLETE: 'assembly.complete',
} as const

export type PermissionKey = (typeof P)[keyof typeof P]
