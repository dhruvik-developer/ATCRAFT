/**
 * Wire shapes and vocabulary for requests for quotation.
 *
 * An RFQ is the step between a requisition and a purchase order: a set of lines
 * sent to several vendors so their prices can be compared before anything is
 * bought. Every type here is derived from the generated contract rather than
 * retyped - `apps/api/src/modules/procurement/schemas.py` is the author of these
 * shapes and a second opinion written by hand is how a screen comes to compile
 * perfectly while rendering nothing.
 *
 * Three properties of the contract shape the screens above it.
 *
 * 1. **`RfqSummary` and `RfqDetail` carry ids, not names.** The warehouse and
 *    the invited vendors arrive as ids, so both are named through the bounded
 *    reference projections their own modules publish (`/warehouses/options`,
 *    `/vendors/options`) - one cached lookup per session rather than a request
 *    per row.
 * 2. **Nothing on an RFQ counts the quotations received.** The status does not
 *    move when a vendor replies (`RfqStatus` says so in as many words), and
 *    there is no response count on either payload. A "2 of 5 replied" column
 *    would be an invention, so the screens do not show one; the comparison
 *    screen is where the replies are read.
 * 3. **Quantities are decimal strings.** `RfqLineRead.quantity` is a NUMERIC
 *    serialised as a string and is only ever computed on through decimal.js.
 */

import { daysUntil } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry has no RFQ branch, so the keys live here in the same
 * hierarchical shape - but *under* the procurement prefix on purpose.
 *
 * An RFQ is a procurement document: raising one against a requisition, and
 * closing one once a vendor is chosen, both belong to the same body of work as
 * the orders that follow. Nesting means a screen that invalidates
 * `queryKeys.procurement.all` refreshes these lists too, and invalidating
 * `rfqKeys.all` from here leaves the order book alone.
 */
export const rfqKeys = {
  all: [...queryKeys.procurement.all, 'rfqs'] as const,
  list: (params: unknown) => [...queryKeys.procurement.all, 'rfqs', 'list', params] as const,
  detail: (id: string) => [...queryKeys.procurement.all, 'rfqs', 'detail', id] as const,
}

// ------------------------------------------------------------ vocabulary ----

export type RfqStatus = SchemaOf<'RfqStatus'>

/**
 * The same vocabulary as a list, for the filter that offers it.
 *
 * `satisfies` keeps the two honest: a status the API drops stops compiling here
 * instead of surviving as an option nothing can match.
 */
export const RFQ_STATUSES = [
  'DRAFT',
  'SENT',
  'CLOSED',
  'CANCELLED',
] as const satisfies readonly RfqStatus[]

/**
 * The lifecycle as a buyer reads it, for the stepper on the detail screen.
 *
 * Three stages, because `CANCELLED` is not a stage - it is the document
 * stopping. It is reported separately rather than being given a box of its own.
 */
export const RFQ_LIFECYCLE: { label: string; statuses: readonly RfqStatus[] }[] = [
  { label: 'Draft', statuses: ['DRAFT'] },
  { label: 'Sent to vendors', statuses: ['SENT'] },
  { label: 'Closed', statuses: ['CLOSED'] },
]

/** Index into `RFQ_LIFECYCLE`, or -1 for a cancelled RFQ. */
export function rfqStageIndex(status: RfqStatus): number {
  return RFQ_LIFECYCLE.findIndex((stage) => stage.statuses.includes(status))
}

/**
 * The state machine, read off `RFQ_LIFECYCLE` in
 * `apps/api/src/modules/procurement/service_rules.py`:
 *
 *     DRAFT -> SENT      DRAFT -> CANCELLED
 *     SENT  -> CLOSED    SENT  -> CANCELLED
 *
 * with CLOSED and CANCELLED terminal. Each predicate below is one of those
 * edges, kept separate rather than folded into a single "is it open" gate,
 * because they genuinely disagree: a draft can be sent but not closed, and a
 * sent RFQ can be closed but not sent again. Offering a transition the machine
 * will refuse puts the buyer through a confirmation - and, for an edit, a whole
 * form - only to be rejected by `assert_transition` at the end of it.
 */
export const isRfqSendable = (status: RfqStatus): boolean => status === 'DRAFT'

/**
 * Closing is legal only from SENT.
 *
 * Deliberately not "anything not yet terminal": there is no DRAFT -> CLOSED
 * edge. An RFQ nobody was ever asked to quote is abandoned by cancelling it,
 * which is what that edge exists for.
 */
export const isRfqClosable = (status: RfqStatus): boolean => status === 'SENT'

export const isRfqCancellable = (status: RfqStatus): boolean =>
  status === 'DRAFT' || status === 'SENT'

/**
 * Editing stops the moment the RFQ leaves the buyer's hands.
 *
 * `update_rfq` refuses anything past DRAFT outright - "has already been sent and
 * can no longer be edited" - because the vendors are quoting against the lines
 * as they were issued, and moving them underneath a quotation would make the
 * comparison meaningless.
 */
export const isRfqEditable = (status: RfqStatus): boolean => status === 'DRAFT'

/** Statuses that end the document. Neither can be reopened. */
export const RFQ_TERMINAL_STATUSES: readonly RfqStatus[] = ['CLOSED', 'CANCELLED']

/**
 * The API caps the invitation list at 50 vendors (`RfqCreate.vendor_ids`,
 * `maxItems`). Enforced in the form so a 51st is refused where it is typed
 * rather than by a 422 after everything else has been filled in.
 */
export const MAX_RFQ_VENDORS = 50

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /procurement/rfqs` (`RfqSummary`). A header only. */
export type RfqRow = SchemaOf<'RfqSummary'>

/**
 * `GET /procurement/rfqs/{id}` (`RfqDetail`).
 *
 * `lines` is *optional* on the wire - the API declares it with a default rather
 * than as a required key - so every reader treats it as possibly absent, the
 * same way a purchase order's lines are treated.
 */
export type RfqDetail = SchemaOf<'RfqDetail'>

/** One line of `GET /procurement/rfqs/{id}` (`RfqLineRead`). */
export type RfqLine = SchemaOf<'RfqLineRead'>

/** One line of the create and update payloads (`RfqLineIn`). */
export type RfqLinePayload = SchemaOf<'RfqLineIn'>

/** Body of `POST /procurement/rfqs` (`RfqCreate`). Unknown keys are refused. */
export type RfqCreatePayload = SchemaOf<'RfqCreate'>

/** Body of `PATCH /procurement/rfqs/{id}` (`RfqUpdate`). */
export type RfqUpdatePayload = SchemaOf<'RfqUpdate'>

/**
 * The three lifecycle commands, which are also their URL segments:
 * `POST /procurement/rfqs/{id}/{command}`. All three enforce `rfq.manage`.
 */
export type RfqCommand = 'send' | 'close' | 'cancel'

// -------------------------------------------------------------- derived -----

/**
 * True when the RFQ names at least one vendor.
 *
 * `transition_rfq` refuses a send on an RFQ with an empty `vendor_ids` - "names
 * no vendors; add at least one before sending it" - so Send is not offered
 * there. The screen says what is missing instead, because the fix is an edit
 * and not a retry.
 */
export const hasInvitedVendors = (rfq: { vendor_ids: string[] }): boolean =>
  rfq.vendor_ids.length > 0

/** Both halves of what the API checks before it will send an RFQ. */
export const canSendRfq = (rfq: { status: RfqStatus; vendor_ids: string[] }): boolean =>
  isRfqSendable(rfq.status) && hasInvitedVendors(rfq)

/**
 * Days past the date vendors were asked to reply by, or null.
 *
 * SENT only, deliberately. A draft has not been asked of anybody yet, so a
 * response date that has passed on one is a planning slip and not a late
 * vendor; and a closed or cancelled RFQ is settled, so chasing it is noise.
 */
export function daysAwaitingResponse(rfq: {
  status: RfqStatus
  response_due_date: string | null
}): number | null {
  if (rfq.status !== 'SENT') return null
  const days = daysUntil(rfq.response_due_date)
  if (days === null || days >= 0) return null
  return Math.abs(days)
}
