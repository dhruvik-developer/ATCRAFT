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
  sessionExpired: 'Your session has expired. Please sign in again.',
  networkOffline: 'You appear to be offline. Check the connection and try again.',
  notEnoughStock: 'Not enough stock',
  invalidStateTransition: 'Action not allowed in this state',
  duplicate: 'Duplicate detected',
  approvalRequired: 'Approval required',
  conflict: 'Changed by someone else',
  rateLimited: 'Too many requests',
  /** Shown by a form when a 422 arrived without field detail. */
  checkTheForm: 'Some fields need attention.',
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
      return errors.unexpected
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
  error: Pick<ApiError, 'status' | 'message' | 'requestId'>,
): string {
  return error.status >= 500 && error.requestId
    ? `${error.message} (reference ${error.requestId.slice(0, 8)})`
    : error.message
}

// ------------------------------------------------------- empty states ------

export const empty = {
  title: 'Nothing here yet',
  filtered: 'No records match the current filters.',
  search: (term: string) => `No results for "${term}".`,
  loadFailedTitle: 'Could not load this list',
  loadFailedAction: 'Try again',
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

export const messages = {
  success,
  errors,
  empty,
  prompts,
  errorTitle,
  errorDescription,
  raisedDocuments,
} as const
