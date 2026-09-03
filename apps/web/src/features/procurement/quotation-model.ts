/**
 * Wire shapes and vocabulary for vendor quotations and the RFQ comparison.
 *
 * A quotation is one vendor's answer to an RFQ. The screens above this file do
 * two jobs that are easy to confuse: recording what a vendor said, and deciding
 * which vendor to buy from. The second is the expensive one - it commits money -
 * so everything here that supports it is arithmetic-safe and states where its
 * numbers came from.
 *
 * Every type is derived from the generated contract rather than retyped;
 * `apps/api/src/modules/procurement/schemas.py` is the author of these shapes,
 * and a hand-written second opinion is how a screen comes to compile perfectly
 * while rendering nothing.
 *
 * Four properties of the contract shape the screens above it.
 *
 * 1. **`QuotationSummary` carries ids, not names.** There is no vendor code, no
 *    vendor name and no RFQ code on a list row. The vendor is named through the
 *    bounded `/vendors/options` projection every other procurement grid uses;
 *    the RFQ is not named at all on the list, because no equivalent projection
 *    exists and one lookup per row is the N+1 the rules forbid. The comparison
 *    payload *does* carry `rfq_code`, `vendor_code`, `vendor_name`, `item_code`
 *    and `item_name`, so the comparison panel needs no lookups whatsoever.
 * 2. **Money and quantities are decimal strings.** Totals, unit prices and
 *    scores are NUMERIC serialised as strings and are only ever computed on
 *    through `toDecimal`. The price difference a buyer reads before committing
 *    is the one number on these screens that must not be a float.
 * 3. **Expiry is the server's judgement, not the browser's.** The comparison
 *    carries `is_expired` per column and per offer, computed against the
 *    server's date, and `decide_quotation` refuses to select a lapsed offer
 *    outright. `hasLapsed` below exists only to warn *before* the round trip.
 * 4. **A mixed-currency board is deliberately unscored.** The API names no
 *    recommendation when vendors quoted in different currencies and says so in
 *    `recommendation_reason`. Nothing here may quietly compare the magnitudes
 *    anyway - 100 EUR against 9,000 INR would recommend the wrong vendor with
 *    total confidence.
 */

import Decimal from 'decimal.js'

import { daysUntil, toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry has no quotation branch, so the keys live here in the
 * same hierarchical shape - and *under* the procurement prefix, for the reason
 * `rfqKeys` gives: a quotation is a procurement document, so a screen that
 * invalidates `queryKeys.procurement.all` refreshes these too, while
 * invalidating `quotationKeys.all` from here leaves the order book alone.
 *
 * `comparison` hangs off the RFQ rather than off a quotation: the board is
 * rebuilt whenever *any* quotation on that RFQ changes, so a decision on one
 * vendor has to invalidate the whole board and not just its own row.
 */
export const quotationKeys = {
  all: [...queryKeys.procurement.all, 'quotations'] as const,
  list: (params: unknown) => [...queryKeys.procurement.all, 'quotations', 'list', params] as const,
  detail: (id: string) => [...queryKeys.procurement.all, 'quotations', 'detail', id] as const,
  comparison: (rfqId: string) =>
    [...queryKeys.procurement.all, 'quotations', 'comparison', rfqId] as const,
}

// ------------------------------------------------------------ vocabulary ----

export type QuotationStatus = SchemaOf<'QuotationStatus'>

/**
 * The same vocabulary as the status filter offers.
 *
 * `satisfies` keeps the two honest: a status the API drops stops compiling here
 * instead of surviving as an option nothing can match.
 */
export const QUOTATION_STATUSES = [
  'RECEIVED',
  'UNDER_REVIEW',
  'SELECTED',
  'REJECTED',
  'EXPIRED',
] as const satisfies readonly QuotationStatus[]

/**
 * The three states that end a quotation, from `QUOTATION_LIFECYCLE`
 * (apps/api/src/modules/procurement/service_rules.py). None can be reopened.
 */
export const QUOTATION_TERMINAL_STATUSES: readonly QuotationStatus[] = [
  'SELECTED',
  'REJECTED',
  'EXPIRED',
]

/**
 * Whether the buyer may still select or reject this offer.
 *
 * RECEIVED and UNDER_REVIEW only - those are the two sources the state machine
 * draws a `SELECTED` or `REJECTED` edge from. Offering the decision on a
 * quotation that is already settled puts the buyer through the confirmation and
 * the reason field only to be refused by `assert_transition` at the end of it.
 */
export const isDecidable = (status: QuotationStatus): boolean =>
  status === 'RECEIVED' || status === 'UNDER_REVIEW'

/**
 * Whether a revised offer may still be recorded against this quotation.
 *
 * The same two states, but for a different reason, so the two predicates stay
 * separate: `update_quotation` refuses a terminal quotation with "this
 * quotation is X and can no longer be revised". They agree today; folding them
 * into one gate would hide the day the API stops letting an offer under review
 * be revised.
 */
export const isQuotationRevisable = (status: QuotationStatus): boolean =>
  !QUOTATION_TERMINAL_STATUSES.includes(status)

/**
 * True when the browser's clock says the offer's validity has passed.
 *
 * A warning, never a gate. The authority is `decide_quotation`, which compares
 * `valid_until` against the *server's* date and refuses the selection with
 * "lapsed on <date>; ask the vendor to requote". Hiding the action on the
 * browser's reading would disagree with the API across a date boundary in
 * either direction; warning before the round trip costs nothing and is right
 * either way. `valid_until` is nullable and an open-ended offer never lapses.
 */
export function hasLapsed(validUntil: string | null): boolean {
  const days = daysUntil(validUntil)
  return days !== null && days < 0
}

/** `QuotationDecisionRequest.reason` - required, and bounded by the contract. */
export const MIN_DECISION_REASON = 3
export const MAX_DECISION_REASON = 255

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /procurement/quotations` (`QuotationSummary`). */
export type QuotationRow = SchemaOf<'QuotationSummary'>

/**
 * `GET /procurement/quotations/{id}` (`QuotationDetail`).
 *
 * Adds the lines, the free-text terms and notes, and - once decided - who
 * decided and the reason they recorded. `decision_reason` is written on both a
 * selection and a rejection, so it is the buyer's justification either way.
 */
export type QuotationDetail = SchemaOf<'QuotationDetail'>

/** One line of a quotation (`QuotationLineRead`). */
export type QuotationLine = SchemaOf<'QuotationLineRead'>

/** One line of the create and revise payloads (`QuotationLineIn`). */
export type QuotationLinePayload = SchemaOf<'QuotationLineIn'>

/**
 * Body of `POST /procurement/quotations` (`QuotationCreate`). Unknown keys are
 * refused, so nothing may be added to it that the contract does not declare.
 */
export type QuotationCreatePayload = SchemaOf<'QuotationCreate'>

/**
 * Body of `PATCH /procurement/quotations/{id}` (`QuotationUpdate`).
 *
 * A revision, not a patch in the usual sense. `update_quotation` skips every
 * field that arrives as null, so a null cannot *clear* a value - and `lines`,
 * when present, replaces the whole set rather than merging into it. The form
 * therefore resends every line it loaded.
 */
export type QuotationUpdatePayload = SchemaOf<'QuotationUpdate'>

/** Body of both decision endpoints (`QuotationDecisionRequest`). */
export type QuotationDecisionPayload = SchemaOf<'QuotationDecisionRequest'>

/** `GET /procurement/rfqs/{id}/comparison` (`QuotationComparison`). */
export type QuotationComparison = SchemaOf<'QuotationComparison'>

/** One vendor's column on the comparison board (`ComparisonVendorColumn`). */
export type ComparisonVendorColumn = SchemaOf<'ComparisonVendorColumn'>

/** One item's row on the comparison board (`ComparisonItemRow`). */
export type ComparisonItemRow = SchemaOf<'ComparisonItemRow'>

/** One vendor's offer for one item (`ComparisonOffer`). */
export type ComparisonOffer = SchemaOf<'ComparisonOffer'>

/**
 * The two decisions, which are also their URL segments:
 * `POST /procurement/quotations/{id}/{decision}`. Both enforce
 * `quotation.manage` - not the `quotation.view` that opened the board.
 */
export type QuotationDecision = 'select' | 'reject'

// -------------------------------------------------------------- derived -----

const ZERO = new Decimal(0)

/**
 * What one vendor's quotation costs relative to every other quote on the RFQ.
 *
 * This is the number a buyer is entitled to see before committing: not "is this
 * the cheapest", but "how much more than the cheapest is it, and by how much
 * does it beat the next one". Every figure is a `Decimal` derived from the
 * decimal strings the API sent; nothing here passes through a float.
 *
 * Expired columns are excluded from `cheapest` but kept in `rivals`. The
 * cheapest price on the board is frequently a lapsed one, and a comparison that
 * silently measured against an offer the API would refuse to select would
 * report a premium the buyer cannot avoid paying.
 *
 * Returns null when the quotation is not on the board at all - a board rebuilt
 * between the list loading and the dialog opening, for instance. The caller
 * shows the decision without the comparison rather than inventing a figure.
 *
 * Callers must check `comparison.mixed_currency` first: this function compares
 * magnitudes, which is meaningless across currencies, and the API's own
 * `recommendation_reason` is the right thing to show in that case.
 */
export interface TotalsStanding {
  chosen: ComparisonVendorColumn
  /** The cheapest quote still live on this board; null when all have lapsed. */
  cheapest: ComparisonVendorColumn | null
  /** `chosen` less `cheapest`. Zero when the chosen quote is the cheapest. */
  premium: Decimal
  /** Every other column, cheapest first, each with its gap to `chosen`. */
  rivals: { column: ComparisonVendorColumn; difference: Decimal }[]
}

/**
 * The lowest-priced quote on the board that has not lapsed, or null.
 *
 * `is_expired` comes from the API, which compares `valid_until` against the
 * server's date - the same comparison `decide_quotation` makes before it will
 * accept a selection. Measuring a premium against an offer the API would refuse
 * would report a saving the buyer cannot actually take.
 */
export function cheapestLiveColumn(comparison: QuotationComparison): ComparisonVendorColumn | null {
  return comparison.vendors
    .filter((column) => !column.is_expired)
    .reduce<ComparisonVendorColumn | null>(
      (best, column) =>
        best === null || toDecimal(column.total_amount).lessThan(toDecimal(best.total_amount))
          ? column
          : best,
      null,
    )
}

export function totalsStanding(
  comparison: QuotationComparison,
  quotationId: string,
): TotalsStanding | null {
  const chosen = comparison.vendors.find((column) => column.quotation_id === quotationId)
  if (!chosen) return null

  const chosenTotal = toDecimal(chosen.total_amount)
  const cheapest = cheapestLiveColumn(comparison)

  const rivals = comparison.vendors
    .filter((column) => column.quotation_id !== quotationId)
    .map((column) => ({ column, difference: chosenTotal.minus(toDecimal(column.total_amount)) }))
    .sort((a, b) => toDecimal(a.column.total_amount).comparedTo(toDecimal(b.column.total_amount)))

  return {
    chosen,
    cheapest,
    premium: cheapest ? chosenTotal.minus(toDecimal(cheapest.total_amount)) : ZERO,
    rivals,
  }
}

/**
 * How many lines each vendor is recommended on, keyed by vendor id.
 *
 * The API names an overall winner by exactly this count - "the vendor that wins
 * the most lines, not the lowest total" - and states the count in
 * `recommendation_reason`. Recomputing it here lets the column headers show
 * *each* vendor's share of the board, which the payload does not carry per
 * vendor, without disagreeing with the sentence the API wrote.
 */
export function linesWonByVendor(comparison: QuotationComparison): Map<string, number> {
  const counts = new Map<string, number>()
  for (const row of comparison.items) {
    if (row.recommended_vendor_id === null) continue
    counts.set(row.recommended_vendor_id, (counts.get(row.recommended_vendor_id) ?? 0) + 1)
  }
  return counts
}

/**
 * What the decision dialog needs to know about the offer being decided.
 *
 * A view model, not a wire shape, and deliberately so. The two places a
 * decision is taken describe the same offer with different fields:
 * `QuotationSummary` carries `vendor_reference` but no vendor name, and
 * `ComparisonVendorColumn` carries the vendor's code and name but no quotation
 * reference. Neither may be padded out with invented values to impersonate the
 * other - a fabricated `discount: "0"` on a board column is a lie about money -
 * so both are projected onto this instead, and the label is whatever actually
 * identifies the offer where the buyer is standing.
 */
export interface DecidableQuotation {
  id: string
  rfqId: string
  vendorId: string
  /** How this offer is identified on the screen the decision was taken from. */
  label: string
  currency: string
  totalAmount: string
  leadTimeDays: number
  validUntil: string | null
}

/** From a list row, where the vendor's own quotation number identifies it. */
export const decidableFromRow = (row: QuotationRow): DecidableQuotation => ({
  id: row.id,
  rfqId: row.rfq_id,
  vendorId: row.vendor_id,
  label: row.vendor_reference,
  currency: row.currency,
  totalAmount: row.total_amount,
  leadTimeDays: row.lead_time_days,
  validUntil: row.valid_until,
})

/** From a comparison column, where the vendor is what the buyer is choosing. */
export const decidableFromColumn = (
  column: ComparisonVendorColumn,
  comparison: QuotationComparison,
): DecidableQuotation => ({
  id: column.quotation_id,
  rfqId: comparison.rfq_id,
  vendorId: column.vendor_id,
  label: column.vendor_name || column.vendor_code,
  currency: column.currency,
  totalAmount: column.total_amount,
  leadTimeDays: column.lead_time_days,
  validUntil: column.valid_until,
})

/**
 * The offers on one row, keyed by vendor, so a column renders in O(1).
 *
 * A vendor that did not quote an item has no offer on that row - the API builds
 * rows from the union of the RFQ's lines and every line any vendor quoted - so
 * the lookup misses and the cell reads "not quoted" rather than zero. A zero
 * price would be the cheapest offer on the board.
 */
export function offersByVendor(row: ComparisonItemRow): Map<string, ComparisonOffer> {
  return new Map(row.offers.map((offer) => [offer.vendor_id, offer]))
}
