/**
 * One loader per lazily-loaded screen, keyed by route path.
 *
 * `React.lazy` in the route table and the sidebar's hover prefetch share these
 * functions, so a chunk that was warmed on hover is the very same import
 * `lazy` resolves when the link is clicked - the browser hands back the module
 * it already has, and the page paints without a spinner.
 *
 * Kept in its own module, without JSX, so the login page can warm the
 * dashboard chunk while the user types their password without importing the
 * route table (which imports the login page - a cycle).
 */

import type { ComponentType } from 'react'

type PageModule = { default: ComponentType }
type PageLoader = () => Promise<PageModule>

/**
 * Route paths without parameters map to the page that serves them; a detail
 * route (`/items/:itemId`) is reached from its list, whose chunk is already
 * loaded by then, so it is not warmed separately.
 */
export const routeLoaders = {
  '/': () => import('@/features/dashboard/dashboard-page'),
  '/items': () => import('@/features/items/items-page'),
  '/items/:itemId': () => import('@/features/items/item-detail-page'),
  '/warehouses': () => import('@/features/warehouses/warehouses-page'),
  '/warehouses/locations': () => import('@/features/warehouses/locations-page'),
  '/inventory': () => import('@/features/inventory/balances-page'),
  '/inventory/ledger': () => import('@/features/inventory/ledger-page'),
  '/inventory/reservations': () => import('@/features/inventory/reservations-page'),
  '/inventory/movements': () => import('@/features/inventory/movements-page'),
  '/bom': () => import('@/features/bom/bom-list-page'),
  '/bom/:bomId': () => import('@/features/bom/bom-detail-page'),
  '/routing': () => import('@/features/routing/routing-page'),
  '/sales-orders': () => import('@/features/sales/sales-orders-page'),
  '/sales-orders/:orderId': () => import('@/features/sales/sales-order-detail-page'),
  '/customers': () => import('@/features/customers/customers-page'),
  '/dealers': () => import('@/features/customers/dealers-page'),
  '/mrp': () => import('@/features/mrp/mrp-page'),
  '/mrp/runs/:runId': () => import('@/features/mrp/mrp-run-detail-page'),
  '/procurement/requisitions': () => import('@/features/procurement/requisitions-page'),
  '/procurement/purchase-orders': () => import('@/features/procurement/purchase-orders-page'),
  '/procurement/purchase-orders/:poId': () =>
    import('@/features/procurement/purchase-order-detail-page'),
  '/procurement/goods-receipts': () => import('@/features/procurement/goods-receipts-page'),
  '/procurement/rfqs': () => import('@/features/procurement/rfqs-page'),
  '/procurement/rfqs/:rfqId': () => import('@/features/procurement/rfq-detail-page'),
  '/procurement/quotations': () => import('@/features/procurement/quotations-page'),
  '/procurement/purchase-returns': () => import('@/features/procurement/purchase-returns-page'),
  '/vendors': () => import('@/features/vendors/vendors-page'),
  '/transporters': () => import('@/features/transporters/transporters-page'),
  '/production': () => import('@/features/production/production-page'),
  '/production/:orderId': () => import('@/features/production/production-detail-page'),
  '/production/shift-roster': () => import('@/features/shifts/shift-roster-page'),
  '/production/work-orders': () => import('@/features/production/work-orders-page'),
  '/assembly': () => import('@/features/assembly/assembly-page'),
  '/job-work': () => import('@/features/job-work/job-work-page'),
  '/quality': () => import('@/features/quality/quality-page'),
  '/quality/templates': () => import('@/features/quality/qc-templates-page'),
  '/packing': () => import('@/features/packing/packing-page'),
  '/dispatch': () => import('@/features/dispatch/dispatch-page'),
  '/finance': () => import('@/features/finance/finance-page'),
  '/finance/sales-invoices': () => import('@/features/finance/sales-invoices-page'),
  '/finance/vendor-invoices': () => import('@/features/finance/vendor-invoices-page'),
  '/finance/payments': () => import('@/features/finance/payments-page'),
  '/finance/accounts': () => import('@/features/finance/accounts-page'),
  '/reports': () => import('@/features/reports/reports-page'),
  '/audit': () => import('@/features/audit/audit-page'),
  '/documents': () => import('@/features/documents/documents-page'),
  '/masters': () => import('@/features/masters/masters-page'),
  '/notifications': () => import('@/features/notifications/notifications-page'),
  '/admin/users': () => import('@/features/admin/users-page'),
  '/admin/organisation': () => import('@/features/admin/organisation-page'),
  '/profile': () => import('@/features/auth/profile-page'),
  '/change-password': () => import('@/features/auth/change-password-page'),
} satisfies Record<string, PageLoader>

export type RoutePath = keyof typeof routeLoaders

const warmed = new Map<string, Promise<unknown>>()

/**
 * Start downloading a route's chunk ahead of navigation.
 *
 * Idempotent and silent: a second call returns the first promise, a path with
 * no loader (a detail route, an external link) does nothing, and a failed
 * download is swallowed - the click that follows will retry through `lazy`
 * and surface the error where the user can act on it.
 */
export function prefetchRoute(path: string): void {
  if (!Object.hasOwn(routeLoaders, path) || warmed.has(path)) return
  const load = routeLoaders[path as RoutePath]
  warmed.set(
    path,
    load().catch(() => {
      warmed.delete(path)
    }),
  )
}

/** Test seam. */
export function __resetPrefetch(): void {
  warmed.clear()
}
