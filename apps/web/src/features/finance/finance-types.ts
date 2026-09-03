/**
 * Wire shapes for the `/finance` endpoints.
 *
 * Every one of these is DERIVED from the generated contract (`types/openapi.ts`,
 * through `SchemaOf`), never retyped. A hand-written mirror of a server schema
 * is a second opinion, and `tsc` can only see a disagreement between two pieces
 * of TypeScript - not between an interface and a live API. That is exactly how
 * these screens came to compile perfectly while rendering nothing. Deriving
 * turns the disagreement into a compile error: change the API, run
 * `make contract`, and every screen reading a field that moved stops building.
 *
 * Two consequences the screens have to live with, and do:
 *
 * * **Money and quantities are decimal STRINGS.** They are never turned into a
 *   JS number for arithmetic; `lib/format.ts` converts through decimal.js.
 * * **Collections are optional in the contract.** `rows`, `lines`, `currencies`,
 *   `findings` and `blocking_reasons` all default to an empty list on the
 *   server, so the generated type marks them optional. Every screen guards them
 *   before iterating - a missing list renders an empty table, never a crash.
 *
 * Only the things with no server counterpart are declared here: the query keys,
 * the narrowing of `PartyType` to the two sides a screen can ask for, the
 * status predicates the vendor invoice screens gate their commands on, and one
 * deliberate mirror of the server's line pricing - see `priceInvoiceLine`,
 * which explains why an entry screen is allowed to compute money at all.
 */

import Decimal from 'decimal.js'

import { toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

/** `NONE` exists on the wire for a ledger entry that is against no party. */
export type PartyType = SchemaOf<'PartyType'>

/**
 * The two sides a screen can actually ask for.
 *
 * Client-side by nature: `NONE` is a value the API reports, never one a URL may
 * request, and the ledger is mounted per side rather than taking a party type.
 */
export type LedgerSide = Extract<PartyType, 'CUSTOMER' | 'VENDOR'>

/**
 * Query keys for finance.
 *
 * The shared registry in `lib/query-client.ts` has no finance namespace yet, so
 * the factories live here. Every key starts with `finance`, which keeps the
 * hierarchy intact: invalidating `financeKeys.all` still sweeps every list and
 * detail on both screens.
 */
export const financeKeys = {
  all: ['finance'] as const,
  outstanding: (params: unknown) => ['finance', 'outstanding', params] as const,
  ageing: (params: unknown) => ['finance', 'ageing', params] as const,
  ledger: (partyType: LedgerSide, partyId: string, params: unknown) =>
    ['finance', 'ledger', partyType, partyId, params] as const,
  valuation: (params: unknown) => ['finance', 'inventory-valuation', params] as const,
  gstSummary: (dateFrom: string, dateTo: string) =>
    ['finance', 'gst-summary', dateFrom, dateTo] as const,
  vendorInvoices: (params: unknown) => ['finance', 'vendor-invoices', params] as const,
  vendorInvoice: (id: string) => ['finance', 'vendor-invoices', 'detail', id] as const,
  vendorInvoiceMatch: (id: string) => ['finance', 'vendor-invoices', 'match', id] as const,
} as const

// -------------------------------------------------------- outstanding ------

export type VendorInvoiceStatus = SchemaOf<'VendorInvoiceStatus'>

/**
 * One open **vendor invoice** on `GET /finance/outstanding`.
 *
 * This endpoint is payables only - it reads the vendor invoice register, not
 * both sides of the ledger. Receivables have no invoice document in this schema
 * and are reached through the ageing report instead.
 *
 * Every row states its own `currency`; two rows in different denominations must
 * never be added together on screen. `days_overdue` and `ageing_bucket` are
 * computed by the API against its own clock and `as_of`, never re-derived here.
 */
export type OutstandingRow = SchemaOf<'OutstandingRow'>

// ------------------------------------------------------------- ageing ------

/**
 * One party's outstanding in ONE denomination, split across the age buckets.
 *
 * `currency` is the currency of the block the row belongs to. The same party
 * appears once per denomination it owes in, never once with the two added
 * together - there is no exchange rate in this contract (docs/assumptions.md
 * section 27). `not_due` is open but inside terms: not an age, and never folded
 * into a bucket.
 */
export type AgeingRow = SchemaOf<'AgeingRow'>

/**
 * One denomination's ageing: its parties, and what they add up to.
 *
 * A block is the unit in which this report is arithmetically meaningful. Detail
 * rows come back for at most five denominations (the primary, then the largest
 * by outstanding), so `rows` can be absent or empty while the totals are not -
 * that block is reported totals-only, it is not empty. `party_count` covers the
 * whole block and can exceed the number of rows returned when capped.
 */
export type AgeingCurrencyBlock = SchemaOf<'AgeingCurrencyBlock'>

/**
 * `GET /finance/ageing` - an ApiResponse, not a paginated list.
 *
 * `currencies` covers every denomination with an open item. The `currency`
 * argument chooses which block is restated at the top level, never which blocks
 * exist, so a company whose debts are all in USD can never be shown an empty
 * report. `currencies[0].currency` always equals `currency`, zeroed if nothing
 * is outstanding in it, and the top-level `rows` and `total_*` fields restate
 * that block rather than being a second figure.
 */
export type AgeingReport = SchemaOf<'AgeingReport'>

// ------------------------------------------------------------- ledger ------

/**
 * One line of a party statement.
 *
 * `running_balance` is computed by PostgreSQL over the whole filtered set plus
 * the opening balance carried in, so it is correct on every page. A paginated
 * ledger cannot derive it in the browser - page three has no idea what came
 * before it.
 */
export type LedgerStatementRow = SchemaOf<'LedgerStatementRow'>

/**
 * The party statement - an ApiResponse, not a paginated list.
 *
 * The endpoint accepts `page` and `limit` but returns no pagination block, so
 * the screen infers "there is more" from a full page of rows rather than
 * inventing a total it was never told.
 */
export type LedgerStatement = SchemaOf<'LedgerStatement'>

// -------------------------------------------------- inventory valuation ----

/**
 * One item-and-warehouse position on `GET /finance/inventory-valuation`.
 *
 * The two money columns are a reconciliation and are not interchangeable:
 *
 * * `value` is the sum of `stock_value` - what the inventory engine last wrote
 *   on the position's balance rows.
 * * `recorded_value` is that same total recomputed from the open FIFO cost
 *   layers themselves (remaining quantity times unit cost), which is what makes
 *   it an independent check rather than a restatement of the balance.
 *
 * A small difference is expected - `value` is rounded per position, the layer
 * product is not, and the two are summed in different orders. A material one is
 * a balance that has drifted from its layers, and is a reconciliation item.
 *
 * `average_cost` is a UNIT PRICE at six decimal places (value divided by on-hand
 * quantity), not money at two - see `formatUnitCost` in the valuation tab.
 */
export type InventoryValuationRow = SchemaOf<'InventoryValuationRow'>

/** Totals for the whole filtered set, not for the page on screen. */
export type InventoryValuationSummary = SchemaOf<'InventoryValuationSummary'>

/**
 * `GET /finance/inventory-valuation`.
 *
 * The pagination block is *inside* `data`, so this is read with `api.get` and
 * the page state is driven by the screen rather than by `useListQuery`, which
 * expects the envelope-level `pagination` of a `PaginatedResponse`.
 */
export type InventoryValuationPage = SchemaOf<'InventoryValuationPage'>

// --------------------------------------------------------- GST summary -----

/** One HSN-and-rate slab of a GST section. `tax_rate` is a percentage as filed. */
export type GstRateRow = SchemaOf<'GstRateRow'>

export type GstSection = SchemaOf<'GstSection'>

/**
 * `GET /finance/gst-summary?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`.
 *
 * `output` is tax charged on sales, `input` is the credit on purchases, and
 * `net_payable` is output less input - negative means credit carried forward.
 */
export type GstSummary = SchemaOf<'GstSummary'>

// ----------------------------------------------------- vendor invoices -----

export type MatchStatus = SchemaOf<'MatchStatus'>

/** One row of `GET /finance/vendor-invoices`. */
export type VendorInvoiceSummary = SchemaOf<'VendorInvoiceSummary'>

export type VendorInvoiceLine = SchemaOf<'VendorInvoiceLineRead'>

/** `GET /finance/vendor-invoices/{id}` - the summary plus lines and decisions. */
export type VendorInvoiceDetail = SchemaOf<'VendorInvoiceDetail'>

/**
 * One line of `GET /finance/vendor-invoices/{id}/match`.
 *
 * Both variances are reported as a signed money amount and as a *ratio* - 0.05
 * is five per cent, not five. Anything rendered as a percentage multiplies by
 * a hundred first; the tolerances on the result below are ratios too.
 *
 * `accepted_quantity` is what was accepted at the gate after QC, not what was
 * delivered. A missing `purchase_order_line_id` is a line with nothing to match
 * against - freight, a service charge.
 */
export type MatchLineVariance = SchemaOf<'MatchLineVariance'>

/**
 * `GET /finance/vendor-invoices/{id}/match`.
 *
 * `matched` is the single question approval asks: false means the API refuses
 * the approval unless an `override_reason` is supplied. The screen mirrors that
 * rather than re-deriving a rule from the variance figures, so the button can
 * never disagree with what the endpoint will accept. `blocking_reasons` is
 * empty exactly when `matched` is true, and `worst_variance` is the largest
 * single-line absolute money variance.
 */
export type MatchResult = SchemaOf<'MatchResult'>

// ----------------------------------------------- vendor invoice: entry -----

/**
 * The lifecycle, read off `VENDOR_INVOICE_LIFECYCLE` in
 * `apps/api/src/modules/finance/service.py`:
 *
 *     DRAFT            -> PENDING_APPROVAL   (vendor_invoice.create)
 *     PENDING_APPROVAL -> APPROVED | DRAFT | DISPUTED
 *     APPROVED         <-> PARTIALLY_PAID <-> PAID
 *
 * The three predicates below answer three different questions and are kept
 * apart even where two of them test the same status today, because they stop
 * agreeing the moment the machine grows an edge. Offering a transition the
 * machine will refuse puts a clerk through a form only to be rejected by
 * `assert_transition` at the end of it.
 */

/**
 * `EDITABLE_INVOICE_STATUSES` in `apps/api/src/modules/finance/models.py`.
 *
 * PENDING_APPROVAL is deliberately included: an approver who sends a bill back
 * for correction does not move it out of the queue, and the API accepts the
 * amendment there. Every later status is excluded because the invoice has
 * reached the ledger - those are disputed or cancelled, never edited.
 */
export const isInvoiceEditable = (status: VendorInvoiceStatus): boolean =>
  status === 'DRAFT' || status === 'PENDING_APPROVAL'

/**
 * Only a draft is submitted.
 *
 * `submit_invoice` would technically accept an invoice already in
 * PENDING_APPROVAL - `_transition_invoice` returns early when the status is
 * already the target, and the match is simply re-run - but offering "Submit" on
 * something already in the approval queue says the wrong thing about where the
 * document is.
 */
export const isInvoiceSubmittable = (status: VendorInvoiceStatus): boolean => status === 'DRAFT'

/**
 * Only a draft is deleted, and the row goes for good.
 *
 * `delete_draft_invoice` hard-deletes: a draft keyed in error never reached the
 * ledger, so there is nothing to correct with an offsetting entry. Anything
 * past DRAFT is cancelled instead - a different command, on a different right -
 * so the trail survives.
 */
export const isInvoiceDeletable = (status: VendorInvoiceStatus): boolean => status === 'DRAFT'

// ----------------------------------------------------------- API bounds ----

/** `VendorInvoiceCreate.vendor_invoice_number`: 1..64. */
export const MAX_VENDOR_INVOICE_NUMBER_LENGTH = 64

/** `VendorInvoiceCreate.notes`, and `VendorInvoiceLineIn.notes`. */
export const MAX_INVOICE_NOTES_LENGTH = 2000

/** `VendorInvoiceLineIn.description` and `.hsn_code`. */
export const MAX_LINE_DESCRIPTION_LENGTH = 500
export const MAX_HSN_CODE_LENGTH = 16

/** `VendorInvoiceCreate.lines`: at least one, at most two hundred. */
export const MAX_INVOICE_LINES = 200

/** `VendorInvoiceLineIn.tax_rate` is a percentage, bounded 0..100. */
export const MAX_TAX_RATE = 100

/** `ReasonRequest.reason`, and the `reason` query parameter on delete: 3..500. */
export const MIN_INVOICE_REASON_LENGTH = 3
export const MAX_INVOICE_REASON_LENGTH = 500

// ---------------------------------------------------------- wire shapes ----

/**
 * Body of `POST /finance/vendor-invoices` (`VendorInvoiceCreate`).
 *
 * `additionalProperties: false`, so an unknown key is refused outright rather
 * than quietly ignored. `currency` and `due_date` are optional, and *absent*
 * means something: the API falls back to the company's own denomination and to
 * the vendor's agreed credit days, which is not the same as sending a blank.
 */
export type VendorInvoiceCreatePayload = SchemaOf<'VendorInvoiceCreate'>

/**
 * Body of `PATCH /finance/vendor-invoices/{id}` (`VendorInvoiceUpdate`).
 *
 * Sending `lines` replaces the WHOLE set - a bill is a document, and patching
 * one line of something somebody is about to approve invites an approval of
 * other than what was reviewed. Note what is absent: no `vendor_id` and no
 * `currency`, because both are frozen at creation.
 */
export type VendorInvoiceUpdatePayload = SchemaOf<'VendorInvoiceUpdate'>

/**
 * One line on the way in (`VendorInvoiceLineIn`).
 *
 * Deliberately narrower than what comes back. `taxable_amount`, the CGST/SGST/
 * IGST split and `line_total` are all derived by `service.price_invoice_line`
 * and are not accepted here, because a client that could post them could post a
 * line whose parts do not add up - which the row's CHECK constraints would then
 * reject with a message nobody can act on.
 *
 * `is_interstate` is input-only and has no counterpart on
 * `VendorInvoiceLineRead`; see `wasInterstate` below for how an amendment
 * recovers it.
 */
export type VendorInvoiceLinePayload = SchemaOf<'VendorInvoiceLineIn'>

/**
 * Body of the submit command (`ReasonRequest`), which is OPTIONAL there.
 *
 * The route declares `payload: ReasonRequest | None = None`, so submitting
 * without a note sends no body at all. Sending `{ reason: '' }` instead would
 * be refused - the field is 3..500 whenever the body is present.
 */
export type InvoiceReasonPayload = SchemaOf<'src__modules__finance__schemas__ReasonRequest'>

/**
 * The purchase order a new invoice is billed against.
 *
 * `queryKeys.procurement` already publishes this one, and it is reused rather
 * than re-spelled so the copy the procurement screens hold is the copy read
 * here. `lib/query-client.ts` is not this feature's to edit.
 */
export const purchaseOrderDetailKey = queryKeys.procurement.purchaseOrder

/**
 * The goods receipt a new invoice is billed against.
 *
 * The shared registry has `grns(params)` for the receipt list but no key for
 * one receipt, so the shape is spelled here - deliberately identical to the one
 * `features/procurement/purchase-return-model.ts` uses, so both screens read
 * and invalidate the same cache entry instead of fetching the receipt twice.
 */
export const goodsReceiptDetailKey = (id: string) =>
  [...queryKeys.procurement.all, 'grn', 'detail', id] as const

// ------------------------------------------------------------- pricing -----

/**
 * Money scale, and the rounding the API applies at every step.
 *
 * `to_money` in `apps/api/src/core/types.py` quantizes to two places with
 * ROUND_HALF_UP - the convention Indian GST invoicing expects - and decimal.js
 * is configured with the same rounding in `lib/format.ts`, so the two agree.
 * Named once here rather than left as a bare `2` in six places.
 */
const MONEY_PLACES = 2

/** Unit price scale: `to_unit_price` quantizes to six places. */
const UNIT_PRICE_PLACES = 6

const ZERO = new Decimal(0)

/** `to_money`: two decimal places, half away from zero. */
const toMoney = (value: Decimal): Decimal => value.toDecimalPlaces(MONEY_PLACES)

/** What one line is worth, and how its tax splits. */
export interface InvoiceLinePricing {
  taxable: Decimal
  cgst: Decimal
  sgst: Decimal
  igst: Decimal
  tax: Decimal
  lineTotal: Decimal
}

/**
 * A faithful mirror of `service.price_invoice_line`, step for step.
 *
 * The entry screen has to show what the bill adds up to *before* it is saved -
 * that is most of the point of keying a vendor invoice, since the clerk is
 * reconciling against a printed document and `round_off_amount` exists to
 * absorb whatever difference is left over. A preview that rounded differently
 * from the server would send them chasing a paisa that is not there, so this
 * rounds at exactly the four points the server rounds at:
 *
 * 1. the gross (quantity times price) is rounded to money,
 * 2. so is the discount, before it is subtracted,
 * 3. so is the tax,
 * 4. and the CGST half is rounded while SGST is the *remainder* - not a second
 *    independent halving - so that on an odd number of paise the two halves
 *    still add up to the tax, which is what the `tax_amount = cgst + sgst +
 *    igst` CHECK on the row requires.
 *
 * The server's figures stay authoritative: the screen re-reads the saved
 * invoice rather than keeping what it computed here.
 */
export function priceInvoiceLine(line: {
  quantity: DecimalString
  unit_price: DecimalString
  discount_amount: DecimalString
  tax_rate: DecimalString
  is_interstate: boolean
}): InvoiceLinePricing {
  const gross = toMoney(toDecimal(line.quantity).times(toDecimal(line.unit_price)))
  const taxable = toMoney(gross.minus(toMoney(toDecimal(line.discount_amount))))
  const tax = toMoney(taxable.times(toDecimal(line.tax_rate)).dividedBy(100))

  const cgst = line.is_interstate ? ZERO : toMoney(tax.dividedBy(2))
  const sgst = line.is_interstate ? ZERO : tax.minus(cgst)
  const igst = line.is_interstate ? tax : ZERO

  return { taxable, cgst, sgst, igst, tax, lineTotal: taxable.plus(tax) }
}

/** The header figures, exactly as `_invoice_totals` derives them. */
export function invoiceTotals(
  lines: readonly InvoiceLinePricing[],
  roundOff: DecimalString,
): { subtotal: Decimal; tax: Decimal; total: Decimal } {
  const subtotal = toMoney(lines.reduce((sum, line) => sum.plus(line.taxable), ZERO))
  const tax = toMoney(lines.reduce((sum, line) => sum.plus(line.tax), ZERO))
  return { subtotal, tax, total: toMoney(subtotal.plus(tax).plus(toMoney(toDecimal(roundOff)))) }
}

/**
 * Whether a saved line was billed as an interstate supply.
 *
 * `is_interstate` is input-only - it decides the split and is then thrown away -
 * so an amendment has to recover it from the split itself. IGST is charged
 * exactly when the supply crossed a state border, so a non-zero `igst_amount`
 * is the answer. The one case it cannot distinguish is a line taxed at zero,
 * where CGST, SGST and IGST are all zero alike: there the flag changes nothing
 * that can be computed, and either answer re-posts the identical row.
 */
export const wasInterstate = (line: VendorInvoiceLine): boolean =>
  toDecimal(line.igst_amount).greaterThan(0)

/**
 * The price per unit a purchase order line actually agreed, after its discount.
 *
 * A mirror of `_ordered_net_price` in the finance service, which is the figure
 * the three-way match compares a bill against. It matters when an invoice is
 * seeded from an order: copying the line's gross `unit_price` and leaving the
 * discount behind would bill the list price the vendor agreed to come off, and
 * the match would - correctly - report a price variance on a delivery that was
 * perfectly in order.
 *
 * Rounded to six places, the scale `unit_price` is stored and sent at
 * (`to_unit_price`). Returns null on a zero-quantity line, which has no price
 * per unit to speak of.
 */
export function orderedNetUnitPrice(line: {
  quantity: DecimalString
  unit_price: DecimalString
  discount: DecimalString
}): DecimalString | null {
  const quantity = toDecimal(line.quantity)
  if (quantity.isZero()) return null
  const net = quantity.times(toDecimal(line.unit_price)).minus(toDecimal(line.discount))
  return net.dividedBy(quantity).toDecimalPlaces(UNIT_PRICE_PLACES).toFixed()
}
