/**
 * Sidebar navigation.
 *
 * Each entry declares the permission that makes it meaningful. The sidebar hides
 * what a user cannot use, which keeps a store keeper from staring at a Finance
 * section that will only ever return 403. This is presentation, not security.
 *
 * The order follows the physical flow of the business - demand, planning,
 * buying, making, checking, shipping, accounting - because that is how the
 * people using it think about their day.
 */

import type { LucideIcon } from 'lucide-react'
import {
  Banknote,
  BookOpen,
  Boxes,
  Building2,
  ClipboardCheck,
  ClipboardList,
  Cog,
  FileBarChart,
  FileStack,
  FileSearch,
  Factory,
  GitBranch,
  Hammer,
  LayoutDashboard,
  Package,
  PackageCheck,
  Receipt,
  Ruler,
  ScrollText,
  Send,
  ShieldCheck,
  ShoppingCart,
  Truck,
  Undo2,
  Users,
  Warehouse,
  Wrench,
} from 'lucide-react'

import { P } from '@/lib/permissions'

export interface NavItem {
  label: string
  to: string
  icon: LucideIcon
  /** Any one of these permissions reveals the item. */
  permissions?: string[]
  /** Key into the badge-count map supplied by the layout. */
  badgeKey?: string
}

export interface NavSection {
  label: string
  items: NavItem[]
}

export const navigation: NavSection[] = [
  {
    label: 'Overview',
    items: [
      { label: 'Dashboard', to: '/', icon: LayoutDashboard, permissions: [P.DASHBOARD_VIEW] },
    ],
  },
  {
    label: 'Sales',
    items: [
      {
        label: 'Sales Orders',
        to: '/sales-orders',
        icon: ClipboardList,
        permissions: [P.SALES_ORDER_VIEW],
        badgeKey: 'openSalesOrders',
      },
      { label: 'Customers', to: '/customers', icon: Users, permissions: [P.CUSTOMER_VIEW] },
      { label: 'Dealers', to: '/dealers', icon: Users, permissions: [P.DEALER_VIEW] },
    ],
  },
  {
    label: 'Inventory',
    items: [
      {
        label: 'Stock Balances',
        to: '/inventory',
        icon: Boxes,
        permissions: [P.INVENTORY_VIEW],
      },
      {
        label: 'Stock Ledger',
        to: '/inventory/ledger',
        icon: ScrollText,
        permissions: [P.INVENTORY_LEDGER_VIEW],
      },
      {
        label: 'Reservations',
        to: '/inventory/reservations',
        icon: PackageCheck,
        permissions: [P.RESERVATION_VIEW],
      },
      {
        label: 'Transfers & Adjustments',
        to: '/inventory/movements',
        icon: Truck,
        permissions: [P.INVENTORY_VIEW],
      },
      { label: 'Warehouses', to: '/warehouses', icon: Warehouse, permissions: [P.WAREHOUSE_VIEW] },
      {
        label: 'Storage Locations',
        to: '/warehouses/locations',
        icon: Boxes,
        permissions: [P.LOCATION_VIEW],
      },
    ],
  },
  {
    label: 'Engineering',
    items: [
      { label: 'Items', to: '/items', icon: Package, permissions: [P.ITEM_VIEW] },
      { label: 'Bills of Material', to: '/bom', icon: GitBranch, permissions: [P.BOM_VIEW] },
      { label: 'Routings', to: '/routing', icon: Wrench, permissions: [P.ROUTING_VIEW] },
      {
        label: 'Item Masters',
        to: '/masters',
        icon: Ruler,
        // The same rights the route admits - one per tab.
        permissions: [
          P.CATEGORY_VIEW,
          P.UOM_VIEW,
          P.PRICE_VIEW,
          P.ITEM_ATTRIBUTE_VIEW,
          P.BRAND_VIEW,
          P.MANUFACTURER_VIEW,
        ],
      },
    ],
  },
  {
    label: 'Planning',
    items: [
      {
        label: 'MRP',
        to: '/mrp',
        icon: Cog,
        permissions: [P.MRP_VIEW],
        badgeKey: 'openMrpSuggestions',
      },
    ],
  },
  {
    label: 'Procurement',
    items: [
      {
        label: 'Requisitions',
        to: '/procurement/requisitions',
        icon: ClipboardList,
        permissions: [P.PURCHASE_REQUISITION_VIEW],
        badgeKey: 'pendingRequisitions',
      },
      {
        label: 'Purchase Orders',
        to: '/procurement/purchase-orders',
        icon: ShoppingCart,
        permissions: [P.PURCHASE_ORDER_VIEW],
        badgeKey: 'pendingPoApprovals',
      },
      {
        label: 'Goods Receipts',
        to: '/procurement/goods-receipts',
        icon: PackageCheck,
        permissions: [P.GRN_VIEW],
      },
      {
        label: 'RFQs',
        to: '/procurement/rfqs',
        icon: FileSearch,
        permissions: [P.RFQ_VIEW],
      },
      {
        label: 'Quotations',
        to: '/procurement/quotations',
        icon: ScrollText,
        permissions: [P.QUOTATION_VIEW],
      },
      {
        label: 'Purchase Returns',
        to: '/procurement/purchase-returns',
        icon: Undo2,
        permissions: [P.PURCHASE_RETURN_VIEW],
      },
      { label: 'Vendors', to: '/vendors', icon: Building2, permissions: [P.VENDOR_VIEW] },
      {
        label: 'Transporters',
        to: '/transporters',
        icon: Truck,
        permissions: [P.TRANSPORTER_VIEW],
      },
    ],
  },
  {
    label: 'Manufacturing',
    items: [
      {
        label: 'Production Orders',
        to: '/production',
        icon: Factory,
        permissions: [P.PRODUCTION_ORDER_VIEW],
        badgeKey: 'productionInProgress',
      },
      {
        label: 'Assembly',
        to: '/assembly',
        icon: Hammer,
        permissions: [P.ASSEMBLY_ORDER_VIEW],
      },
      {
        label: 'Work Orders',
        to: '/production/work-orders',
        icon: Cog,
        permissions: [P.WORK_ORDER_VIEW],
      },
      {
        label: 'Job Work',
        to: '/job-work',
        icon: Send,
        permissions: [P.JOB_WORK_VIEW],
        badgeKey: 'jobWorkAtVendor',
      },
    ],
  },
  {
    label: 'Quality & Dispatch',
    items: [
      {
        label: 'Quality Control',
        to: '/quality',
        icon: ClipboardCheck,
        permissions: [P.QC_VIEW],
        badgeKey: 'qcPending',
      },
      {
        label: 'QC Templates',
        to: '/quality/templates',
        icon: ClipboardCheck,
        permissions: [P.QC_VIEW],
      },
      { label: 'Packing', to: '/packing', icon: Package, permissions: [P.PACKING_VIEW] },
      {
        label: 'Dispatch',
        to: '/dispatch',
        icon: Truck,
        permissions: [P.DISPATCH_VIEW],
        badgeKey: 'dispatchesDelayed',
      },
    ],
  },
  {
    label: 'Finance',
    items: [
      {
        label: 'Ledgers & Outstanding',
        to: '/finance',
        icon: Receipt,
        permissions: [P.FINANCE_VIEW],
      },
      {
        label: 'Sales invoices',
        to: '/finance/sales-invoices',
        icon: Receipt,
        permissions: [P.SALES_INVOICE_VIEW],
      },
      {
        label: 'Vendor Invoices',
        to: '/finance/vendor-invoices',
        icon: Receipt,
        permissions: [P.VENDOR_INVOICE_VIEW],
      },
      {
        label: 'Payments',
        to: '/finance/payments',
        icon: Banknote,
        permissions: [P.PAYMENT_VIEW],
      },
      {
        label: 'Chart of Accounts',
        to: '/finance/accounts',
        icon: BookOpen,
        permissions: [P.LEDGER_VIEW],
      },
    ],
  },
  {
    label: 'Insights',
    items: [
      { label: 'Reports', to: '/reports', icon: FileBarChart, permissions: [P.REPORT_VIEW] },
      { label: 'Audit Trail', to: '/audit', icon: ShieldCheck, permissions: [P.AUDIT_LOG_VIEW] },
      {
        label: 'Documents',
        to: '/documents',
        icon: FileStack,
        permissions: [P.DOCUMENT_VIEW],
      },
    ],
  },
  {
    label: 'Administration',
    items: [
      {
        label: 'Users & Roles',
        to: '/admin/users',
        icon: Users,
        permissions: [P.USER_VIEW, P.ROLE_VIEW],
      },
      {
        label: 'Organisation',
        to: '/admin/organisation',
        icon: Building2,
        permissions: [P.COMPANY_VIEW, P.COST_CENTRE_VIEW],
      },
    ],
  },
]
