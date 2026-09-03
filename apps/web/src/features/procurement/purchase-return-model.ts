/**
 * Wire shapes and vocabulary for purchase returns.
 *
 * A purchase return is how material that was already received goes back to the
 * vendor. It is the *only* correction the ledger allows: a posted goods receipt
 * is terminal on purpose (`GoodsReceiptStatus` says so in as many words),
 * because an append-only ledger is corrected by an offsetting entry and never by
 * retracting the document that created it. So a return is not an edit of the
 * receipt - it is a second document that reverses part of it.
 *
 * Every type here is derived from the generated contract;
 * `apps/api/src/modules/procurement/schemas.py` is the author of these shapes
 * and a hand-written second opinion is how a screen comes to compile perfectly
 * while rendering nothing.
 *
 * Four properties of the contract shape the screens above it.
 *
 * 1. **Nothing is denormalised.** Vendor, warehouse, item and UoM arrive as ids
 *    with no code or name beside them, so they are resolved through the bounded
 *    reference projections their own modules publish - one cached lookup per
 *    session rather than a request per row.
 * 2. **`PurchaseReturnDetail.lines` is optional on the wire.** The API declares
 *    it with a default rather than as a required key, so every reader treats it
 *    as possibly absent - the same trap `PurchaseOrderDetail.lines` carries.
 * 3. **How much is still returnable is not a field.** It is
 *    `accepted_quantity - returned_quantity` on the *goods receipt* line, which
 *    is what `create_purchase_return` and `post_purchase_return` both bound
 *    against. `returnableQuantity` below is that one subtraction, written once.
 * 4. **Money and quantities are decimal strings** and are only ever computed on
 *    through decimal.js. `PurchaseReturnLineIn.quantity` is typed `number |
 *    string` because the API accepts either; the screens always send the string,
 *    because turning "0.1" into a double on the way out is exactly the defect
 *    the string representation exists to prevent.
 */

import Decimal from 'decimal.js'

import { toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import type { GoodsReceiptLine } from './procurement-types'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry has no purchase-return branch, so the keys live here in
 * the same hierarchical shape - but *under* the procurement prefix on purpose.
 *
 * Posting a return writes back to the goods receipt line and the purchase order
 * line it reverses, so a screen that invalidates `queryKeys.procurement.all`
 * must pick these lists up too, and one that invalidates
 * `purchaseReturnKeys.all` from here leaves the order book alone.
 */
export const purchaseReturnKeys = {
  all: [...queryKeys.procurement.all, 'purchase-returns'] as const,
  list: (params: unknown) =>
    [...queryKeys.procurement.all, 'purchase-returns', 'list', params] as const,
  detail: (id: string) => [...queryKeys.procurement.all, 'purchase-returns', 'detail', id] as const,
}

/**
 * The goods receipt a new return is raised against.
 *
 * `queryKeys.procurement` publishes `grns(params)` for the receipt list but no
 * key for one receipt, and `lib/query-client.ts` is not this feature's to edit -
 * so the key is spelled here in the shape that registry already uses for a
 * detail. Keeping it inside the `queryKeys.procurement.all` prefix means
 * posting a return, which writes the returned quantity back onto the receipt
 * line, drops this cache entry along with everything else procurement holds.
 */
export const goodsReceiptDetailKey = (id: string) =>
  [...queryKeys.procurement.all, 'grn', 'detail', id] as const

// ------------------------------------------------------------ vocabulary ----

export type PurchaseReturnStatus = SchemaOf<'PurchaseReturnStatus'>

/**
 * The same vocabulary as a list, for the filter that offers it.
 *
 * `satisfies` keeps the two honest: a status the API drops stops compiling here
 * instead of surviving as an option nothing can match.
 */
export const PURCHASE_RETURN_STATUSES = [
  'DRAFT',
  'POSTED',
  'CANCELLED',
] as const satisfies readonly PurchaseReturnStatus[]

/**
 * The state machine, read off `PURCHASE_RETURN_LIFECYCLE` in
 * `apps/api/src/modules/procurement/service_rules.py`:
 *
 *     DRAFT -> POSTED     (irreversible)
 *     DRAFT -> CANCELLED
 *
 * with POSTED and CANCELLED both terminal. The two predicates happen to test
 * the same status today and are still kept apart, because they answer different
 * questions and the answers stop agreeing the moment the machine grows an edge.
 * Offering a transition the machine will refuse puts the buyer through a
 * confirmation only to be rejected by `assert_transition` at the end of it.
 */
export const isReturnPostable = (status: PurchaseReturnStatus): boolean => status === 'DRAFT'

export const isReturnCancellable = (status: PurchaseReturnStatus): boolean => status === 'DRAFT'

/**
 * The two ways a return ends, which are emphatically not the same thing.
 *
 * POSTED means the stock physically left and the receipt and order lines were
 * written back; CANCELLED means the document was abandoned and nothing moved at
 * all. Neither can be reopened, but a screen that shows them as one "closed"
 * state is telling a storekeeper that material has gone back to the vendor when
 * it is still on the shelf.
 */
export const RETURN_TERMINAL_STATUSES: readonly PurchaseReturnStatus[] = ['POSTED', 'CANCELLED']

// ----------------------------------------------------------- API bounds -----

/** `PurchaseReturnCreate.reason` and `ReasonRequest.reason`: 3..255. */
export const MIN_REASON_LENGTH = 3
export const MAX_REASON_LENGTH = 255

/** `PurchaseReturnCreate.debit_note_number`. */
export const MAX_DEBIT_NOTE_LENGTH = 64

/** `PurchaseReturnCreate.notes`, and `PurchaseReturnLineIn.notes`. */
export const MAX_NOTES_LENGTH = 4000
export const MAX_LINE_NOTES_LENGTH = 2000

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /procurement/purchase-returns` (`PurchaseReturnSummary`).
 *
 * Carries `posted_at` but *not* `cancellation_reason` - that is on the detail
 * only - so a list can say when a return was posted but has to read the status
 * to know that a return was abandoned.
 */
export type PurchaseReturnRow = SchemaOf<'PurchaseReturnSummary'>

/** `GET /procurement/purchase-returns/{id}` (`PurchaseReturnDetail`). */
export type PurchaseReturnDetail = SchemaOf<'PurchaseReturnDetail'>

/**
 * One line of a return (`PurchaseReturnLineRead`).
 *
 * `batch_id` is the lot the goods came in on, copied from the receipt line so
 * the return leaves against the same lot. It is an id with no batch number
 * beside it, so a screen can say a line is tied to a lot but not which one.
 */
export type PurchaseReturnLine = SchemaOf<'PurchaseReturnLineRead'>

/**
 * One line of `POST /procurement/purchase-returns` (`PurchaseReturnLineIn`).
 *
 * `line_number` is required and the model forbids unknown keys, so a payload
 * that omits it is refused outright rather than partially accepted.
 */
export type PurchaseReturnLinePayload = SchemaOf<'PurchaseReturnLineIn'>

/** Body of `POST /procurement/purchase-returns` (`PurchaseReturnCreate`). */
export type PurchaseReturnCreatePayload = SchemaOf<'PurchaseReturnCreate'>

/**
 * `POST /procurement/purchase-returns/{id}/post` (`PurchaseReturnPostResult`).
 *
 * **Not** the return. Posting answers with what it *did* - the return nested
 * under `purchase_return`, and one `PostedMovement` per line beside it. Reading
 * `code` straight off this response is what made a sibling screen's toast
 * announce "undefined is posted".
 */
export type PurchaseReturnPostResult = SchemaOf<'PurchaseReturnPostResult'>

/** Body of the cancel command (`ReasonRequest`). Post takes no body at all. */
export type ReturnReasonPayload = SchemaOf<'src__modules__procurement__schemas__ReasonRequest'>

// -------------------------------------------------------------- derived -----

const ZERO = new Decimal(0)

/**
 * How much of a goods receipt line can still go back to the vendor.
 *
 * Accepted less already returned, exactly as `create_purchase_return` computes
 * it before refusing anything larger: "only N is still returnable". Note it is
 * *accepted*, not received - a quantity rejected at the gate was never taken
 * into stock, so there is nothing of it to send back.
 *
 * Clamped at zero rather than allowed to go negative, so a fully returned line
 * reads as nothing left instead of as a negative allowance.
 */
export function returnableQuantity(line: GoodsReceiptLine): Decimal {
  const remaining = toDecimal(line.accepted_quantity).minus(toDecimal(line.returned_quantity))
  return remaining.greaterThan(0) ? remaining : ZERO
}
