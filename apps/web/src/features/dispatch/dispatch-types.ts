/**
 * Wire shapes and vocabulary for dispatch and the tracking board.
 *
 * The status list is the dispatch state machine from
 * `src/modules/dispatch/service.py`. The screens read it rather than hard-coding
 * which button appears where: an action that is illegal from the current state
 * is never offered, so nobody learns the rule by being refused.
 *
 * Two things the state machine insists on, and the screens must too:
 *
 * * `NOT_DISPATCHED -> READY_FOR_DISPATCH -> DISPATCHED`. There is no jump from
 *   not-dispatched straight to dispatched; the consignment is made ready first.
 * * Every transition takes a recorded reason. `dispatch`, `in-transit`, `ready`
 *   and `cancel` all refuse a bodyless POST.
 *
 * Weights and money are decimal strings (docs/api.md, "Numbers").
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { daysUntil } from '@/lib/format'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { DecimalString } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry has keys for the dispatch list and the tracking board but
 * not for a single dispatch. These are rooted at `dispatch` and shaped the same
 * way, so `queryKeys.dispatch.all` still sweeps them.
 */
export const dispatchKeys = {
  detail: (id: string) => ['dispatch', 'detail', id] as const,
  events: (id: string) => ['dispatch', 'events', id] as const,
}

// ------------------------------------------------------------ vocabulary ----

export const DISPATCH_STATUSES = [
  'NOT_DISPATCHED',
  'READY_FOR_DISPATCH',
  'DISPATCHED',
  'IN_TRANSIT',
  'DELIVERED',
  'QC_PENDING',
  'RECEIVED',
  'QC_COMPLETED',
  'GRN_COMPLETED',
  'CANCELLED',
] as const
export type DispatchStatus = (typeof DISPATCH_STATUSES)[number]

/** `DispatchType`. Four, not six - there is no SAMPLE or SPARE consignment. */
export const DISPATCH_TYPES = ['SALES', 'PURCHASE_RETURN', 'JOB_WORK', 'TRANSFER'] as const
export type DispatchType = (typeof DISPATCH_TYPES)[number]

/**
 * The one type a consignment is raised as. Releasing posts `DISPATCH` - goods that
 * stopped being ours - so the API refuses the other three and names the module that
 * books each movement: a stock transfer in Inventory, a job-work issue in Job Work,
 * a purchase return in Procurement. `DISPATCH_TYPES` keeps all four so historical
 * rows and the list filters still read.
 */
export const RAISED_DISPATCH_TYPE = 'SALES' satisfies DispatchType

/** A non-sales consignment raised before that rule can only be cancelled here. */
export const isSalesConsignment = (type: string): boolean => type === RAISED_DISPATCH_TYPE

export const FREIGHT_PAID_BY = ['CONSIGNOR', 'CONSIGNEE'] as const
export type FreightPaidBy = (typeof FREIGHT_PAID_BY)[number]

export const TRACKING_EVENT_SOURCES = ['MANUAL', 'COURIER_API', 'VENDOR_PORTAL'] as const
export type TrackingEventSource = (typeof TRACKING_EVENT_SOURCES)[number]

/**
 * A tracking event reports a dispatch status, not a vocabulary of its own.
 *
 * Only the ones a human would record by hand are offered; the rest of the
 * lifecycle is reached through its own endpoint, which posts the event itself.
 */
export const MANUAL_EVENT_STATUSES: readonly DispatchStatus[] = [
  'IN_TRANSIT',
  'DELIVERED',
  'QC_PENDING',
  'RECEIVED',
]

/** Anything past delivery is settled and no longer moves on this screen. */
const SETTLED = new Set<string>([
  'DELIVERED',
  'QC_PENDING',
  'RECEIVED',
  'QC_COMPLETED',
  'GRN_COMPLETED',
  'CANCELLED',
])

/** `NOT_DISPATCHED -> READY_FOR_DISPATCH`. */
export const isReadyable = (status: string): boolean => status === 'NOT_DISPATCHED'

/** `READY_FOR_DISPATCH -> DISPATCHED`. Only from ready - never from scratch. */
export const isDispatchable = (status: string): boolean => status === 'READY_FOR_DISPATCH'

export const isMarkableInTransit = (status: string): boolean => status === 'DISPATCHED'

export const isDeliverable = (status: string): boolean =>
  status === 'DISPATCHED' || status === 'IN_TRANSIT'

export const isDispatchCancellable = (status: string): boolean =>
  status === 'NOT_DISPATCHED' || status === 'READY_FOR_DISPATCH'

export const isSettled = (status: string): boolean => SETTLED.has(status)

// -------------------------------------------------------------- payloads ----

/** One row of `GET /dispatch` (`DispatchSummary`). */
export interface DispatchRow {
  sales_invoice_id: string | null
  id: string
  code: string
  dispatch_type: DispatchType
  status: DispatchStatus

  customer_id: string | null
  vendor_id: string | null
  warehouse_id: string
  sales_order_id: string | null

  dispatch_date: string
  expected_delivery_date: string | null
  actual_delivery_date: string | null

  transporter_id: string | null
  courier_name: string | null
  tracking_id: string | null

  total_boxes: number
  total_weight_kg: DecimalString
  created_at: string
}

/** One line of `GET /dispatch/{id}` (`DispatchLineRead`). */
export interface DispatchLine {
  id: string
  line_number: number
  sales_order_line_id: string | null
  item_id: string
  quantity: DecimalString
  batch_id: string | null
  /** One per unit for a serial-tracked item; empty for everything else. */
  serial_ids: string[]
  carton_ids: string[]
  reservation_id: string | null
  inventory_transaction_id: string | null
  notes: string | null
}

/** `GET /dispatch/{id}` (`DispatchDetail`). */
export interface DispatchDetail extends DispatchRow {
  packing_order_id: string | null
  dispatched_at: string | null
  transport_name: string | null
  lr_number: string | null
  vehicle_number: string | null
  driver_name: string | null
  driver_mobile: string | null
  freight_amount: DecimalString
  freight_paid_by: FreightPaidBy | null
  eway_bill_number: string | null
  invoice_document_id: string | null
  delivery_confirmed_by: string | null
  delivery_note: string | null
  delay_reason: string | null
  delay_notified_at: string | null
  notes: string | null
  version: number
  lines: DispatchLine[]
}

/** One line of `DispatchCreate` / `DispatchUpdate` (`DispatchLineIn`). */
export interface DispatchLineIn {
  item_id: string
  quantity: DecimalString
  sales_order_line_id?: string | null
  batch_id?: string | null
  serial_ids: string[]
  carton_ids: string[]
  notes?: string | null
}

/** `POST /dispatch/{id}/dispatch` (`DispatchResult`). */
export interface DispatchResult {
  dispatch: DispatchDetail
  consumed_reservation_ids: string[]
  inventory_transaction_ids: string[]
  sales_order_id: string | null
  sales_order_status: string | null
  open_sales_order_lines: number
}

/**
 * One row of `GET /dispatch/tracking` (`TrackingBoardRow`).
 *
 * It joins both directions of movement: material going out to a customer and
 * material coming in from a vendor against a purchase order. That is why the
 * party is typed rather than named `customer`.
 */
export interface DispatchTrackingRow {
  dispatch_id: string
  code: string
  dispatch_type: DispatchType
  status: DispatchStatus

  party_type: string
  party_name: string | null
  reference_code: string | null

  courier_name: string | null
  transporter_name: string | null
  tracking_id: string | null

  dispatch_date: string
  expected_delivery_date: string | null
  actual_delivery_date: string | null

  is_delayed: boolean
  delay_days: number
  delay_reason: string | null

  qc_pending: boolean
  grn_pending: boolean
}

/** One row of `GET /dispatch/delayed` (`DelayedDispatchRow`). */
export interface DelayedDispatchRow {
  dispatch_id: string
  company_id: string
  code: string
  status: DispatchStatus
  customer_id: string | null
  vendor_id: string | null
  party_name: string | null
  tracking_id: string | null
  dispatch_date: string
  expected_delivery_date: string
  delay_days: number
  delay_reason: string | null
  already_notified: boolean
}

/** One event of `GET /dispatch/{id}/tracking-events` (`TrackingEventRead`). */
export interface DispatchTrackingEvent {
  id: string
  dispatch_id: string
  event_status: DispatchStatus
  event_time: string
  location: string | null
  remark: string | null
  recorded_by_id: string | null
  source: TrackingEventSource
  created_at: string
}

// --------------------------------------------------------------- helpers ----

/**
 * Whole days late, or null when it is not late.
 *
 * The API's `delay_days` is preferred where it is given; this is the fallback so
 * a dispatch row - which carries no delay figure at all - still shows the truth
 * rather than nothing.
 */
export function delayDays(
  expectedDeliveryDate: string | null,
  settled: boolean,
  reported: number | null = null,
): number | null {
  if (reported !== null && reported > 0) return reported
  if (settled) return null
  const remaining = daysUntil(expectedDeliveryDate)
  if (remaining === null || remaining >= 0) return null
  return Math.abs(remaining)
}

/** Money and weights on the create form, to two and three places. */
export const MONEY_INPUT = /^$|^\d{1,14}(\.\d{1,2})?$/
export const WEIGHT_INPUT = /^$|^\d{1,10}(\.\d{1,3})?$/
export const QUANTITY_INPUT = /^\d{1,14}(\.\d{1,4})?$/

/** `delivery_note` and `delay_reason` are `Notes`: free text up to 2000 characters. */
export const NOTE_MAX_LENGTH = 2000

// -------------------------------------------------- identified-stock reads ---

/**
 * The tracking flags for one chosen item.
 *
 * Read by id and cached under the same key the item picker uses, so a screen
 * that has already shown the item does not read it again. This is a lookup for
 * the *one* item being edited - never one per row of a table.
 */
export interface ItemFacts {
  id: string
  code: string
  name: string
  is_batch_tracked: boolean
  is_serial_tracked: boolean
}

/**
 * The query for one item's tracking flags, as options - shared by
 * {@link useItemFacts} and by the dispatch dialog's per-order-line reads, so
 * both hit one cache entry per item.
 */
export const itemFactsQuery = (itemId: string, enabled: boolean) => ({
  queryKey: queryKeys.items.detail(itemId),
  queryFn: () => api.get<ItemFacts>(`/products/items/${itemId}`),
  enabled: Boolean(itemId) && enabled,
  staleTime: staleTimes.master,
})

export function useItemFacts(itemId: string, enabled: boolean) {
  return useQuery(itemFactsQuery(itemId, enabled))
}

/** One row of `GET /inventory/batches` (`StockBatchRead`), as the pickers read it. */
export interface StockBatch {
  id: string
  item_id: string
  batch_number: string
  status: string
  expires_on: string | null
}

/** One row of `GET /inventory/serials` (`StockSerialRead`). */
export interface StockSerial {
  id: string
  serial_number: string
  status: string
  warehouse_id: string | null
}
