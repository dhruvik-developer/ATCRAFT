/**
 * Wire shapes and vocabulary for procurement.
 *
 * Every interface here mirrors a Pydantic model in
 * `apps/api/src/modules/procurement/schemas.py` field for field. They are read
 * off the generated OpenAPI document, not designed here: the API sends foreign
 * keys (`vendor_id`, `warehouse_id`, `uom_id`) and leaves the naming of them to
 * the reference lookups every screen already loads. Inventing `vendor_name` on
 * a summary the API never sends produced a column of dashes that type-checked
 * perfectly, which is exactly the failure this file now exists to prevent.
 *
 * The status vocabularies are the state machines from docs/database.md section
 * 7. The screens read them rather than hard-coding which buttons appear where:
 * an action that is illegal from the current state is never offered, so a user
 * cannot discover the rule by being refused.
 *
 * Money and quantities are decimal strings end to end (docs/api.md, "Numbers").
 * Nothing here converts one to a JS number.
 */

import type { SetURLSearchParams } from 'react-router-dom'

import { ApiError } from '@/lib/api-client'
import { toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import type { DecimalString, SchemaOf } from '@/types/api'

// --------------------------------------------------------------- shared -----

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

/**
 * Where the demand behind a document came from.
 *
 * `PurchaseSourceType` in the API. A buyer reads this to tell a hand-raised
 * order from one the planner produced, which changes how much they trust the
 * quantity on it.
 */
export type PurchaseSourceType = SchemaOf<'PurchaseSourceType'>

/**
 * The same vocabulary as a list, for the screens that offer it as options.
 *
 * `satisfies` is what keeps the two honest: a member the API has dropped stops
 * compiling here instead of surviving as an option nothing can match, and one
 * the API has added is caught by a reviewer rather than by a user.
 */
export const PURCHASE_SOURCE_TYPES = [
  'MANUAL',
  'PURCHASE_REQUISITION',
  'VENDOR_QUOTATION',
  'MRP_SUGGESTION',
  'MRP_RUN',
  'SALES_ORDER',
  'BOM_DEMAND',
  'REORDER',
] as const satisfies readonly PurchaseSourceType[]

/**
 * Priority is an integer 1-9 with 5 as the default, and *lower is more urgent*
 * (`MIN_PRIORITY`/`MAX_PRIORITY` in the API's procurement models). It used to
 * be typed here as a free-text string, so every priority rendered as a raw
 * number with no sense of which end of the scale was bad.
 */
export const MIN_PRIORITY = 1
export const MAX_PRIORITY = 9
export const DEFAULT_PRIORITY = 5

/** Urgent enough to call out. Anything at or below the default is routine. */
export const isUrgentPriority = (priority: number): boolean => priority < DEFAULT_PRIORITY

// -------------------------------------------------------- requisitions -----

/**
 * Query keys for the requisition screens.
 *
 * Kept under `queryKeys.procurement.all` so one invalidation clears requisitions
 * alongside the RFQs and orders they turn into - a requisition that has just
 * been approved or cancelled changes what the sourcing screens may offer, which
 * is why every mutation on it invalidates the whole procurement prefix rather
 * than only these two keys.
 *
 * `list` keeps the shape the queue has always used - no `list` segment - so its
 * cached pages survive this file gaining a detail key.
 */
export const requisitionKeys = {
  list: (params: unknown) => [...queryKeys.procurement.all, 'requisitions', params] as const,
  detail: (id: string) => [...queryKeys.procurement.all, 'requisitions', 'detail', id] as const,
}

export type RequisitionStatus = SchemaOf<'RequisitionStatus'>

export const REQUISITION_STATUSES = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'REJECTED',
  'CONVERTED',
  'CLOSED',
  'CANCELLED',
] as const satisfies readonly RequisitionStatus[]

/** Approval and rejection are legal only while a requisition awaits a decision. */
export const isRequisitionDecidable = (status: string): boolean => status === 'PENDING_APPROVAL'

/**
 * Submitting for approval is the DRAFT -> PENDING_APPROVAL edge of
 * `REQUISITION_LIFECYCLE`, and there is no other way into the approval queue.
 */
export const isRequisitionSubmittable = (status: string): boolean => status === 'DRAFT'

/**
 * Editing is legal only on a DRAFT.
 *
 * `update_requisition` refuses anything further along - "anything past DRAFT is
 * re-raised rather than rewritten" - because a document somebody is already
 * deciding on must not change underneath them.
 */
export const isRequisitionAmendable = (status: string): boolean => status === 'DRAFT'

/**
 * Deleting is legal only on a DRAFT.
 *
 * Same set as `isRequisitionAmendable` today, for a different reason: a draft
 * has never been anybody else's business, so it can go without leaving a hole in
 * the numbering that mattered. `delete_draft_requisition` says the rest plainly
 * - "anything further along is cancelled instead" - so the two rules are kept
 * apart rather than folded into one gate that a later status change would move
 * in the wrong direction for one of them.
 */
export const isRequisitionDeletable = (status: string): boolean => status === 'DRAFT'

/**
 * Statuses from which a requisition may be cancelled.
 *
 * The three sources of a `-> CANCELLED` edge in `REQUISITION_LIFECYCLE`:
 * abandoning a draft, withdrawing one that is awaiting a decision, and calling
 * off approved demand that is no longer wanted. CONVERTED is deliberately
 * excluded - once an RFQ or an order exists against the requisition the only
 * legal move is CLOSED, and cancelling the document would say nothing about the
 * commitment already made to a vendor.
 */
export const CANCELLABLE_REQUISITION_STATUSES: readonly RequisitionStatus[] = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
]

export const isRequisitionCancellable = (status: string): boolean =>
  (CANCELLABLE_REQUISITION_STATUSES as readonly string[]).includes(status)

/**
 * Requisition statuses in which a late required-date is still somebody's
 * problem: the document has not been decided, or it has been approved and not
 * yet sourced.
 *
 * Deliberately *not* the API's `OPEN_REQUISITION_STATUSES`, which is
 * `(APPROVED, CONVERTED)` - the set that still represents unmet demand for
 * planning, and what the `open_only` list filter selects. The two answer
 * different questions and must not be folded together.
 */
export const ACTIONABLE_REQUISITION_STATUSES: readonly RequisitionStatus[] = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
]

/**
 * One row of `GET /procurement/purchase-requisitions` (`RequisitionSummary`).
 *
 * `priority` is an integer 1-9 and lower is the more urgent end.
 */
export type RequisitionRow = SchemaOf<'RequisitionSummary'>

/** One line of `GET /procurement/purchase-requisitions/{id}`. */
export type RequisitionLine = SchemaOf<'RequisitionLineRead'>

/**
 * `GET /procurement/purchase-requisitions/{id}` (`RequisitionDetail`).
 *
 * `lines` is optional on the wire, so a reader copes with its absence rather
 * than assuming an array is there.
 */
export type RequisitionDetail = SchemaOf<'RequisitionDetail'>

/**
 * One line of `POST`/`PATCH` (`RequisitionLineIn`).
 *
 * `line_number` is required and the model forbids unknown keys. `uom_id` is
 * optional and best left null on a new line: the API fills in the item's own
 * order unit, which no form here is in a position to guess.
 */
export type RequisitionLinePayload = SchemaOf<'RequisitionLineIn'>

/** Body of `POST /procurement/purchase-requisitions` (`RequisitionCreate`). */
export type RequisitionCreatePayload = SchemaOf<'RequisitionCreate'>

/**
 * Body of `PATCH /procurement/purchase-requisitions/{id}` (`RequisitionUpdate`).
 *
 * Two things about it decide how an editor has to be written. `lines` replaces
 * the whole set when present, so anything not resent is gone. And the service
 * skips every `None` it is handed for the scalar fields, so this can change a
 * purpose but cannot clear one - the same asymmetry the RFQ update has.
 */
export type RequisitionUpdatePayload = SchemaOf<'RequisitionUpdate'>

// ------------------------------------------------------ purchase orders -----

export type PurchaseOrderStatus = SchemaOf<'PurchaseOrderStatus'>

export const PURCHASE_ORDER_STATUSES = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'REJECTED',
  'SENT',
  'ACKNOWLEDGED',
  'PARTIALLY_RECEIVED',
  'RECEIVED',
  'QC_PENDING',
  'QC_COMPLETED',
  'GRN_COMPLETED',
  'INVOICED',
  'PAID',
  'CLOSED',
  'CANCELLED',
] as const satisfies readonly PurchaseOrderStatus[]

/**
 * The lifecycle as an operator reads it.
 *
 * Ten visible stages, each covering the statuses that mean "this stage is where
 * the document is". PARTIALLY_RECEIVED sits inside Received rather than getting
 * a stage of its own, because partial receipt is a normal condition of that
 * stage and not a separate step in the process.
 */
export const PO_LIFECYCLE: { label: string; statuses: readonly PurchaseOrderStatus[] }[] = [
  { label: 'Draft', statuses: ['DRAFT'] },
  { label: 'Pending approval', statuses: ['PENDING_APPROVAL'] },
  { label: 'Approved', statuses: ['APPROVED'] },
  { label: 'Sent', statuses: ['SENT'] },
  { label: 'Acknowledged', statuses: ['ACKNOWLEDGED'] },
  { label: 'Received', statuses: ['PARTIALLY_RECEIVED', 'RECEIVED'] },
  { label: 'QC', statuses: ['QC_PENDING', 'QC_COMPLETED'] },
  { label: 'GRN', statuses: ['GRN_COMPLETED'] },
  { label: 'Invoiced', statuses: ['INVOICED'] },
  { label: 'Paid', statuses: ['PAID', 'CLOSED'] },
]

/** Statuses that end the lifecycle without completing it. */
export const PO_TERMINAL_BAD: readonly PurchaseOrderStatus[] = ['REJECTED', 'CANCELLED']

/** Index into PO_LIFECYCLE, or -1 for a rejected or cancelled order. */
export function poStageIndex(status: string): number {
  return PO_LIFECYCLE.findIndex((stage) => (stage.statuses as readonly string[]).includes(status))
}

export const isPoSubmittable = (status: string): boolean => status === 'DRAFT'
export const isPoDecidable = (status: string): boolean => status === 'PENDING_APPROVAL'
export const isPoSendable = (status: string): boolean => status === 'APPROVED'
export const isPoAcknowledgeable = (status: string): boolean => status === 'SENT'

/** Cancellation is legal only before anything has been received against the order. */
export const isPoCancellable = (status: string): boolean =>
  ['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SENT', 'ACKNOWLEDGED'].includes(status)

/**
 * Statuses a goods receipt may legally be posted against.
 *
 * `RECEIVABLE_PO_STATUSES` in `procurement/models.py` - the vendor has the
 * order, so goods can legitimately turn up. APPROVED is deliberately *not* in
 * the set: an approved order the vendor has never seen cannot have shipped, and
 * offering it to the storekeeper only earns a refusal at posting time.
 */
export const RECEIVABLE_PO_STATUSES: readonly PurchaseOrderStatus[] = [
  'SENT',
  'ACKNOWLEDGED',
  'PARTIALLY_RECEIVED',
]

export const isPoReceivable = (status: string): boolean =>
  (RECEIVABLE_PO_STATUSES as readonly string[]).includes(status)

/**
 * Statuses in which an order still represents expected inbound quantity.
 *
 * `OPEN_PO_STATUSES` in the API, and what the `open_only` list filter selects.
 */
export const OPEN_PO_STATUSES: readonly PurchaseOrderStatus[] = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'SENT',
  'ACKNOWLEDGED',
  'PARTIALLY_RECEIVED',
]

/**
 * One row of `GET /procurement/purchase-orders` (`PurchaseOrderSummary`).
 *
 * A header only. `priority` is an integer 1-9 with lower the more urgent, the
 * money fields are decimal strings, and there is no received quantity on it -
 * that lives on the lines and on the tracking payload.
 */
export type PurchaseOrderRow = SchemaOf<'PurchaseOrderSummary'>

export type PurchaseOrderLineStatus = SchemaOf<'PurchaseOrderLineStatus'>

export const PURCHASE_ORDER_LINE_STATUSES = [
  'PENDING',
  'PARTIALLY_RECEIVED',
  'RECEIVED',
  'CLOSED',
  'CANCELLED',
] as const satisfies readonly PurchaseOrderLineStatus[]

/**
 * One line of `GET /procurement/purchase-orders/{id}` (`PurchaseOrderLineRead`).
 *
 * The ordered quantity is `quantity`, not `ordered_quantity`. `description` is
 * what the order recorded, because the item master is not joined in, and
 * `requires_qc` is what sends a receipt to QC hold instead of free stock. Every
 * quantity on it is a magnitude - none of them ever carries a sign.
 */
export type PurchaseOrderLine = SchemaOf<'PurchaseOrderLineRead'>

/**
 * Still to arrive on a line, as a decimal string.
 *
 * The API does not send a pending quantity, so it is derived - but with
 * decimal.js from the two decimal strings it does send, never through a float.
 * Clamped at zero because an over-receipt inside tolerance would otherwise read
 * as a negative pending.
 */
export function pendingQuantity(line: {
  quantity: DecimalString
  received_quantity: DecimalString
}): DecimalString {
  const outstanding = toDecimal(line.quantity).minus(toDecimal(line.received_quantity))
  return (outstanding.isNegative() ? toDecimal(0) : outstanding).toFixed(4)
}

/**
 * `GET /procurement/purchase-orders/{id}` (`PurchaseOrderDetail`).
 *
 * `lines` is *optional* on the wire - the API declares it with a default rather
 * than as a required key - so every reader treats it as possibly absent.
 * Declaring it required here is what let `order.lines.filter(...)` be written
 * without a guard.
 */
export type PurchaseOrderDetail = SchemaOf<'PurchaseOrderDetail'>

/** One receipt already booked against an order, inside the tracking payload. */
export type TrackedReceipt = SchemaOf<'TrackedReceipt'>

/**
 * `GET /procurement/purchase-orders/{id}/tracking` (`PurchaseOrderTracking`).
 *
 * The one place the API resolves a vendor's name for procurement, and the only
 * endpoint on the detail screen that states how much of the order has landed.
 * Every quantity on it is a magnitude the API has already clamped at zero -
 * `pending_quantity` in particular - and `days_late` is a positive count of
 * days, so nothing here is rendered with a sign.
 */
export type PurchaseOrderTracking = SchemaOf<'PurchaseOrderTracking'>

/** Body of `POST /procurement/purchase-orders/{id}/shipment` (`ShipmentUpdate`). */
export type ShipmentUpdatePayload = SchemaOf<'ShipmentUpdate'>

// ------------------------------------------------------- goods receipts -----

export type GrnStatus = SchemaOf<'GoodsReceiptStatus'>

export const GRN_STATUSES = ['DRAFT', 'POSTED', 'CANCELLED'] as const satisfies readonly GrnStatus[]

export type ReceiptLineQcStatus = SchemaOf<'ReceiptLineQcStatus'>

export const RECEIPT_LINE_QC_STATUSES = [
  'NOT_REQUIRED',
  'PENDING',
  'PASSED',
  'FAILED',
] as const satisfies readonly ReceiptLineQcStatus[]

/**
 * One row of `GET /procurement/goods-receipts` (`GoodsReceiptSummary`).
 *
 * The vendor's challan is `vendor_delivery_note` on the wire, and the three
 * totals are decimal strings - compare them with `toDecimal(...).isZero()`,
 * never with `=== '0'`: a NUMERIC(18,4) arrives as `"0.0000"`.
 */
export type GoodsReceiptRow = SchemaOf<'GoodsReceiptSummary'>

/** One line of `GET /procurement/goods-receipts/{id}` (`GoodsReceiptLineRead`). */
export type GoodsReceiptLine = SchemaOf<'GoodsReceiptLineRead'>

/**
 * `GET /procurement/goods-receipts/{id}` (`GoodsReceiptDetail`).
 *
 * `lines` is optional on the wire, exactly as it is on a purchase order.
 */
export type GoodsReceiptDetail = SchemaOf<'GoodsReceiptDetail'>

/**
 * One line of `POST /procurement/goods-receipts` (`GoodsReceiptLineIn`).
 *
 * `line_number` is required and the model forbids unknown keys, so a payload
 * that omits it is refused outright rather than partially accepted.
 */
export type GoodsReceiptLinePayload = SchemaOf<'GoodsReceiptLineIn'>

/** Body of `POST /procurement/goods-receipts` (`GoodsReceiptCreate`). */
export type GoodsReceiptCreatePayload = SchemaOf<'GoodsReceiptCreate'>

/**
 * One inventory movement a posting wrote, echoed back.
 *
 * `quantity` is a magnitude; the transaction type carries the direction. Never
 * render it with a sign of this screen's own invention.
 */
export type PostedMovement = SchemaOf<'src__modules__procurement__schemas__PostedMovement'>

/**
 * `POST /procurement/goods-receipts/{id}/post` (`GoodsReceiptPostResult`).
 *
 * Not a receipt: posting answers with what it *did* - the stock it moved, where
 * the order got to, and how much went to QC hold instead of free stock. Reading
 * `code` straight off this response is what produced "undefined is posted" in
 * the confirmation toast.
 */
export type GoodsReceiptPostResult = SchemaOf<'GoodsReceiptPostResult'>

// ----------------------------------------------- duplicate PO detection -----

/** How sure the matcher is, which drives how loudly the screen objects. */
export const DUPLICATE_CONFIDENCES = ['EXACT', 'STRONG', 'POSSIBLE'] as const
export type DuplicateConfidence = (typeof DUPLICATE_CONFIDENCES)[number]

export const DUPLICATE_ACTIONS = [
  'MERGE_WITH_EXISTING',
  'INCREASE_EXISTING_QUANTITY',
  'CREATE_NEW_WITH_APPROVAL',
  'CANCEL_DUPLICATE',
] as const
export type DuplicateActionCode = (typeof DUPLICATE_ACTIONS)[number]

/**
 * What each resolution means, and whether this screen can carry it out.
 *
 * Only `CREATE_NEW_WITH_APPROVAL` has an API path: resubmit the same order with
 * `acknowledge_duplicates` and a reason, which the API refuses without
 * `purchase_order.approve`. The other three are edits to the *existing* order
 * and have no create-time endpoint, so they are presented as instructions with
 * a link, not as buttons that would fail.
 */
export const DUPLICATE_ACTION_COPY: Record<
  DuplicateActionCode,
  { label: string; description: string; actionable: boolean }
> = {
  MERGE_WITH_EXISTING: {
    label: 'Merge into the existing order',
    description:
      'Open that order and add these lines to it, then abandon this one. It is still a draft or awaiting approval, so it can take them.',
    actionable: false,
  },
  INCREASE_EXISTING_QUANTITY: {
    label: 'Increase the quantity already on order',
    description:
      'Nothing has been received against it yet, so raising its quantity buys the same material on one document instead of two.',
    actionable: false,
  },
  CREATE_NEW_WITH_APPROVAL: {
    label: 'Raise this order anyway',
    description:
      'The demand is genuinely additional. Recorded against the order with your reason, and refused unless you hold purchase order approval.',
    actionable: true,
  },
  CANCEL_DUPLICATE: {
    label: 'Cancel the existing order',
    description:
      'It covers the same demand and nothing has arrived against it. Cancel it on its own screen, then raise this one.',
    actionable: false,
  },
}

/**
 * A purchase order line the API believes already covers what is being raised,
 * returned inside a 409 `DUPLICATE_DOCUMENT`.
 *
 * Mirrors the `details.candidates` payload built by
 * `service_rules.duplicate_refusal`. Note `purchase_order_code`, not `code`:
 * the old shape looked for `code` and therefore parsed zero candidates, so the
 * resolution dialog never opened and the buyer got a bare toast instead.
 */
export interface DuplicateCandidate {
  purchase_order_id: string
  purchase_order_code: string
  purchase_order_line_id: string
  item_id: string
  status: string
  /** Still expected on that line, so the buyer can judge the overlap. */
  open_quantity: DecimalString | null
  required_date: string | null
  confidence: DuplicateConfidence
  /** Plain-language reasons the matcher gives, already human-readable. */
  match_reasons: string[]
  recommended_action: DuplicateActionCode
  available_actions: DuplicateActionCode[]
}

export interface DuplicateConflict {
  message: string
  candidate_count: number
  candidates: DuplicateCandidate[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : []
}

function asAction(value: unknown): DuplicateActionCode | null {
  return typeof value === 'string' && (DUPLICATE_ACTIONS as readonly string[]).includes(value)
    ? (value as DuplicateActionCode)
    : null
}

function asConfidence(value: unknown): DuplicateConfidence {
  return typeof value === 'string' && (DUPLICATE_CONFIDENCES as readonly string[]).includes(value)
    ? (value as DuplicateConfidence)
    : 'POSSIBLE'
}

/**
 * Read a duplicate-document conflict out of an API error.
 *
 * Written defensively rather than cast, because this decides whether a
 * blocking dialog opens: a payload that does not carry candidates falls back to
 * the ordinary error path instead of opening an empty dialog the user cannot
 * act on.
 */
export function parseDuplicateConflict(error: unknown): DuplicateConflict | null {
  if (!(error instanceof ApiError)) return null
  if (error.status !== 409 || error.code !== 'DUPLICATE_DOCUMENT') return null

  const details = error.details
  if (!details) return null

  const rawCandidates = Array.isArray(details.candidates) ? details.candidates : []
  const candidates = rawCandidates.flatMap((entry): DuplicateCandidate[] => {
    const row = asRecord(entry)
    if (!row) return []
    const id = asString(row.purchase_order_id)
    const code = asString(row.purchase_order_code)
    const lineId = asString(row.purchase_order_line_id)
    if (!id || !code || !lineId) return []
    return [
      {
        purchase_order_id: id,
        purchase_order_code: code,
        purchase_order_line_id: lineId,
        item_id: asString(row.item_id) ?? '',
        status: asString(row.status) ?? 'DRAFT',
        open_quantity: asString(row.open_quantity),
        required_date: asString(row.required_date),
        confidence: asConfidence(row.confidence),
        match_reasons: asStringList(row.match_reasons),
        recommended_action: asAction(row.recommended_action) ?? 'CREATE_NEW_WITH_APPROVAL',
        available_actions: asStringList(row.available_actions)
          .map(asAction)
          .filter((action): action is DuplicateActionCode => action !== null),
      },
    ]
  })

  if (candidates.length === 0) return null

  const count = details.candidate_count
  return {
    message: error.message,
    candidate_count: typeof count === 'number' ? count : candidates.length,
    candidates,
  }
}
