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
  if (error instanceof ApiError) {
    if (!error.isRetryable) return false
    // A 429 is retried once, with the backoff below giving the window time to
    // roll over rather than hammering it.
    if (error.status === 429) return failureCount < 1
  }
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
      if (!(error instanceof ApiError)) return
      if (error.isAuthError) {
        clearTokens()
        return
      }
      // Background refetches fail silently; only surface an error the user is
      // actually waiting on.
      if (query.state.data !== undefined) return
      if (error.isPermissionError) {
        toast.error('Not permitted', error.message)
        return
      }
      toast.error('Could not load data', describe(error))
    },
  }),

  mutationCache: new MutationCache({
    onError: (error) => {
      if (!(error instanceof ApiError)) {
        toast.error('Something went wrong', 'An unexpected error occurred.')
        return
      }
      if (error.isAuthError) {
        clearTokens()
        return
      }
      // 422 field errors are rendered inline on the form; a toast on top would
      // be redundant noise.
      if (error.status === 422 && Object.keys(error.fieldErrors).length > 0) return
      toast.error(titleFor(error), describe(error))
    },
  }),
})

function titleFor(error: ApiError): string {
  switch (error.code) {
    case 'INSUFFICIENT_AVAILABLE_STOCK':
      return 'Not enough stock'
    case 'INVALID_STATE_TRANSITION':
      return 'Action not allowed in this state'
    case 'DUPLICATE_DOCUMENT':
      return 'Duplicate detected'
    case 'APPROVAL_REQUIRED':
      return 'Approval required'
    case 'CONFLICT':
      return 'Changed by someone else'
    case 'PERMISSION_DENIED':
    case 'FORBIDDEN':
      return 'Not permitted'
    case 'RATE_LIMITED':
      return 'Too many requests'
    default:
      return 'Could not complete'
  }
}

function describe(error: ApiError): string {
  // The correlation id is what support needs to find the request in the logs,
  // so it is always shown on server-side failures.
  return error.status >= 500 && error.requestId
    ? `${error.message} (reference ${error.requestId.slice(0, 8)})`
    : error.message
}

/**
 * Query keys.
 *
 * Hierarchical and centralised so an invalidation cannot miss a related query
 * through a typo: invalidating `queryKeys.items.all` clears every item list and
 * detail at once.
 */
export const queryKeys = {
  auth: {
    me: ['auth', 'me'] as const,
    sessions: ['auth', 'sessions'] as const,
  },
  items: {
    all: ['items'] as const,
    list: (params: unknown) => ['items', 'list', params] as const,
    detail: (id: string) => ['items', 'detail', id] as const,
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
    balances: (params: unknown) => ['inventory', 'balances', params] as const,
    ledger: (params: unknown) => ['inventory', 'ledger', params] as const,
    itemStock: (itemId: string) => ['inventory', 'item-stock', itemId] as const,
  },
  allocation: {
    all: ['allocation'] as const,
    reservations: (params: unknown) => ['allocation', 'reservations', params] as const,
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
    list: (params: unknown) => ['sales', 'list', params] as const,
    detail: (id: string) => ['sales', 'detail', id] as const,
  },
  mrp: {
    all: ['mrp'] as const,
    runs: (params: unknown) => ['mrp', 'runs', params] as const,
    run: (id: string) => ['mrp', 'run', id] as const,
    suggestions: (runId: string, params: unknown) => ['mrp', 'suggestions', runId, params] as const,
    shortageReport: (runId: string, params: unknown) =>
      ['mrp', 'shortage-report', runId, params] as const,
  },
  procurement: {
    all: ['procurement'] as const,
    purchaseOrders: (params: unknown) => ['procurement', 'po', params] as const,
    purchaseOrder: (id: string) => ['procurement', 'po', 'detail', id] as const,
    grns: (params: unknown) => ['procurement', 'grn', params] as const,
    tracking: (params: unknown) => ['procurement', 'tracking', params] as const,
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
  },
  reports: {
    dashboard: ['reports', 'dashboard'] as const,
    report: (key: string, params: unknown) => ['reports', key, params] as const,
  },
  audit: {
    logs: (params: unknown) => ['audit', 'logs', params] as const,
    entity: (type: string, id: string) => ['audit', 'entity', type, id] as const,
  },
  notifications: {
    all: ['notifications'] as const,
    unreadCount: ['notifications', 'unread-count'] as const,
  },
} as const
