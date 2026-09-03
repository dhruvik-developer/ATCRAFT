/**
 * Wire shapes and vocabulary for sales orders.
 *
 * The wire shapes are DERIVED from the generated contract, never retyped. A
 * hand-written interface is a second opinion, and `tsc` can only see a
 * disagreement between two pieces of TypeScript - not between an interface and
 * the API actually answering. The drift that motivated this was real:
 * `LineAllocationRead.available_quantity` and `item_code` are *optional* in the
 * contract, and the hand-written copy declared them required, so the allocation
 * panel's "not reported" branch could never be reached for an absent field and
 * an unknown free-stock figure rendered as a confident `0`.
 *
 * The status list is the state machine from docs/database.md section 7, and the
 * screens read it rather than hard-coding which buttons appear where: an action
 * that is illegal from the current state is never offered. The arrays below are
 * display orders, typed against the contract's enum so that dropping a member
 * from the API is a compile error here.
 *
 * Money and quantities are decimal strings end to end. The order totals shown to
 * the customer are the ones the API calculated - the browser never re-adds them.
 *
 * Two things about this payload are easy to get wrong:
 *
 * * `priority` is an integer rank between 1 and 9 (default 5), not a named
 *   band. Urgency is carried separately by `is_urgent`, which is what the list
 *   filter uses. Nothing here invents a mapping from the number to a word.
 * * Lines are not addressable. There is no `/sales/{id}/lines` endpoint: the
 *   whole line set is sent on `PATCH /sales/{id}`, and the service decides what
 *   a replacement means for the order's current status.
 */

import type { DecimalString, SchemaOf } from '@/types/api'

// ------------------------------------------------------------ vocabulary ----

export type SalesOrderStatus = SchemaOf<'SalesOrderStatus'>
export type SalesOrderLineStatus = SchemaOf<'SalesOrderLineStatus'>

/** Display order for the status filter. Typed against the contract's enum. */
export const SALES_ORDER_STATUSES: readonly SalesOrderStatus[] = [
  'DRAFT',
  'CONFIRMED',
  'ALLOCATED',
  'PARTIALLY_DISPATCHED',
  'DISPATCHED',
  'INVOICED',
  'CLOSED',
  'CANCELLED',
]

export const SALES_ORDER_LINE_STATUSES: readonly SalesOrderLineStatus[] = [
  'PENDING',
  'PARTIALLY_ALLOCATED',
  'ALLOCATED',
  'PARTIALLY_DISPATCHED',
  'DISPATCHED',
  'CANCELLED',
]

/** `SalesOrderCreate.priority` is bounded 1..9 and defaults to 5. */
export const PRIORITY_MIN = 1
export const PRIORITY_MAX = 9
export const PRIORITY_DEFAULT = 5

/**
 * The audit reason on `/confirm`, `/approve` and `/cancel`.
 *
 * `ConfirmRequest`, `ApproveRequest` and `CancelRequest` all bound `reason` to
 * 3..255 characters. A one-character reason is refused by the API as a 422 on a
 * field this screen has no control for, so it is checked before it is sent.
 */
export const REASON_MIN_LENGTH = 3
export const REASON_MAX_LENGTH = 255

/**
 * `DRAFT -> CONFIRMED -> ALLOCATED -> PARTIALLY_DISPATCHED -> DISPATCHED ->
 * INVOICED -> CLOSED`, with `CANCELLED` reachable from DRAFT, CONFIRMED and
 * ALLOCATED only. The API enforces this; the UI uses it to decide what to show.
 */
export const CANCELLABLE_STATUSES: readonly SalesOrderStatus[] = ['DRAFT', 'CONFIRMED', 'ALLOCATED']

export const isDraft = (status: SalesOrderStatus): boolean => status === 'DRAFT'
export const isConfirmable = (status: SalesOrderStatus): boolean => status === 'DRAFT'
export const isCancellable = (status: SalesOrderStatus): boolean =>
  CANCELLABLE_STATUSES.includes(status)

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /sales` (`SalesOrderSummary`). */
export type SalesOrderRow = SchemaOf<'SalesOrderSummary'>

/** One line of `GET /sales/{id}` (`SalesOrderLineRead`). */
export type SalesOrderLine = SchemaOf<'SalesOrderLineRead'>

/** `GET /sales/{id}` (`SalesOrderDetail`). */
export type SalesOrderDetail = SchemaOf<'SalesOrderDetail'>

/**
 * One line of `SalesOrderCreate` / `SalesOrderUpdate` (`SalesOrderLineIn`).
 *
 * The contract accepts `number | string` for every decimal, because the API
 * parses both. This app only ever sends the string form - a `float` near a
 * price is a defect here exactly as it is on the server - so the amounts are
 * narrowed. The intersection keeps the shape structurally a `SalesOrderLineIn`,
 * so a field the contract renames or drops is still a compile error.
 */
export type SalesOrderLineIn = SchemaOf<'SalesOrderLineIn'> & {
  quantity: DecimalString
  unit_price?: DecimalString | null
  discount_percentage?: DecimalString | null
  discount_amount?: DecimalString | null
}

/** One line of `GET /sales/{id}/allocation-status` (`LineAllocationRead`). */
export type AllocationLineStatus = SchemaOf<'LineAllocationRead'>

/** `GET /sales/{id}/allocation-status` (`OrderAllocationRead`). */
export type SalesOrderAllocation = SchemaOf<'OrderAllocationRead'>

/** `POST /sales/{id}/confirm` (`ConfirmResult`). */
export type ConfirmResult = SchemaOf<'ConfirmResult'>

// --------------------------------------------------------------- helpers ----

/**
 * The line set as the API wants it back on a PATCH.
 *
 * `SalesOrderLineRead` and `SalesOrderLineIn` are different shapes - the read
 * carries computed tax and dispatched quantities the write refuses - so a line
 * that is being kept unchanged still has to be narrowed to the input fields.
 */
export function toLineInput(line: SalesOrderLine): SalesOrderLineIn {
  return {
    line_number: line.line_number,
    item_id: line.item_id,
    item_variant_id: line.item_variant_id,
    description: line.description,
    quantity: line.quantity,
    uom_id: line.uom_id,
    unit_price: line.unit_price,
    discount_percentage: line.discount_percentage,
    required_date: line.required_date,
    notes: line.notes,
  }
}

/** Line numbers are 1-based and must stay dense after a removal. */
export function renumber(lines: SalesOrderLineIn[]): SalesOrderLineIn[] {
  return lines.map((line, index) => ({ ...line, line_number: index + 1 }))
}
