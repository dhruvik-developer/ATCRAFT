import type { QueryClient } from '@tanstack/react-query'

import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { toDecimal } from '@/lib/format'
import { messages } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import {
  requisitionKeys,
  type DuplicateCandidate,
  type PurchaseOrderDetail,
} from './procurement-types'

export type DuplicateResolutionRequest = SchemaOf<'PurchaseOrderDuplicateResolutionRequest'>
export type DuplicateResolutionResult = SchemaOf<'PurchaseOrderDuplicateResolutionResult'>
export type DuplicateResolutionAction = DuplicateResolutionRequest['action']
export type DuplicateResolutionDraft = DuplicateResolutionRequest['draft']
export type PurchaseOrderAmendment = SchemaOf<'PurchaseOrderAmendmentRead'>
export type PurchaseOrderAmendPayload = SchemaOf<'PurchaseOrderAmendRequest'>

/** One explicit conversion group; candidate actions never consume unrelated demand. */
export interface DuplicateResolutionScope {
  id: string
  title: string
  draft: DuplicateResolutionDraft
  candidateLineIds: string[]
  itemIds: string[]
  readOnly?: boolean
}

export const MAX_PURCHASE_ORDER_LINES = 200
export const MAX_DUPLICATE_REASON_LENGTH = 1000
export const PURCHASE_QUANTITY_PATTERN = /^\d{1,14}(\.\d{1,4})?$/
export const PURCHASE_PRICE_PATTERN = /^\d{1,12}(\.\d{1,6})?$/

const AMENDABLE_STATUSES = ['APPROVED', 'SENT', 'ACKNOWLEDGED']
const MERGEABLE_STATUSES = ['DRAFT', 'PENDING_APPROVAL', ...AMENDABLE_STATUSES]

/** Receipt history includes draft/cancelled GRNs; zero received counters are insufficient. */
export function isPoAmendable(order: { status: string; has_receipt_history?: boolean }) {
  return order.has_receipt_history === false && AMENDABLE_STATUSES.includes(order.status)
}

export function canResolveDuplicateCandidate(
  candidate: DuplicateCandidate,
  action: 'MERGE' | 'INCREASE',
  incomingItemIds?: readonly string[],
) {
  const hint = action === 'MERGE' ? 'MERGE_WITH_EXISTING' : 'INCREASE_EXISTING_QUANTITY'
  return (
    candidate.has_receipt_history === false &&
    Number.isInteger(candidate.purchase_order_version) &&
    candidate.purchase_order_version > 0 &&
    MERGEABLE_STATUSES.includes(candidate.status) &&
    candidate.available_actions.includes(hint) &&
    (action !== 'INCREASE' ||
      Boolean(incomingItemIds?.length && incomingItemIds.every((id) => id === candidate.item_id)))
  )
}

export function duplicateResolutionPermission(action: DuplicateResolutionAction) {
  if (action === 'CREATE_WITH_APPROVAL') return P.PURCHASE_ORDER_CREATE
  if (action === 'CANCEL_REQUEST') return P.PURCHASE_REQUISITION_CREATE
  return P.PURCHASE_ORDER_UPDATE
}

export function duplicateReasonLimit(action: DuplicateResolutionAction) {
  return action === 'CREATE_WITH_APPROVAL' ? MAX_DUPLICATE_REASON_LENGTH : REASON_MAX_LENGTH
}

export const purchaseOrderChangeKeys = {
  amendments: (orderId: string) =>
    [...queryKeys.procurement.purchaseOrder(orderId), 'amendments'] as const,
}

/** A closed or partially consumed preview is refreshed explicitly by its owner. */
export function invalidatePurchaseOrderChanges(client: QueryClient) {
  void client.invalidateQueries({
    queryKey: queryKeys.procurement.all,
    predicate: (query) =>
      !requisitionKeys.conversions.every((part, index) => query.queryKey[index] === part),
  })
  void client.invalidateQueries({ queryKey: queryKeys.mrp.all })
  void client.invalidateQueries({ queryKey: queryKeys.sales.all })
  void client.invalidateQueries({ queryKey: queryKeys.vendorPortal.all })
}

export function duplicateResolutionSuccess(result: DuplicateResolutionResult) {
  const codes = (result.purchase_orders ?? []).map((order) => order.code).join(', ')
  switch (result.action) {
    case 'MERGE':
      return {
        title: 'Purchase order merged',
        description: `${codes} now includes the selected demand.`,
      }
    case 'INCREASE':
      return {
        title: 'Purchase order quantity increased',
        description: `${codes} now includes the selected quantity.`,
      }
    case 'CREATE_WITH_APPROVAL':
      return {
        title: 'Separate purchase order created',
        description: `${codes} must complete approval before being sent to the vendor.`,
      }
    case 'CANCEL_REQUEST':
      return result.discarded
        ? { title: 'Duplicate request discarded', description: 'No purchase order was created.' }
        : {
            title: 'Duplicate request cancelled',
            description: `${result.cancelled_requisition_line_ids?.length ?? 0} requisition line(s) cancelled. Existing ordered history is retained.`,
          }
  }
}

/** These fields are editable form state, not a second opinion about the API read shape. */
export interface AmendmentLineDraft {
  key: string
  lineId: string | null
  itemId: string
  label: string
  quantity: string
  unitPrice: string
  allocations: {
    requisitionLineId: string
    label: string
    quantity: string
    originalQuantity: string
  }[]
}

export function amendmentLineDrafts(order: PurchaseOrderDetail): AmendmentLineDraft[] {
  return (order.lines ?? []).map((line) => ({
    key: line.id,
    lineId: line.id,
    itemId: line.item_id,
    label: line.item_code
      ? `${line.item_code} - ${line.item_name || line.description || `Order line ${line.line_number}`}`
      : line.description || `Order line ${line.line_number}`,
    quantity: line.quantity,
    unitPrice: line.unit_price,
    allocations: (line.source_allocations ?? [])
      .filter((allocation) => allocation.is_active !== false)
      .map((allocation) => ({
        requisitionLineId: allocation.requisition_line_id,
        label: `${allocation.requisition_code || 'Requisition'}${allocation.requisition_line_number == null ? '' : ` line ${allocation.requisition_line_number}`}`,
        quantity: allocation.quantity,
        originalQuantity: allocation.quantity,
      })),
  }))
}

/** Keep retained line identities and return demand only according to the operator's allocation. */
export function buildPurchaseOrderAmendment(
  reason: string,
  drafts: AmendmentLineDraft[],
  expectedVersion: number,
) {
  const errors: Record<string, string> = {}
  const trimmedReason = reason.trim()
  if (trimmedReason.length < REASON_MIN_LENGTH) errors.reason = messages.errors.reasonTooShort
  if (trimmedReason.length > REASON_MAX_LENGTH)
    errors.reason = `At most ${REASON_MAX_LENGTH} characters`
  if (!drafts.length) errors.lines = 'A purchase order needs at least one line'
  if (drafts.length > MAX_PURCHASE_ORDER_LINES)
    errors.lines = `A purchase order cannot have more than ${MAX_PURCHASE_ORDER_LINES} lines`

  const lines: PurchaseOrderAmendPayload['lines'] = drafts.map((line, index) => {
    const prefix = `lines.${index}`
    const quantity = line.quantity.trim()
    const unitPrice = line.unitPrice.trim()
    const validQuantity =
      PURCHASE_QUANTITY_PATTERN.test(quantity) && toDecimal(quantity).greaterThan(0)
    if (!validQuantity)
      errors[`${prefix}.quantity`] = 'Enter a positive quantity with up to 4 decimal places'
    if (!PURCHASE_PRICE_PATTERN.test(unitPrice))
      errors[`${prefix}.unit_price`] = 'Enter a price with up to 6 decimal places'
    if (!line.itemId) errors[`${prefix}.item_id`] = 'Choose an item'

    let validAllocations = true
    const allocations = line.allocations.flatMap((allocation, allocationIndex) => {
      const allocationQuantity = allocation.quantity.trim()
      if (!PURCHASE_QUANTITY_PATTERN.test(allocationQuantity)) {
        validAllocations = false
        errors[`${prefix}.source_allocations.${allocationIndex}.quantity`] =
          'Enter zero or a positive quantity with up to 4 decimal places'
        return []
      }
      return toDecimal(allocationQuantity).isZero()
        ? []
        : [{ requisition_line_id: allocation.requisitionLineId, quantity: allocationQuantity }]
    })
    const allocatedQuantity = allocations.reduce(
      (sum, allocation) => sum.plus(allocation.quantity),
      toDecimal(0),
    )
    if (validAllocations && validQuantity && allocatedQuantity.greaterThan(quantity))
      errors[`${prefix}.source_allocations`] =
        'Choose the requisition quantities to retain. Their total cannot exceed this order line.'
    const allocationChanged = line.allocations.some(
      (allocation) =>
        !PURCHASE_QUANTITY_PATTERN.test(allocation.quantity.trim()) ||
        !toDecimal(allocation.quantity.trim()).equals(allocation.originalQuantity),
    )
    return {
      ...(line.lineId ? { line_id: line.lineId } : { item_id: line.itemId }),
      line_number: index + 1,
      quantity,
      unit_price: unitPrice,
      ...(allocationChanged ? { source_allocations: allocations } : {}),
    }
  })
  const payload: PurchaseOrderAmendPayload = {
    reason: trimmedReason,
    lines,
    expected_version: expectedVersion,
  }
  return { payload, errors, valid: Object.keys(errors).length === 0 }
}
