/**
 * TanStack Query configuration.
 *
 * Master rules 37: server state lives here, not duplicated into a global store.
 * The defaults below encode the two things that matter for an ERP:
 *
 * 1. **Never retry a business rejection.** A 422 "insufficient stock" or a 409
 *    "invalid state transition" is a correct answer, not a transient failure.
 *    Retrying it three times just delays the message the user needs to see.
 * 2. **Staleness is per-domain, not global.** An item master can be minutes
 *    stale with no consequence. A stock balance cannot be stale at all.
 */

import { QueryCache, QueryClient, MutationCache } from '@tanstack/react-query'

import { ApiError, clearTokens } from './api-client'
import { reportError } from './error-reporting'
import { partitionFieldErrors } from './form-errors'
import { errorDescription, errorTitle, errors, joinValidationMessages } from './messages'
import { toast } from './toast'

/** How long a domain's data may be served from cache before it is refetched. */
export const staleTimes = {
  /** UoM, categories, warehouse types - effectively static during a session. */
  reference: 30 * 60 * 1000,
  /** Items, partners, warehouses. */
  master: 5 * 60 * 1000,
  /** Documents in flight: orders, POs, production orders. */
  transactional: 30 * 1000,
  /** Stock balances, reservations, availability. Always refetched.
   *  Master rules 34: correctness beats speed for mutable inventory state. */
  live: 0,
  /** Dashboard tiles read from summary tables refreshed every 15 minutes. */
  dashboard: 60 * 1000,
} as const

function shouldRetry(failureCount: number, error: unknown): boolean {
  // Transport errors are shaped by api-client. Re-running a transformation or
  // rendering bug cannot recover and only repeats the same failed processing.
  if (!(error instanceof ApiError) || !error.isRetryable) return false
  // A 429 is retried once, with the backoff below giving the window time to
  // roll over rather than hammering it.
  if (error.status === 429) return failureCount < 1
  return failureCount < 2
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: staleTimes.master,
      gcTime: 10 * 60 * 1000,
      retry: shouldRetry,
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
      // Refetching every list on tab focus is wasteful in an app people leave
      // open all day; individual live queries opt back in.
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
    },
    mutations: {
      // A mutation that failed may have partially applied. Retrying it
      // automatically risks double-posting stock or duplicating a document.
      retry: false,
    },
  },

  queryCache: new QueryCache({
    onError: (error, query) => {
      if (!(error instanceof ApiError)) {
        reportError(error, { source: 'query' })
        if (query.state.data === undefined && !query.meta?.suppressErrorToast) {
          toast.error(errors.loadFailed, errors.unexpectedDetail)
        }
        return
      }
      if (error.isAuthError) {
        clearTokens()
        return
      }
      // Background refetches fail silently; only surface an error the user is
      // actually waiting on.
      if (query.state.data !== undefined) return
      if (error.isPermissionError) {
        toast.error(errors.notPermitted, error.message)
        return
      }
      // A query that renders its own expected rejection inline (the UoM
      // conversion check, where a 422 is the business answer) opts out here.
      if (query.meta?.suppressErrorToast) return
      toast.error(errors.loadFailed, errorDescription(error))
    },
  }),

  mutationCache: new MutationCache({
    onError: (error) => {
      if (!(error instanceof ApiError)) {
        reportError(error, { source: 'mutation' })
        toast.error(errors.unexpected, errors.unexpectedDetail)
        return
      }
      if (error.isAuthError) {
        clearTokens()
        return
      }
      if (error.status === 422) {
        const { fields, orphans } = partitionFieldErrors(error)
        // A model-level refusal (`_root`) or a query-parameter error has no
        // control behind it, and not every form renders a root slot, so it is
        // said here. `applyFieldErrors` puts the same text on `root` for the
        // forms that do.
        if (orphans.length > 0) {
          toast.error(errors.checkTheForm, joinValidationMessages(orphans))
          return
        }
        // Field errors are rendered inline on the form; a toast on top would
        // be redundant noise.
        if (Object.keys(fields).length > 0) return
      }
      toast.error(errorTitle(error), errorDescription(error))
    },
  }),
})

/**
 * Query keys.
 *
 * Hierarchical and centralised so an invalidation cannot miss a related query
 * through a typo: invalidating `queryKeys.items.all` clears every item list and
 * detail at once.
 */
export const queryKeys = {
  vendorPortal: {
    all: ['vendor-portal'] as const,
    me: ['vendor-portal', 'me'] as const,
    dashboard: ['vendor-portal', 'dashboard'] as const,
    orders: (params: unknown) => ['vendor-portal', 'orders', params] as const,
    order: (id: string) => ['vendor-portal', 'orders', id] as const,
    shipments: (params: unknown) => ['vendor-portal', 'shipments', params] as const,
    receipts: (params: unknown) => ['vendor-portal', 'receipts', params] as const,
    invoices: (params: unknown) => ['vendor-portal', 'invoices', params] as const,
    invoice: (id: string) => ['vendor-portal', 'invoices', id] as const,
    payments: (params: unknown) => ['vendor-portal', 'payments', params] as const,
    paymentSummary: ['vendor-portal', 'payment-summary'] as const,
    rating: ['vendor-portal', 'rating'] as const,
    transporters: (params: unknown) => ['vendor-portal', 'transporters', params] as const,
  },
  dealerPortal: {
    all: ['dealer-portal'] as const,
    me: ['dealer-portal', 'me'] as const,
    outstanding: ['dealer-portal', 'outstanding'] as const,
    categories: ['dealer-portal', 'categories'] as const,
    brands: ['dealer-portal', 'brands'] as const,
    cart: ['dealer-portal', 'cart'] as const,
    wishlist: ['dealer-portal', 'wishlist'] as const,
    list: (resource: string, params: unknown) => ['dealer-portal', resource, params] as const,
    detail: (resource: string, id: string) => ['dealer-portal', resource, id] as const,
    price: (id: string, quantity: string, variantId?: string) =>
      ['dealer-portal', 'price', id, quantity, variantId] as const,
  },
  auth: {
    me: ['auth', 'me'] as const,
    sessions: ['auth', 'sessions'] as const,
  },
  items: {
    all: ['items'] as const,
    list: (params: unknown) => ['items', 'list', params] as const,
    detail: (id: string) => ['items', 'detail', id] as const,
    variants: (id: string, params: unknown) => ['items', 'detail', id, 'variants', params] as const,
  },
  categories: { all: ['categories'] as const },
  uoms: { all: ['uoms'] as const },
  warehouses: {
    all: ['warehouses'] as const,
    list: (params: unknown) => ['warehouses', 'list', params] as const,
    detail: (id: string) => ['warehouses', 'detail', id] as const,
  },
  inventory: {
    all: ['inventory'] as const,
    batches: (params: unknown) => ['inventory', 'batches', params] as const,
    batch: (itemId: string, batchId: string) =>
      ['inventory', 'batches', 'detail', itemId, batchId] as const,
    serials: (params: unknown) => ['inventory', 'serials', params] as const,
    serial: (serialId: string) => ['inventory', 'serials', 'detail', serialId] as const,
    balances: (params: unknown) => ['inventory', 'balances', params] as const,
    ledger: (params: unknown) => ['inventory', 'ledger', params] as const,
    itemStock: (itemId: string) => ['inventory', 'item-stock', itemId] as const,
  },
  allocation: {
    all: ['allocation'] as const,
    reservations: (params: unknown) => ['allocation', 'reservations', params] as const,
    reallocationRequests: (params: unknown) =>
      ['allocation', 'reallocation-requests', params] as const,
    atp: (itemId: string, warehouseId?: string) =>
      ['allocation', 'atp', itemId, warehouseId ?? 'all'] as const,
  },
  bom: {
    all: ['bom'] as const,
    list: (params: unknown) => ['bom', 'list', params] as const,
    detail: (id: string) => ['bom', 'detail', id] as const,
    explosion: (id: string, qty: string) => ['bom', 'explosion', id, qty] as const,
    whereUsed: (itemId: string) => ['bom', 'where-used', itemId] as const,
  },
  routing: {
    all: ['routing'] as const,
    routings: (params: unknown) => ['routing', 'routings', params] as const,
    routing: (id: string) => ['routing', 'routings', 'detail', id] as const,
    workCenters: (params: unknown) => ['routing', 'work-centers', params] as const,
    machines: (params: unknown) => ['routing', 'machines', params] as const,
  },
  sales: {
    all: ['sales'] as const,
    settings: ['sales', 'settings'] as const,
    list: (params: unknown) => ['sales', 'list', params] as const,
    detail: (id: string) => ['sales', 'detail', id] as const,
  },
  salesReturns: {
    all: ['sales-returns'] as const,
    list: (params: unknown) => ['sales-returns', 'list', params] as const,
    detail: (id: string) => ['sales-returns', 'detail', id] as const,
  },
  mrp: {
    all: ['mrp'] as const,
    runs: (params: unknown) => ['mrp', 'runs', params] as const,
    run: (id: string) => ['mrp', 'run', id] as const,
    suggestions: (runId: string, params: unknown) => ['mrp', 'suggestions', runId, params] as const,
    shortageReport: (runId: string, params: unknown) =>
      ['mrp', 'shortage-report', runId, params] as const,
    salesOrderAvailability: (orderId: string) =>
      ['mrp', 'sales-order-availability', orderId] as const,
  },
  procurement: {
    all: ['procurement'] as const,
    purchaseOrders: (params: unknown) => ['procurement', 'po', params] as const,
    purchaseOrder: (id: string) => ['procurement', 'po', 'detail', id] as const,
    grns: (params: unknown) => ['procurement', 'grn', params] as const,
    tracking: (params: unknown) => ['procurement', 'tracking', params] as const,
    inboundTracking: (params: unknown) => ['procurement', 'inbound', params] as const,
    shipments: (id: string, params: unknown) =>
      ['procurement', 'po', id, 'shipments', params] as const,
    documents: (id: string, params: unknown) =>
      ['procurement', 'po', id, 'documents', params] as const,
    inboundTransporters: (params: unknown) => ['procurement', 'transporters', params] as const,
  },
  production: {
    all: ['production'] as const,
    list: (params: unknown) => ['production', 'list', params] as const,
    detail: (id: string) => ['production', 'detail', id] as const,
  },
  quality: {
    all: ['quality'] as const,
    inspections: (params: unknown) => ['quality', 'inspections', params] as const,
    detail: (id: string) => ['quality', 'detail', id] as const,
  },
  dispatch: {
    all: ['dispatch'] as const,
    list: (params: unknown) => ['dispatch', 'list', params] as const,
    tracking: (params: unknown) => ['dispatch', 'tracking', params] as const,
  },
  vendors: {
    all: ['vendors'] as const,
    list: (params: unknown) => ['vendors', 'list', params] as const,
    detail: (id: string) => ['vendors', 'detail', id] as const,
  },
  customers: {
    all: ['customers'] as const,
    list: (params: unknown) => ['customers', 'list', params] as const,
    detail: (id: string) => ['customers', 'detail', id] as const,
  },
  users: {
    all: ['users'] as const,
    list: (params: unknown) => ['users', 'list', params] as const,
    detail: (id: string) => ['users', 'detail', id] as const,
    roles: ['users', 'roles'] as const,
    permissionCatalogue: ['users', 'permission-catalogue'] as const,
    /**
     * The staff directory as a picker list. `activeOnly` is part of the key
     * because the audit actor filter legitimately needs deactivated people -
     * a historical actor is still an actor - while every other picker offers
     * only staff who can be assigned something today. Both sit under `users`,
     * so the `users.all` invalidation after a create or a deactivation reaches
     * them.
     */
    options: (activeOnly: boolean) => ['users', 'options', activeOnly] as const,
  },
  reports: {
    dashboard: ['reports', 'dashboard'] as const,
    catalogue: ['reports', 'catalogue'] as const,
    report: (key: string, params: unknown) => ['reports', key, params] as const,
  },
  audit: {
    logsAll: ['audit', 'logs'] as const,
    logs: (params: unknown) => ['audit', 'logs', params] as const,
    entity: (type: string, id: string) => ['audit', 'entity', type, id] as const,
    /** The approval queue. Under `audit` so an `['audit']` invalidation sweeps it too. */
    approvals: (params: unknown) => ['audit', 'approvals', params] as const,
  },
  notifications: {
    all: ['notifications'] as const,
    unreadCount: ['notifications', 'unread-count'] as const,
    pushConfig: ['notifications', 'push-config'] as const,
    pushState: ['notifications', 'push-state'] as const,
  },
} as const
