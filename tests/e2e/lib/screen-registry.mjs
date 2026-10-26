import { readFileSync } from 'node:fs'

/** Populate each detail route from its audience's paginated API collection. */
export const detailScreenSources = {
  '/items/:itemId': '/products/items',
  '/bom/:bomId': '/bom',
  '/sales-orders/:orderId': '/sales',
  '/procurement/purchase-orders/:poId': '/procurement/purchase-orders',
  '/procurement/rfqs/:rfqId': '/procurement/rfqs',
  '/production/:orderId': '/production',
  '/job-work/:orderId': '/job-work',
  '/mrp/runs/:runId': '/mrp/runs',
  '/vendor/orders/:orderId': '/vendor-portal/purchase-orders',
  '/vendor/invoices/:invoiceId': '/vendor-portal/invoices',
  '/dealer/catalog/:itemId': '/dealer-portal/catalog/items',
  '/dealer/orders/:orderId': '/dealer-portal/orders',
  '/dealer/invoices/:invoiceId': '/dealer-portal/invoices',
}

/** The application's loader catalogue is the browser checks' source of routes. */
export function parseScreenRoutes(source) {
  const routes = [...source.matchAll(/^\s*'([^']+)':\s*\(\)\s*=>/gm)].map((match) => match[1])
  if (!routes.length || new Set(routes).size !== routes.length)
    throw new Error('The application route registry is empty or contains duplicate routes.')
  return routes
}

export function staticScreenRoutes(routes) {
  // Forced password change is tested with a restricted simulated identity by
  // the readiness sweep; ordinary signed-in sessions redirect away from it.
  return routes.filter((route) => !route.includes(':') && route !== '/change-password')
}

export const registeredScreens = parseScreenRoutes(
  readFileSync(new URL('../../../apps/web/src/app/route-loaders.ts', import.meta.url), 'utf8'),
)
