/**
 * Wire shapes and vocabulary for assembly orders.
 *
 * Assembly is production's smaller sibling: a kit of components is issued
 * against an order and comes back as one finished item. What makes it different
 * on screen is serial traceability - when a component is serial tracked, the
 * exact serials that went into the assembly are the record that answers a
 * warranty claim three years later, so they are shown rather than summarised.
 *
 * Quantities are decimal strings and are only ever computed on with `toDecimal`.
 *
 * Two properties of the current contract shape the screens above it:
 *
 * 1. **Rows are not denormalised.** Item, warehouse and UoM arrive as ids with
 *    no accompanying code or name.
 * 2. **Availability is not on the component.** The kitting list says what is
 *    required, issued and consumed; whether the shortfall can be covered is a
 *    question for the inventory module, not a field on this row.
 */

import Decimal from 'decimal.js'

import { toDecimal } from '@/lib/format'
import type { DecimalString } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no assembly branch, so the keys
 * live here in the same hierarchical shape: invalidating `assemblyKeys.all`
 * clears every assembly list and detail at once.
 */
export const assemblyKeys = {
  all: ['assembly'] as const,
  list: (params: unknown) => ['assembly', 'list', params] as const,
  detail: (id: string) => ['assembly', 'detail', id] as const,
  components: (id: string) => ['assembly', 'detail', id, 'components'] as const,
  availability: (id: string) => ['assembly', 'detail', id, 'availability'] as const,
  boardLines: (params: unknown) => ['assembly', 'board', 'lines', params] as const,
  boardOrders: (params: unknown) => ['assembly', 'board', 'orders', params] as const,
  readyForPacking: (params: unknown) => ['assembly', 'ready-for-packing', params] as const,
}

/**
 * Bounds that mirror the assembly schemas, owned by `lib/constants`.
 *
 * Re-exported so the screens above it keep one import: a priority of 100 is
 * what a new order is stored with, and what its component reservations
 * inherit, when the planner does not choose one.
 */
export {
  ASSEMBLY_LINE_MAX_LENGTH,
  ASSEMBLY_NOTES_MAX_LENGTH,
  ASSEMBLY_PRIORITY_MAX,
  DEFAULT_ASSEMBLY_PRIORITY,
} from '@/lib/constants'

// ------------------------------------------------------------ vocabulary ----

/**
 * `AssemblyOrderStatus`.
 *
 * APPROVED, RELEASED and QC_PENDING are real states the machine passes through;
 * omitting them from this list left three of the nine statuses missing from the
 * filter and unmatched by the helpers below.
 */
export const ASSEMBLY_STATUSES = [
  'DRAFT',
  'APPROVED',
  'RELEASED',
  'COMPONENTS_ISSUED',
  'IN_PROGRESS',
  'QC_PENDING',
  'COMPLETED',
  'CLOSED',
  'CANCELLED',
] as const
export type AssemblyStatus = (typeof ASSEMBLY_STATUSES)[number]

/** What raised the order (`AssemblySourceType`). */
export type AssemblySourceType =
  | 'MANUAL'
  | 'SALES_ORDER'
  | 'MRP_SUGGESTION'
  | 'MRP_RUN'
  | 'PRODUCTION_ORDER'
  | 'STOCK_REPLENISHMENT'

/**
 * Components can still be topped up while the order is being built.
 *
 * A draft has nothing reserved yet, so issuing starts once the order has been
 * released to the bench.
 */
export const acceptsComponentIssue = (status: AssemblyStatus): boolean =>
  status === 'RELEASED' || status === 'COMPONENTS_ISSUED' || status === 'IN_PROGRESS'

/**
 * Edges of `ASSEMBLY_ORDER_LIFECYCLE`, one predicate each.
 *
 * A button is shown only for a status the machine can leave, and only for the
 * permission that edge declares. Draft is the one status that is both editable
 * and cancellable: deleting it removes the row, cancelling it keeps the trail.
 */
export const isEditable = (status: AssemblyStatus): boolean => status === 'DRAFT'
export const isDeletable = (status: AssemblyStatus): boolean => status === 'DRAFT'
export const isApprovable = (status: AssemblyStatus): boolean => status === 'DRAFT'
export const isReleasable = (status: AssemblyStatus): boolean => status === 'APPROVED'
export const isStartable = (status: AssemblyStatus): boolean => status === 'COMPONENTS_ISSUED'
export const isClosable = (status: AssemblyStatus): boolean => status === 'COMPLETED'
export const isCancellable = (status: AssemblyStatus): boolean =>
  status === 'DRAFT' || status === 'APPROVED' || status === 'RELEASED'

/**
 * QC_PENDING leaves only through the inspection that holds the output.
 *
 * The button opens that inspection. An order on hold with no inspection is the
 * older path and is settled through the order's own verdict endpoint instead.
 */
export const hasLinkedInspection = (order: {
  status: AssemblyStatus
  qc_inspection_id: string | null
}): boolean => order.status === 'QC_PENDING' && order.qc_inspection_id !== null

export const needsDirectVerdict = (order: {
  status: AssemblyStatus
  qc_inspection_id: string | null
}): boolean => order.status === 'QC_PENDING' && order.qc_inspection_id === null

/**
 * Only a job actually under way can be completed.
 *
 * `IN_PROGRESS` alone, deliberately: `ASSEMBLY_ORDER_LIFECYCLE`
 * (apps/api/src/modules/assembly/service.py) has an edge
 * `COMPONENTS_ISSUED -> IN_PROGRESS` and an edge `IN_PROGRESS -> COMPLETED`,
 * and none from `COMPONENTS_ISSUED` straight to `COMPLETED`. Offering Complete
 * on a kitted-but-unstarted order puts the operator through the whole
 * completion form - quantities, reason, a serial per tracked line - only to be
 * refused by `assert_transition` at the end of it.
 *
 * The kitted order is not stuck: starting it is the missing step, and that is
 * the action to offer there.
 */
export const isCompletable = (status: AssemblyStatus): boolean => status === 'IN_PROGRESS'

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /assembly` (`AssemblyOrderSummary`). */
export interface AssemblyOrderRow {
  id: string
  code: string
  status: AssemblyStatus
  version: number

  item_id: string
  bom_id: string
  /** Where the components are drawn from. */
  warehouse_id: string
  /** Where the finished assembly is booked in. */
  output_warehouse_id: string

  planned_quantity: DecimalString
  assembled_quantity: DecimalString
  rejected_quantity: DecimalString

  /** The bench or line this is built on. */
  assembly_line: string | null

  planned_start: string | null
  planned_end: string | null
  actual_start: string | null
  actual_end: string | null

  /** Lower is more urgent. Carried onto the component reservations at release. */
  priority: number

  requires_qc: boolean
  qc_inspection_id: string | null
  qc_held_at: string | null

  packing_order_id: string | null
  packed_at: string | null

  source_type: AssemblySourceType
  source_id: string | null

  cancellation_reason: string | null
  notes: string | null
}

/**
 * `GET /assembly/{id}` (`AssemblyOrderDetail`).
 *
 * The header carries no extra fields over the list row, but it does inline the
 * kitting list - it is bounded at write time by `MAX_COMPONENTS_PER_ORDER` and
 * "a kitting list is only meaningful whole". The panel still reads the dedicated
 * components endpoint, because availability must never be served from a cached
 * header, so this is optional rather than relied on.
 */
export interface AssemblyOrderDetail extends AssemblyOrderRow {
  components?: AssemblyComponent[]
}

/** One row of `GET /assembly/{id}/components` (`AssemblyComponentRead`). */
export interface AssemblyComponent {
  id: string
  assembly_order_id: string
  line_number: number
  item_id: string
  uom_id: string

  required_quantity: DecimalString
  issued_quantity: DecimalString
  consumed_quantity: DecimalString

  is_serial_tracked_component: boolean
  /** The serials already eaten by this order; ids only, no serial numbers. */
  consumed_serial_ids: string[]

  reservation_id: string | null
  notes: string | null
}

/**
 * `POST /assembly/{id}/complete` (`CompletionResult`).
 *
 * **Not** the order. The endpoint answers with what the completion posted -
 * how many consume movements, how much output, and whether that output went to
 * QC hold instead of the available pool - with the order nested inside. Reading
 * `code` off the envelope gives `undefined`, which is how a toast came to
 * announce "undefined is complete".
 */
export interface AssemblyCompletionResult {
  order: AssemblyOrderDetail
  consume_movement_count: number
  output_quantity: DecimalString
  /** True when the output was received onto QC hold rather than into stock. */
  output_held_for_qc: boolean
  qc_inspection_created: boolean
}

/** One component's live position (`AssemblyComponentAvailability`). */
export interface AssemblyComponentAvailability {
  line_number: number
  item_id: string
  required_quantity: DecimalString
  issued_quantity: DecimalString
  is_reserved: boolean
  available_quantity: DecimalString
  shortage_quantity: DecimalString
}

/** One open order on `GET /assembly/board/orders` (`AssemblyBoardOrder`). */
export interface AssemblyBoardOrder {
  id: string
  code: string
  item_id: string
  status: AssemblyStatus
  assembly_line: string | null
  warehouse_id: string
  output_warehouse_id: string
  priority: number
  planned_quantity: DecimalString
  assembled_quantity: DecimalString
  rejected_quantity: DecimalString
  planned_start: string | null
  planned_end: string | null
  actual_start: string | null
  actual_end: string | null
  requires_qc: boolean
  is_late: boolean
  start_overdue: boolean
  days_late: number
}

/** One line's load on `GET /assembly/board/lines` (`AssemblyLineLoad`). */
export interface AssemblyLineLoad {
  assembly_line: string | null
  open_orders: number
  not_started_orders: number
  in_progress_orders: number
  qc_pending_orders: number
  late_orders: number
  planned_quantity: DecimalString
  earliest_planned_start: string | null
  latest_planned_end: string | null
}

/** One row of `GET /assembly/ready-for-packing` (`ReadyForPackingRow`). */
export interface ReadyForPackingRow {
  assembly_order_id: string
  code: string
  item_id: string
  output_warehouse_id: string
  assembled_quantity: DecimalString
  available_quantity: DecimalString
  completed_at: string | null
  requires_qc: boolean
  source_type: AssemblySourceType
  source_id: string | null
}

/** One row of `GET /inventory/serials` (`StockSerialRead`). */
export interface StockSerial {
  id: string
  item_id: string
  serial_number: string
  warehouse_id: string | null
  status: string
}

// -------------------------------------------------------------- derived -----

const ZERO = new Decimal(0)

export function outstandingQuantity(component: AssemblyComponent): Decimal {
  const outstanding = toDecimal(component.required_quantity).minus(
    toDecimal(component.issued_quantity),
  )
  return outstanding.greaterThan(0) ? outstanding : ZERO
}

/** Issued but not yet eaten - what is sitting on the bench. */
export function onBenchQuantity(component: AssemblyComponent): Decimal {
  const onBench = toDecimal(component.issued_quantity).minus(toDecimal(component.consumed_quantity))
  return onBench.greaterThan(0) ? onBench : ZERO
}

/** Planned less already assembled - the default for a completion. */
export function remainingToAssemble(order: AssemblyOrderRow): Decimal {
  const remaining = toDecimal(order.planned_quantity).minus(toDecimal(order.assembled_quantity))
  return remaining.greaterThan(0) ? remaining : ZERO
}
