/**
 * Wire shapes and vocabulary for job work.
 *
 * The status list is the state machine from docs/database.md section 7:
 *
 *   DRAFT -> APPROVED -> ISSUED -> PARTIALLY_RECEIVED -> RECEIVED
 *         -> QC_PENDING -> COMPLETED -> CLOSED
 *
 * The one thing worth stating loudly, because every screen in this folder turns
 * on it: material issued to a job-work vendor is still owned by the company. It
 * sits in `inventory_balances.job_work_quantity` with `stock_state = JOB_WORK`
 * (docs/database.md sections 4.2 and 4.3), it is counted in valuation, and it is
 * the company's loss if it never comes back. Ageing is therefore not decoration
 * - it is the measure of how much stock is sitting outside the factory.
 *
 * Two properties of the current contract shape the screens above it:
 *
 * 1. **An order is a header plus lines.** The list row carries the vendor, the
 *    process and the charges, but no item and no quantity - those live on the
 *    lines, which only `GET /job-work/{id}` returns.
 * 2. **Rows are not denormalised.** Vendor, item and warehouse arrive as ids
 *    with no accompanying code or name.
 */

import { useQuery } from '@tanstack/react-query'
import type { SetURLSearchParams } from 'react-router-dom'

import { api } from '@/lib/api-client'
import { daysUntil } from '@/lib/format'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no job-work branch, so the keys
 * live here in the same hierarchical shape: invalidating `jobWorkKeys.all`
 * clears every job-work list and detail at once.
 */
export const jobWorkKeys = {
  all: ['job-work'] as const,
  list: (params: unknown) => ['job-work', 'list', params] as const,
  detail: (id: string) => ['job-work', 'detail', id] as const,
  pendingAtVendor: (params: unknown) => ['job-work', 'pending-at-vendor', params] as const,
  billableLines: (orderId: string) => ['job-work', 'billable-lines', orderId] as const,
  challans: (params: unknown) => ['job-work', 'challans', params] as const,
  orderChallans: (orderId: string) => ['job-work', 'order-challans', orderId] as const,
  receipts: (orderId: string) => ['job-work', 'receipts', orderId] as const,
  reconciliation: (orderId: string) => ['job-work', 'reconciliation', orderId] as const,
  vendorLedger: (params: unknown) => ['job-work', 'vendor-ledger', params] as const,
}

// ------------------------------------------------------------ vocabulary ----

export type JobWorkStatus = SchemaOf<'JobWorkOrderStatus'>

/**
 * The same vocabulary as a list, for the screens that offer it as options.
 *
 * `satisfies` is what keeps the two honest: a member the API has dropped stops
 * compiling here instead of surviving as a filter option nothing can match.
 */
export const JOB_WORK_STATUSES = [
  'DRAFT',
  'APPROVED',
  'ISSUED',
  'PARTIALLY_RECEIVED',
  'RECEIVED',
  'QC_PENDING',
  'COMPLETED',
  'CLOSED',
  'CANCELLED',
] as const satisfies readonly JobWorkStatus[]

/** What the vendor is doing to the material (`JobWorkProcess`). */
export type JobWorkProcess = SchemaOf<'JobWorkProcess'>

export const JOB_WORK_PROCESSES = [
  'POLISHING',
  'COATING',
  'PVD',
  'LASER_MARKING',
  'MACHINING',
  'OTHER',
] as const satisfies readonly JobWorkProcess[]

/** How the vendor charges (`RateBasis`). */
export type RateBasis = SchemaOf<'RateBasis'>

export const RATE_BASES = ['PER_PIECE', 'PER_KG', 'LOT'] as const satisfies readonly RateBasis[]

/** What raised the order (`JobWorkSourceType`). */
export type JobWorkSourceType = SchemaOf<'JobWorkSourceType'>

/** What happens to output the vendor produced badly (`RejectionAction`). */
export type RejectionAction = SchemaOf<'RejectionAction'>

export const REJECTION_ACTIONS = [
  'RETURN_TO_STOCK',
  'REWORK_AT_VENDOR',
  'SCRAP_AT_VENDOR',
] as const satisfies readonly RejectionAction[]

/**
 * Over-receipt tolerance the API allows before refusing a lot.
 *
 * Mirrors `EXCESS_RECEIPT_TOLERANCE_PERCENT` in
 * `apps/api/src/modules/job_work/service.py`: a vendor may return up to this
 * much more output than the order expected. Shown so a storekeeper keying a
 * slightly generous lot knows where the refusal comes from.
 */
export const EXCESS_RECEIPT_TOLERANCE_PERCENT = '2%'

/** Statuses in which company stock is physically sitting at a vendor. */
export const AT_VENDOR_STATUSES: readonly JobWorkStatus[] = ['ISSUED', 'PARTIALLY_RECEIVED']

export const isApprovable = (status: JobWorkStatus): boolean => status === 'DRAFT'

/** A second challan against an already-issued order is normal. */
export const acceptsIssue = (status: JobWorkStatus): boolean =>
  status === 'APPROVED' || status === 'ISSUED' || status === 'PARTIALLY_RECEIVED'

export const acceptsReceipt = (status: JobWorkStatus): boolean =>
  status === 'ISSUED' || status === 'PARTIALLY_RECEIVED'

/**
 * QC_PENDING is deliberately absent.
 *
 * The state machine gives that status one outbound edge and it is not this one:
 * QC_PENDING -> COMPLETED, by recording the inspection verdict. Offering Close
 * here produced a button that always failed, and because nothing in the
 * application recorded a verdict either, the order and its held stock had no
 * way forward at all. `awaitsInspection` below is what that status offers.
 */
export const isClosable = (status: JobWorkStatus): boolean =>
  status === 'RECEIVED' || status === 'COMPLETED'

/** Output is on QC hold and the verdict is the only thing that moves it. */
export const awaitsInspection = (status: JobWorkStatus): boolean => status === 'QC_PENDING'

/**
 * Only a draft is still editable.
 *
 * `PATCH /job-work/{id}` refuses anything further along, and says why: from
 * APPROVED the order holds hard reservations that other documents have been
 * refused material because of, and from ISSUED it holds company stock on
 * somebody else's premises. Neither is a thing a line edit may quietly restate.
 */
export const isEditable = (status: JobWorkStatus): boolean => status === 'DRAFT'

/**
 * Deletion is a hard delete, and so is offered from DRAFT alone.
 *
 * Nothing references an unapproved order - no reservation, no challan, no
 * ledger row - so removing one loses no history. Anything past DRAFT is
 * cancelled instead, which keeps the trail.
 */
export const isDeletable = (status: JobWorkStatus): boolean => status === 'DRAFT'

/**
 * Cancellation is reachable only before the material has left.
 *
 * The state machine has no edge out of ISSUED except through a receipt: past
 * that point the order is the only record of where company stock went, and a
 * consignment that never comes back is written off by a final receipt so the
 * loss is stated rather than implied by a cancelled document. RECEIVED and
 * everything after it are excluded for the same reason - they are closed, not
 * cancelled.
 */
export const isCancellable = (status: JobWorkStatus): boolean =>
  status === 'DRAFT' || status === 'APPROVED'

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /job-work` (`JobWorkOrderSummary`).
 *
 * A header: the vendor, the process and the charges, but no item and no
 * quantity - those live on the lines. `return_warehouse_id` is where the
 * processed material comes back to, often not the store it left. Every money
 * field is a decimal string, so compare one with `toDecimal(...).isZero()`
 * rather than against the literal `'0'` - a NUMERIC(18,4) arrives as
 * `"0.0000"` and never equals `'0'`.
 */
export type JobWorkOrderRow = SchemaOf<'JobWorkOrderSummary'>

/**
 * One line of `GET /job-work/{id}` (`JobWorkOrderLineRead`).
 *
 * What comes back is often a different item from what went out - a plated
 * version of the input - which is what `expected_output_item_id` names. Every
 * quantity on it is a magnitude.
 */
export type JobWorkOrderLine = SchemaOf<'JobWorkOrderLineRead'>

/**
 * `GET /job-work/{id}` (`JobWorkOrderDetail`).
 *
 * `lines` is optional on the wire - omitted rather than empty - so readers go
 * through `linesOf` rather than assuming an array.
 */
export type JobWorkOrderDetail = SchemaOf<'JobWorkOrderDetail'>

/** The order's lines, which the detail endpoint omits rather than empties. */
export function linesOf(order: JobWorkOrderDetail | undefined): JobWorkOrderLine[] {
  return order?.lines ?? []
}

/**
 * `POST /job-work` (`JobWorkOrderCreate`).
 *
 * Every field is one the contract declares - the payload forbids unknown keys,
 * so a helpful extra is a 422 rather than something the API quietly ignores.
 */
export type JobWorkOrderCreate = SchemaOf<'JobWorkOrderCreate'>

/**
 * `PATCH /job-work/{id}` (`JobWorkOrderUpdate`).
 *
 * `vendor_id` is absent from it on purpose, and the omission is a rule rather
 * than an oversight: sending the same material to a different processor is a
 * different order, not an edit, and the challan would name the wrong party.
 * Supplying `lines` replaces the whole set, so a caller that edits one line
 * must send them all.
 */
export type JobWorkOrderUpdate = SchemaOf<'JobWorkOrderUpdate'>

/**
 * One line of either write payload (`JobWorkOrderLineCreate`).
 *
 * `expected_output_item_id` may be omitted when the process does not change
 * what the thing is - polishing a handle returns a handle - and
 * `expected_output_quantity` when the yield is 1:1. The API fills both in from
 * the input line, and the stored order then carries them explicitly.
 */
export type JobWorkOrderLineCreate = SchemaOf<'JobWorkOrderLineCreate'>

/**
 * One row of `GET /job-work/pending-at-vendor` (`PendingAtVendorRow`).
 *
 * This is an aggregate over open orders, grouped by vendor, item and warehouse
 * - not one row per challan. There is no row id, so the table keys on the
 * grouping instead.
 */
export type PendingAtVendorRow = SchemaOf<'PendingAtVendorRow'>

/** The grouping that identifies a pending-at-vendor row. */
export const pendingRowKey = (row: PendingAtVendorRow): string =>
  `${row.vendor_id}:${row.item_id}:${row.warehouse_id}`

/**
 * One receipt line a job-work invoice may still bill
 * (`GET /job-work/{id}/billable-lines`, `BillableLine`).
 *
 * Only lines with unbilled charges are listed. Every money field is a decimal
 * string; compare one with `toDecimal(...).isZero()` rather than against a
 * literal. `tax_rate` and `sac_code` ride along from the order because the
 * invoice dialog prefills from them.
 */
export type BillableLine = SchemaOf<'BillableLine'>

/** One row of `GET /job-work/challans` (`ChallanRead`, without its lines). */
export type ChallanRow = SchemaOf<'ChallanRead'>

/** One lot received back against an order (`GET /job-work/{id}/receipts`). */
export type ReceiptRow = SchemaOf<'ReceiptRead'>

/** `GET /job-work/{id}/reconciliation` (`ReconciliationReport`). */
export type ReconciliationReport = SchemaOf<'ReconciliationReport'>

/** One input item on a vendor's material account (`VendorLedgerItemRow`). */
export type VendorLedgerItemRow = SchemaOf<'VendorLedgerItemRow'>

/** `GET /job-work/vendor-ledger` (`VendorLedgerReport`). */
export type VendorLedgerReport = SchemaOf<'VendorLedgerReport'>

/** A queued background job (`AcceptedJobResponse` data half). */
export type QueuedJobData = {
  job_id: string
  status: string
  status_url: string
}

/** Status of a background report job (`GET /reports/jobs/{id}`). */
export type ReportJob = SchemaOf<'ReportJobRead'>

// ----------------------------------------------------------------- reads ----

/**
 * One order with its lines.
 *
 * It lives here rather than in a dialog module because three dialogs now need
 * it - issue, receive and amend - and they must share the cache entry: the
 * list row carries no item and no quantity, so every one of them starts by
 * fetching the same detail.
 */
export function useJobWorkOrder(orderId: string, enabled: boolean) {
  return useQuery({
    queryKey: jobWorkKeys.detail(orderId),
    queryFn: () => api.get<JobWorkOrderDetail>(`/job-work/${orderId}`),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(orderId),
  })
}

/**
 * The receipt lines of one order a job-work invoice may still bill.
 *
 * Read when the record-invoice dialog opens an order, and refetched after a
 * bill is saved: approval moves `invoiced_amount` in the same transaction, so
 * a cached copy would offer charges that are no longer unbilled.
 */
export function useBillableLines(orderId: string, enabled: boolean) {
  return useQuery({
    queryKey: jobWorkKeys.billableLines(orderId),
    queryFn: () => api.get<BillableLine[]>(`/job-work/${orderId}/billable-lines`),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(orderId),
  })
}

/** Every delivery challan raised for one order, with its lines. */
export function useOrderChallans(orderId: string, enabled: boolean) {
  return useQuery({
    queryKey: jobWorkKeys.orderChallans(orderId),
    queryFn: () => api.get<ChallanRow[]>(`/job-work/${orderId}/challans`),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(orderId),
  })
}

/** Every lot received back against one order, with its lines. */
export function useOrderReceipts(orderId: string, enabled: boolean) {
  return useQuery({
    queryKey: jobWorkKeys.receipts(orderId),
    queryFn: () => api.get<ReceiptRow[]>(`/job-work/${orderId}/receipts`),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(orderId),
  })
}

/** Issued vs received vs loss for one order, beside the live job-work pool. */
export function useReconciliation(orderId: string, enabled: boolean) {
  return useQuery({
    queryKey: jobWorkKeys.reconciliation(orderId),
    queryFn: () => api.get<ReconciliationReport>(`/job-work/${orderId}/reconciliation`),
    staleTime: staleTimes.transactional,
    enabled: enabled && Boolean(orderId),
  })
}

export interface VendorLedgerParams {
  vendorId: string | null
  dateFrom: string
  dateTo: string
}

/** One vendor's material account for a period. Whole, like the reconciliation. */
export function useVendorLedger({ vendorId, dateFrom, dateTo }: VendorLedgerParams) {
  return useQuery({
    queryKey: jobWorkKeys.vendorLedger({ vendorId, dateFrom, dateTo }),
    queryFn: () =>
      api.get<VendorLedgerReport>('/job-work/vendor-ledger', {
        params: { vendor_id: vendorId, date_from: dateFrom, date_to: dateTo },
      }),
    staleTime: staleTimes.transactional,
    enabled: Boolean(vendorId) && Boolean(dateFrom) && Boolean(dateTo),
  })
}

/**
 * Poll a background report job until it leaves QUEUED/RUNNING.
 *
 * A challan PDF is rendered by Celery, so the Download button queues and then
 * watches the same job row the packing list uses. Polling stops at a terminal
 * state; the caller reads `download_url` from there.
 */
export function useReportJob(jobId: string | null) {
  const job = useQuery({
    queryKey: ['reports', 'jobs', jobId] as const,
    queryFn: () => api.get<ReportJob>(`/reports/jobs/${jobId}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'QUEUED' || status === 'RUNNING' ? 1500 : false
    },
  })
  return job
}

// ------------------------------------------------------------ url filters ---

/**
 * Clear a set of filter keys in one write.
 *
 * Calling `setParam` once per key reads like several clears but is only one:
 * react-router resolves the updater passed to `setSearchParams` against the
 * search params of the render that produced the setter, so every call in one
 * handler starts from the *same* base and only the last write survives - the
 * earlier keys are silently left in the URL and keep filtering the list. The
 * whole set is therefore computed once and written once.
 */
export function clearSearchKeys(
  setSearchParams: SetURLSearchParams,
  keys: readonly string[],
): void {
  setSearchParams(
    (current) => {
      const next = new URLSearchParams(current)
      for (const key of keys) next.delete(key)
      // Page 6 of the filtered list does not exist in the unfiltered one.
      next.delete('page')
      return next
    },
    { replace: true },
  )
}

// -------------------------------------------------------------- derived -----

/** Whole days since the material left the factory; null when never issued. */
export function daysOut(issuedAt: string | null): number | null {
  const days = daysUntil(issuedAt)
  return days === null ? null : Math.max(0, -days)
}

/** Days past the promised return; null when it is not yet due or not set. */
export function daysOverdue(expectedReturnDate: string | null): number | null {
  const days = daysUntil(expectedReturnDate)
  return days === null || days >= 0 ? null : -days
}
