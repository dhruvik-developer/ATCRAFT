/**
 * User-facing message catalogue.
 *
 * The shared vocabulary of the SPA: the error headlines, empty-state sentences,
 * confirmation prompts and the success builders for the standard verbs
 * (created, updated, approved, ...) that more than one screen composes. A
 * situation that reads the same on every screen lives here; a sentence that
 * belongs to one screen's own workflow stays with that screen. Messages that
 * the API composes (a business-rule rejection, a field error) are shown
 * verbatim - the server knows why it refused, and rewording it here would only
 * lose information.
 *
 * Success messages are builders rather than strings because they name the
 * record: `success.created('Purchase order')` reads "Purchase order created",
 * and the caller passes the document code or name as the toast description.
 */

import type { ApiError } from '@/lib/api-client'
import { REASON_MIN_LENGTH } from '@/lib/constants'

// ----------------------------------------------------------- successes -----

export const success = {
  created: (noun: string) => `${noun} created`,
  updated: (noun: string) => `${noun} updated`,
  saved: (noun: string) => `${noun} saved`,
  deleted: (noun: string) => `${noun} deleted`,
  retired: (noun: string) => `${noun} retired`,
  restored: (noun: string) => `${noun} restored`,
  submitted: (noun: string) => `${noun} submitted`,
  approved: (noun: string) => `${noun} approved`,
  rejected: (noun: string) => `${noun} rejected`,
  confirmed: (noun: string) => `${noun} confirmed`,
  selected: (noun: string) => `${noun} selected`,
  cancelled: (noun: string) => `${noun} cancelled`,
  closed: (noun: string) => `${noun} closed`,
  reopened: (noun: string) => `${noun} reopened`,
  opened: (noun: string) => `${noun} opened`,
  released: (noun: string) => `${noun} released`,
  completed: (noun: string) => `${noun} completed`,
  posted: (noun: string) => `${noun} posted`,
  uploaded: (noun: string) => `${noun} uploaded`,
  sent: (noun: string) => `${noun} sent`,
  raised: (noun: string) => `${noun} raised`,
  activated: (noun: string) => `${noun} activated`,
  deactivated: (noun: string) => `${noun} deactivated`,
  reset: (noun: string) => `${noun} reset`,
  changed: (noun: string) => `${noun} changed`,
  revoked: (noun: string) => `${noun} revoked`,
  removed: (noun: string) => `${noun} removed`,
  added: (noun: string) => `${noun} added`,
  recorded: (noun: string) => `${noun} recorded`,
  issued: (noun: string) => `${noun} issued`,
  queued: (noun: string) => `${noun} queued`,
  attached: (noun: string) => `${noun} attached`,
  enabled: (noun: string) => `${noun} enabled`,
  disabled: (noun: string) => `${noun} disabled`,
  exported: (noun: string) => `${noun} exported`,
  started: (noun: string) => `${noun} started`,
  booked: (noun: string) => `${noun} booked`,
  /** A status change that has no better verb than the target state itself. */
  statusChanged: (noun: string, status: string) => `${noun} marked ${status.toLowerCase()}`,
} as const

/**
 * The toast for one action that raised several documents at once - a
 * requisition split into one purchase order per vendor and category.
 *
 * The title counts them ("3 purchase orders raised", or "Purchase order raised"
 * for one) and the description names every code, because the buyer's next step
 * is to open each of them and a toast that only counts sends them hunting.
 */
export function raisedDocuments(
  codes: readonly string[],
  noun: { one: string; many: string },
): { title: string; description: string } {
  return {
    title: codes.length === 1 ? success.raised(noun.one) : `${codes.length} ${noun.many} raised`,
    description: codes.join(', '),
  }
}

// -------------------------------------------------------------- errors -----

export const errors = {
  /** Headline for a failed data load the user was actually waiting on. */
  loadFailed: 'Could not load data',
  /** Headline for any failed action the API rejected without a more specific code. */
  couldNotComplete: 'Could not complete',
  notPermitted: 'Not permitted',
  unexpected: 'Something went wrong',
  unexpectedDetail: 'An unexpected error occurred.',
  /** A non-JSON 5xx: nginx, a gateway timeout - nothing structured to report. */
  serviceUnavailable: 'The service is temporarily unavailable. Please try again.',
  requestFailed: 'Request failed',
  invalidResponse: 'The service returned an invalid response. Please try again.',
  connectionFailed: 'Could not connect to the service',
  networkFailed: 'Could not reach the service. Check your connection and try again.',
  sessionExpired: 'Your session has expired. Please sign in again.',
  networkOffline: 'You appear to be offline. Check the connection and try again.',
  notEnoughStock: 'Not enough stock',
  invalidStateTransition: 'Action not allowed in this state',
  duplicate: 'Duplicate detected',
  approvalRequired: 'Approval required',
  conflict: 'Changed by someone else',
  rateLimited: 'Too many requests',
  /** Shown by a form when a 422 arrived without field detail. */
  checkTheForm: 'Check the entered information',
  /** A lifecycle reason shorter than `REASON_MIN_LENGTH`, held back before the round trip. */
  reasonTooShort: 'Record at least three characters of reason',
} as const

/** The toast headline for an API error code. The API's own message is the description. */
export function errorTitle(error: Pick<ApiError, 'code'>): string {
  switch (error.code) {
    case 'INSUFFICIENT_AVAILABLE_STOCK':
    case 'INSUFFICIENT_STOCK':
    case 'NEGATIVE_STOCK_BLOCKED':
      return errors.notEnoughStock
    case 'INVALID_STATE_TRANSITION':
      return errors.invalidStateTransition
    case 'DUPLICATE_DOCUMENT':
      return errors.duplicate
    case 'APPROVAL_REQUIRED':
      return errors.approvalRequired
    case 'CONFLICT':
    case 'STOCK_LOCK_TIMEOUT':
      return errors.conflict
    case 'PERMISSION_DENIED':
    case 'FORBIDDEN':
      return errors.notPermitted
    case 'RATE_LIMITED':
      return errors.rateLimited
    case 'SERVICE_UNAVAILABLE':
    case 'INVALID_RESPONSE':
      return errors.unexpected
    case 'NETWORK_ERROR':
      return errors.connectionFailed
    case 'VALIDATION_ERROR':
      return errors.checkTheForm
    default:
      return errors.couldNotComplete
  }
}

/**
 * The toast description for an API error.
 *
 * The correlation id is what support needs to find the request in the logs, so
 * it is always shown on server-side failures.
 */
export function errorDescription(
  error: Pick<ApiError, 'status' | 'message' | 'requestId'> & Partial<Pick<ApiError, 'code'>>,
): string {
  return (error.status >= 500 || error.code === 'INVALID_RESPONSE') && error.requestId
    ? `${error.message} (reference ${error.requestId.slice(0, 8)})`
    : error.message
}

/** A useful fallback for direct request handlers as well as query/mutation errors. */
export function describeError(error: unknown, fallback: string = errors.unexpectedDetail): string {
  if (!(error instanceof Error) || !error.message.trim()) return fallback
  const status = 'status' in error && typeof error.status === 'number' ? error.status : 0
  const requestId =
    'requestId' in error && typeof error.requestId === 'string' ? error.requestId : undefined
  const code = 'code' in error && typeof error.code === 'string' ? error.code : undefined
  return errorDescription({ status, message: error.message, requestId, code })
}

/** Join server validation sentences without adding duplicate full stops. */
export function joinValidationMessages(messages: readonly string[]): string {
  return messages
    .map((message) => message.trim())
    .filter(Boolean)
    .reduce(
      (combined, message) =>
        combined ? `${combined}${/[.!?…]$/.test(combined) ? ' ' : '. '}${message}` : message,
      '',
    )
}

/** Shared form validation vocabulary; workflow-specific guidance stays with its form. */
export const validation = {
  required: (field: string) => `${field} is required.`,
  choose: (noun: string) => `Choose ${noun}.`,
  positive: (field: string) => `${field} must be greater than zero.`,
  nonnegative: (field: string) => `${field} must be zero or greater.`,
  integer: (field: string) => `${field} must be a whole number.`,
  email: 'Enter a valid email address.',
} as const

export const fileMessages = {
  queued: success.queued('File'),
  generating: 'The file is being generated in the background.',
  downloadStarted: success.started('Download'),
  downloading: 'The file is saving to your device.',
  noFileTitle: 'No file available',
  noFile: 'This job finished without producing a file.',
  generationFailed: 'The file could not be generated. Try again.',
} as const

/** Describe partial notification updates accurately, including singular counts. */
export function notificationsRead(
  marked: number,
  remaining: number,
): { title: string; description: string } {
  const markedDescription = `${marked} notification${marked === 1 ? '' : 's'} marked as read.`
  return {
    title: remaining > 0 ? success.updated('Notifications') : 'All notifications read',
    description:
      remaining > 0 ? `${markedDescription} ${remaining} still unread.` : markedDescription,
  }
}

// ------------------------------------------------------- empty states ------

export const empty = {
  title: 'Nothing here yet',
  filtered: 'No records match the current filters.',
  search: (term: string) => `No results for "${term}".`,
  loadFailedTitle: 'Could not load this list',
  loadFailedAction: 'Try again',
  /**
   * A stored reference the lookup list cannot name - a value retired since it
   * was assigned, or one this account may not read. Shown instead of the raw id.
   */
  unlistedReference: 'Not in the active list',
} as const

// ------------------------------------------------------------ prompts ------

export const prompts = {
  unsavedChanges: 'You have unsaved changes. Leave without saving?',
  confirmRetire: (noun: string) =>
    `Retire this ${noun.toLowerCase()}? It stays in history but can no longer be used on new documents.`,
  confirmDelete: (noun: string) => `Delete this ${noun.toLowerCase()}? This cannot be undone.`,
  confirmCancel: (noun: string) => `Cancel this ${noun.toLowerCase()}? This cannot be undone.`,
  /** Under the reason box of a status change, retirement or transition dialog. */
  reasonRequired: `Give a reason of at least ${REASON_MIN_LENGTH} characters - it is kept in the audit trail.`,
} as const

// --------------------------------------------------------- fulfilment ------

/**
 * Sales-order fulfilment, explained the same way on the order page and in the
 * dispatch dialog it opens. Both show what each line has reserved, what may
 * leave now and what is still short, and the two must never describe those
 * figures differently.
 */
export const fulfilment = {
  allocateAction: 'Allocate available stock',
  /** Toast after a manual top-up, by outcome. */
  allocatedAll: 'Every line is now fully reserved',
  allocatedSome: (shortLines: number) =>
    `${shortLines} line${shortLines === 1 ? '' : 's'} still short of stock`,
  reservedColumn: 'Reserved',
  dispatchableColumn: 'Dispatchable now',
  shortColumn: 'Short',
  /** Under the sales-order select of the dispatch dialog. */
  linkedDispatch:
    "The order's lines that hold a live stock reservation are loaded below, each at the most it may carry now. Lower a quantity or remove a line; anything more has to be reserved first.",
  loadingDispatchable: 'Loading the order lines that may leave...',
  nothingDispatchable:
    'Nothing on this order can leave yet: no line holds a live stock reservation. Allocate available stock to the order first.',
  quantityTooHigh: (most: string) =>
    `At most ${most} may leave now - the line's live reservation, less what other open consignments already carry.`,
  quantityInvalid: 'Enter a quantity above zero, with up to 4 decimal places.',
  /** Why the warehouse cannot be changed once an order is chosen. */
  orderWarehouse:
    "A consignment against an order leaves from the order's warehouse, where its stock is reserved.",
} as const

export const messages = {
  success,
  errors,
  empty,
  prompts,
  fulfilment,
  errorTitle,
  errorDescription,
  raisedDocuments,
  describeError,
  joinValidationMessages,
  validation,
  fileMessages,
  notificationsRead,
} as const

export const salesInvoiceMessages = {
  issued: success.issued('Sales invoice'),
  preview:
    'Review the dispatch quantities, agreed prices and tax before issuing. The number is assigned when you confirm.',
  snapshot: 'Party details, prices and tax are fixed as they were at issue.',
  cancel:
    'The receivable journal and invoiced quantities are reversed together. Paid invoices or invoices outside the cancellation window need a credit note.',
  gst: 'Output GST comes from issued sales invoice lines by invoice date. Uninvoiced dispatches are shown separately for follow-up.',
} as const
